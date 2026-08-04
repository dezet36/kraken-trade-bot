"""
Сравнительный бэктест: SMC-стратегия против текущей фибо-стратегии (v3).

Обе стратегии прогоняются через ОДИН движок (smc_engine) с одинаковыми
правилами: реальный налив лимитных ордеров, комиссии, кулдаун, лимит
одновременных позиций и одинаковый риск на сделку. Только при таких условиях
сравнение говорит что-то о преимуществе стратегии, а не о разнице допущений.

SMC-сигналы берутся из Live_Bot/smc — того же кода, который будет работать в
бою. Фибо-сигналы берутся из Live_Bot/strategy.py — тоже боевого кода.
Копий стратегии в этом файле нет намеренно: расхождение живого кода и
бэктеста было главной архитектурной проблемой прежней версии.

Запуск:
    python research/backtest_smc.py                # обе стратегии, весь пул
    python research/backtest_smc.py --pairs BTCUSDT,ETHUSDT
    python research/backtest_smc.py --only smc
"""

import argparse
import os
import pickle
import sys
from collections import Counter

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc import params as smc_params, signal as smc_signal  # noqa: E402
from smc_engine import (INITIAL_BALANCE, Order, compute_stats,  # noqa: E402
                        print_stats, run_portfolio, to_naive_ns)

# Каталог кэша выбирается переменной окружения: помимо бычьего периода
# 2025-2026 есть кэш медвежьего рынка 2022-2023 (backtest_cache_bear).
# Проверка на другом рыночном режиме — обязательное условие деплоя, поэтому
# все исследовательские скрипты должны уметь переключаться одной переменной.
CACHE_DIR = os.getenv(
    'SMC_CACHE_DIR', os.path.join(ROOT, 'research', 'backtest_cache_12m'))

DEFAULT_PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT',
    'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT',
]

# Единые правила портфеля для обеих стратегий
RISK_PCT = 1.0
MAX_POSITIONS = 5
COOLDOWN_HOURS = 12.0


# ── Данные ───────────────────────────────────────────────────────────────────
def load_cached(pair, timeframe):
    path = os.path.join(CACHE_DIR, f'{pair}_{timeframe}.pkl')
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        raw = pickle.load(fh)
    df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    return df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)


def resample(df, rule):
    """Агрегация в старший таймфрейм — избавляет от докачки 1d данных."""
    return (df.set_index('timestamp')
              .resample(rule)
              .agg({'open': 'first', 'high': 'max', 'low': 'min',
                    'close': 'last', 'volume': 'sum'})
              .dropna()
              .reset_index())


def load_pair(pair):
    """
    Возвращает {'1h','4h','5m','1d','15m'} или None, если кэша нет.

    15m и 1d получаются агрегацией из имеющихся 5m и 1h — докачка данных не
    требуется. Агрегация корректна: старшая свеча целиком состоит из младших.
    """
    df_1h = load_cached(pair, '1h')
    df_5m = load_cached(pair, '5m')
    if df_1h is None or df_5m is None:
        return None
    df_4h = load_cached(pair, '4h')
    if df_4h is None:
        df_4h = resample(df_1h, '4h')
    return {
        '1h': df_1h, '4h': df_4h, '5m': df_5m,
        '1d': resample(df_1h, '1D'),
        '15m': resample(df_5m, '15min'),
    }


