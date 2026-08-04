"""
Pair scanner: uses fixed pool from config, filters by liquidity in LIVE mode.
In DEMO mode volume filter is skipped (exchange returns 0 volume).
"""

import config
import scan_report as report
from exchange import fetch_ohlcv
from strategy import find_recent_impulse, get_zones, get_htf_trend, calculate_trade_params
from logger import log


def _norm_symbol(symbol: str) -> str:
    """'BTC/USDT:USDT' и 'BTCUSDT' -> 'BTCUSDT'."""
    return (symbol or '').replace('/', '').replace(':USDT', '').replace(':', '').upper()


def get_liquid_pairs(exchange) -> list:
    """
    Returns tradeable pairs from TRADING_PAIRS_POOL.
    In DEMO mode returns full pool (Bybit Demo has no real volume data).
    In LIVE/PAPER mode filters by MIN_VOLUME_24H_USD.
    """
    if config.TRADING_MODE == 'DEMO':
        log(f"   DEMO: фильтр объёма пропущен, сканируем все {len(config.TRADING_PAIRS_POOL)} пар")
        return list(config.TRADING_PAIRS_POOL)

    try:
        tickers = exchange.fetch_tickers(config.TRADING_PAIRS_POOL)
        # ccxt возвращает ключи в своей нотации ('BTC/USDT:USDT'), а пул задан
        # биржевыми символами ('BTCUSDT'). Без приведения объём каждой пары
        # читался как 0, фильтр не срабатывал ни разу и весь пул проходил
        # дальше по аварийной ветке «ни одной ликвидной пары».
        by_symbol = {_norm_symbol(key): value for key, value in tickers.items()}
        liquid = []
        for symbol in config.TRADING_PAIRS_POOL:
            ticker = by_symbol.get(_norm_symbol(symbol), {})
            vol = ticker.get('quoteVolume') or 0
            if vol >= config.MIN_VOLUME_24H_USD:
                liquid.append(symbol)
            else:
                log(f"   {symbol}: объём ${vol/1e6:.1f}M < порога, пропускаем")
        log(f"   Ликвидных: {len(liquid)}/{len(config.TRADING_PAIRS_POOL)}")
        return liquid if liquid else list(config.TRADING_PAIRS_POOL)
    except Exception as e:
        log(f"⚠️ Ошибка получения тикеров: {e} — сканируем все пары")
        return list(config.TRADING_PAIRS_POOL)


def _score_legacy(setup):
    """
    Старая формула (только размер импульса) — сохранена для сравнения в логах,
    не используется для сортировки.
    """
    size_pct = setup['size'] / setup['end_price'] * 100
    return min(size_pct / 30.0, 1.0) * 100  # normalise against 30% impulse


def _score(setup, rr, htf_strength, proximity):
    """
    Многофакторный скор 0-100: размер импульса + RR + сила HTF-тренда +
    близость цены к границе входа зоны A. Веса и капы — config.SCORE_*.
    """
    size_pct = setup['size'] / setup['end_price'] * 100
    impulse_score   = min(size_pct / 30.0, 1.0) * 100
    rr_score        = min(rr / config.SCORE_RR_CAP, 1.0) * 100
    htf_score       = min(htf_strength / config.SCORE_HTF_STRENGTH_CAP, 1.0) * 100
    proximity_score = proximity * 100
    return (config.SCORE_WEIGHT_IMPULSE * impulse_score +
            config.SCORE_WEIGHT_RR * rr_score +
            config.SCORE_WEIGHT_HTF * htf_score +
            config.SCORE_WEIGHT_PROXIMITY * proximity_score)


