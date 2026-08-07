"""
Адаптер стратегии «RSI и полосы Боллинджера» под интерфейс живого бота.

Задача та же, что у strategy_levels и strategy_smc: отдать исполнителю сигнал
в общем виде, не таща знание о бирже внутрь чистого пакета rsibb/.

ЧТО ЭТА СТРАТЕГИЯ ТОРГУЕТ, И ЭТО НЕ УЧЕБНИК. Замер отверг канонический сетап
на всех масштабах: RSI 30/70 даёт −0.70 и −0.78 R на двух периодах при
просадке 100%, а добавление ADX доводит до −0.87 и −1.05. Без RSI результат
ЛУЧШЕ, чем с ним. Причина измерена: RSI на полосе помечает не истощение
продавца, а действующий импульс — ровно ту «ходьбу по полосе», от которой
фильтр должен был защищать.

Торгуется ОБРАТНОЕ прочтение: покупка нижней полосы тогда, когда импульс НЕ
слаб (RSI выше 50), и симметрично для шорта. На часовом графике, с широким
стопом в целую полуширину канала.

    период      валовый край   издержки   чистый   просадка
    бык         +0.083         0.058      +0.025    17%
    медведь     +0.112         0.062      +0.050    10%

ЧЕСТНО О СТАТУСЕ: ЭТО КАНДИДАТ, А НЕ ПРИНЯТАЯ СТРАТЕГИЯ. Приёмку проекта она
не прошла — интервалы [−0.049; +0.100] и [−0.028; +0.124] накрывают ноль.
Знак края устойчив на двух периодах, двух размерах пула и двух таймфреймах, но
разрешения выборки не хватает, чтобы отделить его от нуля.

Поэтому она включена в бумажную торговлю ради данных ВНЕ выборки, а не потому
что доказана. Ставить её на реальные деньги наравне с FIBO, LEVELS и SMC
нельзя до тех пор, пока живые наблюдения не сдвинут интервал.

ПОЧЕМУ ЧАС, А НЕ ПЯТЬ МИНУТ. Издержки в единицах риска равны кругу комиссий,
делённому на расстояние до стопа. На пятиминутках стоп упирался в пол 0.4% и
круг стоил 0.10-0.16 R, съедая весь валовый край. На часе полосы шире, стоп
около 1.2%, тот же круг стоит 0.058 R. Это единственная величина в формуле,
которой можно управлять, не трогая саму идею.

ВХОД ЛИМИТНЫЙ И ЭТО ОБЯЗАТЕЛЬНО. Заявка стоит НА полосе, цена приходит к ней
сама — круг мейкер-мейкер 0.040% вместо 0.210% у тейкера. Вход по рынку
превратил бы работающую арифметику в заведомо убыточную.
"""

import numpy as np
import pandas as pd

import config
import scan_report as report
import settings_store as settings
from exchange import fetch_ohlcv
from logger import log
from rsibb import core, params

NAME = 'RSIBB'

_cache = {}          # pair -> (последний timestamp, свечи, индикаторы)
_last_reason = {}


def _drop_forming_candle(df):
    """
    Последняя свеча у биржи ещё формируется — считать по ней нельзя.

    Полосы Боллинджера и RSI строятся по ЗАКРЫТИЯМ, а закрытие формирующейся
    свечи меняется каждую секунду. Сигнал по ней то появлялся бы, то исчезал
    в пределах одной минуты.
    """
    if df is None or len(df) < 2:
        return None
    return df.iloc[:-1].reset_index(drop=True)


def _context(pair, client=None):
    """Свечи и индикаторы по паре. Пересчёт только на новой закрытой свече."""
    need = max(params.BB_PERIOD, params.RSI_PERIOD, params.ADX_PERIOD * 2,
               params.WIDTH_WINDOW) + 60
    raw = fetch_ohlcv(params.TIMEFRAME, limit=need + 5, symbol=pair,
                      client=client)
    df = _drop_forming_candle(raw)
    if df is None or len(df) < need:
        return None

    stamp = str(df['timestamp'].iloc[-1])
    cached = _cache.get(pair)
    if cached and cached[0] == stamp:
        return cached[1:]

    ind = core.indicators(df['open'].to_numpy(dtype=float),
                          df['high'].to_numpy(dtype=float),
                          df['low'].to_numpy(dtype=float),
                          df['close'].to_numpy(dtype=float))
    _cache[pair] = (stamp, df, ind)
    return _cache[pair][1:]