# ── Генерация ордеров: SMC ───────────────────────────────────────────────────
def smc_orders(pair, data, reasons=None):
    """
    Прогоняет MarketContext по всем свечам 1H и превращает сетапы в ордера.

    Дедупликация: генератор выдаёт сетап на каждой свече, пока цена идёт к
    зоне. Это ОДИН торговый сетап — ключуем по (пара, зона, направление),
    чтобы движок не открыл сто сделок из одной идеи.
    """
    ctx = smc_signal.build_context(
        {'bias': data['1d'], 'htf': data['4h'], 'poi': data['1h']}, pair=pair)

    df = data['1h']
    orders = []
    seen = set()
    expiry = np.timedelta64(int(smc_params.PENDING_ORDER_MAX_HOURS * 3600), 's')

    for i in range(60, len(df)):
        setup, why = ctx.evaluate(i, balance=INITIAL_BALANCE)
        if setup is None:
            if reasons is not None:
                reasons[why.split('(')[0].strip()] += 1
            continue

        poi = setup['poi']
        key = (pair, poi['type'], poi['index'], setup['direction'])
        if key in seen:
            continue
        seen.add(key)

        created = np.datetime64(pd.Timestamp(setup['time']).tz_convert('UTC').tz_localize(None))
        trade = setup['params']

        orders.append(Order(
            pair=pair,
            direction=setup['direction'],
            entry=trade['entry'],
            stop=trade['stop_loss'],
            targets=trade['targets'],
            fractions=trade['fractions'],
            created=created,
            expires=created + expiry,
            key=key,
            meta={
                'poi_type': poi['type'],
                'confluence': setup['confluence'],
                'rr': trade['rr'],
                'factors': setup['factors'],
            },
        ))

    return orders


# ── Генерация ордеров: текущая фибо-стратегия (v3) ───────────────────────────
def fibo_orders(pair, data, reasons=None):
    """
    Воспроизводит боевую логику v3: гейты сканера + analyze_market.

    Импортируем настоящие модули бота, а не копию: если живой код изменится,
    бэктест поедет вместе с ним, и расхождения между моделью и боем не будет.
    """
    import config
    import strategy

    df_1h = data['1h']
    df_4h = data['4h']
    orders = []
    seen = set()
    expiry = np.timedelta64(int(config.PENDING_ORDER_MAX_HOURS * 3600), 's')

    ts_4h = to_naive_ns(df_4h['timestamp'])
    lookback = config.LOOKBACK_CANDLES

    for i in range(lookback + 10, len(df_1h)):
        window = df_1h.iloc[i - lookback: i + 1]
        now = window.iloc[-1]['timestamp']
        now_naive = pd.Timestamp(now).tz_convert('UTC').tz_localize(None).to_datetime64()

        # Сессионный фильтр v3: блок рождения сетапов в 12-16 UTC
        if now.hour in config.BLOCK_ENTRY_HOURS_UTC:
            if reasons is not None:
                reasons['заблокированный час'] += 1
            continue

        setup = strategy.find_recent_impulse(window, lookback_candles=lookback)
        if not setup:
            if reasons is not None:
                reasons['импульс не найден'] += 1
            continue

        size_pct = setup['size'] / setup['end_price'] * 100
        if size_pct < config.MIN_IMPULSE_PCT:
            if reasons is not None:
                reasons['импульс мал'] += 1
            continue

        # HTF-фильтр тренда по 4H (EMA50/EMA200)
        pos_4h = int(np.searchsorted(ts_4h, now_naive, side='right'))
        df_4h_window = df_4h.iloc[max(0, pos_4h - 220):pos_4h]
        htf_trend = strategy.get_htf_trend(df_4h_window)
        if htf_trend == 'BULLISH' and setup['type'] == 'SHORT':
            if reasons is not None:
                reasons['контртренд'] += 1
            continue
        if htf_trend == 'BEARISH' and setup['type'] == 'LONG':
            if reasons is not None:
                reasons['контртренд'] += 1
            continue

        signal = strategy.analyze_market(window, None, pair, INITIAL_BALANCE)
        if not signal:
            if reasons is not None:
                reasons['analyze_market отказал'] += 1
            continue

        prm = signal['params']
        key = (pair, setup['type'], round(setup['start_price'], 8),
               round(setup['end_price'], 8))
        if key in seen:
            continue
        seen.add(key)

        created = now_naive
        orders.append(Order(
            pair=pair,
            direction='LONG' if setup['type'] == 'LONG' else 'SHORT',
            entry=prm['entry'],
            stop=prm['stop_loss'],
            targets=[prm['take_profit_1']],
            fractions=[1.0],
            created=created,
            expires=created + expiry,
            key=key,
            # v3: безубыток при пробое уровня B импульса
            be_trigger=prm['be_level'] if config.BREAKEVEN_AT_B else None,
            meta={'rr': prm['rr'], 'htf': htf_trend},
        ))

    return orders


