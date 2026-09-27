"""
Импульс между монетами на ШИРОКОМ наборе: все бессрочные контракты Bybit к
USDT, отбор по обороту на каждую дату, фактический фандинг за удержание.

ЗАЧЕМ. На 20 парах кэшей (research/ai_momentum_grid.py) «купить монеты с
лучшим ходом за 7–30 дней, продать худшие, держать неделю» — в плюсе после
издержек во всех четырёх режимах рынка, плато по настройкам. Эти 20 пар —
те же, на которых смотрелось всё остальное, так что подгонка к ним возможна.
Здесь — другие монеты: в каждую дату перестановки берутся N контрактов с
наибольшим оборотом за прошлые 30 дней (только прошлое), так что набор
меняется со временем, как менялся бы у бота.

Набор: по умолчанию только контракты старше года (дата листинга — первая
дневная свеча Bybit): новые листинги 2025–2026 ходят на +150…+530% и
−70…−92% за день, и книга шортила бы самые разогнанные. Для сравнения
сетка считается и без этого фильтра.

Механика: перестановка в 00:00 UTC по закрытию прошлого дня; лонг k лучших
по ходу за L дней, шорт k худших; удержание H дней; вход и выход по закрытию
дня. Издержки: «полные» — 0.21% круга на позицию каждый период; «оборот» —
0.105% за вход и за выход, оставшиеся в книге не платят. Фандинг — сумма
фактических ставок за дни удержания (лонг платит положительный, шорт
получает).

Риск шорта. Слабую монету могут разогнать в разы за неделю (на частичных
данных книга теряла −49% на позицию за неделю). Поэтому варианты:
    размер «равный» — равный номинал; «по волатильности» — номинал обратно
        30-дневной волатильности монеты (в среднем по книге тот же);
    стоп m·σ — выход, если цена ушла против позиции на m недельных
        колебаний монеты (σ дня × √H); проверка по дневным максимумам и
        минимумам, исполнение по уровню стопа хуже на 0.1%, выход платит
        половину круга сразу, возврат в книгу — новый вход.

Портфельные числа — книга с валовым номиналом, равным капиталу (половина в
лонгах, половина в шортах): доходность периода = средняя позиция. Кривая
строится по одному дню старта, итог усредняется по всем дням.

Оговорка о выживших: в наборе только ныне торгуемые контракты.

Запуск (после research/fetch_universe.py):
    python research/ai_momentum_universe.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import ci                                 # noqa: E402

CACHE = os.path.join(HERE, 'universe_cache')
FULL_RT = 0.21
HALF_RT = 0.105
STOP_SLIP = 0.1
PERIODS = [('bear', '2022-01-01', '2023-06-30'), ('mid1', '2023-07-01', '2024-06-30'),
           ('mid2', '2024-07-01', '2025-04-30'), ('recent', '2025-05-01', '2026-12-31')]
EXCLUDE = {'USDCUSDT', 'USDEUSDT', 'FDUSDUSDT', 'DAIUSDT', 'TUSDUSDT', 'BUSDUSDT', 'USTCUSDT',
           # не криптовалюта: золото, серебро, акции и фонды (листинги Bybit 2022–2026)
           'PAXGUSDT', 'XAUTUSDT', 'XAUUSDT', 'XAGUSDT', 'AAPLUSDT', 'AMZNUSDT', 'COINUSDT', 'CRCLUSDT',
           'GOOGLUSDT', 'HOODUSDT', 'METAUSDT', 'MSFTUSDT', 'MSTRUSDT', 'NVDAUSDT', 'QQQUSDT', 'SPYUSDT',
           'TSLAUSDT', 'SPXLUSDT', 'GDXUSDT', 'NFLXUSDT', 'LRCXUSDT'}


def load():
    cols = {'close': {}, 'high': {}, 'low': {}, 'turnover': {}}
    funding = {}
    for name in sorted(os.listdir(os.path.join(CACHE, 'candles'))):
        pair = name[:-7]
        if pair in EXCLUDE or not name.endswith('_1d.csv'):
            continue
        f = pd.read_csv(os.path.join(CACHE, 'candles', name))
        idx = pd.to_datetime(f['timestamp'], utc=True).dt.floor('D')
        for c in cols:
            cols[c][pair] = pd.Series(f[c].to_numpy(dtype=float), index=idx)
        path = os.path.join(CACHE, 'funding', f'{pair}.csv')
        if os.path.exists(path):
            fr = pd.read_csv(path)
            ts = pd.to_datetime(fr['timestamp'], utc=True).dt.floor('D')
            funding[pair] = pd.Series(fr['funding_rate'].to_numpy(dtype=float), index=ts).groupby(level=0).sum()
    close = pd.DataFrame(cols['close']).sort_index()
    days = pd.date_range(close.index.min(), close.index.max(), freq='D', tz='UTC')
    frames = {c: pd.DataFrame(v).reindex(index=days, columns=close.columns) for c, v in cols.items()}
    frames['funding'] = pd.DataFrame(funding).reindex(index=days, columns=close.columns)
    return frames


class Context:
    """Всё, что считается один раз на набор: массивы numpy и кэш сигналов."""

    def __init__(self, frames):
        self.days = frames['close'].index
        self.pairs = np.array(frames['close'].columns)
        self.close = frames['close'].to_numpy()
        self.high = frames['high'].to_numpy()
        self.low = frames['low'].to_numpy()
        self.liq = frames['turnover'].rolling(30, min_periods=20).mean().to_numpy()
        # Оборот за полгода: короткий всплеск мемкоина не поднимает его в набор
        # «крупных» (разгоны SIREN +414% и PIPPIN +271% за неделю в шорте).
        self.liq180 = frames['turnover'].rolling(180, min_periods=120).mean().to_numpy()
        logret = np.log(frames['close']).diff()
        self.sigma = logret.rolling(30, min_periods=20).std().to_numpy()
        self.fund = frames['funding'].fillna(0.0)
        self.has_fund = frames['funding'].notna().any().to_numpy()
        # Возраст контракта в днях на каждую дату — известен заранее; новые
        # листинги 2025–2026 давали дневные ходы +150…+530% и −70…−92%.
        # Дата листинга — первая дневная свеча Bybit (fetch_universe.py
        # listing): свечи качаются с 2021-06-01, и по ним всё старое выглядело
        # бы листингом этого дня.
        path = os.path.join(CACHE, 'listing.csv')
        if os.path.exists(path):
            first = pd.read_csv(path).set_index('pair')['first_day']
            start = pd.to_datetime(first.reindex(self.pairs).fillna(self.days[0].date().isoformat()), utc=True)
            day_n = (self.days - self.days[0]).days.to_numpy()[:, None]
            start_n = (start - self.days[0]).dt.days.to_numpy()[None, :]
            self.age = (day_n - start_n).astype(float)
        else:
            self.age = np.cumsum(~np.isnan(self.close), axis=0).astype(float)
        self.period = np.array([period_of(d) for d in self.days], dtype=object)
        self._mom, self._ahead, self._fsum = {}, {}, {}

    def mom(self, lookback):
        if lookback not in self._mom:
            c = self.close
            self._mom[lookback] = np.full_like(c, np.nan)
            self._mom[lookback][lookback:] = c[lookback:] / c[:-lookback] - 1
        return self._mom[lookback]

    def ahead(self, hold):
        if hold not in self._ahead:
            c = self.close
            self._ahead[hold] = np.full_like(c, np.nan)
            self._ahead[hold][:-hold] = c[hold:] / c[:-hold] - 1
        return self._ahead[hold]

    def fsum(self, hold):
        """Фандинг, выплаченный в дни D..D+hold−1, для решения по строке D−1."""
        if hold not in self._fsum:
            s = self.fund[::-1].rolling(hold, min_periods=1).sum()[::-1].shift(-1)
            self._fsum[hold] = s.to_numpy()
        return self._fsum[hold]


def period_of(day):
    for name, a, b in PERIODS:
        if pd.Timestamp(a, tz='UTC') <= day <= pd.Timestamp(b, tz='UTC'):
            return name
    return None


def books(ctx, n_top, lookback, hold, k, offset, sizing='equal', stop_m=None, min_age=365, liq_days=30):
    """
    Книги одного дня старта. Строка дня D — свеча, открытая в D 00:00; к 00:00
    дня D известно закрытие свечи D−1. Решение в 00:00 дня D: сигнал по
    закрытиям до D−1, вход по закрытию D−1, выход по закрытию D−1+hold или
    по стопу внутри свечей D..D+hold−1.
    """
    mom, ahead, fsum = ctx.mom(lookback), ctx.ahead(hold), ctx.fsum(hold)
    liq = ctx.liq180 if liq_days == 180 else ctx.liq
    out, prev = [], {}
    for i in range(max(lookback, 30) + 1, len(ctx.days) - hold - 1):
        if (i - offset) % hold or ctx.period[i] is None:
            continue
        row = i - 1
        ok = ~np.isnan(liq[row]) & ~np.isnan(mom[row]) & ~np.isnan(ahead[row]) & ~np.isnan(ctx.sigma[row])
        ok &= ctx.age[row] >= min_age
        cand = np.flatnonzero(ok)
        if len(cand) < max(10, 2 * k):
            prev = {}
            continue
        pool = cand[np.argsort(-liq[row, cand])][:n_top]
        ranked = pool[np.argsort(mom[row, pool])]
        book = [(c, -1) for c in ranked[:k]] + [(c, +1) for c in ranked[-k:]]
        weights = np.ones(len(book))
        if sizing == 'vol':
            inv = np.array([1.0 / max(ctx.sigma[row, c], 1e-4) for c, _ in book])
            weights = inv / inv.mean()
        pnl, stopped, legs = [], set(), {+1: [], -1: []}
        for (c, side), w in zip(book, weights):
            entry = ctx.close[row, c]
            ret, held = side * ahead[row, c], hold
            if stop_m:
                x = stop_m * ctx.sigma[row, c] * np.sqrt(hold)
                level = entry * (1 - side * x)
                for d in range(i, i + hold):
                    hit = ctx.low[d, c] <= level if side > 0 else ctx.high[d, c] >= level
                    if hit:
                        ret, held = -x - STOP_SLIP / 100, d - i + 1
                        stopped.add((c, side))
                        break
            fnd = -side * fsum[row, c] * held / hold if ctx.has_fund[c] else 0.0
            p = (ret + fnd) * 100
            pnl.append(w * p)
            legs[side].append(p)
        n = len(book)
        gross = float(np.sum(pnl) / n)
        now = dict(book)
        changed = sum(1 for c, s in book if prev.get(c) != s) + sum(1 for c, s in prev.items() if now.get(c) != s)
        turn_cost = HALF_RT * (changed + len(stopped)) / n
        market = float(np.nanmean(ahead[row, pool]) * 100)
        out.append((ctx.days[i], ctx.period[i], gross - FULL_RT, gross - turn_cost,
                    float(np.mean(legs[+1])), float(np.mean(legs[-1])), market, len(stopped) / n))
        prev = {c: s for c, s in book if (c, s) not in stopped}
    return pd.DataFrame(out, columns=['day', 'period', 'full', 'turn', 'long', 'short', 'market', 'stopped'])


def curve_stats(frame, hold):
    r = frame['turn'].to_numpy() / 100
    if len(r) < 5:
        return None
    eq = np.cumprod(1 + r)
    peak = np.maximum.accumulate(eq)
    dd = (1 - eq / peak).max() * 100
    years = len(r) * hold / 365.0
    cagr = (eq[-1] ** (1 / years) - 1) * 100 if eq[-1] > 0 else -100.0
    sharpe = r.mean() / r.std() * np.sqrt(365.0 / hold) if r.std() > 0 else np.nan
    return cagr, dd, r.min() * 100, sharpe


def per_period(frames, column='turn'):
    return {p: np.mean([f[f.period == p][column].mean() for f in frames if (f.period == p).any()])
            for p, _, _ in PERIODS}


def main():
    frames = load()
    ctx = Context(frames)
    print(f'контрактов {len(ctx.pairs)}, дней {len(ctx.days)} ({ctx.days[0].date()} … {ctx.days[-1].date()}); '
          f'с фандингом {int(ctx.has_fund.sum())}')
    print('% на позицию за период; «оборот» — издержки только за вход/выход; периоды: bear | mid1 | mid2 | recent')
    sections = ((7, 365, 30, 'старше года, оборот за 30 дней'),
                (7, 365, 180, 'старше года, оборот за 180 дней («крупные»)'),
                (7, 180, 180, 'старше полугода, оборот за 180 дней'),
                (7, 0, 30, 'все контракты, включая новые листинги, оборот за 30 дней'),
                (14, 365, 180, 'старше года, оборот за 180 дней'))
    for hold, min_age, liq_days, who in sections:
        print(f'\n== УДЕРЖАНИЕ {hold} дн, {who}; равный номинал, без стопа (среднее по {hold} дням старта)')
        print(f'   {"N":>4s} {"окно":>5s} {"k":>3s}  {"полные":>8s}  {"оборот":>8s}  {"разброс дней":>18s}   '
              f'по периодам (оборот)                     в плюс   в год  просадка')
        for n_top in (10, 20, 30, 50):
            for lookback in (7, 14, 21, 30):
                for k in (3, 5):
                    if 2 * k > n_top:
                        continue
                    fr = [books(ctx, n_top, lookback, hold, k, off, min_age=min_age, liq_days=liq_days)
                          for off in range(hold)]
                    full = np.mean([f['full'].mean() for f in fr])
                    per_off = [f['turn'].mean() for f in fr]
                    per = per_period(fr)
                    pos = sum(1 for v in per.values() if v > 0)
                    stats = [s for f in fr if (s := curve_stats(f, hold))]
                    print(f'   {n_top:4d} {lookback:4d}д {k:3d}  {full:+8.3f}  {np.mean(per_off):+8.3f}  '
                          f'{min(per_off):+7.3f} … {max(per_off):+7.3f}   '
                          + ' | '.join(f'{per[p]:+.3f}' for p, _, _ in PERIODS) + f'   {pos} из 4  '
                          f'{np.mean([s[0] for s in stats]):+6.1f}%  {np.mean([s[1] for s in stats]):4.0f}%',
                          flush=True)
    print('\nРИСК: размер и стоп (оборот). В год, наибольшая просадка, худший период, Шарп — среднее по дням '
          'старта; ноги — до издержек')
    variants = (('равный, без стопа', 'equal', None), ('по волатильности', 'vol', None),
                ('равный, стоп 2σ', 'equal', 2.0), ('равный, стоп 3σ', 'equal', 3.0),
                ('по волатильности, стоп 3σ', 'vol', 3.0))
    for n_top, lookback, hold, k, liq_days in ((20, 14, 7, 3, 180), (20, 30, 7, 3, 180), (30, 14, 7, 3, 180),
                                               (20, 14, 7, 3, 30), (30, 14, 7, 3, 30), (50, 21, 7, 5, 30)):
        print(f'\n  N={n_top} (оборот за {liq_days} дн, старше года) окно {lookback} д, удержание {hold} д, k={k}')
        for name, sizing, stop_m in variants:
            fr = [books(ctx, n_top, lookback, hold, k, off, sizing, stop_m, liq_days=liq_days)
                  for off in range(hold)]
            stats = [s for f in fr if (s := curve_stats(f, hold))]
            cagr, dd, worst, sharpe = (np.mean([s[j] for s in stats]) for j in range(4))
            per = per_period(fr)
            pos = sum(1 for v in per.values() if v > 0)
            first = fr[0]['turn'].to_numpy()
            lo, hi = ci(first)
            both = pd.concat(fr)
            corr = np.corrcoef(both['turn'], both['market'])[0, 1]
            print(f'   {name:26s} {both["turn"].mean():+.3f}% [день 0: {lo:+.3f}; {hi:+.3f}]  '
                  + ' | '.join(f'{per[p]:+.3f}' for p, _, _ in PERIODS) + f' ({pos} из 4)  '
                  f'в год {cagr:+6.1f}%  просадка {dd:5.1f}%  худший {worst:+6.2f}%  Шарп {sharpe:4.2f}  '
                  f'лонг {both["long"].mean():+.2f} шорт {both["short"].mean():+.2f}  связь с рынком {corr:+.2f}'
                  + (f'  стопов {both["stopped"].mean() * 100:.0f}%' if stop_m else ''), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
