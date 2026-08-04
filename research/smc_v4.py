"""
Круг 4: поиск середины между доходностью и просадкой.

Круг 3 дал две крайности. Текущая конфигурация зарабатывает больше всех на
бычьем периоде (+866.8%), но платит просадкой 37.6% на медвежьем. Жёсткая
связка «кэп 2 + conf 5.0 + без DOT/UNI» роняет просадку до 18.5%, но теряет
две трети бычьего дохода.

Разбор круга 3 показал, ГДЕ именно кроется разница. На медвежьем периоде
суммарный R почти не зависит от конфигурации (52-78 R), а просадка меняется
вдвое. То есть направленный кэп не добавляет прибыльности — он разносит
убытки во времени: без него несколько коррелированных лонгов гибнут в одной
коррекции, и просадка складывается. Значит середину надо искать по кэпу и
мягкому порогу confluence, а не по отсеву пар (он режет edge на медвежьем:
sumR 76 -> 52).

Порог confluence между 4.5 и 5.0 в круге 3 не проверялся вообще, а шаг между
ними огромен: bull sumR 247 -> 109. Середина почти наверняка там.

Запуск:
    python research/smc_v4.py
"""

import os
import sys
from copy import deepcopy

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BULL_CACHE = os.path.join(ROOT, 'research', 'backtest_cache_12m')
BEAR_CACHE = os.path.join(ROOT, 'research', 'backtest_cache_bear')

BULL_PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'HYPEUSDT',
    'SUIUSDT', '1000PEPEUSDT', 'ADAUSDT', 'ZECUSDT', 'LINKUSDT', 'WIFUSDT',
    'BNBUSDT', 'AVAXUSDT', 'LTCUSDT', 'TAOUSDT', 'DOTUSDT', 'ARBUSDT',
    'BCHUSDT', 'UNIUSDT',
]
BEAR_PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT',
    'LINKUSDT', 'BNBUSDT', 'AVAXUSDT', 'LTCUSDT', 'DOTUSDT', 'BCHUSDT',
    'UNIUSDT', 'XLMUSDT',
]

# Пары, убыточные на ОБОИХ периодах по разбору вкладов
BAD_PAIRS = {'DOTUSDT', 'UNIUSDT'}

# Исходные веса — для замера эффекта от пересчёта
OLD_WEIGHTS = {
    'htf_bias_aligned': 1.0, 'liquidity_swept': 1.0, 'premium_discount': 1.0,
    'poi_fresh': 0.8, 'fvg_present': 0.6, 'structure_break': 0.8,
    'ote_zone': 0.5, 'killzone': 0.5, 'law_of_effort': 0.4,
}

CONFIGS = [
    # Опорные точки круга 3 — чтобы новые цифры читались в том же масштабе
    ('кэп 2 (опора)',          {'MAX_SAME_DIRECTION': 2}, False),
    ('кэп 3 (опора)',          {'MAX_SAME_DIRECTION': 3}, False),
    # Кэп мягче: сколько доходности возвращается за рост просадки
    ('кэп 4',                  {'MAX_SAME_DIRECTION': 4}, False),
    # Не проверенный промежуток порога confluence
    ('кэп 2 + conf 4.7',       {'MAX_SAME_DIRECTION': 2, 'MIN_CONFLUENCE_SCORE': 4.7}, False),
    ('кэп 2 + conf 4.8',       {'MAX_SAME_DIRECTION': 2, 'MIN_CONFLUENCE_SCORE': 4.8}, False),
    ('кэп 3 + conf 4.7',       {'MAX_SAME_DIRECTION': 3, 'MIN_CONFLUENCE_SCORE': 4.7}, False),
    ('кэп 3 + conf 4.8',       {'MAX_SAME_DIRECTION': 3, 'MIN_CONFLUENCE_SCORE': 4.8}, False),
    ('кэп 4 + conf 4.8',       {'MAX_SAME_DIRECTION': 4, 'MIN_CONFLUENCE_SCORE': 4.8}, False),
    ('кэп 2 + без DOT/UNI',    {'MAX_SAME_DIRECTION': 2}, True),
]

TRACKED = ['CONFLUENCE_WEIGHTS', 'REQUIRE_OTE', 'MAX_RR', 'LEG_BARS_MIN',
           'LEG_BARS_MAX', 'MIN_CONFLUENCE_SCORE', 'MAX_SAME_DIRECTION']


def load_period(cache_dir, pairs, label):
    """Загружает период и строит контексты (один раз — они не зависят от конфигов)."""
    os.environ['SMC_CACHE_DIR'] = cache_dir
    for module in ('backtest_smc', 'smc_sweep'):
        sys.modules.pop(module, None)
    import backtest_smc as bt
    from smc import signal as smc_signal

    print(f'[{label}] загрузка...', flush=True)
    data, contexts = {}, {}
    for pair in pairs:
        loaded = bt.load_pair(pair)
        if loaded is None:
            continue
        data[pair] = loaded
        contexts[pair] = smc_signal.build_context({
            'bias': loaded['1d'], 'htf': loaded['4h'], 'poi': loaded['1h'],
        }, pair=pair)
    print(f'   пар: {len(data)}', flush=True)
    return {'data': data, 'contexts': contexts, 'bt': bt}


def evaluate(period, drop_bad):
    """Прогоняет портфель при ТЕКУЩИХ параметрах."""
    from smc import params as P
    from smc_sweep import build_orders
    from smc_engine import compute_stats, run_portfolio

    bt = period['bt']
    pairs = [p for p in period['data']
             if not (drop_bad and p in BAD_PAIRS)]

    orders = []
    for pair in pairs:
        orders += build_orders(period['contexts'][pair], pair,
                               period['data'][pair]['1h'])
    if not orders:
        return None

    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in pairs},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION)
    if not result['trades']:
        return None
    stats = compute_stats(result, label='')
    stats['orders'] = len(orders)
    return stats


def main():
    from smc import params as P

    bull = load_period(BULL_CACHE, BULL_PAIRS, 'бычий 25-26')
    bear = load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 22-23')

    defaults = {key: deepcopy(getattr(P, key)) for key in TRACKED}
    rows = []

    for name, overrides, drop_bad in CONFIGS:
        for key, value in defaults.items():
            setattr(P, key, value)
        for key, value in overrides.items():
            setattr(P, key, value)

        stats_bull = evaluate(bull, drop_bad)
        stats_bear = evaluate(bear, drop_bad)
        if not stats_bull or not stats_bear:
            print(f'   {name}: сделок нет', flush=True)
            continue

        rows.append({'name': name, 'bull': stats_bull, 'bear': stats_bear})
        print(f'   {name:<28} бычий {stats_bull["return_pct"]:+8.1f}% '
              f'(PF {stats_bull["profit_factor"]:.3f})  |  медвежий '
              f'{stats_bear["return_pct"]:+8.1f}% (PF {stats_bear["profit_factor"]:.3f})',
              flush=True)

    print()
    print('=' * 118)
    print('КРУГ 4: СЕРЕДИНА МЕЖДУ ДОХОДНОСТЬЮ И ПРОСАДКОЙ')
    print('=' * 118)
    header = (f'{"конфигурация":<30}'
              f'{"БЫЧИЙ: сделок":>14}{"доход%":>10}{"DD%":>7}{"PF":>7}{"R/сд":>7}'
              f'{"МЕДВЕЖИЙ: сделок":>18}{"доход%":>10}{"DD%":>7}{"PF":>7}{"R/сд":>7}')
    print(header)
    print('-' * len(header))
    for row in rows:
        b, r = row['bull'], row['bear']
        print(f'{row["name"]:<30}'
              f'{b["trades"]:>14}{b["return_pct"]:>+10.1f}{b["max_dd_pct"]:>7.1f}'
              f'{b["profit_factor"]:>7.3f}{b["expectancy_r"]:>7.3f}'
              f'{r["trades"]:>18}{r["return_pct"]:>+10.1f}{r["max_dd_pct"]:>7.1f}'
              f'{r["profit_factor"]:>7.3f}{r["expectancy_r"]:>7.3f}')

    print('\nОриентир — фибо-стратегия:')
    print('   бычий (24 пары): +1276.1%, DD 17.3%, PF 1.410, R/сд 0.153')
    print('   медвежий (14 пар): +703.9%, DD 27.8%, PF 1.171, R/сд 0.105')
    print('\nПринимается только конфигурация, улучшающая ОБА периода.')


if __name__ == '__main__':
    main()