# ── Прогон ───────────────────────────────────────────────────────────────────
def run(pairs, which='both'):
    print(f'Загрузка данных: {len(pairs)} пар из {CACHE_DIR}')
    data = {}
    for pair in pairs:
        loaded = load_pair(pair)
        if loaded is None:
            print(f'   {pair}: нет кэша, пропуск')
            continue
        data[pair] = loaded
    if not data:
        print('Нет данных — сначала прогони research/backtest.py для наполнения кэша.')
        return

    span = next(iter(data.values()))['1h']
    print(f'   период: {span.timestamp.iloc[0].date()} .. {span.timestamp.iloc[-1].date()}')
    print(f'   пар загружено: {len(data)}')

    exec_data = {pair: frames['5m'] for pair, frames in data.items()}
    results = []

    if which in ('both', 'smc'):
        print('\n[SMC] генерация сигналов...')
        reasons = Counter()
        orders = []
        for pair, frames in data.items():
            found = smc_orders(pair, frames, reasons)
            orders += found
            print(f'   {pair}: {len(found)} сетапов')
        print(f'   всего сетапов: {len(orders)}')
        print('   воронка отсева (топ-6):')
        for reason, count in reasons.most_common(6):
            print(f'      {count:7d}  {reason}')

        outcome = run_portfolio(
            orders, exec_data, risk_pct=RISK_PCT, max_positions=MAX_POSITIONS,
            cooldown_hours=COOLDOWN_HOURS,
            breakeven_after_tp1=smc_params.BREAKEVEN_AFTER_TP1,
            max_hold_hours=smc_params.MAX_POSITION_HOLD_HOURS)
        results.append(compute_stats(outcome, label='SMC'))
        _print_breakdown(outcome, 'SMC')

    if which in ('both', 'fibo'):
        print('\n[Фибо v3] генерация сигналов...')
        reasons = Counter()
        orders = []
        for pair, frames in data.items():
            found = fibo_orders(pair, frames, reasons)
            orders += found
            print(f'   {pair}: {len(found)} сетапов')
        print(f'   всего сетапов: {len(orders)}')

        import config
        outcome = run_portfolio(
            orders, exec_data, risk_pct=RISK_PCT, max_positions=MAX_POSITIONS,
            cooldown_hours=COOLDOWN_HOURS, breakeven_after_tp1=False,
            max_hold_hours=config.MAX_POSITION_HOLD_HOURS or 336.0)
        results.append(compute_stats(outcome, label='Фибо v3'))
        _print_breakdown(outcome, 'Фибо v3')

    print('\n' + '=' * 70)
    print(f'СРАВНЕНИЕ  (риск {RISK_PCT}%/сделку, до {MAX_POSITIONS} позиций, '
          f'комиссии включены)')
    print('=' * 70)
    print_stats(results)


def _print_breakdown(outcome, label):
    """Разбор причин выхода и вклада типов зон."""
    trades = outcome['trades']
    if not trades:
        print(f'   [{label}] сделок нет')
        return

    reasons = Counter(t['exit_reason'] for t in trades)
    print(f'   [{label}] причины выхода: {dict(reasons)}')

    types = {}
    for trade in trades:
        key = trade['meta'].get('poi_type', '-')
        entry = types.setdefault(key, {'n': 0, 'pnl': 0.0})
        entry['n'] += 1
        entry['pnl'] += trade['pnl']
    if len(types) > 1:
        print(f'   [{label}] по типам зон:')
        for key, stat in sorted(types.items(), key=lambda kv: -kv[1]['pnl']):
            print(f'      {key:<14} n={stat["n"]:4d}  pnl=${stat["pnl"]:+9.0f}')


def main():
    parser = argparse.ArgumentParser(description='Сравнительный бэктест SMC vs фибо v3')
    parser.add_argument('--pairs', default=','.join(DEFAULT_PAIRS),
                        help='список пар через запятую')
    parser.add_argument('--only', choices=['smc', 'fibo', 'both'], default='both',
                        help='какую стратегию прогонять')
    args = parser.parse_args()

    run([p.strip() for p in args.pairs.split(',') if p.strip()], which=args.only)


if __name__ == '__main__':
    main()
