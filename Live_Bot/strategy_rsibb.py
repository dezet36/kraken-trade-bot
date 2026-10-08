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

ПРОВЕРКА 26.09.2026: КРАЯ НЕТ, ОН БЫЛ ОТ ЧАСОВОГО ИСПОЛНЕНИЯ
(research/rsibb_live_sim.py, results/rsibb_live_sim_run.txt). Замер 08.08
повторён точно — те же +0.063, +0.045, +0.112, +0.128 R на отложенных парах.
Он исполнял заявки по ЧАСОВЫМ свечам, а цель здесь близко (средняя линия):
37% сделок входили и выходили внутри одного часа, 70% из них в плюс, и они
дали +130.5R из +114.1R итога — по OHLC часа не видно, что цена сначала
сходила к цели и лишь потом вернулась к полосе. Те же заявки на 5-минутках
(как у брокера): −0.110R/сд [−0.169; −0.053] на пяти периодах. С правилами
брокера и пределом издержек, то есть ровно как торгует бот: −0.082R/сд
[−0.164; +0.000] по 510 сделкам, на отложенных парах −0.200R [−0.319; −0.084].

ПОЧЕМУ ЧАС, А НЕ ПЯТЬ МИНУТ. Издержки в единицах риска равны кругу комиссий,
делённому на расстояние до стопа. На пятиминутках стоп упирался в пол 0.4% и
круг стоил 0.10-0.16 R, съедая весь валовый край. На часе полосы шире, стоп
около 1.2%, тот же круг стоит 0.058 R. Это единственная величина в формуле,
которой можно управлять, не трогая саму идею.

ВХОД ЛИМИТНЫЙ И ЭТО ОБЯЗАТЕЛЬНО. Заявка стоит НА полосе, цена приходит к ней
сама — круг мейкер-мейкер 0.040% вместо 0.210% у тейкера. Вход по рынку
превратил бы работающую арифметику в заведомо убыточную.
"""

import pandas as pd

import scan_report as report
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


def analyze_market(pair, client=None):
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
    return _to_bot_signal(setup, trade, pair, df)


def _to_bot_signal(setup, trade, pair, df):
    # Сетап без денег: риск, размер и предел в одну сторону решает счёт
    # стратегии (accounts/paper.py).
    dist = abs(trade['entry'] - trade['stop'])

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


def scan_for_setups(pairs, trade_manager, client=None):
    """Кандидаты, отсортированные по силе расхождения RSI с ценой."""
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

            signal = analyze_market(pair, client=client)
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


# ── Единый набор функций реестра стратегий (strategies/registry.py, 08.10.2026) ──
# Раньше эти куски жили ветками в общих модулях: сканирование и сборка сигнала —
# в bot.py, величины исполнения — в strategy_profile.py, разметка — в
# setup_geometry.py. Поведение не изменилось — его держат эталоны tests/golden/.

_BAR_HOURS = {'1m': 1 / 60, '5m': 1 / 12, '15m': 0.25, '30m': 0.5,
              '1h': 1.0, '2h': 2.0, '4h': 4.0, '1d': 24.0}


def scan(pairs, gate, client=None):
    return scan_for_setups(pairs, gate, client=client)


def build_signal(candidate):
    pair = candidate['pair']
    # Сканер уже вернул готовый сигнал: он и есть результат анализа.
    signal = candidate.get('signal')
    if not signal:
        log(f"   RSIBB {pair}: кандидат без сигнала — пропускаю")
        return None, None
    bb = signal.get('rsibb') or {}
    signal['scan'] = {
        'score': candidate.get('score'),
        'rr_est': candidate.get('rr'),
        'rsi': bb.get('rsi'),
        'adx': bb.get('adx'),
        'stop_pct': bb.get('stop_pct'),
    }
    log(f"\n[RSIBB] {pair}: полоса {bb.get('band')}, "
        f"RSI {bb.get('rsi', 0):.0f}, RR {candidate.get('rr', 0):.2f}")
    return signal, candidate.get('df_1h')


def profile():
    # Модуль параметров — заново при каждом вызове: тесты перезагружают его, и
    # схваченный при импорте адаптера был бы чужим (как и в strategy_profile).
    from rsibb import params as p
    bar = _BAR_HOURS.get(str(p.TIMEFRAME), 1.0)
    return {'expiry_hours': p.EXPIRY_BARS * bar, 'cooldown_hours': p.COOLDOWN_HOURS,
            'cost_limit_pct': p.MAX_ENTRY_COST_SHARE_PCT, 'max_hold_hours': p.MAX_HOLD_BARS * bar,
            'fills_through_market': p.FILL_THROUGH_MARKET, 'min_stop_pct': p.MIN_STOP_PCT,
            'max_same_direction': p.MAX_SAME_DIRECTION}


def geometry(signal, g):
    bb = signal.get('rsibb') or {}
    # Канал целиком — это и есть сетап: цена вышла за край, цель на середине.
    g.band(bb.get('lower'), bb.get('upper'),
           f"канал Боллинджера · RSI {bb['rsi']:.0f}" if bb.get('rsi') is not None
           else 'канал Боллинджера')
    if bb.get('mid'):
        g.lines.append({'price': float(bb['mid']), 'label': 'средняя линия — цель', 'main': True})
    if bb.get('band'):
        g.lines.append({
            'price': float(bb['band']),
            'label': 'полоса — вход',
            # Главная линия сетапа: на ней стоит лимитная заявка.
            'main': True,
        })
    start_at = g.iso_time(g.setup.get('start_time'))
    if start_at:
        g.out['from'] = start_at
