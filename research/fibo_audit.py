"""
Аудит стратегии Фибоначчи: есть ли у неё край и какие фильтры его дают.

ЗАЧЕМ. Из трёх стратегий проекта эта — единственная, которую никто не
разбирал. И она же самая слабая на сделку: +0.089 R на бычьем периоде и
+0.072 R на медвежьем против +0.32…+0.48 у двух других. При этом сделок она
делает вчетверо больше, то есть весь её результат держится на объёме, а не на
крае. Круг издержек в модели — комиссии 0.02/0.055% и проскальзывание 0.05% —
при типичном стопе в полтора-два процента съедает порядка 0.05-0.07 R. То
есть издержки сопоставимы со всем краем целиком, и первый вопрос звучит
неприятно: остаётся ли там вообще что-нибудь.

Второй повод. За один день у этой стратегии вскрылись подряд две
необоснованные настройки: часовой блок 12-16 UTC оказался шумом (и отбрасывал
лучшую четверть сделок), а безубыток никто не мерил. Пороги выглядят так же
подозрительно: MIN_IMPULSE_PCT=3.0, MAX_IMPULSE_CANDLES=24,
MIN_IMPULSE_VELOCITY=0.30, MIN_RR=1.1 — круглые числа, поставленные по одному
периоду.

КАК УСТРОЕН ЗАМЕР. Поиск сетапов у этой стратегии дорогой: боевая
analyze_market зовётся на каждой часовой свече каждой пары. Гонять его заново
под каждый выключенный фильтр — часы счёта. Поэтому здесь ОДИН прогон с
ОСЛАБЛЕННЫМИ порогами, и для каждой сделки запоминается, какие значения она
имела: размер импульса, длительность, скорость, RR, тренд старшего ТФ, час
входа. Дальше фильтры проверяются разбиением уже полученных сделок.

ЧЕСТНАЯ ОГОВОРКА О МЕТОДЕ. Разбиение готовых сделок — не то же самое, что
прогон с включённым фильтром: фильтр меняет ещё и занятость слотов, а значит
и состав портфеля. Здесь отвечается более узкий вопрос — «отличаются ли по
качеству сделки, которые фильтр пропускает, от тех, которые он режет». Если
не отличаются, фильтр не окупает выброшенных сделок, и дальше уже имеет смысл
платить за полный прогон. Если отличаются — тем более.

Один фильтр проверить так нельзя: правило «не меньше 60% свечей направлены»
зашито в код без настройки. Оно остаётся включённым во всех вариантах.

Запуск:
    python research/fibo_audit.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS)

BOOTSTRAP = 10_000
RNG = np.random.default_rng(20260806)

# Пул сокращён ради времени: поиск сетапов зовёт боевую analyze_market на
# каждой часовой свече. Для вопроса «работает ли фильтр» этого достаточно —
# сделок остаются тысячи, а фильтр не зависит от того, какие именно пары в
# пуле.
PAIRS_LIMIT = 8


def ci(values, alpha=0.05):
    v = np.asarray(values, dtype=float)
    if len(v) < 3:
        return (np.nan, np.nan)
    draws = RNG.choice(v, size=(BOOTSTRAP, len(v)), replace=True).mean(axis=1)
    return tuple(np.percentile(draws, [alpha / 2 * 100, (1 - alpha / 2) * 100]))


def diff_ci(a, b, alpha=0.05):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3 or len(b) < 3:
        return (np.nan, np.nan)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return tuple(np.percentile(d, [alpha / 2 * 100, (1 - alpha / 2) * 100]))


def hush():
    """
    Глушит журнал стратегии на время поиска.

    Боевой код пишет строку на КАЖДУЮ просмотренную свечу — «нет сигнала,
    цена вне окна входа». В работе это полезно, в замере это двести тысяч
    записей на пару, и упирается прогон именно в них, а не в счёт. С
    выключенным журналом одна пара считается минуты вместо десятков минут.
    """
    import strategy
    saved = strategy.log
    strategy.log = lambda *a, **k: None
    return saved


def unhush(saved):
    import strategy
    strategy.log = saved


def relax(config):
    """
    Снимает пороги, чтобы прогон дал НАДМНОЖЕСТВО сетапов.

    Возвращает прежние значения: их надо вернуть перед разбором, иначе
    «текущая настройка» будет сравниваться сама с собой.
    """
    saved = {name: getattr(config, name) for name in
             ('MIN_IMPULSE_PCT', 'MAX_IMPULSE_CANDLES', 'MIN_IMPULSE_VELOCITY',
              'MIN_RR', 'BLOCK_ENTRY_HOURS_UTC')}
    config.MIN_IMPULSE_PCT = 0.0
    config.MAX_IMPULSE_CANDLES = 10 ** 6
    config.MIN_IMPULSE_VELOCITY = 0.0
    config.MIN_RR = 0.0
    config.BLOCK_ENTRY_HOURS_UTC = frozenset()
    return saved


def build_orders(pair, data, htf_filter=False):
    """
    Сетапы Фибоначчи с записью всего, по чему потом проверяются фильтры.

    Повторяет боевой путь: та же find_recent_impulse, та же analyze_market.
    Отличие ровно одно — гейты сканера здесь не применяются, а
    ЗАПИСЫВАЮТСЯ. Иначе замер проверял бы не фильтры, а собственную копию
    стратегии.
    """
    import config
    import strategy
    from smc_engine import Order

    df_1h, df_4h = data['1h'], data['4h']
    lookback = config.LOOKBACK_CANDLES
    expiry = np.timedelta64(int(config.PENDING_ORDER_MAX_HOURS * 3600), 's')
    ts_4h = pd.to_datetime(df_4h['timestamp']).dt.tz_localize(None).to_numpy()

    orders, seen = [], set()
    for i in range(lookback + 10, len(df_1h)):
        window = df_1h.iloc[i - lookback: i + 1]
        now = window.iloc[-1]['timestamp']
        now_naive = pd.Timestamp(now).tz_convert('UTC').tz_localize(None).to_datetime64()

        # Зовём ТОЛЬКО analyze_market: она сама внутри ищет импульс и отдаёт
        # его в ответе. Отдельный вызов find_recent_impulse снаружи удваивал
        # бы самую дорогую часть работы, а поиск и так идёт по каждой часовой
        # свече каждой пары.
        signal = strategy.analyze_market(window, None, pair, 10_000)
        if not signal:
            continue

        setup = signal['setup']
        prm = signal['params']
        size_pct = setup['size'] / setup['end_price'] * 100
        # Длительность импульса стратегия не отдаёт числом, но отдаёт края.
        # Данные часовые, поэтому считаем по времени — так же, как считает
        # сам фильтр внутри find_recent_impulse.
        span = pd.Timestamp(setup['end_time']) - pd.Timestamp(setup['start_time'])
        candles = int(round(span.total_seconds() / 3600)) + 1
        pos_4h = int(np.searchsorted(ts_4h, now_naive, side='right'))
        htf = strategy.get_htf_trend(df_4h.iloc[max(0, pos_4h - 220):pos_4h])
        key = (pair, setup['type'], round(setup['start_price'], 8),
               round(setup['end_price'], 8))
        if key in seen:
            continue
        seen.add(key)

        created = now_naive
        orders.append(Order(
            pair=pair,
            direction='LONG' if setup['type'] == 'LONG' else 'SHORT',
            entry=prm['entry'], stop=prm['stop_loss'],
            targets=[prm['take_profit_1']], fractions=[1.0],
            created=created, expires=created + expiry, key=key,
            be_trigger=prm['be_level'] if config.BREAKEVEN_AT_B else None,
            meta={'impulse_pct': size_pct,
                  'candles': candles,
                  'velocity': size_pct / candles if candles else 0.0,
                  'rr': prm['rr'],
                  'htf': htf,
                  'direction': setup['type'],
                  'hour': pd.Timestamp(now).hour,
                  # «Контртренд» — то, что вырезал бы фильтр старшего ТФ.
                  'counter_trend': (htf == 'BULLISH' and setup['type'] == 'SHORT')
                                   or (htf == 'BEARISH' and setup['type'] == 'LONG')},
        ))
    return orders


def run_period(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt
    import config
    from smc_engine import run_portfolio

    print(f'[{label}] загрузка...', flush=True)
    data = {}
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
    print(f'   пар: {len(data)}', flush=True)

    saved = relax(config)
    quiet = hush()
    orders = []
    try:
        for pair in data:
            orders += build_orders(pair, data[pair])
            print(f'      {pair}: заявок всего {len(orders)}', flush=True)
    finally:
        unhush(quiet)
        for name, value in saved.items():
            setattr(config, name, value)

    result = run_portfolio(
        orders, {p: data[p]['5m'] for p in data},
        risk_pct=config.RISK_PER_TRADE,
        max_positions=getattr(config, 'MAX_OPEN_POSITIONS', 5),
        cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
        max_same_direction=getattr(config, 'MAX_SAME_DIRECTION', 0),
        breakeven_after_tp1=False)

    rows = []
    for t in result['trades']:
        if not t.get('risk'):
            continue
        meta = t.get('meta') or {}
        gross = t['pnl'] + t.get('fees', 0) + t.get('funding', 0)
        rows.append({
            'r': t['pnl'] / t['risk'],
            'r_gross': gross / t['risk'],
            'costs_r': (t.get('fees', 0) + t.get('funding', 0)) / t['risk'],
            'reason': str(t.get('exit_reason', '')),
            **{k: meta.get(k) for k in ('impulse_pct', 'candles', 'velocity',
                                        'rr', 'htf', 'direction', 'hour',
                                        'counter_trend')},
        })
    return pd.DataFrame(rows)


def describe(df):
    if df.empty:
        return dict(n=0, wr=np.nan, mean=np.nan, total=np.nan)
    return dict(n=len(df), wr=(df['r'] > 0).mean() * 100,
                mean=df['r'].mean(), total=df['r'].sum())


def block(title):
    print()
    print('=' * 96)
    print(title)
    print('=' * 96)


def costs_report(frames):
    block('1. ИЗДЕРЖКИ: остаётся ли край, если считать честно')
    head = (f'{"период":<18}{"сделок":>8}{"R до издержек":>15}{"издержки":>11}'
            f'{"R после":>10}{"доля издержек":>16}')
    print(head); print('-' * len(head))
    for label, df in frames.items():
        gross, costs, net = df['r_gross'].mean(), df['costs_r'].mean(), df['r'].mean()
        share = costs / gross * 100 if gross else np.nan
        print(f'{label:<18}{len(df):>8}{gross:>15.3f}{costs:>11.3f}{net:>10.3f}'
              f'{share:>15.1f}%')
    print()
    print('Читается так: если издержки съедают больше половины валового края,')
    print('стратегия живёт на грани, и любая недооценка комиссий её обнуляет.')


def filter_report(frames, name, mask_fn, description):
    block(f'{name}')
    print(description)
    print()
    head = (f'{"период":<18}{"сторона":<12}{"сделок":>8}{"винрейт":>9}'
            f'{"R/сделку":>11}{"сумма R":>10}{"интервал среднего":>24}')
    print(head); print('-' * len(head))
    verdicts = []
    for label, df in frames.items():
        keep = df[mask_fn(df)]
        drop = df[~mask_fn(df)]
        for side, part in (('пропускает', keep), ('режет', drop)):
            s = describe(part)
            lo, hi = ci(part['r']) if len(part) >= 3 else (np.nan, np.nan)
            print(f'{label:<18}{side:<12}{s["n"]:>8}{s["wr"]:>8.1f}%'
                  f'{s["mean"]:>11.3f}{s["total"]:>10.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>24}')
        if len(keep) >= 3 and len(drop) >= 3:
            lo, hi = diff_ci(keep['r'], drop['r'])
            gain = keep['r'].mean() - drop['r'].mean()
            crosses = not (lo > 0 or hi < 0)
            print(f'{"":<18}{"разница":<12}{gain:>+19.3f}{"":>10}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>24}'
                  f'{"  шум" if crosses else "  ЕСТЬ"}')
            verdicts.append((label, gain, crosses))
        print('-' * len(head))
    works = all(g > 0 and not c for _, g, c in verdicts) and len(verdicts) == len(frames)
    print(f'ВЕРДИКТ: фильтр {"оправдан" if works else "НЕ оправдан"} '
          f'(нужно: лучше на обоих периодах и интервал не накрывает ноль)')
    return works


def main():
    frames = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        frames[label] = run_period(cache, pairs, label)

    import config
    costs_report(frames)

    filter_report(frames, f'2. РАЗМЕР ИМПУЛЬСА (сейчас >= {config.MIN_IMPULSE_PCT}%)',
                  lambda d: d['impulse_pct'] >= config.MIN_IMPULSE_PCT,
                  'Отсекает мелкие движения. Проверяем, действительно ли крупные лучше.')

    filter_report(frames, f'3. ДЛИТЕЛЬНОСТЬ (сейчас <= {config.MAX_IMPULSE_CANDLES} свечей)',
                  lambda d: d['candles'] <= config.MAX_IMPULSE_CANDLES,
                  'Импульс, а не долгая торговля. Проверяем, хуже ли длинные ходы.')

    filter_report(frames, f'4. СКОРОСТЬ (сейчас >= {config.MIN_IMPULSE_VELOCITY}% на свечу)',
                  lambda d: d['velocity'] >= config.MIN_IMPULSE_VELOCITY,
                  'Резкость хода. Проверяем, лучше ли быстрые импульсы.')

    filter_report(frames, f'5. ПОРОГ RR (сейчас >= {config.MIN_RR})',
                  lambda d: d['rr'] >= config.MIN_RR,
                  'Отсекает сделки с плохой геометрией.')

    filter_report(frames, '6. ФИЛЬТР ТРЕНДА СТАРШЕГО ТФ',
                  lambda d: ~d['counter_trend'].astype(bool),
                  'Не торговать против направления 4H. Проверяем, хуже ли контртренд.')

    block('7. НАПРАВЛЕНИЕ')
    head = (f'{"период":<18}{"сторона":<9}{"сделок":>8}{"винрейт":>9}'
            f'{"R/сделку":>11}{"сумма R":>10}{"интервал среднего":>24}')
    print(head); print('-' * len(head))
    for label, df in frames.items():
        for side in ('LONG', 'SHORT'):
            part = df[df['direction'] == side]
            s = describe(part)
            lo, hi = ci(part['r']) if len(part) >= 3 else (np.nan, np.nan)
            print(f'{label:<18}{side:<9}{s["n"]:>8}{s["wr"]:>8.1f}%'
                  f'{s["mean"]:>11.3f}{s["total"]:>10.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>24}')

    block('8. ПРИЧИНЫ ВЫХОДА')
    for label, df in frames.items():
        print(f'{label}:')
        grouped = df.groupby('reason')['r'].agg(['count', 'mean', 'sum'])
        for reason, row in grouped.sort_values('count', ascending=False).iterrows():
            print(f'   {reason:<16}{int(row["count"]):>7}  R/сделку {row["mean"]:+.3f}'
                  f'  сумма {row["sum"]:+.1f}')


if __name__ == '__main__':
    main()
