"""
ФИБО: тренд 4ч у шорта и безубыток на уровне B — проверка H1, H2 аудита
сделок 07.10.2026 (docs/Аудит_сделок_2026-10-07.md, раздел 8; протокол
закоммичен до прогона, ebd7187).

Вживую все 41 шорт ФИБО открыты при тренде 4ч NEUTRAL: фильтр сканера
запрещает шорт только при BULLISH, а config.HTF_ALLOW_NEUTRAL пропускает
NEUTRAL. Здесь на истории «как в боте»:

    база  F3 — шорты против толпы (как торгует бот);
    H1    F3 и тренд 4ч BEARISH на момент заявки;
    H2    F3 без безубытка на уровне B;
    для понимания (решений нет): F3 и NEUTRAL, лонги по трендам.

Тренд — боевой strategy.get_htf_trend по 4ч: закрытые свечи до начала
текущей четырёхчасовки и формирующаяся (её close — последняя закрытая
5-минутка до заявки), как в fibo_live_sim.orders_for_pair.

Приёмка: R на сделку лучше базы в каждом из четырёх периодов на 10 парах
замера И на 10 других парах; итог R ≥ 0 на всех 20 парах вместе при наливе
«насквозь».

    python research/fibo_htf_split.py   → results/fibo_htf_split.txt
"""
import copy
import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fibo_live_sim as FS                            # noqa: E402  (папка данных sim-data, тихий логгер)
import ai_setups as S                                 # noqa: E402
import backtest_smc as bt                             # noqa: E402
import config                                         # noqa: E402
import smc_engine                                     # noqa: E402
from strategies.fibo import strategy  # noqa: E402
from strategies import strategy_profile  # noqa: E402
from common import ci                                 # noqa: E402
from fibo_crowd import funding_at                     # noqa: E402
from fibo_other_pairs import OTHER, _path            # noqa: E402

CACHES = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
          '12m': 'backtest_cache_12m'}
BASE = dict(risk_pct=bt.RISK_PCT, max_positions=99, cooldown_hours=strategy_profile.cooldown_hours('FIBO'),
            breakeven_after_tp1=False, max_hold_hours=strategy_profile.max_hold_hours('FIBO') or 336.0,
            max_same_direction=0, occupy_while_pending=True, cooldown_from_placement=True,
            cancel_at_target=True)
N4 = config.HTF_EMA_SLOW + 20


def htf_for(orders, data):
    """Тренд 4ч на момент каждой заявки пары."""
    h4, m5 = data['4h'], data['5m']
    t4 = FS.naive(h4['timestamp'])
    t5 = FS.naive(m5['timestamp'])
    c4 = h4['close'].to_numpy(dtype=float)
    c5 = m5['close'].to_numpy(dtype=float)
    out = {}
    for o in orders:
        now = np.datetime64(pd.Timestamp(o.created).tz_localize(None) if pd.Timestamp(o.created).tzinfo
                            else pd.Timestamp(o.created))
        hour = np.datetime64(pd.Timestamp(now - np.timedelta64(1, 'm')).floor('h'))
        k4 = int(np.searchsorted(t4, hour, side='right')) - 1
        j = int(np.searchsorted(t5, now, side='left'))
        if k4 < 0 or j < 1:
            out[id(o)] = 'NEUTRAL'
            continue
        closes = np.append(c4[max(0, k4 - N4 + 1):k4], c5[j - 1])
        out[id(o)] = strategy.get_htf_trend(pd.DataFrame({'close': closes}))
    return out


def no_be(orders):
    res = []
    for o in orders:
        c = copy.copy(o)
        c.be_trigger = None
        res.append(c)
    return res


RULES = [
    ('база F3 — шорты против толпы (как в боте)', lambda o, f, t: o.direction == 'SHORT' and f <= 0, False),
    ('H1 F3 и тренд 4ч BEARISH', lambda o, f, t: o.direction == 'SHORT' and f <= 0 and t == 'BEARISH', False),
    ('H2 F3 без безубытка на уровне B', lambda o, f, t: o.direction == 'SHORT' and f <= 0, True),
    ('F3 и тренд 4ч NEUTRAL (для понимания)', lambda o, f, t: o.direction == 'SHORT' and f <= 0 and t == 'NEUTRAL', False),
    ('шорты BEARISH, без толпы (для понимания)', lambda o, f, t: o.direction == 'SHORT' and t == 'BEARISH', False),
    ('лонги BULLISH (для понимания)', lambda o, f, t: o.direction == 'LONG' and t == 'BULLISH', False),
    ('лонги NEUTRAL (для понимания)', lambda o, f, t: o.direction == 'LONG' and t == 'NEUTRAL', False),
]