def scan_for_setups(liquid_pairs, trade_manager, client=None):
    """
    Loads 1H data for each liquid pair, checks for an active Fibonacci
    setup where price is currently inside a zone (pre-BoS filter).

    Returns a list of candidate dicts sorted by setup quality (best first):
        {pair, setup, df_1h, zone, score, size_pct}

    Only loads 5M data later (in bot.py) for the top candidates to
    minimise API calls.

    client=None -> legacy single-user OHLCV (get_exchange). В мульти-тенант
    режиме передаётся общий keyless market-client (make_market_client), чтобы
    скан рынка делался ОДИН раз и не зависел от ключей конкретного юзера.
    """
    candidates = []
    report.begin('FIBO')

    for pair in liquid_pairs:
        try:
            # Cooldown check — skip if recently traded
            if not trade_manager.check_cooldown(pair):
                log(f"   {pair}: кулдаун активен, пропускаем")
                report.record('FIBO', pair, 'кулдаун активен')
                continue

            df_1h = fetch_ohlcv('1h', limit=config.LOOKBACK_CANDLES + 20, symbol=pair, client=client)
            if df_1h is None or len(df_1h) < config.LOOKBACK_CANDLES:
                report.record('FIBO', pair, 'мало данных по паре')
                continue

            setup = find_recent_impulse(df_1h, lookback_candles=config.LOOKBACK_CANDLES)
            if not setup:
                report.record('FIBO', pair, 'импульс не найден')
                continue

            # Minimum impulse size
            size_pct = setup['size'] / setup['end_price'] * 100
            if size_pct < config.MIN_IMPULSE_PCT:
                log(f"   {pair}: импульс {size_pct:.1f}% < {config.MIN_IMPULSE_PCT}%, пропускаем")
                report.record('FIBO', pair, f'импульс {size_pct:.1f}% меньше порога')
                continue

            zone_a, zone_b = get_zones(setup)

            current_price = df_1h.iloc[-1]['close']

            # W3: фильтр микроценовых пар
            if pair in config.MICRO_PRICE_BLACKLIST:
                log(f"   {pair}: в черном списке, пропускаем")
                report.record('FIBO', pair, 'пара в чёрном списке')
                continue
            if current_price < config.MIN_ENTRY_PRICE:
                log(f"   {pair}: цена ${current_price:.8f} < ${config.MIN_ENTRY_PRICE}, пропускаем")
                report.record('FIBO', pair, 'слишком низкая цена инструмента')
                continue

            ep, sz = setup['end_price'], setup['size']

            # Invalidation: price closed beyond 88.6% level
            if setup['type'] == 'LONG':
                if current_price < ep - sz * config.ZONE_B_TOP:
                    report.record('FIBO', pair, 'сетап инвалидирован')
                    continue
            else:
                if current_price > ep + sz * config.ZONE_B_TOP:
                    report.record('FIBO', pair, 'сетап инвалидирован')
                    continue

            # W11: пред-входовая область — цена подходит к границе зоны A (38.2%) по ходу
            # коррекции, лимит будет «отдыхать» ниже/выше рынка. Зона B отключена.
            if setup['type'] == 'LONG':
                entry_level = zone_a['top']
                if not (entry_level <= current_price <= ep):
                    report.record('FIBO', pair, 'цена вне пред-входовой области')
                    continue
            else:
                entry_level = zone_a['bottom']
                if not (ep <= current_price <= entry_level):
                    report.record('FIBO', pair, 'цена вне пред-входовой области')
                    continue
            active_zone = zone_a

            # ── HTF trend filter ─────────────────────────────────────────────
            df_4h = fetch_ohlcv(config.HTF_TIMEFRAME,
                                 limit=config.HTF_EMA_SLOW + 20,
                                 symbol=pair, client=client)
            htf_trend, htf_strength = get_htf_trend(df_4h, return_strength=True)

            setup_dir = setup['type']
            if htf_trend == 'BULLISH' and setup_dir == 'SHORT':
                log(f"   {pair}: HTF BULLISH — SHORT пропущен (контртренд)")
                report.record('FIBO', pair, 'против старшего тренда')
                continue
            if htf_trend == 'BEARISH' and setup_dir == 'LONG':
                log(f"   {pair}: HTF BEARISH — LONG пропущен (контртренд)")
                report.record('FIBO', pair, 'против старшего тренда')
                continue
            if htf_trend == 'NEUTRAL' and not config.HTF_ALLOW_NEUTRAL:
                log(f"   {pair}: HTF NEUTRAL — пропускаем (HTF_ALLOW_NEUTRAL=False)")
                report.record('FIBO', pair, 'нет направления на старшем таймфрейме')
                continue

            params_est = calculate_trade_params(setup, entry_level, config.SCORE_NOMINAL_BALANCE,
                                                 log_reject=False)
            rr_est = params_est['rr'] if params_est else 0.0

            window_size = abs(ep - entry_level)
            if window_size <= 0:
                proximity = 0.0
            else:
                dist = (current_price - entry_level) if setup_dir == 'LONG' else (entry_level - current_price)
                proximity = 1.0 - min(max(dist / window_size, 0.0), 1.0)

            score = _score(setup, rr_est, htf_strength, proximity)
            score_legacy = _score_legacy(setup)

            candidates.append({
                'pair':         pair,
                'setup':        setup,
                'df_1h':        df_1h,
                'zone':         active_zone['name'],
                'score':        score,
                'score_legacy': round(score_legacy, 1),
                'rr_est':       round(rr_est, 2),
                'htf_strength': round(htf_strength, 4),
                'proximity':    round(proximity, 2),
                'size_pct':     round(size_pct, 1),
                'htf_trend':    htf_trend,
            })
            report.record('FIBO', pair, None)
            log(f"   {pair}: {setup_dir} {active_zone['name']} | HTF={htf_trend}({htf_strength:.3f}) | "
                f"импульс {size_pct:.1f}% | RR~{rr_est:.2f} | prox {proximity:.2f} | "
                f"score {score:.0f} (legacy {score_legacy:.0f})")

        except Exception as e:
            log(f"   {pair}: ошибка сканирования — {e}")
            report.record('FIBO', pair, f'ошибка сканирования: {e}')

    report.finish('FIBO')
    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates
