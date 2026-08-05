"""
Стратегия горизонтальных уровней. Самостоятельная, ни на чём нашем не стоит.

ПРИНЦИП. Не наследует у SMC и фибо ничего: ни поиска экстремумов, ни
порогов риска, ни числа слотов, ни лимита направления. Все числа — в
levels_params.py. Оценивается сама по себе: прибыльна ли, какая просадка,
воспроизводится ли на двух независимых периодах.

МЕТОД И ЧТО ОКАЗАЛОСЬ ГЛАВНЫМ. Уровень — цена, к которой рынок возвращался
несколько раз, возможно с разных сторон. Первая версия ставила лимит прямо
на уровень и дала -1104 R: вход происходил и когда уровень устоял, и когда
его прошли насквозь — в момент постановки заявки это неразличимо.

Добавление ПОДТВЕРЖДЕНИЯ перевернуло результат на том же наборе уровней:

    лимит на уровне           бык -1104 R    медведь -1467 R
    прокол + возврат          бык  +138 R    медведь  +112 R

Работает не уровень сам по себе, а отказ входить до того, как рынок показал
реакцию. Стоп при этом уезжает за экстремум прокола — то есть за точку,
куда цена уже сходила и откуда вернулась, а не в зону, где собирают стопы.

ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ. К подтверждённому входу по одному добавляются
критерии классики, и каждый принимается только если улучшает ОБА периода:

    объём на возврате     защита уровня крупным участником
    цель на следующем уровне   вместо кратной риску
    безубыток после 1R    строгий money management
    ближайшие уровни      живой трейдер держит на графике единицы уровней
    зеркальность, касания, круглые числа, скорость подхода

Из метода НЕ воспроизводится биржевой стакан (плотность, крупная заявка).
Ни в кэше, ни в истории ccxt его нет. Объём свечи — единственный доступный
след присутствия крупного участника.

ЧЕСТНОСТЬ. Экстремум известен через PIVOT_N баров после себя; уровень — с
подтверждения последнего входящего в него касания. Прокол и возврат
считаются по закрытым свечам, вход — по закрытию свечи возврата. ATR,
объём и скорость подхода берутся по барам до свечи решения включительно.

Запуск:
    python research/levels_gerchik.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import levels_params as LP  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402
from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period)

LONG, SHORT = 'LONG', 'SHORT'
RNG = np.random.default_rng(20260805)
BOOTSTRAP = 10_000


def diff_ci(a, b):
    """Интервал разности средних. Пересекает ноль — разница недоказуема."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [2.5, 97.5]), float((d > 0).mean())


def atr(df, period=14):
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    out = np.full(len(tr), np.nan)
    if len(tr) > period:
        out[period] = tr[1:period + 1].mean()
        for i in range(period + 1, len(tr)):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def pivots(df, n):
    """
    Точки касания: локальные экстремумы с n барами по обе стороны.

    Важно ровно одно свойство — момент, когда экстремум СТАНОВИТСЯ ИЗВЕСТЕН:
    это n баров после него. Уровень, построенный раньше, знал бы будущее.
    """
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    out = []
    for i in range(n, len(df) - n):
        wh = high[i - n:i + n + 1]
        wl = low[i - n:i + n + 1]
        if high[i] == wh.max() and wh.argmax() == n:
            out.append({'index': i, 'price': float(high[i]), 'kind': 'high',
                        'known_at': i + n})
        if low[i] == wl.min() and wl.argmin() == n:
            out.append({'index': i, 'price': float(low[i]), 'kind': 'low',
                        'known_at': i + n})
    return sorted(out, key=lambda p: p['index'])


def round_distance_pct(price):
    """
    Близость цены к круглому числу, в процентах.

    Шаг круглости — на порядок мельче цены: для 62 800 это 1 000, для 0.85
    это 0.01. Без привязки к порядку «круглое число» на биткоине и на
    дожкоине значило бы совершенно разное.
    """
    if price <= 0:
        return 100.0
    step = 10.0 ** (np.floor(np.log10(price)) - 1)
    nearest = round(price / step) * step
    return abs(price - nearest) / price * 100


def build_levels(df, tolerance_pct=None, min_touches=None):
    """
    Уровни: кластеры касаний на одной цене.

    Вершины и низы — в ОДНОМ пуле. Уровень, который сначала останавливал
    рост, а потом держал падение, и есть зеркальный. Раздельные пулы таких
    уровней не видят вовсе.
    """
    tolerance_pct = LP.TOLERANCE_PCT if tolerance_pct is None else tolerance_pct
    min_touches = LP.MIN_TOUCHES if min_touches is None else min_touches

    points = pivots(df, LP.PIVOT_N)
    levels, used = [], set()
    for i, first in enumerate(points):
        if i in used:
            continue
        members, idxs = [first], {i}
        for j in range(i + 1, len(points)):
            second = points[j]
            if second['index'] - first['index'] > LP.MAX_SPAN_BARS:
                break
            if abs(second['price'] - first['price']) / first['price'] * 100 <= tolerance_pct:
                members.append(second)
                idxs.add(j)
        if len(members) < min_touches:
            continue
        used |= idxs
        levels.append({
            'price': float(np.mean([m['price'] for m in members])),
            'touches': len(members),
            'mirror': len({m['kind'] for m in members}) > 1,
            'known_at': max(m['known_at'] for m in members),
        })
    return levels


