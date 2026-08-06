"""
Фибоначчи: окупаются ли четыре порога отбора и что делать с издержками.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ АУДИТА.

Аудит (fibo_audit.py) уже показал, что ни один из четырёх порогов не
оправдан. Но он отвечал на УЗКИЙ вопрос: «отличаются ли по качеству сделки,
которые порог пропускает, от тех, которые он режет». Разбиение готовых
сделок не меняет занятость слотов, а фильтр меняет: выброшенная сделка
освобождает место следующей. На Фибоначчи это оказалось решающим — вариант
«только шорты» дал не 929 сделок, как считало разбиение, а 1710, и средний
результат вырос не на четверть, а втрое.

Поэтому здесь каждый вариант считается ПОЛНЫМ прогоном портфеля.

ЧЕСТНАЯ ОГОВОРКА О МЕТОДЕ, БЕЗ НЕЁ ЧИТАТЬ НЕЛЬЗЯ. Сетапы ищутся ОДИН раз с
ослабленными порогами, а варианты строятся отбором из полученного. Это не
то же самое, что прогон с другим конфигом: порог длительности стоит ВНУТРИ
поиска импульса, и с ним поиск иногда возвращает другой импульс, а не тот
же самый минус фильтр. Строгим надмножеством ослабленный прогон не является.

Насколько это врёт — проверяется прямо в отчёте: вариант «как сейчас»
собран из ослабленного прогона и обязан повторить живые числа (+0.046 R на
бычьем, +0.038 на медвежьем). Если повторяет — допущение рабочее. Если нет
— всё, что ниже, читать не стоит, и это будет видно первой же строкой.

ИЗДЕРЖКИ. Аудит намерил, что комиссии и проскальзывание съедают 53-62%
валового края — это главная беда стратегии. Рычаг тут ровно один и он
арифметический: издержки берутся с ОБЪЁМА позиции, а объём равен риску,
делённому на расстояние до стопа. Значит в единицах R издержки обратно
пропорциональны стопу: круг 0.21% при стопе 1% стоит 0.21 R, а при стопе
2% — уже 0.105 R. Отсюда варианты с полом по расстоянию до стопа: они не
«улучшают вход», они меняют то, какую долю края отдаёт биржа.

ПРИЁМКА. Двусторонняя: лучше «как сейчас» на ОБОИХ периодах и интервал
разницы средних не накрывает ноль.

Запуск:
    python research/fibo_filters.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from fibo_audit import ci, diff_ci, hush, relax, unhush  # noqa: E402

PAIRS_LIMIT = 8

# Живые пороги фиксируем числами ЗДЕСЬ, а не читаем из config во время
# разбора: к тому моменту config уже побывал ослабленным и восстановленным,
# и один пропущенный restore молча превратил бы «как сейчас» в «без порогов».
LIVE = {'impulse_pct': 3.0, 'candles': 24, 'velocity': 0.30, 'rr': 1.1}


def collect_setups(pair, data):
    """Сетапы с ослабленными порогами и записью всего, по чему потом отбор."""
    import config
    import strategy

    df_1h = data['1h']
    lookback = config.LOOKBACK_CANDLES
    expiry = np.timedelta64(int(config.PENDING_ORDER_MAX_HOURS * 3600), 's')

    out, seen = [], set()
    for i in range(lookback + 10, len(df_1h)):
        window = df_1h.iloc[i - lookback: i + 1]
        signal = strategy.analyze_market(window, None, pair, 10_000)
        if not signal:
            continue
        setup, prm = signal['setup'], signal['params']
        key = (pair, setup['type'], round(setup['start_price'], 8),
               round(setup['end_price'], 8))
        if key in seen:
            continue
        seen.add(key)

        now = pd.Timestamp(window.iloc[-1]['timestamp'])
        created = now.tz_convert('UTC').tz_localize(None).to_datetime64()
        size_pct = setup['size'] / setup['end_price'] * 100
        candles = setup.get('candles') or 1
        entry, stop = prm['entry'], prm['stop_loss']
        out.append({
            'pair': pair, 'type': setup['type'],
            'entry': entry, 'stop': stop,
            'target': prm['take_profit_1'], 'be': prm['be_level'],
            'created': created, 'expires': created + expiry, 'key': key,
            'impulse_pct': size_pct,
            'candles': candles,
            'velocity': size_pct / candles,
            'rr': prm['rr'],
            'stop_pct': abs(entry - stop) / entry * 100,
            'source': setup.get('source', '?'),
        })
    return out


# ── Варианты: каждый — набор порогов ────────────────────────────────────────
# Отсутствие ключа означает «порог снят». Так вариант читается целиком, и
# нельзя случайно оставить включённым то, что собирался выключить.

def rule(**gates):
    def check(s):
        if 'impulse_pct' in gates and s['impulse_pct'] < gates['impulse_pct']:
            return False
        if 'candles' in gates and s['candles'] > gates['candles']:
            return False
        if 'velocity' in gates and s['velocity'] < gates['velocity']:
            return False
        if 'rr' in gates and s['rr'] < gates['rr']:
            return False
        if 'min_stop' in gates and s['stop_pct'] < gates['min_stop']:
            return False
        return True
    return check


VARIANTS = [
    ('как сейчас',                rule(**LIVE)),
    ('без длительности',          rule(impulse_pct=3.0, velocity=0.30, rr=1.1)),
    ('без размера импульса',      rule(candles=24, velocity=0.30, rr=1.1)),
    ('без скорости',              rule(impulse_pct=3.0, candles=24, rr=1.1)),
    ('без порога RR',             rule(impulse_pct=3.0, candles=24, velocity=0.30)),
    ('без всех четырёх',          rule()),
    ('стоп не меньше 1.0%',       rule(min_stop=1.0, **LIVE)),
    ('стоп не меньше 1.5%',       rule(min_stop=1.5, **LIVE)),
    ('стоп не меньше 2.0%',       rule(min_stop=2.0, **LIVE)),
    ('без всех + стоп 1.5%',      rule(min_stop=1.5)),
]


def run_variant(setups, data, check):
    import config
    from smc_engine import Order, compute_stats, run_portfolio

    orders = []
    for s in setups:
        if not check(s):
            continue
        orders.append(Order(
            pair=s['pair'], direction=s['type'],
            entry=s['entry'], stop=s['stop'],
            targets=[s['target']], fractions=[1.0],
            created=s['created'], expires=s['expires'], key=s['key'],
            be_trigger=s['be'] if config.BREAKEVEN_AT_B else None,
            meta={'direction': s['type'], 'stop_pct': s['stop_pct']},
        ))
    if len(orders) < 3:
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
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    gross = r + costs
    share = float(costs.mean() / gross.mean() * 100) if gross.mean() else np.nan
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'mean': float(r.mean()), 'total': float(r.sum()),
            'wr': float((r > 0).mean() * 100),
            'costs': float(costs.mean()), 'gross': float(gross.mean()),
            'share': share, 'dd': stats['max_dd_pct'], 'ret': stats['return_pct']}


def load(cache_dir, pairs, label):
    import config

    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    data = {}
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is not None:
            data[pair] = loaded

    saved = relax(config)
    quiet = hush()
    setups = []
    try:
        for pair in data:
            setups += collect_setups(pair, data[pair])
            print(f'      {pair}: сетапов всего {len(setups)}', flush=True)
    finally:
        unhush(quiet)
        for name, value in saved.items():
            setattr(config, name, value)
    return data, setups


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = load(cache, pairs, label)

    results = {}
    for label, (data, setups) in periods.items():
        print()
        print('=' * 112)
        print(f'{label}   сетапов в ослабленном прогоне: {len(setups)}')
        print('=' * 112)
        head = (f'{"вариант":<26}{"заявок":>8}{"сделок":>8}{"винрейт":>9}'
                f'{"R вал.":>9}{"издержки":>10}{"доля":>7}{"R/сделку":>10}'
                f'{"сумма R":>9}{"DD%":>7}{"интервал":>24}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, check in VARIANTS:
            res = run_variant(setups, data, check)
            if res is None:
                print(f'{name:<26}{"— сделок нет":>16}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<26}{res["orders"]:>8}{res["n"]:>8}{res["wr"]:>8.1f}%'
                  f'{res["gross"]:>9.3f}{res["costs"]:>10.3f}{res["share"]:>6.0f}%'
                  f'{res["mean"]:>10.3f}{res["total"]:>9.1f}{res["dd"]:>7.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>24}')

    # ── Проверка допущения ──────────────────────────────────────────────────
    print()
    print('=' * 112)
    print('ПРОВЕРКА ДОПУЩЕНИЯ: «как сейчас» из ослабленного прогона против живых чисел')
    print('=' * 112)
    known = {'бык 2025-26': 0.046, 'медведь 2022-23': 0.038}
    ok = True
    for label, expect in known.items():
        got = results.get(label, {}).get('как сейчас')
        if not got:
            print(f'{label:<20} — не собрался')
            ok = False
            continue
        lo, hi = ci(got['r'])
        inside = lo <= expect <= hi
        ok = ok and inside
        print(f'{label:<20} живое {expect:+.3f}   здесь {got["mean"]:+.3f} '
              f'[{lo:+.3f}; {hi:+.3f}]   {"совпадает" if inside else "РАСХОДИТСЯ"}')
    print()
    print('Совпадает — ослабленный прогон можно считать надмножеством и читать' if ok
          else 'РАСХОДИТСЯ — ослабленный прогон даёт другие сетапы; таблицу ниже не читать')

    print()
    print('=' * 112)
    print('СРАВНЕНИЕ С НЫНЕШНЕЙ НАСТРОЙКОЙ (интервал разницы средних)')
    print('=' * 112)
    head = f'{"вариант":<26}' + ''.join(f'{lbl:>34}' for lbl in results)
    print(head)
    print('-' * len(head))
    for name, _check in VARIANTS:
        if name == 'как сейчас':
            continue
        cells, verdicts = '', []
        for label, table in results.items():
            res, base = table.get(name), table.get('как сейчас')
            if not res or not base:
                cells += f'{"—":>34}'
                verdicts.append(False)
                continue
            lo, hi = diff_ci(res['r'], base['r'])
            gain = res['mean'] - base['mean']
            cell = f'{gain:+.3f} [{lo:+.3f}; {hi:+.3f}] n={res["n"]}'
            cells += f'{cell:>34}'
            verdicts.append(gain > 0 and lo > 0)
        mark = '  ЛУЧШЕ на обоих' if all(verdicts) and verdicts else ''
        print(f'{name:<26}{cells}{mark}')

    print()
    print('Столбец «доля» — какую часть валового края забирают издержки. Пороги')
    print('по стопу двигают именно её: чем шире стоп, тем меньше объём позиции')
    print('при том же риске, и тем дешевле обходится круг в единицах R.')


if __name__ == '__main__':
    main()
