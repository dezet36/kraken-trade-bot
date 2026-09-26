"""
Боллинджер (RSIBB): замер 08.08.2026 против того, что торгует бот, — по шагам.

ЗАЧЕМ. Последний вердикт (rsibb_four.py, удалён 20.09 вместе с другими
скриптами замеров, есть в git до ea8995c): «не доказан, сидит на пороге» —
+0.087R на сделку на отложенных парах, нижняя граница интервала ровно на нуле.
Тот замер считал не совсем то, что торгует бот. Здесь — шаг за шагом от
замера к бою, на пяти периодах:

    1. замер 08.08       исполнение по часовым свечам, после сигнала пара
                         молчит 12 свечей, правил брокера и предела издержек нет;
    2. + 5-минутки       исполнение по 5-минутным свечам, как у брокера;
    3. + правила брокера сигнал на каждой закрытой свече, пару держит заявка,
                         пауза 2 ч с постановки, снятие у цели без входа,
                         смещение лимита 0.1%;
    4. = БОЙ             + предел издержек 8% риска (стоп не теснее 0.94%).

Решение — боевое ядро rsibb/core на ЗАКРЫТОЙ часовой свече (бот отбрасывает
идущую), параметры — rsibb/params как есть. Заявка рождается на закрытии
свечи сигнала.

Запуск: python research/rsibb_live_sim.py
"""

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, HERE)

from rsibb import core, params as RP  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402

