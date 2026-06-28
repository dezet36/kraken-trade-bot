"""
Pair scanner: uses fixed pool from config, filters by liquidity in LIVE mode.
In DEMO mode volume filter is skipped (exchange returns 0 volume).
"""

import config
from exchange import fetch_ohlcv
from strategy import find_recent_impulse, get_zones, price_in_zone, get_htf_trend
from logger import log


def get_liquid_pairs(exchange) -> list:
    """
    Returns tradeable pairs from TRADING_PAIRS_POOL.
    In DEMO mode returns full pool (Bybit Demo has no real volume data).
    In LIVE mode filters by MIN_VOLUME_24H_USD.
    """
    if config.TRADING_MODE == 'DEMO':
        log(f"   DEMO: фильтр объёма пропущен, сканируем все {len(config.TRADING_PAIRS_POOL)} пар")
        return list(config.TRADING_PAIRS_POOL)

    try:
        tickers = exchange.fetch_tickers(config.TRADING_PAIRS_POOL)
        liquid = []
        for symbol in config.TRADING_PAIRS_POOL:
            ticker = tickers.get(symbol, {})
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


def _score(setup):
    """
    Rates setup quality 0–100.
    Larger impulse (in % of price) → higher score.
    """
    size_pct = setup['size'] / setup['end_price'] * 100
    return min(size_pct / 30.0, 1.0) * 100  # normalise against 30% impulse


def scan_for_setups(liquid_pairs, trade_manager):
    """
    Loads 1H data for each liquid pair, checks for an active Fibonacci
    setup where price is currently inside a zone (pre-BoS filter).

    Returns a list of candidate dicts sorted by setup quality (best first):
        {pair, setup, df_1h, zone, score, size_pct}

    Only loads 5M data later (in bot.py) for the top candidates to
    minimise API calls.
    """
    candidates = []

    for pair in liquid_pairs:
        try:
            # Cooldown check — skip if recently traded
            if not trade_manager.check_cooldown(pair):
                log(f"   {pair}: кулдаун активен, пропускаем")
                continue

            df_1h = fetch_ohlcv('1h', limit=config.LOOKBACK_CANDLES + 20, symbol=pair)
            if df_1h is None or len(df_1h) < config.LOOKBACK_CANDLES:
                continue

            setup = find_recent_impulse(df_1h, lookback_candles=config.LOOKBACK_CANDLES)
            if not setup:
                continue

            # Minimum impulse size
            size_pct = setup['size'] / setup['end_price'] * 100
            if size_pct < config.MIN_IMPULSE_PCT:
                log(f"   {pair}: импульс {size_pct:.1f}% < {config.MIN_IMPULSE_PCT}%, пропускаем")
                continue

            zone_a, zone_b = get_zones(setup)
            zones = [zone_a, zone_b]

            current_price = df_1h.iloc[-1]['close']

            # W3: фильтр микроценовых пар
            if pair in config.MICRO_PRICE_BLACKLIST:
                log(f"   {pair}: в черном списке, пропускаем")
                continue
            if current_price < config.MIN_ENTRY_PRICE:
                log(f"   {pair}: цена ${current_price:.8f} < ${config.MIN_ENTRY_PRICE}, пропускаем")
                continue

            ep, sz = setup['end_price'], setup['size']

            # Invalidation: price closed beyond 88.6% level
            if setup['type'] == 'LONG':
                if current_price < ep - sz * config.ZONE_B_TOP:
                    continue
            else:
                if current_price > ep + sz * config.ZONE_B_TOP:
                    continue

            # W11: пред-входовая область — цена подходит к границе зоны A (38.2%) по ходу
            # коррекции, лимит будет «отдыхать» ниже/выше рынка. Зона B отключена.
            if setup['type'] == 'LONG':
                entry_level = zone_a['top']
                if not (entry_level <= current_price <= ep):
                    continue
            else:
                entry_level = zone_a['bottom']
                if not (ep <= current_price <= entry_level):
                    continue
            active_zone = zone_a

            # ── HTF trend filter ─────────────────────────────────────────────
            df_4h = fetch_ohlcv(config.HTF_TIMEFRAME,
                                 limit=config.HTF_EMA_SLOW + 20,
                                 symbol=pair)
            htf_trend = get_htf_trend(df_4h)

            setup_dir = setup['type']
            if htf_trend == 'BULLISH' and setup_dir == 'SHORT':
                log(f"   {pair}: HTF BULLISH — SHORT пропущен (контртренд)")
                continue
            if htf_trend == 'BEARISH' and setup_dir == 'LONG':
                log(f"   {pair}: HTF BEARISH — LONG пропущен (контртренд)")
                continue
            if htf_trend == 'NEUTRAL' and not config.HTF_ALLOW_NEUTRAL:
                log(f"   {pair}: HTF NEUTRAL — пропускаем (HTF_ALLOW_NEUTRAL=False)")
                continue

            score = _score(setup)
            candidates.append({
                'pair':      pair,
                'setup':     setup,
                'df_1h':     df_1h,
                'zone':      active_zone['name'],
                'score':     score,
                'size_pct':  round(size_pct, 1),
                'htf_trend': htf_trend,
            })
            log(f"   {pair}: {setup_dir} {active_zone['name']} | "
                f"HTF={htf_trend} | импульс {size_pct:.1f}% | оценка {score:.0f}")

        except Exception as e:
            log(f"   {pair}: ошибка сканирования — {e}")

    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates
