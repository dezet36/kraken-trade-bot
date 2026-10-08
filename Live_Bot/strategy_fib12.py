"""
Адаптер стратегии «Фибо 12ч» (FIB12) под интерфейс живого бота.

Задача та же, что у strategy_smcs и strategy_levels: отдать исполнителю
сигнал в общем виде, не таща знание о бирже внутрь чистого пакета fib12/.

ЧТО ТОРГУЕТСЯ — fib12/params.py, там же замер. Коротко: импульс 12ч (нога
от свинга до свинга, фрактал 3+3), лимит на откате 0.382 ноги, стоп за 0.786,
цель — расширение 2.618, заявка живёт 6 суток и снимается, если цена ушла за
конец импульса; позиция — до 30 суток.

Это НОВАЯ стратегия, а не правка старой ФИБО (strategy.py, pair_scanner.py):
та работает как работала, обе торгуются параллельно — выбор владельца 07.10.

КОГДА СТАВИТСЯ ЗАЯВКА. Нога рождается на закрытии бара B+3 (00:00 и 12:00
UTC). Замер ставил лимит на первой минуте после него; бот проверяет пары раз в
5 минут и ставит заявку в первый час после закрытия бара — позже это уже не
та заявка.
"""

from datetime import datetime, timezone

import pandas as pd

import config
import scan_report as report
import settings_store as settings
from exchange import fetch_ohlcv
from fib12 import core, params
from logger import log

NAME = 'FIB12'
BAR_MIN = 720

_last_reason = {}


def _closed_bars(pair, client=None):
    """Закрытые свечи 12ч: формирующаяся последняя отбрасывается.
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


def analyze_market(pair, balance, client=None, now=None):
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
    setup, reason = core.evaluate(o, h, l, c, k=params.SWING_K, atr_period=params.ATR_PERIOD,
                                  retrace=params.RETRACE, stop_level=params.STOP_LEVEL,
                                  stop_buffer_atr=params.STOP_BUFFER_ATR,
                                  target_ext=params.TARGET_EXT)
    _last_reason[pair] = reason
    if setup is None:
        log(f"   {pair}: нет сигнала — {reason}")
        return None
    d = setup['dir']
    # Нога продолжилась, пока бот ждал первого цикла после закрытия бара.
    if (price - setup['B']) * d > 0:
        _last_reason[pair] = 'цена уже за концом импульса'
        log(f"   {pair}: нет сигнала — цена {price} уже за концом импульса {setup['B']:.6g}")
        return None
    log(f"   {pair}: импульс 12ч {setup['direction']} {setup['A']:.6g} → {setup['B']:.6g} "
        f"({setup['size_atr']:.1f} ATR) | вход {setup['entry']:.6g} | стоп {setup['stop_pct']:.2f}%")
    return _to_bot_signal(setup, pair, df, price)


def _stamp(df, idx):
    try:
        st = pd.Timestamp(df['timestamp'].iloc[idx])
        if st.tzinfo is not None:
            st = st.tz_convert('UTC').tz_localize(None)
        return st.strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:                              # noqa: BLE001
        return None


def _to_bot_signal(setup, pair, df, price):
    is_long = setup['direction'] == 'LONG'
    entry, stop, target = setup['entry'], setup['stop'], setup['target']
    risk_pct = settings.risk_pct(NAME)
    dist = abs(entry - stop)
    rr = abs(target - entry) / dist if dist else 0.0
    side_word = 'вверх' if is_long else 'вниз'
    why = (f"Импульс 12ч {side_word} {setup['A']:.6g} → {setup['B']:.6g}: лимит на откате 38.2% "
           f"({entry:.6g}), стоп за 78.6% ({setup['stop_pct']:.2f}%), цель 2.618 ({target:.6g}); "
           f"заявка — до 6 сут, снимается при уходе за {setup['B']:.6g}; позиция ≤ "
           f"{params.MAX_POSITION_HOLD_HOURS / 24:.0f} сут")
    return {
        'trading_pair': pair,
        'strategy': NAME,
        # Цена биржи на момент решения: по ней брокер исполняет лимит, если
        # рынок уже за ним (strategy_profile.fills_through_market).
        'market_price': price,
        'setup': {
            'type': setup['direction'],
            'start_price': setup['A'],
            'end_price': setup['B'],
            'size': setup['size'],
            'start_time': _stamp(df, setup['a']),
        },
        'params': {
            'entry': entry,
            'stop_loss': stop,
            'take_profit_1': target,
            'take_profit_2': target,
            'tp_targets': [target],
            'tp_fractions': [1.0],
            # Безубытка нет: в замере стоп не двигался.
            'be_level': None,
            'breakeven_after_tp': False,
            'max_same_direction': params.MAX_SAME_DIRECTION,
            'max_hold_hours': params.MAX_POSITION_HOLD_HOURS,
            'risk_pct': risk_pct,
            'rr': rr,
            'sl_distance': dist,
            'invalidation': stop,
            # Снять заявку, если цена ушла за конец импульса до налива.
            'cancel_beyond': setup['B'],
        },
        'trigger': {'zone': 'FIB 0.382', 'entry_type': 'LIMIT', 'trigger_price': entry},
        'zone': 'FIB 0.382',
        'htf_trend': 'BULLISH' if is_long else 'BEARISH',
        # Очередь по размеру импульса (в ATR): других факторов нет.
        'score': round(setup['size_atr'], 3),
        'why': why,
        'fib12': {
            'A': setup['A'], 'B': setup['B'], 'entry': entry, 'stop': stop, 'target': target,
            'level_786': setup['B'] - setup['dir'] * 0.786 * setup['size'],
            'atr': setup['atr'], 'stop_pct': setup['stop_pct'], 'size_atr': setup['size_atr'],
            'leg_bars': setup['leg_bars'], 'rr': rr,
        },
    }


def scan_for_setups(pairs, trade_manager, client=None, balance=None, now=None):
    """Кандидаты, отсортированные по размеру импульса."""
    balance = config.BALANCE if balance is None else balance
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
            signal = analyze_market(pair, balance, client=client, now=now)
            report.record(NAME, pair, None if signal else _last_reason.get(pair))
            if signal:
                candidates.append({
                    'pair': pair,
                    'signal': signal,
                    'score': signal['score'],
                    'rr': signal['params']['rr'],
                    'poi_type': 'FIB',
                    'df_1h': None,
                })
        except Exception as exc:                   # noqa: BLE001
            log(f"   {pair}: ошибка сканирования FIB12 — {exc}")
            report.record(NAME, pair, f'ошибка сканирования: {exc}')
    report.finish(NAME)
    candidates.sort(key=lambda c: -c['score'])
    return candidates
