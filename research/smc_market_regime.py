"""
Где именно стратегия теряет: разбор сделок по режиму рынка.

Прежние проверки делили историю по КАЛЕНДАРНЫМ отрезкам («бычий период»,
«медвежий период»). Это грубо: внутри медвежьего периода были и обвал, и
полугодовой боковик, и они смешивались в одно среднее. Здесь режим
определяется объективно и по каждой сделке отдельно.

Мера режима — коэффициент эффективности Кауфмана по BTC на дневках:

    ER = |P(t) - P(t-n)| / Σ|P(i) - P(i-1)|,   n = 30 дней

Он отвечает ровно на нужный вопрос: какая доля пройденного пути превратилась
в направленное движение. ER около 1 — чистый тренд, около 0 — пила, где цена
исходила ту же дистанцию и вернулась. Порог и знак дают три режима:

    рост    — ER >= порога, цена выше, чем 30 дней назад
    падение — ER >= порога, цена ниже
    боковик — ER < порога, независимо от знака

BTC взят как прокси рынка: альткоины ходят за ним, и режим у них общий.
Классификация считается ТОЛЬКО по закрытым дневным свечам до момента входа —
иначе разбор подглядывал бы в будущее и все выводы были бы недействительны.

Запуск:
    python research/smc_market_regime.py
"""

import os
import sys
from copy import deepcopy

import numpy as np
import pandas as pd

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

ER_WINDOW = 30          # дней
BOOTSTRAP = 10_000
RNG = np.random.default_rng(20260804)

# Порог направленности берётся не «на глаз», а как ВЕРХНЯЯ ТРЕТЬ фактических
# значений ER за 2.5 года. Фиксированное значение 0.35 загнало 83% сделок в
# одну корзину: сравнивать при таком перекосе нечего — у двух корзин просто не
# хватает наблюдений, чтобы отличаться от нуля.
ER_TREND_QUANTILE = 0.667

REGIMES = ('рост', 'падение', 'боковик')


# ── Режим рынка ──────────────────────────────────────────────────────────────

def regime_series(daily):
    """
    Режим на каждый день по закрытым дневным свечам BTC.

    Значение на дне i рассчитано по свечам ДО i включительно и применяется к
    сделкам, открытым ПОСЛЕ закрытия этой свечи, — в разборе не должно быть
    ни одного бита информации из будущего.
    """
    close = daily['close'].to_numpy(dtype=float)
    times = pd.to_datetime(daily['timestamp'])

    direction = np.full(len(close), np.nan)
    efficiency = np.full(len(close), np.nan)
    steps = np.abs(np.diff(close, prepend=close[0]))

    for i in range(ER_WINDOW, len(close)):
        moved = close[i] - close[i - ER_WINDOW]
        path = steps[i - ER_WINDOW + 1:i + 1].sum()
        direction[i] = moved
        efficiency[i] = abs(moved) / path if path > 0 else 0.0

    known = efficiency[~np.isnan(efficiency)]
    threshold = float(np.quantile(known, ER_TREND_QUANTILE)) if len(known) else 0.35

    labels = []
    for d, e in zip(direction, efficiency):
        if np.isnan(e):
            labels.append(None)
        elif e < threshold:
            labels.append('боковик')
        else:
            labels.append('рост' if d > 0 else 'падение')
    return pd.DataFrame({'time': times, 'regime': labels, 'er': efficiency}), threshold


def _naive(value):
    """
    Приводит момент времени к наивному UTC.

    В проекте часть источников отдаёт время с зоной, часть без: сравнение
    таких меток падает с TypeError, а тихое приведение одной из них дало бы
    сдвиг на часы и неверную разметку режима.
    """
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert('UTC').tz_localize(None)
    return stamp


def build_lookup(regimes):
    """Функция «время -> режим», берущая ПОСЛЕДНИЙ закрытый день."""
    times = np.array([_naive(t).to_datetime64() for t in regimes['time']])
    labels = regimes['regime'].tolist()

    def lookup(when):
        idx = int(np.searchsorted(times, _naive(when).to_datetime64(), 'right')) - 1
        if idx < 0 or idx >= len(labels):
            return None
        return labels[idx]

    return lookup


# ── Загрузка и прогон ────────────────────────────────────────────────────────

def load_period(cache_dir, pairs, label):
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
    regimes, threshold = regime_series(data['BTCUSDT']['1d'])
    lookup = build_lookup(regimes)
    print(f'   пар: {len(data)} | порог направленности ER = {threshold:.3f}', flush=True)
    return {'data': data, 'contexts': contexts, 'bt': bt, 'regime': lookup,
            'label': label}