def _reclaim(high, low, close, i, level, side, atr_now):
    """
    Прокол уровня с возвратом в ближайших барах после i.

    Возвращает (индекс свечи возврата, экстремум прокола) или None. Решение
    принимается на закрытии свечи возврата — раньше подтверждения нет.
    """
    limit = min(i + LP.RECLAIM_BARS + 1, len(close))
    need = LP.PIERCE_ATR * atr_now

    pierce_at = None
    for k in range(i, limit):
        if side == LONG and low[k] <= level - need:
            pierce_at = k
            break
        if side == SHORT and high[k] >= level + need:
            pierce_at = k
            break
    if pierce_at is None:
        return None

    extreme = low[pierce_at] if side == LONG else high[pierce_at]
    for k in range(pierce_at, min(pierce_at + LP.RECLAIM_BARS + 1, len(close))):
        extreme = min(extreme, low[k]) if side == LONG else max(extreme, high[k])
        if k > pierce_at and (close[k] > level if side == LONG else close[k] < level):
            return k, float(extreme)
    return None


def build_orders(pair, df, min_touches=None, require_mirror=False,
                 max_round_pct=None, speed_mode=None, rr_target=None,
                 min_stop_pct=None, tolerance_pct=None, nearest=None,
                 volume_ratio=None, target_next_level=False,
                 breakeven_r=None, max_same_bar=None):
    """Заявки на отбой от уровня с подтверждением реакции."""
    rr_target = LP.RR_TARGET if rr_target is None else rr_target
    min_stop_pct = LP.MIN_STOP_PCT if min_stop_pct is None else min_stop_pct
    nearest = LP.NEAREST_LEVELS if nearest is None else nearest

    ts = pd.to_datetime(df['timestamp'])
    if getattr(ts.dt, 'tz', None) is not None:
        ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
    ts = ts.to_numpy(dtype='datetime64[ns]')
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    vol = (df['volume'].to_numpy(dtype=float) if 'volume' in df.columns
           else np.ones(len(df)))
    vol_avg = pd.Series(vol).rolling(LP.VOLUME_WINDOW).mean().to_numpy()
    a = atr(df)
    bar_ns = int(np.median(np.diff(ts).astype('int64'))) if len(ts) > 2 else 0
    expiry = np.timedelta64(int(LP.EXPIRY_HOURS * 3600), 's')

    levels = build_levels(df, tolerance_pct, min_touches)
    if not levels:
        return []
    levels.sort(key=lambda x: x['known_at'])
    known_at = np.array([lv['known_at'] for lv in levels])
    prices = np.array([lv['price'] for lv in levels])

    orders, seen = [], set()
    for i in range(60, len(df)):
        if np.isnan(a[i]) or a[i] <= 0:
            continue
        upto = int(np.searchsorted(known_at, i, 'right'))
        if upto == 0:
            continue
        price = close[i]

        if speed_mode:
            j = max(0, i - LP.SPEED_BARS)
            speed = abs(price - close[j]) / a[i]
            if speed_mode == 'fast' and speed < LP.SPEED_THRESHOLD:
                continue
            if speed_mode == 'slow' and speed >= LP.SPEED_THRESHOLD:
                continue

        # Только ближайшие к цене уровни: трейдер держит на графике единицы
        # уровней, а не сотню. Без этого одна пара давала тысячи заявок.
        candidates = [k for k in range(upto)
                      if i - levels[k]['known_at'] <= LP.MAX_AGE_BARS]
        if not candidates:
            continue
        candidates.sort(key=lambda k: abs(prices[k] - price))
        candidates = candidates[:nearest]

        for k in candidates:
            lv = levels[k]
            if require_mirror and not lv['mirror']:
                continue
            if max_round_pct is not None and round_distance_pct(lv['price']) > max_round_pct:
                continue

            gap = price - lv['price']
            if abs(gap) > LP.TRIGGER_ATR * a[i] or abs(gap) < LP.MIN_GAP_ATR * a[i]:
                continue
            side = LONG if gap > 0 else SHORT     # цена выше уровня -> поддержка

            key = (pair, round(lv['price'], 8), side, int(i // 24))
            if key in seen:
                continue
            seen.add(key)

            found = _reclaim(high, low, close, i, lv['price'], side, a[i])
            if found is None:
                continue
            r_at, extreme = found

            if volume_ratio is not None:
                avg = vol_avg[r_at]
                if not np.isfinite(avg) or avg <= 0 or vol[r_at] / avg < volume_ratio:
                    continue

            entry = float(close[r_at])
            dist = max(abs(entry - extreme) + LP.STOP_PAD_ATR * a[r_at],
                       entry * min_stop_pct / 100)
            stop = entry - dist if side == LONG else entry + dist

            if target_next_level:
                # Цель — следующий уровень по ходу сделки. Так выходит
                # трейдер, торгующий от уровней: движение живёт до
                # следующего препятствия, а не до круглого числа R.
                ahead = [prices[m] for m in range(upto)
                         if (prices[m] > entry + dist if side == LONG
                             else prices[m] < entry - dist)]
                if not ahead:
                    continue
                target = min(ahead) if side == LONG else max(ahead)
                if abs(target - entry) / dist < 1.5:
                    continue
            else:
                target = (entry + rr_target * dist if side == LONG
                          else entry - rr_target * dist)

            created = ts[r_at] + np.timedelta64(bar_ns, 'ns')
            be = None
            if breakeven_r:
                be = (entry + breakeven_r * dist if side == LONG
                      else entry - breakeven_r * dist)

            orders.append(Order(
                pair=pair, direction=side, entry=entry, stop=float(stop),
                targets=[float(target)], fractions=[1.0],
                created=created, expires=created + expiry, key=key,
                entry_type='stop', be_trigger=be,
                meta={'touches': lv['touches'], 'mirror': lv['mirror'],
                      'round_pct': round_distance_pct(lv['price']),
                      'rr': abs(target - entry) / dist},
            ))
    return orders


def run(period, orders):
    """Прогон на СОБСТВЕННЫХ портфельных настройках стратегии."""
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in period['data']},
        risk_pct=LP.RISK_PCT, max_positions=LP.MAX_POSITIONS,
        cooldown_hours=LP.COOLDOWN_HOURS,
        max_same_direction=LP.MAX_SAME_DIRECTION,
        max_hold_hours=LP.MAX_HOLD_HOURS,
        breakeven_after_tp1=False)
    if not result['trades']:
        return None
    stats = compute_stats(result, label='')
    rows = []
    for t in result['trades']:
        if not t.get('risk'):
            continue
        rows.append({
            'r': t['pnl'] / t['risk'],
            'regime': period['regime'](t['entry_time']),
            'direction': 'LONG' if t['direction'] in ('BULLISH', 'LONG') else 'SHORT',
            'touches': t['meta'].get('touches', 0),
            'mirror': bool(t['meta'].get('mirror')),
            'entry_time': pd.Timestamp(t['entry_time']),
            'days': (pd.Timestamp(t['exit_time']) - pd.Timestamp(t['entry_time'])
                     ).total_seconds() / 86400,
        })
    stats['rows'] = pd.DataFrame(rows)
    stats['orders'] = len(orders)
    return stats


