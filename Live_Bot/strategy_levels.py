"""
Адаптер стратегии уровней под интерфейс живого бота.

Задача та же, что у strategy_smc: отдать исполнителю сигнал в понятном ему
виде, не таща знание о бирже внутрь чистого пакета levels/.

Две вещи решаются здесь и отсутствуют в бэктесте:

1. НЕЗАКРЫТАЯ СВЕЧА. ccxt отдаёт последней ещё формирующуюся. Считать по
   ней уровни нельзя: её экстремумы меняются каждую секунду.

2. КЭШ УРОВНЕЙ. Построение уровней перебирает пары экстремумов, а бот
   сканирует пул каждые пять минут при часовом рабочем ТФ. Пересчитываем
   только когда появилась новая закрытая свеча.
"""

import numpy as np
import pandas as pd

import config
import scan_report as report
import settings_store as settings
from exchange import fetch_ohlcv
from levels import core, params
from logger import log

NAME = 'LEVELS'

_cache = {}          # pair -> (последний timestamp, свечи, уровни, atr)
_last_reason = {}


def _drop_forming_candle(df):
    if df is None or len(df) < 2:
        return None
    return df.iloc[:-1].reset_index(drop=True)


def _context(pair, client=None):
    """Свечи, уровни и ATR по паре. Пересчёт только на новой закрытой свече."""
    raw = fetch_ohlcv(params.TIMEFRAME, limit=params.LOOKBACK + 5,
                      symbol=pair, client=client)
    df = _drop_forming_candle(raw)
    if df is None or len(df) < 120:
        return None

    stamp = str(df['timestamp'].iloc[-1])
    cached = _cache.get(pair)
    if cached and cached[0] == stamp:
        return cached[1:]

    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    volume = (df['volume'].to_numpy(dtype=float) if 'volume' in df.columns
              else np.ones(len(df)))
    levels = core.build_levels(high, low)
    atr_values = core.atr(high, low, close)
    _cache[pair] = (stamp, df, (high, low, close, volume), levels, atr_values)
    return _cache[pair][1:]


def analyze_market(pair, balance, client=None):
    """Сетап по паре или None."""
    ctx = _context(pair, client=client)
    if ctx is None:
        _last_reason[pair] = 'мало данных по паре'
        return None
    df, (high, low, close, volume), levels, atr_values = ctx

    setup, reason = core.evaluate(high, low, close, volume, len(close) - 1,
                                  levels=levels, atr_values=atr_values)
    _last_reason[pair] = reason
    if setup is None:
        log(f"   {pair}: нет сигнала — {reason}")
        return None

    log(f"   {pair}: {setup['direction']} от уровня {setup['level']:.6f} | "
        f"касаний {setup['touches']} | объём {setup['volume_ratio']:.1f}x | "
        f"RR {setup['rr']:.2f}")
    return _to_bot_signal(setup, pair, balance, df)


def _to_bot_signal(setup, pair, balance, df):
    risk_pct = settings.risk_pct(NAME)
    dist = setup['sl_distance']
    risk_amount = balance * (risk_pct / 100)
    size = risk_amount / dist if dist else 0.0

    why = (f"{setup['direction']} от уровня {setup['level']:.6f}: прокол с "
           f"возвратом, объём {setup['volume_ratio']:.1f}x от среднего, "
           f"касаний {setup['touches']}"
           f"{', зеркальный' if setup['mirror'] else ''}, RR {setup['rr']:.2f}")

    # Номера баров превращаем во время ЗДЕСЬ: дальше по пути свечей уже нет,
    # а дашборду и графику нужны отметки времени. Индекс, ушедший в журнал как
    # число, через неделю не значит ничего — таблица к тому времени другая.
    def _at(index):
        try:
            stamp = pd.Timestamp(df['timestamp'].iloc[int(index)])
            if stamp.tzinfo is not None:
                stamp = stamp.tz_convert('UTC').tz_localize(None)
            return stamp.strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception:                          # noqa: BLE001
            return None

    touches = []
    for point in (setup.get('points') or []):
        at = _at(point.get('index'))
        if at:
            touches.append({'at': at, 'price': float(point['price'])})
    first_touch = _at(setup.get('first_index'))

    return {
        'trading_pair': pair,
        'setup': {
            'type': setup['direction'],
            'start_price': setup['level'],
            'end_price': setup['entry'],
            'size': abs(setup['entry'] - setup['level']),
            # Начало сетапа у этой стратегии — не начало импульса, а ПЕРВОЕ
            # КАСАНИЕ уровня: именно с него уровень начал существовать. По
            # нему график сделки разворачивается назад, и касания видно.
            'start_time': first_touch,
            'touches_at': touches,
        },
        'params': {
            'entry': setup['entry'],
            'stop_loss': setup['stop_loss'],
            'take_profit_1': setup['target'],
            'take_profit_2': setup['target'],
            'tp_targets': [setup['target']],
            'tp_fractions': [1.0],
            # Безубыток выключен: у этой стратегии цель и так близко, и
            # подтянутый стоп успевает выбить позицию до неё.
            #
            # 2026-08-05: прежняя формулировка здесь утверждала, что замер
            # отверг безубыток «у всех трёх стратегий проекта». Про фибо это
            # неверно — там его никто не мерил, пока не измерили отдельно
            # (research/fibo_breakeven.py), и он остался включённым.
            'be_level': None,
            'breakeven_after_tp': False,
            'max_same_direction': params.MAX_SAME_DIRECTION,
            'risk_pct': risk_pct,
            'position_size': size,
            'risk_amount': risk_amount,
            'rr': setup['rr'],
            'sl_distance': dist,
        },
        # ОБЯЗАТЕЛЬНОЕ ПОЛЕ ОБЩЕГО ДОГОВОРА, а не украшение. Его читают шесть
        # разных мест: сборка контекста сделки, журнал, исполнитель, дашборд.
        # Здесь его не было, и до сих пор это ничего не ломало ровно потому,
        # что стратегия уровней не доходила до них НИ РАЗУ: диспетчер отдавал
        # её кандидатов ветке Фибоначчи. Починив диспетчер, я открыл дорогу к
        # падению на первом же входе — KeyError('trigger') вместо сделки.
        'trigger': {'zone': 'LEVEL', 'entry_type': 'MARKET',
                    'trigger_price': setup['entry']},
        'zone': 'LEVEL',
        'htf_trend': 'NEUTRAL',
        'score': setup['volume_ratio'] * 10,
        'why': why,
        'levels': setup,
    }


def scan_for_setups(pairs, trade_manager, client=None, balance=None):
    """Кандидаты, отсортированные по объёму на возврате (сильные первыми)."""
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

            signal = analyze_market(pair, balance, client=client)
            report.record(NAME, pair, None if signal else _last_reason.get(pair))
            if signal:
                candidates.append({
                    'pair': pair,
                    'signal': signal,
                    'score': signal['score'],
                    'rr': signal['params']['rr'],
                    'poi_type': 'LEVEL',
                    'df_1h': _cache.get(pair, (None, None))[1],
                })
        except Exception as exc:                   # noqa: BLE001
            log(f"   {pair}: ошибка сканирования уровней — {exc}")
            report.record(NAME, pair, f'ошибка сканирования: {exc}')

    report.finish(NAME)
    candidates.sort(key=lambda c: (-c['score'], -c['rr']))
    return candidates