def analyze_market(pair, balance, client=None):
    """Сетап по паре или None."""
    ctx = _context(pair, client=client)
    if ctx is None:
        _last_reason[pair] = 'мало данных по паре'
        return None
    df, ind = ctx

    setup, reason = core.evaluate(ind, len(ind['close']) - 1)
    _last_reason[pair] = reason
    if setup is None:
        log(f"   {pair}: нет сигнала — {reason}")
        return None

    trade = core.build_trade(setup)
    if trade is None:
        _last_reason[pair] = 'геометрия не годится'
        log(f"   {pair}: нет сигнала — геометрия не годится")
        return None

    log(f"   {pair}: {setup['direction']} от полосы {setup['band']:.6f} | "
        f"RSI {setup['rsi']:.0f} | стоп {trade['stop_pct']:.2f}% | "
        f"RR {trade['rr']:.2f}")
    return _to_bot_signal(setup, trade, pair, balance, df)


def _to_bot_signal(setup, trade, pair, balance, df):
    risk_pct = settings.risk_pct(NAME)
    dist = abs(trade['entry'] - trade['stop'])
    risk_amount = balance * (risk_pct / 100)
    size = risk_amount / dist if dist else 0.0

    why = (f"{setup['direction']} от {'нижней' if setup['direction'] == 'LONG' else 'верхней'} "
           f"полосы {setup['band']:.6f}: RSI {setup['rsi']:.0f} — импульс не "
           f"подтверждает выход, цель на средней линии {setup['mid']:.6f}, "
           f"RR {trade['rr']:.2f}")

    # Время бара сигнала: по нему дашборд разворачивает окно графика назад,
    # чтобы был виден сам выход за полосу, а не только вход.
    start_at = None
    try:
        stamp = pd.Timestamp(df['timestamp'].iloc[-1])
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert('UTC').tz_localize(None)
        # Отматываем на ширину окна полос — столько, сколько их и построило.
        back = pd.Timedelta(minutes=params.BB_PERIOD * _bar_minutes())
        start_at = (stamp - back).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:                              # noqa: BLE001
        start_at = None

    return {
        'trading_pair': pair,
        'setup': {
            'type': setup['direction'],
            # «Импульс» этой стратегии — расстояние от полосы до средней линии:
            # именно его она и собирается забрать.
            'start_price': setup['band'],
            'end_price': setup['mid'],
            'size': abs(setup['mid'] - setup['band']),
            'start_time': start_at,
        },
        'params': {
            'entry': trade['entry'],
            'stop_loss': trade['stop'],
            'take_profit_1': trade['target'],
            'take_profit_2': trade['target'],
            'tp_targets': [trade['target']],
            'tp_fractions': [1.0],
            # Безубыток выключен: замер ведения позиции на этой стратегии не
            # проводился, а включать непроверенное — значит торговать не то,
            # что измерено. Ровно эта ошибка стоила месяца у стратегии уровней.
            'be_level': None,
            'breakeven_after_tp': False,
            'max_same_direction': params.MAX_SAME_DIRECTION,
            'risk_pct': risk_pct,
            'position_size': size,
            'risk_amount': risk_amount,
            'rr': trade['rr'],
            'sl_distance': dist,
        },
        # ОБЯЗАТЕЛЬНОЕ ПОЛЕ ОБЩЕГО ДОГОВОРА. Его читают шесть мест: сборка
        # контекста сделки, журнал, исполнитель, дашборд. У стратегии уровней
        # его однажды забыли, и первый же вход упал бы с KeyError('trigger').
        #
        # Тип входа ЛИМИТНЫЙ, и это не оформление: заявка стоит на полосе, цена
        # приходит к ней сама. Вся арифметика издержек построена на мейкерской
        # комиссии, вход по рынку сделал бы стратегию заведомо убыточной.
        'trigger': {'zone': 'BAND', 'entry_type': 'LIMIT',
                    'trigger_price': trade['entry']},
        'zone': 'BAND',
        'htf_trend': 'NEUTRAL',
        # Чем дальше RSI от порога, тем сильнее расхождение с ценой. Это и
        # ставим в очередь приоритета — других факторов у стратегии нет.
        'score': abs(setup['rsi'] - 50) * 2,
        'why': why,
        'rsibb': {
            'band': setup['band'],
            'mid': setup['mid'],
            'upper': setup['mid'] + setup['half_width'],
            'lower': setup['mid'] - setup['half_width'],
            'rsi': setup['rsi'],
            'adx': setup['adx'],
            'width_ratio': setup['width_ratio'],
            'rr': trade['rr'],
            'stop_pct': trade['stop_pct'],
        },
    }


def _bar_minutes():
    table = {'1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '4h': 240}
    return table.get(params.TIMEFRAME, 60)


def scan_for_setups(pairs, trade_manager, client=None, balance=None):
    """Кандидаты, отсортированные по силе расхождения RSI с ценой."""
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
                    'poi_type': 'BAND',
                    'df_1h': _cache.get(pair, (None, None))[1],
                })
        except Exception as exc:                   # noqa: BLE001
            log(f"   {pair}: ошибка сканирования Боллинджера — {exc}")
            report.record(NAME, pair, f'ошибка сканирования: {exc}')

    report.finish(NAME)
    candidates.sort(key=lambda c: (-c['score'], -c['rr']))
    return candidates
