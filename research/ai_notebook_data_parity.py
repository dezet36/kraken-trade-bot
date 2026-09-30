"""
Живые данные тетради против исторических (30.09.2026).

Закономерности тетради искались и проверялись на истории, скачанной
research/ai_flow_fetch.py. Вживую бот читает те же источники сам
(Live_Bot/flow_data.py) через несколько минут после закрытия часа — и видит
последний час таким, каким он был в тот момент: доля счетов в лонге, ОИ или
новый фандинг могут прийти с опозданием. Здесь — насколько сигналы живого
бота совпадают с сигналами истории на тех же часах.

Живое — bot_data/flow/<пара>.jsonl сервера: строка часа в том виде, в каком
бот получил её впервые (в момент решения). История — те же часы, скачанные
заново кодом исследования. Сравнение:
  1) сырые столбцы последнего часа (пропуски и расхождения);
  2) признаки и условия закономерностей на час решения (живой вид = история
     до часа + живая строка часа, последние 1000 ч — как в боте);
  3) сигналы alerts_at по всем закономерностям тетради (и выключенной P2 —
     для числа событий).

    scp -P 53547 root@195.133.35.5:/opt/kraken/bot_data/flow/*.jsonl <папка>
    python research/ai_notebook_data_parity.py <папка>
"""
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(os.path.dirname(HERE), 'Live_Bot')
H = 3_600_000
HISTORY_H = 1000
RAW = ('c', 'v', 'qv', 'trades', 'tb', 'buy_ratio', 'oi', 'funding')
KEYS = ('ret_4h', 'oi_chg_4h', 'ret_24h', 'rel_24h', 'btc_ret_24h', 'btc_ret_30d', 'oi_chg_24h', 'vol_z',
        'funding_bp', 'buy_ratio_pct_30d')


def live_rows(folder):
    """Пара -> строки часов в том виде, в каком бот получил их впервые."""
    out = {}
    for path in sorted(glob.glob(os.path.join(folder, '*.jsonl'))):
        rows = {}
        for line in open(path, encoding='utf-8'):
            rec = json.loads(line)
            rows.setdefault(rec['ts'], rec)            # повтор после перезапуска — не то, что видел бот
        df = pd.DataFrame(list(rows.values())).set_index('ts').sort_index()
        df.index = pd.to_datetime(df.index, unit='ms', utc=True)
        out[os.path.basename(path)[:-6]] = df.astype(float)
    return out


def history(pairs, start, cache):
    """Те же часы, скачанные кодом исследования (research/ai_flow_fetch.fetch) в свою папку."""
    os.environ['FLOW_START'] = f'{start:%Y-%m-%d %H:%M}'
    os.environ['FLOW_CACHE'] = cache
    os.environ.pop('FLOW_END', None)
    sys.argv = sys.argv[:1] + ['скачать']             # fetch() качает заново, если в argv есть пары
    import ai_flow_fetch as F
    os.makedirs(F.OUT, exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(F.fetch, pairs))
    return {p: pd.read_pickle(os.path.join(F.OUT, f'{p}.pkl')) for p in pairs}


