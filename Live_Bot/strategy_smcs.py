"""
Адаптер стратегии «SMC-структура 4ч» (SMCS) под интерфейс живого бота.

Задача та же, что у strategy_rsibb и strategy_levels: отдать исполнителю
сигнал в общем виде, не таща знание о бирже внутрь чистого пакета smcs/.

ЧТО ТОРГУЕТСЯ — smcs/params.py, там же замер. Коротко: BOS на 4ч по ходу
структуры, вход по рынку сразу после закрытия бара слома, стоп за началом
ноги, цель 8R, не дольше 30 суток.

ВХОД ПО РЫНКУ, И ЭТО НЕ ОФОРМЛЕНИЕ. Замер входил на первой минуте после
закрытия бара слома тейкером; лимит в ордер-блок там же был ХУЖЕ — откат
наливается чаще у сломов, которые потом проваливаются. Поэтому заявка —
лимит ЗА рынком (исполняется сразу, по рынку, с комиссией тейкера), а вход
позже SIGNAL_MAX_AGE_MIN после закрытия бара не делается вовсе: это уже
другая сделка.
"""

from datetime import datetime, timezone

import pandas as pd

import scan_report as report
# Данные — через дверь анализа (analysis/market.py), не со сборщиков и не с
# биржи напрямую (реорганизация, этап 4).
from analysis.market import fetch_ohlcv
from logger import log
from strategies.smcs import core, params

NAME = 'SMCS'
BAR_MIN = 240

_last_reason = {}


def _closed_bars(pair, client=None):
    """Закрытые свечи 4ч: формирующаяся последняя отбрасывается.
    Возвращает (df закрытых, цена последней сделки) или (None, None)."""
    raw = fetch_ohlcv(params.TIMEFRAME, limit=params.HISTORY_BARS + 1, symbol=pair,
                      client=client)
    if raw is None or len(raw) < 3:
        return None, None
    price = float(raw['close'].iloc[-1])
    return raw.iloc[:-1].reset_index(drop=True), price


def _bar_close_utc(df):
    stamp = pd.Timestamp(df['timestamp'].iloc[-1])
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize('UTC')
    return stamp.tz_convert('UTC') + pd.Timedelta(minutes=BAR_MIN)


def analyze_market(pair, client=None, now=None):
    """Сигнал по паре или None (причина — в _last_reason)."""
    df, price = _closed_bars(pair, client=client)
    if df is None or price is None or price <= 0:
        _last_reason[pair] = 'мало данных по паре'
        return None
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.tz_localize('UTC')
    age_min = (now - _bar_close_utc(df)).total_seconds() / 60
    if age_min > params.SIGNAL_MAX_AGE_MIN:
        _last_reason[pair] = 'нового закрытого бара нет'
        return None

    o = df['open'].astype(float).tolist()
    h = df['high'].astype(float).tolist()
    l = df['low'].astype(float).tolist()
    c = df['close'].astype(float).tolist()
    setup, reason = core.evaluate(o, h, l, c, k=params.SWING_K,
                                  atr_period=params.ATR_PERIOD,
                                  stop_buffer_atr=params.STOP_BUFFER_ATR,
                                  target_r=params.TARGET_R,
                                  bos_only=params.BOS_ONLY)
    _last_reason[pair] = reason
    if setup is None:
        log(f"   {pair}: нет сигнала — {reason}")
        return None
    # Цена уже ушла за стоп или за цель — сетапа больше нет.
    d = setup['dir']
    if (price - setup['stop']) * d <= 0 or (setup['target'] - price) * d <= 0:
        _last_reason[pair] = 'цена уже за стопом или целью'
        log(f"   {pair}: нет сигнала — цена {price} уже за стопом или целью")
        return None
    log(f"   {pair}: BOS 4ч {setup['direction']} — уровень {setup['level']:.6g}, "
        f"начало ноги {setup['org_px']:.6g} | стоп {setup['stop_pct']:.2f}% | цель 8R")
    return _to_bot_signal(setup, pair, df, price)


def _to_bot_signal(setup, pair, df, price):
    is_long = setup['direction'] == 'LONG'
    sgn = 1.0 if is_long else -1.0
    # лимит за рынком — исполняется сразу, тейкером; доля — предел худшей цены
    entry = price * (1 + sgn * params.MARKET_CAP_PCT / 100)
    stop, target = setup['stop'], setup['target']
    dist = abs(entry - stop)
    rr = abs(target - entry) / dist if dist else 0.0

    start_at = None
    try:
        stamp = pd.Timestamp(df['timestamp'].iloc[-1 - setup['piv_back']])
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert('UTC').tz_localize(None)
        start_at = stamp.strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:                              # noqa: BLE001
        start_at = None

    side_word = 'вверх' if is_long else 'вниз'
    why = (f"BOS 4ч {side_word}: закрытие {setup['ref']:.6g} за свингом "
           f"{setup['level']:.6g}; стоп за началом ноги {setup['org_px']:.6g} "
           f"+0.1 ATR ({setup['stop_pct']:.2f}%), цель 8R {target:.6g}, "
           f"не дольше {params.MAX_POSITION_HOLD_HOURS / 24:.0f} сут")
    return {
        'trading_pair': pair,
        'strategy': NAME,
        # Цена, по которой стратегия считала: по ней брокер исполняет лимит,
        # стоящий за рынком (strategy_profile.fills_through_market).
        'market_price': price,
        'setup': {
            'type': setup['direction'],
            # «Импульс» — нога слома: от её начала до экстремума.
            'start_price': setup['org_px'],
            'end_price': setup['ext'],
            'size': abs(setup['ext'] - setup['org_px']),
            'start_time': start_at,
        },
        'params': {
            'entry': entry,
            'stop_loss': stop,
            'take_profit_1': target,
            'take_profit_2': target,
            'tp_targets': [target],
            'tp_fractions': [1.0],
            # Безубытка нет: в замере стоп не двигался ни разу.
            'be_level': None,
            'breakeven_after_tp': False,
            'max_hold_hours': params.MAX_POSITION_HOLD_HOURS,
            'rr': rr,
            'sl_distance': dist,
            'invalidation': stop,
        },
        'trigger': {'zone': 'BOS', 'entry_type': 'LIMIT', 'trigger_price': entry},
        'zone': 'BOS',
        'htf_trend': 'BULLISH' if is_long else 'BEARISH',
        # Очередь по силе импульса ноги (в ATR): других факторов нет.
        'score': round(setup['disp_atr'], 3),
        'why': why,
        'smcs': {
            'kind': setup['kind'],
            'level': setup['level'],
            'ref': setup['ref'],
            'org_px': setup['org_px'],
            'ext': setup['ext'],
            'ob_hi': setup['ob_hi'],
            'ob_lo': setup['ob_lo'],
            'atr': setup['atr'],
            'stop_pct': setup['stop_pct'],
            'disp_atr': setup['disp_atr'],
            'leg_bars': setup['leg_bars'],
            'rr': rr,
        },
    }