BASE = 'база: подтверждение, 2 касания, RR3'

# Финальный круг. Из двенадцати проверенных дополнений приёмку прошло одно —
# объём на свече возврата: он улучшил доход И просадку на ОБОИХ периодах, а
# на бычьем разница доказуема (интервал не пересекает ноль). Цель 4R тоже
# улучшила оба периода по доходу, но подняла просадку на медвежьем с 40% до
# 53.5%, поэтому проверяется в связке с объёмом, который просадку снижает.
#
# Критерии силы уровня (зеркальность, три касания, круглые числа, скорость
# подхода) отклонены: каждый улучшал один период и портил другой.
# ПРОВЕРКА УСТОЙЧИВОСТИ ПОБЕДИТЕЛЯ, а не поиск лучшего числа.
#
# Принята конфигурация «объём + цель на следующем уровне»: единственная за
# сессию, доказуемая на ОБОИХ периодах (бык ΔR +0.177 [+0.017; +0.336],
# медведь +0.288 [+0.104; +0.476]), просадка 9.9% и 5.7% против 33.7% и
# 40.0% у базы.
#
# Вопрос здесь один: держится ли результат при сдвиге настроек. Если да —
# находка настоящая. Если пик приходится ровно на выбранные числа, а по
# краям всё разваливается, значит это подгонка, и настоящий вывод —
# «работает подтверждение входа», а не «работают именно эти пороги».
CONFIGS = [
    (BASE,                      dict()),
    ('цель на уровне (без объёма)',
     dict(target_next_level=True)),
    ('объём x1.2 + цель на уровне',
     dict(volume_ratio=1.2, target_next_level=True)),
    ('объём x1.5 + цель на уровне',
     dict(volume_ratio=1.5, target_next_level=True)),
    ('объём x2.0 + цель на уровне',
     dict(volume_ratio=2.0, target_next_level=True)),
    ('объём x2.5 + цель на уровне',
     dict(volume_ratio=2.5, target_next_level=True)),
    ('объём x1.5 + цель + 3 касания',
     dict(volume_ratio=1.5, target_next_level=True, min_touches=3)),
    ('объём x1.5 + цель + 1 уровень',
     dict(volume_ratio=1.5, target_next_level=True, nearest=1)),
    ('объём x1.5 + цель + 4 уровня',
     dict(volume_ratio=1.5, target_next_level=True, nearest=4)),
]


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    results = {}
    for name, kw in CONFIGS:
        for period in periods:
            orders = []
            for pair, data in period['data'].items():
                orders += build_orders(pair, data['1h'], **kw)
            stats = run(period, orders) if orders else None
            if stats is None:
                print(f'   [{period["label"]}] {name}: сделок нет')
                continue
            results[(period['label'], name)] = stats
            df = stats['rows']
            print(f'   [{period["label"]}] {name}: заявок {stats["orders"]}, '
                  f'{len(df)} сделок, {stats["return_pct"]:+.1f}%, '
                  f'DD {stats["max_dd_pct"]:.1f}%, сумма R {df.r.sum():+.1f}',
                  flush=True)

    for period in periods:
        label = period['label']
        print()
        print('=' * 110)
        print(label.upper())
        print('=' * 110)
        head = (f'{"конфигурация":<34}{"сделок":>8}{"винрейт":>9}{"R/сделку":>10}'
                f'{"сумма R":>9}{"доход%":>9}{"DD%":>7}{"доход/DD":>10}{"дней":>7}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            df = stats['rows']
            dd = stats['max_dd_pct']
            print(f'{name:<34}{len(df):>8}{(df.r > 0).mean() * 100:>8.0f}%'
                  f'{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{stats["return_pct"]:>+9.1f}{dd:>7.1f}'
                  f'{stats["return_pct"] / dd if dd else float("nan"):>10.2f}'
                  f'{df.days.median():>7.1f}')

        base = results.get((label, BASE))
        if not base:
            continue
        print()
        print('Разница с базой (интервал через ноль = разница недоказуема):')
        for name, _ in CONFIGS[1:]:
            stats = results.get((label, name))
            if not stats:
                continue
            (lo, hi), p = diff_ci(stats['rows'].r, base['rows'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<34} ΔR {stats["rows"].r.mean() - base["rows"].r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('=' * 110)
    print('СПРАВОЧНО: ПО РЕЖИМАМ РЫНКА (не критерий приёмки)')
    print('=' * 110)
    head = f'{"конфигурация":<34}' + ''.join(f'{r:>26}' for r in REGIMES)
    print(head)
    print('-' * len(head))
    for name, _ in CONFIGS:
        frames = [results[(p['label'], name)]['rows'] for p in periods
                  if (p['label'], name) in results]
        if not frames:
            continue
        merged = pd.concat(frames, ignore_index=True)
        parts = []
        for reg in REGIMES:
            sub = merged[merged.regime == reg]
            if len(sub) < 3:
                parts.append(f'{"—":>26}')
                continue
            lo, hi = ci(sub.r)
            parts.append(f'{sub.r.mean():>8.3f} [{lo:+.2f};{hi:+.2f}]'.rjust(26))
        print(f'{name:<34}' + ''.join(parts))

    print()
    print('=' * 110)
    print('БАЗОВАЯ КОНФИГУРАЦИЯ ПОДРОБНО')
    print('=' * 110)
    for period in periods:
        stats = results.get((period['label'], 'объём x1.5 + цель на уровне'))
        if not stats:
            continue
        df = stats['rows']
        print()
        print(f'{period["label"]}: {len(df)} сделок, {stats["return_pct"]:+.1f}%, '
              f'просадка {stats["max_dd_pct"]:.1f}%')
        for side in (LONG, SHORT):
            sub = df[df.direction == side]
            if len(sub) < 5:
                continue
            lo, hi = ci(sub.r)
            print(f'   {side:<6}{len(sub):>6} сделок  винрейт {(sub.r > 0).mean() * 100:>3.0f}%  '
                  f'R/сделку {sub.r.mean():+.3f}  [{lo:+.3f}; {hi:+.3f}]')
        month = df.set_index('entry_time').resample('MS').r.agg(['count', 'sum'])
        month = month[month['count'] > 0]
        pos = (month['sum'] > 0).mean() * 100
        print(f'   прибыльных месяцев: {pos:.0f}% из {len(month)}')

    print()
    print('ПРИЁМКА: изменение принимается, только если улучшает ОБА периода.')


if __name__ == '__main__':
    main()
