"""
Фибоначчи: разбор построения сетки и наложение структуры рынка.

ДВА ВОПРОСА, И ВТОРОЙ ВАЖНЕЕ.

1. НА ЧТО НАТЯГИВАЕТСЯ СЕТКА. «Импульс» в этой стратегии — отрезок между
   ПОСЛЕДНИМ swing-low и ПОСЛЕДНИМ swing-high. Слово «последним» здесь стоит
   дважды, и это не одно и то же, что «соседними»: между ними могут лежать
   другие свинги. Тогда отрезок — не импульс, а кусок пилы, и сетка меряет
   размах болтанки. Плюс есть запасной путь: если свинговый метод не дал
   ничего, берётся глобальный максимум и минимум за 48 свечей — то есть
   ДИАПАЗОН, а не ход. Сколько сделок приходит оттуда — здесь и меряется.

2. КУДА ИДЁТ РЫНОК. Стратегия этого не спрашивает вовсе. Функция
   get_htf_trend в strategy.py есть, но analyze_market её не зовёт: тренд
   участвует только в отборе пар в сканере, а решение о сделке принимается
   без него. То есть сетка натягивается на любой импульс, в том числе на
   отскок против направления рынка.

   Проверяется ДВА разных понимания «структуры», потому что это разные вещи:

       тренд     EMA50/EMA200 на 4H — медленный, инерционный;
       структура HH+HL / LH+LL по свингам 1H — быстрый, ломается сразу.

   Второе — это и есть «структура рынка» в обычном смысле, и она своя, с
   собственными параметрами: чужие сюда не заимствуются.

ЗАЧЕМ ЭТО ПОСЛЕ «ТОЛЬКО ШОРТОВ». Прошлый замер принял вариант «торговать
только вниз»: лонги дают ровно ноль на обоих периодах. Но «только шорты» —
это грубая замена структуры. Если лонги мертвы ПОТОМУ ЧТО их берут в
нисходящей структуре, то правильный ответ не «выключить лонги», а «брать
лонги там, где рынок растёт». Тогда сделок станет больше, а не меньше.
Поэтому обе гипотезы меряются рядом, на одних и тех же сетапах.

КОНТРОЛЬ. Меряется и вариант «против структуры». Если торговля по структуре
работает, против неё обязана быть хуже. Если оба варианта одинаковы, значит
структура не измеряет ничего, и совпадение первого с нулём было удачей.

ПРИЁМКА. Двусторонняя: лучше на ОБОИХ периодах и интервал разницы средних
не накрывает ноль.

Запуск:
    python research/fibo_structure.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from common import ci, diff_ci, hush, unhush  # noqa: E402

PAIRS_LIMIT = 8

# ── Параметры структуры: свои, ни у кого не заимствованы ─────────────────────
# Размах свинга шире, чем у поиска импульса (там 2): структура — вещь более
# крупная, и пивот из пяти баров ловит перегибы, а не рябь.
SWING_N = 3
# Свинги старше этого срока структуру уже не описывают. 120 часовых баров —
# пять суток; за неделю рынок успевает передумать.
SWING_MAX_AGE = 120

BULL, BEAR, RANGE = 'BULL', 'BEAR', 'RANGE'


def structure_series(df, n=SWING_N, max_age=SWING_MAX_AGE):
    """
    Метка структуры для КАЖДОГО бара: BULL, BEAR или RANGE.

    BULL   последний максимум выше предыдущего И последний минимум выше
           предыдущего (HH + HL) — рынок идёт вверх ступенями;
    BEAR   зеркально (LH + LL);
    RANGE  всё остальное, включая случай, когда свинги устарели.

    БЕЗ ЗАГЛЯДЫВАНИЯ ВПЕРЁД. Пивот с центром в баре c подтверждается только
    баром c+n, и раньше этого момента в расчёт не идёт. Метка на баре i
    построена исключительно по барам до i включительно.
    """
    high = df['high'].to_numpy(float)
    low = df['low'].to_numpy(float)
    size = len(df)
    labels = np.array([RANGE] * size, dtype=object)

    highs, lows = [], []
    for i in range(size):
        c = i - n                       # пивот, который подтверждается баром i
        if c >= n:
            wh = high[c - n:c + n + 1]
            wl = low[c - n:c + n + 1]
            if high[c] >= wh.max():
                highs.append((c, float(high[c])))
            if low[c] <= wl.min():
                lows.append((c, float(low[c])))

        if len(highs) < 2 or len(lows) < 2:
            continue
        if i - highs[-1][0] > max_age or i - lows[-1][0] > max_age:
            continue                    # структура протухла

        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        if hh and hl:
            labels[i] = BULL
        elif not hh and not hl:
            labels[i] = BEAR
    return labels


def collect_setups(pair, data):
    """
    Сетапы боевой стратегии с записью структуры на момент сигнала.

    Зовётся та же analyze_market, что и в бою. Структура и тренд РЯДОМ
    записываются, но ни на что не влияют: фильтры проверяются потом, полными
    прогонами портфеля, а не разбиением готовых сделок.
    """
    import config
    import strategy

    df_1h, df_4h = data['1h'], data['4h']
    lookback = config.LOOKBACK_CANDLES
    expiry = np.timedelta64(int(config.PENDING_ORDER_MAX_HOURS * 3600), 's')

    struct = structure_series(df_1h)
    ts_4h = pd.to_datetime(df_4h['timestamp']).dt.tz_localize(None).to_numpy()

    out, seen = [], set()
    for i in range(lookback + 10, len(df_1h)):
        window = df_1h.iloc[i - lookback: i + 1]
        signal = strategy.analyze_market(window, None, pair, 10_000)
        if not signal:
            continue
        setup = signal['setup']
        key = (pair, setup['type'], round(setup['start_price'], 8),
               round(setup['end_price'], 8))
        if key in seen:
            continue
        seen.add(key)

        now = pd.Timestamp(window.iloc[-1]['timestamp'])
        created = now.tz_convert('UTC').tz_localize(None).to_datetime64()
        pos_4h = int(np.searchsorted(ts_4h, created, side='right'))
        htf = strategy.get_htf_trend(df_4h.iloc[max(0, pos_4h - 220):pos_4h])

        prm = signal['params']
        out.append({
            'pair': pair,
            'type': setup['type'],
            'entry': prm['entry'], 'stop': prm['stop_loss'],
            'target': prm['take_profit_1'], 'be': prm['be_level'],
            'rr': prm['rr'],
            'created': created, 'expires': created + expiry, 'key': key,
            'struct': struct[i],
            'htf': htf,
            'source': setup.get('source', '?'),
            'swings_between': setup.get('swings_between', 0),
            'candles': setup.get('candles', 0),
        })
    return out


# ── Правила отбора ──────────────────────────────────────────────────────────
# Каждое — функция от сетапа к «брать или нет». Так вариант читается одной
# строкой, и невозможно случайно сравнить правило само с собой.

def _with(field, bull_ok, bear_ok):
    """По направлению поля: лонг только в росте, шорт только в падении."""
    def rule(s):
        state = s[field]
        return (s['type'] == 'LONG' and state == bull_ok) or \
               (s['type'] == 'SHORT' and state == bear_ok)
    return rule


def _against(field, bull_ok, bear_ok):
    def rule(s):
        state = s[field]
        return (s['type'] == 'LONG' and state == bear_ok) or \
               (s['type'] == 'SHORT' and state == bull_ok)
    return rule


VARIANTS = [
    ('как сейчас',              lambda s: True),
    ('только шорты',            lambda s: s['type'] == 'SHORT'),
    # Структура 1H
    ('по структуре 1H',         _with('struct', BULL, BEAR)),
    ('против структуры 1H',     _against('struct', BULL, BEAR)),
    ('лонг по структуре + все шорты',
     lambda s: s['type'] == 'SHORT' or s['struct'] == BULL),
    ('шорты по структуре',      lambda s: s['type'] == 'SHORT' and s['struct'] == BEAR),
    # Тренд 4H
    ('по тренду 4H',            _with('htf', 'BULLISH', 'BEARISH')),
    ('лонг по тренду + все шорты',
     lambda s: s['type'] == 'SHORT' or s['htf'] == 'BULLISH'),
    # Происхождение импульса
    ('только чистый импульс',   lambda s: s['swings_between'] == 0),
    ('без запасного пути',      lambda s: s['source'] != 'range'),
]


def run_variant(setups, data, rule):
    import config
    from smc_engine import Order, run_portfolio

    orders = []
    for s in setups:
        if not rule(s):
            continue
        orders.append(Order(
            pair=s['pair'], direction=s['type'],
            entry=s['entry'], stop=s['stop'],
            targets=[s['target']], fractions=[1.0],
            created=s['created'], expires=s['expires'], key=s['key'],
            be_trigger=s['be'] if config.BREAKEVEN_AT_B else None,
            meta={'direction': s['type'], 'struct': s['struct']},
        ))
    if not orders:
        return None

    result = run_portfolio(
        orders, {p: data[p]['5m'] for p in data},
        risk_pct=config.RISK_PER_TRADE,
        max_positions=getattr(config, 'MAX_OPEN_POSITIONS', 5),
        cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
        max_same_direction=getattr(config, 'MAX_SAME_DIRECTION', 0),
        breakeven_after_tp1=False)

    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 3:
        return None
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    longs = np.array([t['pnl'] / t['risk'] for t in trades
                      if (t.get('meta') or {}).get('direction') == 'LONG'])
    from smc_engine import compute_stats
    stats = compute_stats(result)
    return {'r': r, 'n': len(trades), 'mean': float(r.mean()),
            'total': float(r.sum()), 'wr': float((r > 0).mean() * 100),
            'longs': len(longs), 'orders': len(orders),
            'dd': stats['max_dd_pct'], 'ret': stats['return_pct']}


def load(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    data, setups = {}, []
    quiet = hush()
    try:
        for pair in pairs[:PAIRS_LIMIT]:
            loaded = bt.load_pair(pair)
            if loaded is None:
                continue
            data[pair] = loaded
            setups += collect_setups(pair, loaded)
            print(f'      {pair}: сетапов всего {len(setups)}', flush=True)
    finally:
        unhush(quiet)
    return data, setups


def anatomy(setups, label):
    """Из чего вообще состоят сетапы: на что натянута сетка и что вокруг."""
    df = pd.DataFrame(setups)
    print()
    print('=' * 100)
    print(f'ИЗ ЧЕГО СДЕЛАНЫ СЕТАПЫ · {label}   всего {len(df)}')
    print('=' * 100)

    print('происхождение импульса:')
    for src, part in df.groupby('source'):
        name = {'swing': 'свинги (штатный путь)',
                'range': 'весь диапазон (запасной путь)'}.get(src, src)
        print(f'   {name:<34}{len(part):>7}   {len(part) / len(df) * 100:>5.1f}%')

    print('свингов ВНУТРИ «импульса»:')
    for k, part in df.groupby('swings_between'):
        tag = 'чистый ход' if k == 0 else f'{k} промежуточных'
        print(f'   {tag:<34}{len(part):>7}   {len(part) / len(df) * 100:>5.1f}%')

    print('структура 1H в момент сигнала (строки — сторона сделки):')
    cross = pd.crosstab(df['type'], df['struct'])
    print(cross.to_string())
    print('тренд 4H в момент сигнала:')
    print(pd.crosstab(df['type'], df['htf']).to_string())


def report(results, base_name='как сейчас'):
    print()
    print('=' * 100)
    print('СРАВНЕНИЕ С НЫНЕШНЕЙ НАСТРОЙКОЙ (интервал разницы средних)')
    print('=' * 100)
    head = f'{"вариант":<34}' + ''.join(f'{lbl:>32}' for lbl in results)
    print(head)
    print('-' * len(head))
    for name, _rule in VARIANTS:
        if name == base_name:
            continue
        cells, verdicts = '', []
        for label, table in results.items():
            res, base = table.get(name), table.get(base_name)
            if not res or not base:
                cells += f'{"—":>32}'
                verdicts.append(False)
                continue
            lo, hi = diff_ci(res['r'], base['r'])
            gain = res['mean'] - base['mean']
            cell = f'{gain:+.3f} [{lo:+.3f}; {hi:+.3f}] n={res["n"]}'
            cells += f'{cell:>32}'
            verdicts.append(gain > 0 and lo > 0)
        mark = '  ЛУЧШЕ на обоих' if all(verdicts) and verdicts else ''
        print(f'{name:<34}{cells}{mark}')

    print()
    print('Контроль: «против структуры» обязана быть ХУЖЕ, чем «по структуре».')
    print('Если они неотличимы — структура не измеряет ничего, и совпадение')
    print('первой с плюсом было бы случайностью.')


def main():
    results, periods = {}, {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = load(cache, pairs, label)

    for label, (data, setups) in periods.items():
        anatomy(setups, label)
        print()
        print('=' * 100)
        print(f'{label}')
        print('=' * 100)
        head = (f'{"вариант":<34}{"заявок":>8}{"сделок":>8}{"лонгов":>8}'
                f'{"винрейт":>9}{"R/сделку":>10}{"сумма R":>9}'
                f'{"доход%":>9}{"DD%":>7}{"интервал":>24}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, rule in VARIANTS:
            res = run_variant(setups, data, rule)
            if res is None:
                print(f'{name:<34}{"— сделок нет":>16}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<34}{res["orders"]:>8}{res["n"]:>8}{res["longs"]:>8}'
                  f'{res["wr"]:>8.1f}%{res["mean"]:>10.3f}{res["total"]:>9.1f}'
                  f'{res["ret"]:>9.1f}{res["dd"]:>7.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>24}')

    report(results)


if __name__ == '__main__':
    main()