def load(group, cache):
    if group == 'замер':
        with open(os.path.join(HERE, 'results', f'fibo_live_orders_{cache}.pkl'), 'rb') as fh:
            orders = pickle.load(fh)
        pairs = list(bt.DEFAULT_PAIRS)
    else:
        pairs = [p for p in OTHER if os.path.exists(_path(cache, p))]
        orders = []
        for p in pairs:
            with open(_path(cache, p), 'rb') as fh:
                orders += pickle.load(fh)
    return [o for o in orders if o.meta['cost_share'] <= FS.COST_LIMIT], pairs


def main():
    totals = {}
    for group in ('замер', 'другие'):
        for period, cache in CACHES.items():
            orders, pairs = load(group, cache)
            bt.CACHE_DIR = os.path.join(HERE, cache)
            data = {p: d for p in pairs if (d := bt.load_pair(p)) is not None}
            exec_data = {p: d['5m'] for p, d in data.items()}
            fund = {p: S.load_series(cache, 'funding', p, 'funding_rate') for p in pairs}
            trend, signed = {}, {}
            for p in pairs:
                own = [o for o in orders if o.pair == p]
                if p in data:
                    trend.update(htf_for(own, data[p]))
            for o in orders:
                t = int(pd.Timestamp(o.created).value // 10 ** 6)
                signed[id(o)] = funding_at(fund.get(o.pair), t) * (1.0 if o.direction == 'LONG' else -1.0)
            mix = pd.Series([trend.get(id(o), '?') + '/' + o.direction for o in orders]).value_counts().to_dict()
            cells = []
            for name, rule, drop_be in RULES:
                pool = [o for o in orders if rule(o, signed[id(o)], trend.get(id(o)))]
                if drop_be:
                    pool = no_be(pool)
                for through in (0.0, 0.0005):
                    smc_engine.FILL_THROUGH_PCT = through
                    try:
                        out = smc_engine.run_portfolio(pool, exec_data, **BASE)
                    finally:
                        smc_engine.FILL_THROUGH_PCT = 0.0
                    rs = [t['pnl'] / t['risk'] for t in out['trades']]
                    key = (name, through)
                    totals.setdefault(key, {})[(group, period)] = rs
                r0 = totals[(name, 0.0)][(group, period)]
                cells.append(f'{name[:24]}: {len(r0)} сд {np.mean(r0) if r0 else 0:+.3f}')
            print(f'{group:6s} {period:4s} заявок {len(orders)} {mix}\n   ' + '\n   '.join(cells), flush=True)

    print('\nИТОГ: R на сделку по периодам (касание), затем всё вместе [95%] касание / насквозь')
    for name, _, _ in RULES:
        for group in ('замер', 'другие'):
            per = totals[(name, 0.0)]
            cells = ' '.join(f'{p} {np.mean(per[(group, p)]) if per[(group, p)] else float("nan"):+.3f}({len(per[(group, p)])})'
                             for p in CACHES)
            print(f'  {name:46s} {group:6s} {cells}')
        for through in (0.0, 0.0005):
            allr = np.concatenate([np.array(v) for v in totals[(name, through)].values()])
            lo, hi = ci(allr) if len(allr) > 2 else (np.nan, np.nan)
            print(f'      {"касание" if not through else "насквозь"}: {len(allr)} сд итог {allr.sum():+.1f}R '
                  f'{allr.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]')

    print('\nПРИЁМКА (база — F3)')
    for name in ('H1 F3 и тренд 4ч BEARISH', 'H2 F3 без безубытка на уровне B'):
        base, test = totals[(RULES[0][0], 0.0)], totals[(name, 0.0)]
        better = all((np.mean(test[k]) if test[k] else -9) > (np.mean(base[k]) if base[k] else -9) for k in base)
        through = np.concatenate([np.array(v) for v in totals[(name, 0.0005)].values()]).sum()
        print(f'  {name}: лучше базы во всех 8 ячейках — {"да" if better else "нет"}; '
              f'итог насквозь {through:+.1f}R — {"≥ 0" if through >= 0 else "< 0"} → '
              f'{"ПРИНЯТА" if better and through >= 0 else "не принята"}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