COST_ROUND_TRIP = 0.00075      # мейкер 0.02% + тейкер 0.055%, как у брокера
COST_LIMIT_PCT = RP.MAX_ENTRY_COST_SHARE_PCT
LIMIT_OFFSET = 0.001
OLD_GAP = max(4, RP.MAX_HOLD_BARS // 4)


def periods():
    # Пулы — из common, как у rsibb_four 08.08: половины (настройка/проверка)
    # делятся через одну по этому порядку.
    import common as cm
    fresh = sorted({f.rsplit('_', 1)[0] for f in os.listdir(os.path.join(HERE, 'backtest_cache_fresh'))
                    if f.endswith('_1h.pkl')})
    return [('2022-23 падение', cm.BEAR_CACHE, cm.BEAR_PAIRS),
            ('2023-24 рост', cm.RISING_CACHES[0], cm.RISING_PAIRS),
            ('2024-25 рост', cm.RISING_CACHES[1], cm.RISING_PAIRS),
            ('2025-26 падение', cm.BULL_CACHE, cm.BULL_PAIRS),
            ('2025-11…2026-09', os.path.join(HERE, 'backtest_cache_fresh'), fresh)]


def load(cache, pair):
    import backtest_smc as bt
    bt.CACHE_DIR = cache
    return bt.load_cached(pair, '1h'), bt.load_cached(pair, '5m')


def setups(df):
    """(индекс, сетап, сделка) на каждой закрытой свече, где боевое ядро видит вход."""
    ind = core.indicators(*(df[c].to_numpy(float) for c in ('open', 'high', 'low', 'close')))
    out = []
    for i in range(RP.BB_PERIOD + 40, len(df) - 1):
        setup, _why = core.evaluate(ind, i)
        if setup is None:
            continue
        trade = core.build_trade(setup)
        if trade is not None:
            out.append((i, setup, trade))
    return out


def orders(pair, df, found, old_gap, offset, cost_limit, at_open=False):
    stamps = df['timestamp'].dt.tz_convert('UTC').dt.tz_localize(None).to_numpy(dtype='datetime64[ns]')
    # Движок начинает исполнение со свечи СТРОГО после created. Замер 08.08
    # ставил created на открытие свечи сигнала и исполнял по часовым — первой
    # шла следующая часовая, то есть сразу после закрытия. На 5-минутках
    # created — закрытие: первая 5-минутка после него, как у бота, который
    # ставит заявку в течение цикла после закрытия часа.
    hour = np.timedelta64(0 if at_open else 3600, 's')
    life = np.timedelta64(int(RP.EXPIRY_BARS * 3600), 's')
    out, last = [], -10 ** 9
    for i, setup, trade in found:
        if old_gap and i - last < OLD_GAP:
            continue
        long_side = setup['direction'] == 'LONG'
        entry = trade['entry'] * ((1 + offset) if long_side else (1 - offset))
        dist = abs(entry - trade['stop'])
        if cost_limit and entry / dist * COST_ROUND_TRIP * 100 > cost_limit:
            continue
        last = i
        created = stamps[i] + hour
        out.append(Order(pair=pair, direction=setup['direction'], entry=entry, stop=trade['stop'],
                         targets=[trade['target']], fractions=[1.0], created=created,
                         expires=created + life, key=(pair, i), entry_type='limit',
                         meta={'stop_pct': dist / entry * 100}))
    return out


def run(all_orders, exec_data, live_rules, slots=99):
    res = run_portfolio(all_orders, exec_data, risk_pct=RP.RISK_PCT, max_positions=slots,
                        cooldown_hours=RP.COOLDOWN_HOURS, breakeven_after_tp1=False,
                        max_hold_hours=RP.MAX_HOLD_BARS, max_same_direction=RP.MAX_SAME_DIRECTION,
                        cancel_at_target=live_rules, occupy_while_pending=live_rules,
                        cooldown_from_placement=live_rules)
    tr = [t for t in res['trades'] if t.get('risk')]
    r = np.array([t['pnl'] / t['risk'] for t in tr], dtype=float)
    return r, compute_stats(res) if tr else {}


def ci(r):
    if len(r) < 10:
        return float('nan'), float('nan')
    rng = np.random.default_rng(7)
    draws = rng.choice(r, size=(10_000, len(r)), replace=True).mean(axis=1)
    return tuple(np.percentile(draws, [2.5, 97.5]))


STEPS = (
    ('1. замер 08.08 как был', dict(exec_tf='1h', at_open=True, old_gap=True, offset=0.0, cost=0,
                                     rules=False, slots=6)),
    ('2. + 5-минутное исполнение', dict(exec_tf='5m', at_open=False, old_gap=True, offset=0.0, cost=0,
                                         rules=False, slots=6)),
    ('3. + правила брокера', dict(exec_tf='5m', at_open=False, old_gap=False, offset=LIMIT_OFFSET, cost=0,
                                   rules=True, slots=99)),
    ('4. = БОЙ (+ предел издержек)', dict(exec_tf='5m', at_open=False, old_gap=False, offset=LIMIT_OFFSET,
                                           cost=COST_LIMIT_PCT, rules=True, slots=99)),
)
HALVES = (('все пары', slice(None)), ('настройка', slice(0, None, 2)), ('проверка', slice(1, None, 2)))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    print(f'боевые параметры: полосы {RP.BB_PERIOD}×{RP.BB_MULT}σ, RSI {RP.RSI_MODE} {RP.RSI_LOW}/{RP.RSI_HIGH}, '
          f'вход {RP.ENTRY_MODE}, стоп {RP.STOP_FRAC}·полуширины, цель {RP.TARGET_FRAC}, '
          f'мин. стоп {RP.MIN_STOP_PCT}% ({RP.THIN_STOP}), MIN_RR {RP.MIN_RR}, заявка {RP.EXPIRY_BARS} ч, '
          f'держать {RP.MAX_HOLD_BARS} ч, пауза {RP.COOLDOWN_HOURS} ч, предел издержек {COST_LIMIT_PCT}%')
    pooled = {(label, half): [] for label, _cfg in STEPS for half, _s in HALVES}
    for plabel, cache, pairs in periods():
        data = {}
        for pair in pairs:
            h1, m5 = load(cache, pair)
            if h1 is not None and m5 is not None and len(h1) > 200:
                data[pair] = (h1, m5)
        found = {pair: setups(h1) for pair, (h1, _m5) in data.items()}
        print(f'\n== {plabel}: пар {len(data)}, сигналов ядра {sum(len(v) for v in found.values())}')
        for label, cfg in STEPS:
            for half, cut in HALVES:
                chosen = [p for p in pairs if p in data][cut]
                all_orders = []
                for pair in chosen:
                    all_orders += orders(pair, data[pair][0], found[pair], cfg['old_gap'], cfg['offset'],
                                         cfg['cost'], at_open=cfg['at_open'])
                exec_data = {pair: data[pair][0 if cfg['exec_tf'] == '1h' else 1] for pair in chosen}
                r, st = run(all_orders, exec_data, cfg['rules'], cfg['slots'])
                name = f'{label} · {half}'
                if not len(r):
                    print(f'   {name:46s} сделок нет')
                    continue
                lo, hi = ci(r)
                pooled[(label, half)].append(r)
                print(f'   {name:46s} заявок {len(all_orders):5d} · сделок {len(r):4d} · '
                      f'WR {(r > 0).mean() * 100:4.1f}% · {r.mean():+.3f}R/сд [{lo:+.3f}; {hi:+.3f}] · '
                      f'сумма {r.sum():+6.1f}R · просадка {st.get("max_dd_pct", 0):4.1f}%', flush=True)
    print('\n== все пять периодов вместе')
    for label, _cfg in STEPS:
        for half, _cut in HALVES:
            r = np.concatenate(pooled[(label, half)]) if pooled[(label, half)] else np.array([])
            if len(r):
                lo, hi = ci(r)
                print(f'   {label + " · " + half:46s} сделок {len(r):5d} · {r.mean():+.3f}R/сд '
                      f'[{lo:+.3f}; {hi:+.3f}] · сумма {r.sum():+7.1f}R')


if __name__ == '__main__':
    main()
