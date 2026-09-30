"""
Опись записей стакана, ленты и ликвидаций (30.09.2026) — годятся ли они для
исследования микроструктуры (~27.10, docs/ИИ_тетрадь_2026-09-28.md, раздел 5).

Биржа отдаёт стакан только «сейчас», поминутную ленту — на коротком окне,
поток ликвидаций — только вживую: что не записано, потеряно. Поэтому опись
делается заранее, пока пропуск ещё можно заметить и закрыть.

По каждой паре:
  стакан (book.jsonl) — с какого часа, снимков, шаг (медиана), дыры > 15 мин,
      докуда достаёт книга (reach_pct: доля снимков, покрывших 0.25/1/2%);
  лента (delta.jsonl) — с какого часа, минут с записью против минут периода
      (после перехода на поток ws), дыры > 15 мин;
  ликвидации (liquidations.jsonl) — событий в сутки; общие для всех пар тихие
      окна > 60 мин (обрыв потока, а не тихий рынок).

    scp -P 53547 "root@195.133.35.5:/opt/kraken/bot_data/positioning/{book,delta,liquidations}.jsonl" <папка>
    python research/ai_micro_inventory.py <папка>
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
MIN = 60_000


def read(path):
    return pd.DataFrame([json.loads(x) for x in open(path, encoding='utf-8')])


def gaps(ts, limit_min):
    """Дыры длиннее limit_min: [(начало, минут)]."""
    ts = np.sort(np.unique(np.asarray(ts, dtype='int64')))
    d = np.diff(ts) / MIN
    return [(pd.Timestamp(ts[i], unit='ms', tz='UTC'), float(d[i])) for i in np.where(d > limit_min)[0]]


def fmt(t):
    return f'{t:%d.%m %H:%M}'


def main(folder):
    now = None
    lines = []

    book = read(os.path.join(folder, 'book.jsonl'))
    now = int(book['ts'].max())
    lines.append(f"Опись записей микроструктуры на {fmt(pd.Timestamp(now, unit='ms', tz='UTC'))} UTC")
    lines += ['', 'СТАКАН (book.jsonl): пара, с какого часа, снимков, шаг (мин), дыр > 15 мин (минут всего), '
                  'доля снимков, достающих до 0.25% / 1% / 2%']
    for pair, g in book.groupby('pair'):
        ts = np.sort(g['ts'].to_numpy())
        step = float(np.median(np.diff(ts)) / MIN) if len(ts) > 1 else float('nan')
        gp = gaps(ts, 15)
        r = g['reach_pct']
        lines.append(f"  {pair:14s} с {fmt(pd.Timestamp(ts[0], unit='ms', tz='UTC'))}  {len(ts):5d}  шаг {step:4.1f}  "
                     f"дыр {len(gp):2d} ({sum(m for _, m in gp):5.0f})  "
                     f"{(r >= 0.25).mean():4.0%} / {(r >= 1).mean():4.0%} / {(r >= 2).mean():4.0%}")
    all_gaps = gaps(book['ts'], 15)
    lines.append(f"  общие дыры (ни одной пары) > 15 мин: {len(all_gaps)} — "
                 + ', '.join(f'{fmt(t)} {m:.0f} мин' for t, m in all_gaps[:12]))

    delta = read(os.path.join(folder, 'delta.jsonl'))
    src = delta['src'] if 'src' in delta else pd.Series(index=delta.index, dtype=object)
    lines += ['', 'ЛЕНТА (delta.jsonl): пара, с какого часа, с какого часа поток ws, минут с записью после '
                  'перехода на ws / минут периода, дыр > 15 мин после перехода (минут всего)']
    for pair, g in delta.groupby('pair'):
        ts = np.sort(g['ts'].to_numpy())
        ws = g[src.reindex(g.index) == 'ws']['ts']
        if len(ws):
            w0 = int(ws.min())
            mins = np.unique(g[g['ts'] >= w0]['ts'].to_numpy())
            span = (now - w0) / MIN + 1
            gp = gaps(mins, 15)
            lines.append(f"  {pair:14s} с {fmt(pd.Timestamp(ts[0], unit='ms', tz='UTC'))}  ws с "
                         f"{fmt(pd.Timestamp(w0, unit='ms', tz='UTC'))}  {len(mins):5d} / {span:5.0f} "
                         f"({len(mins) / span:4.0%})  дыр {len(gp):2d} ({sum(m for _, m in gp):5.0f})")
        else:
            lines.append(f"  {pair:14s} с {fmt(pd.Timestamp(ts[0], unit='ms', tz='UTC'))}  потока ws нет")

    liq = read(os.path.join(folder, 'liquidations.jsonl'))
    t0, t1 = int(liq['ts'].min()), int(liq['ts'].max())
    days = (t1 - t0) / (24 * 60 * MIN)
    lines += ['', f"ЛИКВИДАЦИИ (liquidations.jsonl): {len(liq)} событий, {fmt(pd.Timestamp(t0, unit='ms', tz='UTC'))} … "
                  f"{fmt(pd.Timestamp(t1, unit='ms', tz='UTC'))} ({days:.1f} сут); в сутки по парам "
                  f"(лонгов / шортов), с первого события пары:"]
    for pair, g in liq.groupby('pair'):
        span = max((t1 - int(g['ts'].min())) / (24 * 60 * MIN), 0.5)
        lines.append(f"  {pair:14s} с {fmt(pd.Timestamp(int(g['ts'].min()), unit='ms', tz='UTC'))}  "
                     f"{(g['side'] == 'long').sum() / span:6.1f} / {(g['side'] == 'short').sum() / span:6.1f}")
    quiet = gaps(liq['ts'], 60)
    lines.append(f"  тихие окна всего потока > 60 мин: {len(quiet)} — "
                 + ', '.join(f'{fmt(t)} {m:.0f} мин' for t, m in quiet[:15]))

    text = '\n'.join(lines)
    print(text, flush=True)
    with open(os.path.join(HERE, 'results', 'ai_micro_inventory.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(os.path.abspath(sys.argv[1]))
