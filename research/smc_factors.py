"""
Разбор факторов confluence — заново, на исправленном ядре и строго.

Прежний разбор (research/smc_attribution.py) дал веса, которые оказались
подгонкой. У него две методические дыры, и обе исправлены здесь.

ПЕРВАЯ: он сравнивал средние без доверительных интервалов. Разница «+0.42R
против −0.24R» на полусотне сделок выглядит убедительно и почти всегда
оказывается шумом — что и подтвердили круги 5-8.

ВТОРАЯ, более коварная: он мерил факторы на сделках, ОТОБРАННЫХ по сумме этих
же факторов. Сетап без ote_zone проходил порог только если добирал баллы
другими факторами, поэтому в выборке «без OTE» систематически оказывались
сетапы с сильным всем остальным. Сравнение таких групп ничего не говорит о
самом OTE. Здесь порог confluence снят полностью: измеряется вся популяция
сетапов, а не её отфильтрованный хвост.

Жёсткие гейты (bias, premium/discount, свежесть зоны) в выборке всегда
истинны и потому неизмеримы — их вес двигает только общий уровень баллов, а
не отбор. Измеряются шесть факторов, которые реально варьируются.

Признак принимается предиктором, только если интервал разности не пересекает
ноль на ОБОИХ периодах и знак совпадает.

Запуск:
    python research/smc_factors.py
"""

import os
import sys
from copy import deepcopy

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, load_period)

BOOTSTRAP = 10_000
RNG = np.random.default_rng(20260804)

MEASURABLE = ('liquidity_swept', 'fvg_present', 'structure_break',
              'ote_zone', 'killzone', 'law_of_effort')

# Порог снимается: иначе факторы измеряются на выборке, которую сами же
# и отобрали. Прочие мягкие гейты тоже отключаются.
OPEN_GATES = {
    'MIN_CONFLUENCE_SCORE': 0.0,
    'REQUIRE_OTE': False,
    'MAX_RR': 0.0,
    'LEG_BARS_MIN': 0,
    'LEG_BARS_MAX': 0,
    'KILLZONE_AS_GATE': False,
}
TRACKED = list(OPEN_GATES) + ['MAX_SAME_DIRECTION', 'TP_MODE']


def collect(period):
    """Все сделки периода без отбора по confluence."""
    from smc import params as P
    from smc_sweep import build_orders
    from smc_engine import run_portfolio

    bt = period['bt']
    pairs = list(period['data'])
    orders = []
    for pair in pairs:
        orders += build_orders(period['contexts'][pair], pair,
                               period['data'][pair]['1h'])
    if not orders:
        return []

    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in pairs},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS,
        breakeven_after_tp1=P.BREAKEVEN_AFTER_TP1,
        max_hold_hours=P.MAX_POSITION_HOLD_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION)
    return [t for t in result['trades'] if t.get('risk')]


def diff_ci(a, b):
    """Интервал разности средних. Пересекает ноль — разница недоказуема."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 5 or len(b) < 5:
        return None, None, None
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    lo, hi = np.percentile(d, [2.5, 97.5])
    return float(np.mean(d)), float(lo), float(hi)


def main():
    from smc import params as P

    defaults = {key: deepcopy(getattr(P, key)) for key in TRACKED}
    for key, value in OPEN_GATES.items():
        setattr(P, key, value)

    periods = {}
    for cache, pairs, label in ((BULL_CACHE, BULL_PAIRS, 'бычий'),
                                (BEAR_CACHE, BEAR_PAIRS, 'медвежий')):
        # Контексты строятся ПОСЛЕ снятия порога: он влияет на evaluate
        period = load_period(cache, pairs, label)
        trades = collect(period)
        periods[label] = trades
        print(f'   {label}: сделок без отбора по confluence — {len(trades)}',
              flush=True)

    print()
    print('=' * 104)
    print('ВЛИЯНИЕ ФАКТОРА НА СРЕДНИЙ R (порог confluence снят)')
    print('=' * 104)
    head = (f'{"фактор":<20}' +
            ''.join(f'{p + ": с / без  разница [интервал]":>42}' for p in periods))
    print(head)
    print('-' * len(head))

    verdicts = {}
    for name in MEASURABLE:
        line = f'{name:<20}'
        signs = []
        for label, trades in periods.items():
            with_f = [t['pnl'] / t['risk'] for t in trades
                      if (t['meta'].get('factors') or {}).get(name)]
            without = [t['pnl'] / t['risk'] for t in trades
                       if not (t['meta'].get('factors') or {}).get(name)]
            if len(with_f) < 5 or len(without) < 5:
                line += f'{"мало наблюдений":>42}'
                signs.append(None)
                continue
            delta, lo, hi = diff_ci(with_f, without)
            proven = lo is not None and (lo > 0 or hi < 0)
            signs.append(np.sign(delta) if proven else 0)
            line += (f'{np.mean(with_f):>+7.3f} /{np.mean(without):>+7.3f}'
                     f'{delta:>+8.3f} [{lo:+.2f};{hi:+.2f}]'
                     f'{"  ЕСТЬ" if proven else "  шум":>8}')
        print(line)
        verdicts[name] = signs

    print()
    print('ВЕРДИКТ (предиктор — только доказуемый на ОБОИХ периодах с одним знаком):')
    proven_any = False
    for name, signs in verdicts.items():
        if all(s is not None and s != 0 for s in signs) and len(set(signs)) == 1:
            print(f'   {name}: ПРЕДИКТОР, знак {"+" if signs[0] > 0 else "−"}')
            proven_any = True
    if not proven_any:
        print('   ни один фактор не показал доказуемого влияния на обоих периодах.')
        print()
        print('   Это значит, что подобранные веса не имеют опоры в данных, и')
        print('   честный выбор — равные веса либо значения из методички,')
        print('   а не числа, подогнанные под шум конкретной выборки.')

    for key, value in defaults.items():
        setattr(P, key, value)


if __name__ == '__main__':
    main()