def run(period):
    """Прогон портфеля при текущих параметрах. Отдаёт сделки с режимом входа."""
    from smc import params as P
    from smc_sweep import build_orders
    from smc_engine import compute_stats, run_portfolio

    bt = period['bt']
    pairs = list(period['data'])
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
    rows = []
    for t in result['trades']:
        if not t.get('risk'):
            continue
        reason = str(t.get('exit_reason', ''))
        rows.append({
            'r': t['pnl'] / t['risk'],
            'regime': period['regime'](t['entry_time']),
            'direction': 'LONG' if t.get('direction') in ('BULLISH', 'LONG') else 'SHORT',
            'reason': reason,
            # Сколько целей успели сработать до выхода — «SL_after_TP2»
            # означает, что три четверти позиции уже зафиксированы в плюс.
            'tps': int(reason[-1]) if reason[-1].isdigit() else 0,
            'mfe_r': float(t.get('mfe_r', 0) or 0),
            'entry_time': _naive(t['entry_time']),
            'days': (_naive(t['exit_time']) - _naive(t['entry_time'])).total_seconds() / 86400,
        })
    stats['rows'] = pd.DataFrame(rows)
    return stats


# ── Статистика ───────────────────────────────────────────────────────────────

def ci(values, alpha=0.05):
    v = np.asarray(values, dtype=float)
    if len(v) < 3:
        return (np.nan, np.nan)
    draws = RNG.choice(v, size=(BOOTSTRAP, len(v)), replace=True).mean(axis=1)
    return tuple(np.percentile(draws, [alpha / 2 * 100, (1 - alpha / 2) * 100]))


def report(df, title):
    print()
    print('=' * 92)
    print(title)
    print('=' * 92)
    head = (f'{"режим":<12}{"сделок":>8}{"доля":>7}{"R/сделку":>10}'
            f'{"95% интервал":>21}{"сумма R":>10}{"винрейт":>9}{"дней":>7}')
    print(head)
    print('-' * len(head))
    total = len(df)
    for name in REGIMES:
        sub = df[df.regime == name]
        if not len(sub):
            print(f'{name:<12}{0:>8}')
            continue
        lo, hi = ci(sub.r)
        wr = (sub.r > 0).mean() * 100
        print(f'{name:<12}{len(sub):>8}{len(sub) / total * 100:>6.0f}%'
              f'{sub.r.mean():>10.3f}{f"[{lo:+.3f}; {hi:+.3f}]":>21}'
              f'{sub.r.sum():>10.1f}{wr:>8.1f}%{sub.days.mean():>7.1f}')
    lo, hi = ci(df.r)
    print('-' * len(head))
    print(f'{"ВСЕГО":<12}{total:>8}{100:>6.0f}%{df.r.mean():>10.3f}'
          f'{f"[{lo:+.3f}; {hi:+.3f}]":>21}{df.r.sum():>10.1f}'
          f'{(df.r > 0).mean() * 100:>8.1f}%{df.days.mean():>7.1f}')


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]

    frames = []
    for period in periods:
        stats = run(period)
        if stats is None:
            continue
        df = stats['rows'].dropna(subset=['regime'])
        df = df.assign(period=period['label'])
        frames.append(df)
        report(df, f'{period["label"].upper()}: разбор по режиму рынка')

    if not frames:
        return
    both = pd.concat(frames, ignore_index=True)
    report(both, 'ОБА ПЕРИОДА ВМЕСТЕ (2.5 года, три режима)')

    print()
    print('Направление сделки внутри режима:')
    head = (f'{"режим":<12}{"направление":<13}{"сделок":>8}{"R/сделку":>10}'
            f'{"95% интервал":>21}{"сумма R":>10}')
    print(head)
    print('-' * len(head))
    for name in REGIMES:
        for side in ('LONG', 'SHORT'):
            sub = both[(both.regime == name) & (both.direction == side)]
            if len(sub) < 3:
                continue
            lo, hi = ci(sub.r)
            print(f'{name:<12}{side:<13}{len(sub):>8}{sub.r.mean():>10.3f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>21}{sub.r.sum():>10.1f}')

    print()
    print('Как далеко доходит сделка (доля позиции, зафиксированная до выхода):')
    head = (f'{"режим":<12}{"ни одной цели":>15}{"взята 1-я":>12}{"взяты 2":>10}'
            f'{"все 3":>8}{"тайм-стоп":>11}{"средний MFE":>13}')
    print(head)
    print('-' * len(head))
    for name in REGIMES:
        sub = both[both.regime == name]
        if not len(sub):
            continue
        n = len(sub)
        time_stop = (sub.reason.str.contains('TIME')).mean() * 100
        print(f'{name:<12}{(sub.tps == 0).mean() * 100:>14.1f}%'
              f'{(sub.tps == 1).mean() * 100:>11.1f}%{(sub.tps == 2).mean() * 100:>9.1f}%'
              f'{(sub.tps == 3).mean() * 100:>7.1f}%{time_stop:>10.1f}%'
              f'{sub.mfe_r.mean():>13.2f}R')

    print()
    print('Что было бы, если бы стратегия НЕ торговала в каком-то режиме:')
    total_r = both.r.sum()
    for name in REGIMES:
        without = both[both.regime != name]
        share = len(both[both.regime == name]) / len(both) * 100
        print(f'   без «{name}»: сумма R {without.r.sum():+7.1f} против {total_r:+7.1f} '
              f'(отказ от {share:.0f}% сделок)')


if __name__ == '__main__':
    main()
