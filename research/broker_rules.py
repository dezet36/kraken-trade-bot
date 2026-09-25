"""
Цена правил живого брокера для стратегии: заявки строятся ОДИН раз на период,
потом портфель считается с разными наборами правил (smc_engine.run_portfolio).

ЗАЧЕМ. 25.09.2026 оказалось, что брокер (paper_broker) живёт по правилам,
которых не было в движке, по которому стратегии настраивались: снимает заявку
у цели без входа, держит пару занятой неналитой заявкой и считает её в
направленном кэпе, ставит паузу с постановки. Мерить их надо ВСЕ разом: одно
правило без остальных дало SMC «~50R за период», а с живыми — +3.6R на росте
и +65.6R на падении.

Запуск (кэш периода — как у backtest_smc.py):
    SMC_CACHE_DIR=research/backtest_cache_12m python research/broker_rules.py smc
    SMC_CACHE_DIR=research/backtest_cache_bear python research/broker_rules.py fibo

Фибоначчи строит заявки по каждой свече — часы на период; лог стратегии
заглушён, иначе строка на свечу тормозит прогон в разы.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

import logger  # noqa: E402
logger.log = lambda *a, **k: None

import numpy as np  # noqa: E402

import backtest_smc as bt  # noqa: E402
from smc import params as smc_params  # noqa: E402
from smc_engine import compute_stats, run_portfolio  # noqa: E402

LIVE = dict(occupy_while_pending=True, cooldown_from_placement=True)


def variants(strategy):
    if strategy == 'smc':
        cap = smc_params.MAX_SAME_DIRECTION
        base = dict(max_positions=bt.MAX_POSITIONS, cooldown_hours=smc_params.COOLDOWN_HOURS,
                    breakeven_after_tp1=smc_params.BREAKEVEN_AFTER_TP1,
                    max_hold_hours=smc_params.MAX_POSITION_HOLD_HOURS, max_same_direction=cap)
        return base, [
            ('движок как был (без кэпа)', dict(max_same_direction=0)),
            (f'движок + кэп {cap}', dict()),
            ('+ снятие у цели', dict(cancel_at_target=True)),
            ('+ заявка занимает пару', dict(occupy_while_pending=True)),
            ('+ пауза с постановки', dict(cooldown_from_placement=True)),
            ('живые правила, заявка ждёт у цели', dict(LIVE)),
            ('живые правила, снятие у цели', dict(LIVE, cancel_at_target=True)),
            ('живые, кэп только по позициям', dict(LIVE, pending_in_cap=False)),
        ]
    import config
    base = dict(max_positions=bt.MAX_POSITIONS, cooldown_hours=bt.COOLDOWN_HOURS,
                breakeven_after_tp1=False, max_hold_hours=config.MAX_POSITION_HOLD_HOURS or 336.0,
                max_same_direction=getattr(config, 'MAX_SAME_DIRECTION', 0))
    return base, [
        ('движок как был', dict()),
        ('+ снятие у цели', dict(cancel_at_target=True)),
        ('+ заявка занимает пару', dict(occupy_while_pending=True)),
        ('+ пауза с постановки', dict(cooldown_from_placement=True)),
        ('живые правила, снятие у цели', dict(LIVE, cancel_at_target=True)),
        ('живые правила, заявка ждёт у цели', dict(LIVE)),
    ]


def main(strategy):
    data = {p: d for p in bt.DEFAULT_PAIRS if (d := bt.load_pair(p)) is not None}
    build = bt.smc_orders if strategy == 'smc' else bt.fibo_orders
    orders = []
    for pair, frames in data.items():
        found = build(pair, frames)
        orders += found
        print(f'   {pair}: {len(found)} заявок', flush=True)
    span = next(iter(data.values()))['1h']
    print(f'{strategy}: {span.timestamp.iloc[0].date()}..{span.timestamp.iloc[-1].date()}, '
          f'заявок {len(orders)}')
    exec_data = {p: f['5m'] for p, f in data.items()}
    by_key = {o.key: o for o in orders}
    base, rows = variants(strategy)
    print(f"{'вариант':36s} {'сделок':>6s} {'сумма R':>8s} {'R/сд':>6s} {'PF':>6s} "
          f"{'просадка':>8s} {'налив в часе сигнала':>21s}")
    for name, extra in rows:
        out = run_portfolio(orders, exec_data, risk_pct=bt.RISK_PCT, **{**base, **extra})
        trades = out['trades']
        total = sum(t['pnl'] / t['risk'] for t in trades)
        # Налив раньше закрытия свечи сигнала — заглядывание вперёд
        # (created — время ОТКРЫТИЯ часовой свечи, а сетап известен на закрытии).
        early = [t for t in trades
                 if t['entry_time'] < by_key[t['key']].created + np.timedelta64(1, 'h')]
        s = compute_stats(out)
        print(f"{name:36s} {len(trades):6d} {total:8.1f} {total / max(1, len(trades)):6.3f} "
              f"{s.get('profit_factor', 0):6.3f} {s.get('max_dd_pct', 0):7.1f}% "
              f"{len(early):6d} ({sum(t['pnl'] / t['risk'] for t in early):+.1f}R)", flush=True)


if __name__ == '__main__':
    main((sys.argv[1] if len(sys.argv) > 1 else 'smc').lower())
