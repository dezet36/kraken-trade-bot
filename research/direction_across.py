"""
Перекос лонг/шорт: свойство СТРАТЕГИИ или свойство РЫНКА?

ЗАЧЕМ ИМЕННО ТАК ПОСТАВЛЕН ВОПРОС. У Фибоначчи лонги дают ровно ноль на
обоих периодах, а «только шорты» принято дважды разными замерами. У SMC
перекос в ту же сторону известен давно, и по нему уже отвергнуты две
гипотезы. Продолжать копать в SMC отдельно — значит третий раз объяснять
одно и то же явление у одной стратегии.

Гораздо дешевле сначала выяснить, ОДНО ли это явление. Если шорты сильнее
лонгов у ВСЕХ трёх стратегий, устроенных совершенно по-разному — коррекция
Фибоначчи, ордер-блоки, отбой от уровня, — то дело не в стратегиях. Дело в
том, что криптовалюты падают резче, чем растут: падения короткие и
импульсные, рост пологий и упорный, и любой контртрендовый вход вниз
отрабатывает быстрее, чем такой же вверх.

Тогда и решение принимается ОДНО, на уровне портфеля, а не три раза по
отдельности. А если перекос есть только у одной-двух — значит он про их
устройство, и копать надо там.

КАК МЕРЯЕТСЯ. Каждая стратегия своим боевым кодом, своими параметрами, на
одних и тех же двух периодах. Для каждой — три полных прогона портфеля:
обе стороны, только лонги, только шорты. Разбиением готовых сделок это
делать нельзя: выключенное направление освобождает слоты, и состав портфеля
меняется — на Фибоначчи разница оказалась трёхкратной.

ЧТО СЧИТАТЬ ОТВЕТОМ:
    перекос общий    у всех трёх шорты лучше лонгов, и интервалы разницы
                     не накрывают ноль хотя бы у двух;
    перекос частный  хотя бы у одной стратегии лонги не хуже шортов.

Запуск:
    python research/direction_across.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from fibo_audit import ci, diff_ci, hush, unhush  # noqa: E402

PAIRS_LIMIT = 8
SIDES = (('обе стороны', ('LONG', 'SHORT')),
         ('только лонги', ('LONG',)),
         ('только шорты', ('SHORT',)))


# ── Сбор заявок: у каждой стратегии свой боевой путь ─────────────────────────

def orders_fibo(data, pairs):
    import config
    import strategy
    from smc_engine import Order

    expiry = np.timedelta64(int(config.PENDING_ORDER_MAX_HOURS * 3600), 's')
    lookback = config.LOOKBACK_CANDLES
    out, seen = [], set()
    for pair in pairs:
        df_1h = data[pair]['1h']
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
            created = pd.Timestamp(window.iloc[-1]['timestamp']) \
                .tz_convert('UTC').tz_localize(None).to_datetime64()
            out.append(Order(
                pair=pair, direction=setup['type'],
                entry=prm['entry'], stop=prm['stop_loss'],
                targets=[prm['take_profit_1']], fractions=[1.0],
                created=created, expires=created + expiry, key=key,
                be_trigger=prm['be_level'] if config.BREAKEVEN_AT_B else None,
                meta={'direction': setup['type']}))
    return out


def orders_levels(data, pairs):
    """
    Уровни: боевая evaluate на каждой свече, как в их собственном замере.

    Своя геометрия и свои параметры — ни одно число не заимствовано у
    соседей, как и договаривались с самого начала.
    """
    from levels import core, params as P
    from smc_engine import Order

    expiry = np.timedelta64(int(P.EXPIRY_HOURS * 3600), 's')
    out, seen = [], set()
    for pair in pairs:
        df = data[pair]['1h']
        high = df['high'].to_numpy(float)
        low = df['low'].to_numpy(float)
        close = df['close'].to_numpy(float)
        volume = (df['volume'].to_numpy(float) if 'volume' in df.columns
                  else np.ones(len(df)))
        stamps = pd.to_datetime(df['timestamp'])
        if getattr(stamps.dt, 'tz', None) is not None:
            stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
        stamps = stamps.to_numpy()
        levels = core.build_levels(high, low)
        atr_values = core.atr(high, low, close)

        for i in range(60, len(close)):
            setup, _reason = core.evaluate(high, low, close, volume, i,
                                           levels=levels, atr_values=atr_values)
            if not setup:
                continue
            key = (pair, setup['direction'], round(setup['level'], 8), int(i))
            if key in seen:
                continue
            seen.add(key)
            created = stamps[i]
            out.append(Order(
                pair=pair, direction=setup['direction'],
                entry=setup['entry'], stop=setup['stop_loss'],
                targets=[setup['target']], fractions=[1.0],
                created=created, expires=created + expiry, key=key,
                entry_type='stop',
                meta={'direction': setup['direction']}))
    return out


def orders_smc(period):
    from smc_sweep import build_orders

    out = []
    for pair in period['data']:
        out += build_orders(period['contexts'][pair], pair,
                            period['data'][pair]['1h'])
    return out


def _side_of(order):
    """
    Сторона заявки одним словом.

    SMC называет направления BULLISH и BEARISH, две другие стратегии — LONG и
    SHORT. Сравнивать напрямую нельзя: фильтр по 'SHORT' не нашёл бы у SMC ни
    одной заявки, и «только шорты» тихо оказалось бы пустым прогоном, а не
    ошибкой.
    """
    value = str(getattr(order, 'direction', '')).upper()
    if value in ('BULLISH', 'LONG'):
        return 'LONG'
    if value in ('BEARISH', 'SHORT'):
        return 'SHORT'
    return value


# ── Прогон ───────────────────────────────────────────────────────────────────

def run_side(orders, exec_data, sides, risk_pct, max_positions,
             cooldown_hours, max_same, max_hold=None):
    from smc_engine import compute_stats, run_portfolio

    chosen = [o for o in orders if _side_of(o) in sides]
    if len(chosen) < 3:
        return None
    kwargs = dict(risk_pct=risk_pct, max_positions=max_positions,
                  cooldown_hours=cooldown_hours, max_same_direction=max_same,
                  breakeven_after_tp1=False)
    if max_hold:
        kwargs['max_hold_hours'] = max_hold
    result = run_portfolio(chosen, exec_data, **kwargs)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 3:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    return {'r': r, 'n': len(trades), 'orders': len(chosen),
            'mean': float(r.mean()), 'total': float(r.sum()),
            'wr': float((r > 0).mean() * 100),
            'dd': stats['max_dd_pct'], 'ret': stats['return_pct']}


def load(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    for module in ('backtest_smc', 'smc_sweep'):
        sys.modules.pop(module, None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    data = {}
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
    return data


def main():
    import config
    from levels import params as LP
    from smc import params as SP
    from smc_market_regime import load_period

    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        data = load(cache, pairs, label)
        smc_period = load_period(cache, list(data)[:PAIRS_LIMIT], label + ' · smc')
        periods[label] = (data, smc_period)

    results = {}
    for label, (data, smc_period) in periods.items():
        pairs = list(data)
        exec_1h = {p: data[p]['1h'] for p in pairs}
        exec_5m = {p: data[p]['5m'] for p in pairs}

        quiet = hush()
        try:
            print(f'[{label}] сбор заявок...', flush=True)
            books = {
                'FIBO': (orders_fibo(data, pairs), exec_5m,
                         dict(risk_pct=config.RISK_PER_TRADE,
                              max_positions=getattr(config, 'MAX_OPEN_POSITIONS', 5),
                              cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
                              max_same=getattr(config, 'MAX_SAME_DIRECTION', 0))),
                'LEVELS': (orders_levels(data, pairs), exec_1h,
                           dict(risk_pct=LP.RISK_PCT, max_positions=LP.MAX_POSITIONS,
                                cooldown_hours=LP.COOLDOWN_HOURS,
                                max_same=LP.MAX_SAME_DIRECTION,
                                max_hold=LP.MAX_HOLD_HOURS)),
                # Параметры портфеля у SMC живут в backtest_smc, а не в
                # smc/params: там их и берёт её собственный замер. Взять их из
                # другого места значило бы мерить не ту стратегию.
                'SMC': (orders_smc(smc_period), exec_5m,
                        dict(risk_pct=smc_period['bt'].RISK_PCT,
                             max_positions=smc_period['bt'].MAX_POSITIONS,
                             cooldown_hours=smc_period['bt'].COOLDOWN_HOURS,
                             max_same=SP.MAX_SAME_DIRECTION)),
            }
        finally:
            unhush(quiet)

        print()
        print('=' * 104)
        print(f'{label}')
        print('=' * 104)
        head = (f'{"стратегия":<10}{"вариант":<16}{"заявок":>8}{"сделок":>8}'
                f'{"винрейт":>9}{"R/сделку":>10}{"сумма R":>9}{"доход%":>9}'
                f'{"DD%":>7}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, (orders, exec_data, cfg) in books.items():
            results[label][name] = {}
            for side_name, sides in SIDES:
                res = run_side(orders, exec_data, sides, **cfg)
                if res is None:
                    print(f'{name:<10}{side_name:<16}{"— мало сделок":>18}')
                    continue
                results[label][name][side_name] = res
                lo, hi = ci(res['r'])
                print(f'{name:<10}{side_name:<16}{res["orders"]:>8}{res["n"]:>8}'
                      f'{res["wr"]:>8.1f}%{res["mean"]:>10.3f}{res["total"]:>9.1f}'
                      f'{res["ret"]:>9.1f}{res["dd"]:>7.1f}'
                      f'{f"[{lo:+.3f}; {hi:+.3f}]":>22}')
            print('-' * len(head))

    print()
    print('=' * 104)
    print('ШОРТЫ ПРОТИВ ЛОНГОВ (интервал разницы средних)')
    print('=' * 104)
    head = f'{"стратегия":<12}' + ''.join(f'{lbl:>36}' for lbl in results)
    print(head)
    print('-' * len(head))
    verdicts = {}
    for name in ('FIBO', 'SMC', 'LEVELS'):
        cells, clear = '', 0
        for label, table in results.items():
            item = table.get(name) or {}
            longs, shorts = item.get('только лонги'), item.get('только шорты')
            if not longs or not shorts:
                cells += f'{"—":>36}'
                continue
            lo, hi = diff_ci(shorts['r'], longs['r'])
            gap = shorts['mean'] - longs['mean']
            if lo > 0:
                clear += 1
            cell = f'{gap:+.3f} [{lo:+.3f}; {hi:+.3f}]'
            cells += f'{cell:>36}'
        verdicts[name] = clear
        mark = '  ШОРТЫ ЯВНО СИЛЬНЕЕ' if clear == len(results) else (
            '  шорты сильнее на одном' if clear else '')
        print(f'{name:<12}{cells}{mark}')

    print()
    if all(v == len(results) for v in verdicts.values()):
        print('ВЫВОД: перекос ОБЩИЙ — он про рынок, а не про стратегии.')
        print('Решение принимается один раз на уровне портфеля.')
    elif any(v == 0 for v in verdicts.values()):
        print('ВЫВОД: перекос ЧАСТНЫЙ — есть стратегия, где лонги не хуже.')
        print('Значит дело в устройстве конкретных стратегий, копать надо там.')
    else:
        print('ВЫВОД: картина смешанная — перекос есть у всех, но убедителен')
        print('не везде. Общего правила по такому основанию вводить нельзя.')


if __name__ == '__main__':
    main()