def scan_for_setups(pairs, trade_manager, client=None, now=None):
    """Кандидаты, отсортированные по силе импульса ноги."""
    candidates = []
    report.begin(NAME)
    for pair in pairs:
        try:
            if not trade_manager.check_cooldown(pair):
                report.record(NAME, pair, 'кулдаун активен')
                continue
            if trade_manager.has_position_or_order(pair):
                report.record(NAME, pair, 'позиция или ордер уже есть')
                continue
            signal = analyze_market(pair, client=client, now=now)
            report.record(NAME, pair, None if signal else _last_reason.get(pair))
            if signal:
                candidates.append({
                    'pair': pair,
                    'signal': signal,
                    'score': signal['score'],
                    'rr': signal['params']['rr'],
                    'poi_type': 'BOS',
                    # Не 4ч: df_1h читают журнал (ATR% по часу — общий для
                    # всех стратегий признак обстановки) и график; свечи 4ч
                    # там дали бы другое число под тем же именем.
                    'df_1h': None,
                })
        except Exception as exc:                   # noqa: BLE001
            log(f"   {pair}: ошибка сканирования SMCS — {exc}")
            report.record(NAME, pair, f'ошибка сканирования: {exc}')
    report.finish(NAME)
    candidates.sort(key=lambda c: -c['score'])
    return candidates


# ── Единый набор функций реестра стратегий (strategies/registry.py, 08.10.2026) ──
# Раньше эти куски жили ветками в общих модулях: сканирование и сборка сигнала —
# в bot.py, величины исполнения — в strategy_profile.py, разметка — в
# setup_geometry.py. Поведение не изменилось — его держат эталоны tests/golden/.

def scan(pairs, gate, client=None):
    return scan_for_setups(pairs, gate, client=client)


def build_signal(candidate):
    pair = candidate['pair']
    # Сканер уже вернул готовый сигнал: он и есть результат анализа.
    signal = candidate.get('signal')
    if not signal:
        log(f"   SMCS {pair}: кандидат без сигнала — пропускаю")
        return None, None
    sm = signal.get('smcs') or {}
    signal['scan'] = {
        'score': candidate.get('score'),
        'rr_est': candidate.get('rr'),
        'kind': sm.get('kind'),
        'stop_pct': sm.get('stop_pct'),
        'disp_atr': sm.get('disp_atr'),
        'poi_type': 'BOS',
    }
    log(f"\n[SMCS] {pair}: BOS 4ч {signal['setup'].get('type')}, "
        f"стоп {sm.get('stop_pct', 0):.2f}%, импульс {sm.get('disp_atr', 0):.1f} ATR")
    return signal, candidate.get('df_1h')


def profile():
    # Модуль параметров — заново при каждом вызове: тесты перезагружают его, и
    # схваченный при импорте адаптера был бы чужим (как и в strategy_profile).
    from strategies.smcs import params as p
    return {'expiry_hours': p.PENDING_ORDER_MAX_HOURS, 'cooldown_hours': p.COOLDOWN_HOURS,
            'cost_limit_pct': p.MAX_ENTRY_COST_SHARE_PCT, 'max_hold_hours': p.MAX_POSITION_HOLD_HOURS,
            'drops_at_target': p.CANCEL_PENDING_AT_TARGET,
            # Вход SMCS — по рынку: лимит за рынком исполняется сразу, как замер.
            'fills_through_market': p.FILL_THROUGH_MARKET, 'min_stop_pct': p.MIN_STOP_PCT,
            'max_same_direction': p.MAX_SAME_DIRECTION}


def geometry(signal, g):
    sm = signal.get('smcs') or {}
    # Сетап SMCS — сам слом: свинг, за которым закрылась свеча 4ч, и нога, его
    # сломавшая. Вход по рынку, поэтому главной зоны нет; ордер-блок ноги —
    # граница, за которой стоит стоп.
    g.leg('начало ноги — за ним стоп', 'экстремум ноги')
    if sm.get('level'):
        g.lines.append({'price': float(sm['level']), 'label': 'сломанный свинг 4ч (BOS)', 'main': True})
    g.band(sm.get('ob_lo'), sm.get('ob_hi'), 'ордер-блок ноги')
