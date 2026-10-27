"""
Сверка переноса LuxAlgo SMC с TradingView: свежие свечи Bybit, разметка
lux.build и картинка в том виде, в каком индикатор рисует её с настройками по
умолчанию (внутренняя и свинговая структура, 5 последних живых внутренних
блоков; свинговые блоки, равные вершины, FVG по умолчанию выключены или не
перенесены).

    python research/luxsmc/chart_check.py BTCUSDT 60 [bars_on_chart]
    → results/luxsmc/check_<пара>_<ТФ>.png и .txt

На TradingView: BYBIT:<пара>.P, тот же ТФ, индикатор «Smart Money Concepts
[LuxAlgo]» с настройками по умолчанию, время графика UTC.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lux                                     # noqa: E402

OUT = os.path.join(os.path.dirname(HERE), 'results', 'luxsmc')
API = 'https://api.bybit.com/v5/market/kline?category=linear&symbol={s}&interval={i}&limit=1000&end={e}'


def fetch(symbol, interval, total=4000):
    """Закрытые свечи Bybit linear, от старых к новым."""
    rows, end = [], int(time.time() * 1000)
    while len(rows) < total:
        with urllib.request.urlopen(API.format(s=symbol, i=interval, e=end), timeout=20) as r:
            data = json.load(r)['result']['list']
        if not data:
            break
        rows += data
        end = int(data[-1][0]) - 1
    df = pd.DataFrame(rows, columns=['t', 'o', 'h', 'l', 'c', 'v', 'turnover']).astype(float)
    df = df.drop_duplicates('t').sort_values('t').reset_index(drop=True)
    step = int(interval) * 60_000
    df = df[df.t + step <= time.time() * 1000]           # последняя незакрытая — не берём
    df['time'] = pd.to_datetime(df.t, unit='ms', utc=True)
    return df.reset_index(drop=True)


def alive_blocks(ev, h, l, scope, keep=5):
    """Живые блоки к последней свече: снимаются, когда high выше верха
    медвежьего / low ниже низа бычьего (Order Block Mitigation: High/Low)."""
    out = []
    for e in ev[ev.scope == scope].itertuples():
        after = slice(e.i, len(h))                     # снятие проверяется уже на свече слома
        if e.side == 1:
            dead = (l[after] < e.ob_bar_low).any()
        else:
            dead = (h[after] > e.ob_bar_high).any()
        if not dead:
            out.append(e)
    return out[::-1][:keep]                            # новые первыми, как в скрипте


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else 'BTCUSDT'
    interval = sys.argv[2] if len(sys.argv) > 2 else '60'
    show = int(sys.argv[3]) if len(sys.argv) > 3 else 160
    df = fetch(symbol, interval)
    h, l, c, o = (df[k].to_numpy() for k in ('h', 'l', 'c', 'o'))
    ev = pd.DataFrame(lux.build(h, l, c))
    first = len(df) - show
    iob = alive_blocks(ev, h, l, 'internal')

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    fig, ax = plt.subplots(figsize=(18, 9))
    x = np.arange(first, len(df))
    up = c[first:] >= o[first:]
    ax.vlines(x, l[first:], h[first:], color=np.where(up, '#089981', '#F23645'), linewidth=0.8)
    ax.bar(x, np.abs(c[first:] - o[first:]) + 1e-9, bottom=np.minimum(o[first:], c[first:]),
           color=np.where(up, '#089981', '#F23645'), width=0.7)
    for e in iob:
        color = '#3179f5' if e.side == 1 else '#f77c80'
        x0 = max(e.ob_i, first)
        ax.add_patch(Rectangle((x0 - 0.5, min(e.ob_bar_low, e.ob_bar_high)), len(df) - x0,
                               abs(e.ob_bar_high - e.ob_bar_low), color=color, alpha=0.25, lw=0))
    lines = []
    for e in ev[ev.i >= first].itertuples():
        internal = e.scope == 'internal'
        color = '#089981' if e.side == 1 else '#F23645'
        ax.hlines(e.level, max(e.pivot_i, first), e.i, colors=color,
                  linestyles='dashed' if internal else 'solid', linewidth=0.9 if internal else 1.4)
        ax.text((max(e.pivot_i, first) + e.i) / 2, e.level, e.tag, color=color,
                fontsize=7 if internal else 9, ha='center',
                va='bottom' if e.side == 1 else 'top', fontweight='normal' if internal else 'bold')
        lines.append(f"{df.time[e.i]:%Y-%m-%d %H:%M} UTC  {'внутр.' if internal else 'СВИНГ '} "
                     f"{'бычий' if e.side == 1 else 'медвежий':8s} {e.tag:5s} уровень {e.level:.2f}  "
                     f"(точка {df.time[e.pivot_i]:%m-%d %H:%M})  блок {df.time[e.ob_i]:%m-%d %H:%M} "
                     f"{min(e.ob_bar_low, e.ob_bar_high):.2f}–{max(e.ob_bar_low, e.ob_bar_high):.2f}")
    ticks = x[::max(1, show // 16)]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f'{df.time[i]:%m-%d %H:%M}' for i in ticks], rotation=30, fontsize=8)
    ax.set_xlim(first - 1, len(df) + 3)
    lo_, hi_ = l[first:].min(), h[first:].max()
    ax.set_ylim(lo_ - (hi_ - lo_) * 0.03, hi_ + (hi_ - lo_) * 0.05)
    ax.grid(alpha=0.2)
    ax.set_title(f'Перенос LuxAlgo SMC · BYBIT:{symbol}.P · {interval} мин · UTC · '
                 f'пунктир — внутренняя структура, сплошная — свинговая, блоки — 5 живых внутренних')
    tf = {'60': '1h', '240': '4h', '15': '15m'}.get(interval, interval)
    png = os.path.join(OUT, f'check_{symbol}_{tf}.png')
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    blocks = [f"  {'бычий' if e.side == 1 else 'медвежий':8s} свеча {df.time[e.ob_i]:%Y-%m-%d %H:%M} UTC  "
              f"{min(e.ob_bar_low, e.ob_bar_high):.2f}–{max(e.ob_bar_low, e.ob_bar_high):.2f}" for e in iob]
    text = '\n'.join([f'BYBIT:{symbol}.P {tf}, свечей в расчёте {len(df)}, последняя закрытая '
                      f'{df.time.iloc[-1]:%Y-%m-%d %H:%M} UTC', '', 'сломы на картинке:', *lines, '',
                      'живые внутренние блоки (как рисует индикатор):', *blocks])
    with open(png[:-4] + '.txt', 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')
    print(text)
    print(png)


if __name__ == '__main__':
    main()