def main(folder):
    sys.path.insert(0, LIVE)
    os.environ.setdefault('BOT_DATA_DIR', os.path.join(folder, '_bot_data'))
    import flow_features
    import llm_notebook as N

    live = live_rows(folder)
    pairs = [p for p in N.UNIVERSE if p in live]
    first = min(df.index.min() for df in live.values())
    last = max(df.index.max() for df in live.values())
    hist = history(pairs, first - pd.Timedelta(hours=HISTORY_H + 48), os.path.join(folder, '_history'))
    hist = {p: df[df.index <= last] for p, df in hist.items()}  # идущий час в истории — не закрыт

    lines = [f'Живые данные тетради против истории: {len(pairs)} пар, часы {first:%d.%m %H:%M} … '
             f'{last:%d.%m %H:%M} UTC (открытие часа)', '']

    # 1) Сырые столбцы часа решения
    lines.append('1) Строка часа в момент решения против истории (пары × часы):')
    tot = 0
    miss = {c: 0 for c in RAW}
    diff = {c: 0 for c in RAW}
    for p in pairs:
        lv, hs = live[p], hist[p]
        common = lv.index.intersection(hs.index)
        tot += len(common)
        for c in RAW:
            a, b = lv.loc[common, c], hs.loc[common, c]
            miss[c] += int((a.isna() & b.notna()).sum())
            both = a.notna() & b.notna()
            diff[c] += int((~np.isclose(a[both], b[both], rtol=1e-6, atol=1e-12)).sum())
    for c in RAW:
        lines.append(f'   {c:10s} нет вживую, есть в истории: {miss[c]:4d} ({miss[c] / max(tot, 1):5.1%}); '
                     f'расходится: {diff[c]:4d} ({diff[c] / max(tot, 1):5.1%})')
    lines.append(f'   всего строк: {tot}')

    # 2) Признаки и условия на час решения; 3) сигналы
    hours = sorted(set().union(*(live[p].index for p in pairs)))
    hist_data = N.market(hist)                       # признаки истории: окна смотрят только назад
    cond_diff = {k: 0 for k in N.PATTERNS}
    cond_on = {k: [0, 0] for k in N.PATTERNS}
    feat_err = {k: [] for k in KEYS}
    alerts_live, alerts_hist = [], []
    off = N.OFF
    N.OFF = ()                                       # для сверки — и выключенная P2: больше событий
    try:
        for t in hours:
            view = {}
            for p in pairs:
                h = hist[p][hist[p].index < t]
                row = live[p].loc[[t]] if t in live[p].index else hist[p].loc[[t]] if t in hist[p].index else None
                if row is None:
                    continue
                view[p] = pd.concat([h, row[h.columns]]).iloc[-HISTORY_H:]
            live_data = N.market(view)
            for p in view:
                fl, fh = live_data[p][1], hist_data[p][1]
                if t not in fl.index or t not in fh.index:
                    continue
                for k in KEYS:
                    a, b = fl.at[t, k], fh.at[t, k]
                    if not (np.isnan(a) and np.isnan(b)):
                        feat_err[k].append(abs(a - b) if not (np.isnan(a) or np.isnan(b)) else np.inf)
                for key, pat in N.PATTERNS.items():
                    if pat.get('universe') == 'core' and p not in N.POOL:
                        continue
                    a = bool(N._mask(fl.loc[[t]], pat['conditions'])[0])
                    b = bool(N._mask(fh.loc[[t]], pat['conditions'])[0])
                    cond_diff[key] += a != b
                    cond_on[key][0] += a
                    cond_on[key][1] += b
            al = N.alerts_at(live_data, t)
            ah = N.alerts_at({p: hist_data[p] for p in view}, t)
            alerts_live += [(t, p, k) for p, k in al]
            alerts_hist += [(t, p, k) for p, k in ah]
    finally:
        N.OFF = off

    lines += ['', '2) Признаки часа решения: живой вид против истории (|разница|; inf — есть только в одном):']
    for k in KEYS:
        e = np.array(feat_err[k])
        fin = e[np.isfinite(e)]
        lines.append(f'   {k:18s} n {len(e):5d}  медиана {np.median(fin) if len(fin) else float("nan"):.4f}  '
                     f'95% {np.quantile(fin, 0.95) if len(fin) else float("nan"):.4f}  '
                     f'макс {fin.max() if len(fin) else float("nan"):.4f}  только в одном {int(np.isinf(e).sum())}')
    lines += ['', '   условия закономерностей на час (пары × часы): расходятся / выполнены вживую / в истории']
    for key in N.PATTERNS:
        lines.append(f'   {key}: {cond_diff[key]:3d} / {cond_on[key][0]:3d} / {cond_on[key][1]:3d}'
                     + ('   (выключена)' if key in off else ''))

    sl, sh = set(alerts_live), set(alerts_hist)
    lines += ['', f'3) Сигналы (первый час серии): вживую {len(sl)}, в истории {len(sh)}, общих {len(sl & sh)}']
    for t, p, k in sorted(sl | sh):
        where = 'оба' if (t, p, k) in sl and (t, p, k) in sh else ('только вживую' if (t, p, k) in sl
                                                                  else 'только в истории')
        lines.append(f'   {t:%d.%m %H:%M} {p:14s} {k}{" (выкл.)" if k in off else ""}  {where}')

    # Насколько далеко от срабатывания P4 сейчас
    t = hours[-1]
    lines += ['', f'P4 на последнем часе ({t:%d.%m %H:%M}): фандинг ≤ −2 bp и доля лонгов ≤ 0.03 перцентиля месяца']
    rows = []
    for p in pairs:
        f = hist_data[p][1]
        if t in f.index:
            rows.append((f.at[t, 'buy_ratio_pct_30d'], f.at[t, 'funding_bp'], p))
    for pct, fb, p in sorted(rows, key=lambda x: (np.nan_to_num(x[0], nan=9), x[1]))[:6]:
        lines.append(f'   {p:14s} доля лонгов, перцентиль {pct:.2f}  фандинг {fb:+.2f} bp')

    text = '\n'.join(lines)
    print(text, flush=True)
    with open(os.path.join(HERE, 'results', 'ai_notebook_data_parity.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(os.path.abspath(sys.argv[1]))
