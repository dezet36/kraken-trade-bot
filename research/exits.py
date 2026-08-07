"""
Ведение позиции на трёх работающих стратегиях: частичная фиксация, безубыток,
подтяжка стопа.

ОТКУДА ЗАДАЧА. Механика ведения позиции строилась и проверялась на волновой
стратегии, где ей нечего было защищать: там вход не знает направления
(медианный ход в пользу позиции 1.00 R, против 1.03 R). У FIBO, LEVELS и SMC
край принят на двух независимых периодах — значит есть что защищать, и та же
механика может дать прибавку там, где она превращается в деньги.

ВАЖНОЕ УСТРОЙСТВО ЗАМЕРА: СРАВНЕНИЕ ПАРНОЕ. Варианты отличаются ТОЛЬКО
геометрией выхода и применяются к одним и тем же заявкам. Значит одна и та же
заявка даёт сделку в каждом варианте, и разницу надо считать по совпадающим
ключам, а не по средним двух независимых выборок.

Это не педантизм, а вопрос о том, способен ли замер вообще что-то увидеть. У
уровней около 450 сделок за период, интервал средней ±0.17 R; интервал РАЗНИЦЫ
двух независимых выборок был бы около ±0.24 R. Требовать улучшения крупнее
0.24 R при базе 0.279 — это не проверка, а её вид: такое правило невыполнимо
по построению. Ровно эту ошибку в проекте уже допускали однажды, и повторять
её под конец разбора выходов было бы обидно. Парная разница снимает из дисперсии
всё, что относится ко входу — время, пару, направление, — и оставляет только
вклад выхода.

ЧЕГО ЗАМЕР НЕ ДЕЛАЕТ. Не трогает вход, отбор сетапов и пороги: они у каждой
стратегии свои, измерены отдельно и здесь не пересматриваются. Портфельные
параметры (риск, пауза, предел позиций, предел удержания) тоже берутся у каждой
стратегии свои — как в её собственном замере.

Цели задаются В ЕДИНИЦАХ РИСКА, а не в процентах и не в ATR: только так вариант
осмыслен сразу для трёх стратегий с разной геометрией. «Своя цель» означает ту,
которую стратегия ставит сейчас.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА:

    на ОБОИХ периодах парная разница положительна, её интервал не накрывает
    ноль И просадка выросла не более чем на 3 процентных пункта.

Последнее условие обязательно: частичная фиксация и безубыток почти всегда
поднимают долю прибыльных сделок, и без ограничения на просадку «улучшением»
окажется любое размывание риска.

Запуск:
    python research/exits.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import direction_across as da  # noqa: E402
from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from fibo_audit import ci, hush, unhush  # noqa: E402
from smc_market_regime import load_period  # noqa: E402

# Цель задаётся парой: ('r', X) — X единиц риска от входа;
#                      ('own', k) — k долей от СВОЕЙ цели стратегии.
#   tps    — список целей, fracs — доли позиции на каждой,
#   be     — безубыток после первой цели, trail — подтяжка стопа в R.
BASE = {'tps': None, 'fracs': None, 'be': False, 'trail': 0.0}

VARIANTS = [
    ('как сейчас', BASE),

    # ── Частичная фиксация ─────────────────────────────────────────────────
    ('половина на 1R, остаток на своей цели',
     {'tps': [('r', 1.0), ('own', 1.0)], 'fracs': [0.5, 0.5]}),
    ('треть на 1R, остаток на своей цели',
     {'tps': [('r', 1.0), ('own', 1.0)], 'fracs': [0.33, 0.67]}),
    ('половина на своей цели, остаток вдвое дальше',
     {'tps': [('own', 1.0), ('own', 2.0)], 'fracs': [0.5, 0.5]}),

    # ── То же плюс безубыток ───────────────────────────────────────────────
    ('половина на 1R · дальше безубыток',
     {'tps': [('r', 1.0), ('own', 1.0)], 'fracs': [0.5, 0.5], 'be': True}),
    ('половина на своей цели · дальше безубыток вдвое дальше',
     {'tps': [('own', 1.0), ('own', 2.0)], 'fracs': [0.5, 0.5], 'be': True}),

    # ── Подтяжка стопа ─────────────────────────────────────────────────────
    ('трейлинг 1.5R',                   {'trail': 1.5}),
    ('трейлинг 2.5R',                   {'trail': 2.5}),
    ('цель вдвое дальше + трейлинг 2R', {'tps': [('own', 2.0)], 'fracs': [1.0],
                                         'trail': 2.0}),
    ('половина на 1R, остаток трейлингом 2R',
     {'tps': [('r', 1.0), ('own', 3.0)], 'fracs': [0.5, 0.5], 'trail': 2.0}),
]


def reshape(order, spec):
    """
    Та же заявка с другой геометрией выхода. None — геометрия рассыпалась.

    Цели пересобираются в единицах риска и сортируются: вариант может задать
    цель БЛИЖЕ той, что у стратегии уже стоит, и порядок нарушится. Слипшиеся
    цели складывают доли, а не порождают вторую заявку на том же уровне.
    """
    from smc_engine import Order

    risk = abs(order.entry - order.stop)
    if risk <= 0:
        return None
    trail = (spec.get('trail') or 0.0) * risk
    if spec.get('tps') is None:
        if not trail:
            return order
        levels, fracs = list(order.targets), list(order.fractions)
    else:
        long_side = order.direction in ('LONG', 'BULLISH', 'BUY')
        own_r = abs(order.targets[-1] - order.entry) / risk
        wanted = []
        for (kind, value), frac in zip(spec['tps'], spec['fracs']):
            rr = value if kind == 'r' else own_r * value
            wanted.append((rr, frac))
        keep_r, keep_f = [], []
        for rr, frac in sorted(wanted):
            if rr <= 0:
                continue
            if keep_r and rr <= keep_r[-1] + 1e-9:
                keep_f[-1] += frac
                continue
            keep_r.append(rr)
            keep_f.append(frac)
        if not keep_r:
            return None
        total = sum(keep_f)
        keep_f = [f / total for f in keep_f]
        levels = [order.entry + rr * risk if long_side
                  else order.entry - rr * risk for rr in keep_r]
        fracs = keep_f

    return Order(
        pair=order.pair, direction=order.direction, entry=order.entry,
        stop=order.stop, targets=levels, fractions=fracs,
        created=order.created, expires=order.expires, key=order.key,
        meta=order.meta, be_trigger=order.be_trigger,
        entry_type=order.entry_type, trail_distance=trail or None)


def run(orders, exec_data, spec, portfolio):
    from smc_engine import compute_stats, run_portfolio

    shaped = [reshape(o, spec) for o in orders]
    shaped = [o for o in shaped if o is not None]
    if len(shaped) < 20:
        return None
    kwargs = dict(portfolio)
    max_hold = kwargs.pop('max_hold', None)
    if max_hold:
        kwargs['max_hold_hours'] = max_hold
    result = run_portfolio(shaped, exec_data,
                           breakeven_after_tp1=bool(spec.get('be')), **kwargs)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 20:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    return {'r': r, 'n': len(trades), 'mean': float(r.mean()),
            'total': float(r.sum()), 'wr': float((r > 0).mean() * 100),
            'dd': stats['max_dd_pct'], 'ret': stats['return_pct'],
            'by_key': {t['key']: t['pnl'] / t['risk'] for t in trades}}


def paired(base, other):
    """
    Разница по СОВПАДАЮЩИМ заявкам и её интервал.

    Заявка может налиться в одном варианте и не налиться в другом: занятость
    слотов зависит от того, когда закрылись предыдущие сделки, а это и меняет
    выход. Поэтому берётся пересечение ключей — сравнивать несравнимое
    означало бы приписать выходу разницу в наборе сделок.
    """
    keys = set(base['by_key']) & set(other['by_key'])
    if len(keys) < 20:
        return None
    diff = np.array([other['by_key'][k] - base['by_key'][k] for k in keys],
                    dtype=float)
    lo, hi = ci(diff)
    return {'n': len(keys), 'mean': float(diff.mean()), 'lo': lo, 'hi': hi,
            'share': len(keys) / max(base['n'], 1) * 100}


def books_for(label, data, smc_period):
    """Заявки и портфельные параметры трёх стратегий — каждой свои."""
    import config
    from levels import params as LP
    from smc import params as SP

    pairs = list(data)
    exec_1h = {p: data[p]['1h'] for p in pairs}
    exec_5m = {p: data[p]['5m'] for p in pairs}

    quiet = hush()
    try:
        print(f'[{label}] сбор заявок...', flush=True)
        return {
            'FIBO': (da.orders_fibo(data, pairs), exec_5m,
                     dict(risk_pct=config.RISK_PER_TRADE,
                          max_positions=getattr(config, 'MAX_OPEN_POSITIONS', 5),
                          cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
                          max_same_direction=getattr(config, 'MAX_SAME_DIRECTION', 0))),
            'LEVELS': (da.orders_levels(data, pairs), exec_1h,
                       dict(risk_pct=LP.RISK_PCT, max_positions=LP.MAX_POSITIONS,
                            cooldown_hours=LP.COOLDOWN_HOURS,
                            max_same_direction=LP.MAX_SAME_DIRECTION,
                            max_hold=LP.MAX_HOLD_HOURS)),
            'SMC': (da.orders_smc(smc_period), exec_5m,
                    dict(risk_pct=smc_period['bt'].RISK_PCT,
                         max_positions=smc_period['bt'].MAX_POSITIONS,
                         cooldown_hours=smc_period['bt'].COOLDOWN_HOURS,
                         max_same_direction=SP.MAX_SAME_DIRECTION)),
        }
    finally:
        unhush(quiet)


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        data = da.load(cache, pairs, label)
        smc = load_period(cache, list(data)[:da.PAIRS_LIMIT], label + ' · smc')
        periods[label] = (data, smc)

    results = {}
    for label, (data, smc) in periods.items():
        books = books_for(label, data, smc)
        print()
        print('=' * 122)
        print(f'{label}')
        print('=' * 122)
        results[label] = {}
        for name, (orders, exec_data, portfolio) in books.items():
            base = run(orders, exec_data, BASE, portfolio)
            if base is None:
                print(f'{name}: база не собралась')
                continue
            head = (f'{name:<12}{"вариант":<44}{"сделок":>8}{"винрейт":>9}'
                    f'{"R/сделку":>10}{"сумма":>9}{"DD%":>7}'
                    f'{"парная разница":>18}{"интервал разницы":>24}')
            print(head)
            print('-' * len(head))
            for vname, spec in VARIANTS:
                res = run(orders, exec_data, dict(BASE, **spec), portfolio)
                if res is None:
                    print(f'{"":<12}{vname:<44}{"— мало сделок":>16}')
                    continue
                pair_stat = None if spec is BASE else paired(base, res)
                cell = f'{pair_stat["mean"]:+.3f}' if pair_stat else '—'
                span = (f'[{pair_stat["lo"]:+.3f}; {pair_stat["hi"]:+.3f}]'
                        if pair_stat else '—')
                results[label][(name, vname)] = (res, pair_stat, base)
                print(f'{"":<12}{vname:<44}{res["n"]:>8}{res["wr"]:>8.1f}%'
                      f'{res["mean"]:>10.3f}{res["total"]:>9.1f}{res["dd"]:>7.1f}'
                      f'{cell:>18}{span:>24}')
            print()

    print('=' * 122)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: на ОБОИХ периодах парная разница')
    print('положительна, интервал разницы не накрывает ноль И просадка выросла')
    print('не более чем на 3 процентных пункта.')
    print('=' * 122)
    labels = list(results)
    for strategy in ('FIBO', 'LEVELS', 'SMC'):
        for vname, spec in VARIANTS:
            if spec is BASE:
                continue
            cells, ok = '', []
            for label in labels:
                found = results[label].get((strategy, vname))
                if not found or not found[1]:
                    cells += f'{"—":>38}'
                    ok.append(False)
                    continue
                res, pair_stat, base = found
                grew = res['dd'] - base['dd']
                ok.append(pair_stat['mean'] > 0 and pair_stat['lo'] > 0
                          and grew <= 3.0)
                cell = (f'{pair_stat["mean"]:+.3f} [{pair_stat["lo"]:+.3f}] '
                        f'DD {res["dd"]:.0f}% ({grew:+.0f})')
                cells += f'{cell:>38}'
            print(f'{strategy:<8}{vname[:38]:<40}{cells}'
                  f'{"  ПРИНЯТ" if ok and all(ok) else ""}')
        print('-' * 122)

    print()
    print('КАК ЧИТАТЬ. «Парная разница» — среднее изменение результата на ОДНОЙ')
    print('и той же заявке. Это единственная колонка, где виден вклад выхода')
    print('отдельно от разницы в наборе сделок. Столбец «сумма» полезен для')
    print('масштаба, но сам по себе ничего не доказывает: он меняется и просто')
    print('от того, сколько заявок налилось.')


if __name__ == '__main__':
    main()
