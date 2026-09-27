"""
Аудит живой ИИ-ветки по данным сервера (27.09.2026, docs/ИИ_аудит_2026-09-27.md).

    python research/ai_live_audit.py <папка с данными сервера>

В папке: llm_calls.csv, paper_trades.jsonl, llm_outcomes_state.json (копии
из /opt/kraken/bot_data). Часовые свечи Bybit докачиваются в ту же папку
(px1h.pkl). Что меряется:
  1. итог сделок ИИ и интервал среднего;
  2. сторона и скопление однонаправленных позиций; что дал бы предел стороны;
  3. путь цены (MFE) и варианты сопровождения на тех же сделках;
  4. рынок вокруг входа: погоня за ходом суток, против 4-дневного хода,
     что было после стопа;
  5. навык направления: bias каждого разбора против хода пары через 4 и 24 ч;
  6. воронка разборов и соблюдение промта (p, условие now, встречный план);
  7. ворота «ожидание ≤ 0»: доля целей по касаниям трекера против сделок.
"""
import json
import os
import pickle
import random
import sys

import pandas as pd


def load(folder):
    rows = [json.loads(line) for line in open(os.path.join(folder, 'paper_trades.jsonl'), encoding='utf-8')
            if line.strip()]
    t = pd.DataFrame([r for r in rows if r.get('strategy') == 'LLM'])
    t['open'] = pd.to_datetime(t['open_time'], utc=True)
    t['close'] = pd.to_datetime(t['close_time'], utc=True)
    t['stop_pct'] = (t['entry_price'] - t['stop_loss']).abs() / t['entry_price'] * 100
    t = t.sort_values('open').reset_index(drop=True)
    c = pd.read_csv(os.path.join(folder, 'llm_calls.csv'), encoding='utf-8', low_memory=False)
    c['at'] = pd.to_datetime(c['at'], utc=True)
    state = json.load(open(os.path.join(folder, 'llm_outcomes_state.json'), encoding='utf-8'))
    return t, c, state


def candles(folder, pairs, since='2026-09-15T00:00:00Z'):
    """Часовые свечи Bybit по парам; кэш — px1h.pkl в папке данных."""
    path = os.path.join(folder, 'px1h.pkl')
    px = {}
    if os.path.exists(path):
        px = {k: (v.set_index('t') if 't' in v.columns else v) for k, v in pickle.load(open(path, 'rb')).items()}
    need = sorted(set(pairs) - set(px))
    if need:
        import ccxt
        ex = ccxt.bybit({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        for p in need:
            got, start = [], ex.parse8601(since)
            while True:
                b = ex.fetch_ohlcv(p.replace('USDT', '/USDT:USDT'), '1h', since=start, limit=1000)
                got += b
                if len(b) < 1000:
                    break
                start = b[-1][0] + 1
            df = pd.DataFrame(got, columns=['ts', 'o', 'h', 'l', 'c', 'v']).drop_duplicates('ts')
            df['t'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
            px[p] = df.set_index('t')
        pickle.dump({k: v.reset_index() for k, v in px.items()}, open(path, 'wb'))
    return px


def close_before(px, pair, ts):
    """Закрытие последней часовой свечи, закрытой к моменту ts."""
    d = px[pair]['c']
    d = d[d.index <= ts.floor('h') - pd.Timedelta(hours=1)]
    return d.iloc[-1] if len(d) else None


def result(t):
    print('=== 1. ИТОГ ===')
    n, r = len(t), t['pnl_r']
    random.seed(1)
    boots = sorted(sum(random.choice(list(r)) for _ in range(n)) / n for _ in range(20000))
    print(f'сделок {n}, сумма {r.sum():+.2f}R, среднее {r.mean():+.3f}R [{boots[500]:+.3f}; {boots[19500]:+.3f}], '
          f'медиана {r.median():+.2f}R')
    print('исходы:', t['exit_reason'].value_counts().to_dict())
    print(f"$ {t['pnl_usd'].sum():+.2f} (грязными {t['gross_pnl_usd'].sum():+.2f}, комиссии {t['fees_usd'].sum():.2f}); "
          f"издержки {t['cost_share_pct'].mean():.2f}% риска = {t['cost_share_pct'].sum() / 100:.2f}R на все сделки")
    print(f"стоп медиана {t['stop_pct'].median():.2f}%, R:R первой цели медиана {t['rr'].median():.2f}")


def sides_and_cluster(t):
    print('\n=== 2. СТОРОНА И СКОПЛЕНИЕ ===')
    for side, g in t.groupby('direction'):
        print(f"{side:5s} сделок {len(g):2d}: цель {int(g['exit_reason'].str.startswith('TP').sum())}, "
              f"БУ {int((g['exit_reason'] == 'BE').sum())}, стоп {int((g['exit_reason'] == 'SL').sum())}, "
              f"{g['pnl_r'].sum():+.2f}R")
    peak = 0
    for _, r in t.iterrows():
        same = t[(t['open'] < r['open']) & (t['close'] > r['open']) & (t['direction'] == r['direction'])]
        peak = max(peak, len(same) + 1)
    print('наибольшее число открытых позиций ИИ в одну сторону:', peak)
    for cap in (0, 3, 2):
        taken = []
        for _, r in t.iterrows():
            same = [x for x in taken
                    if x['direction'] == r['direction'] and x['open'] < r['open'] and x['close'] > r['open']]
            if cap and len(same) >= cap:
                continue
            taken.append(r)
        k = pd.DataFrame(taken)
        eq = k['pnl_r'].cumsum()
        print(f"предел стороны {cap or 'нет'}: сделок {len(k)}, {k['pnl_r'].sum():+.2f}R "
              f"({k['pnl_r'].mean():+.3f}R/сд), худшая просадка {(eq - eq.cummax()).min():.2f}R")


def path_variants(t):
    print('\n=== 3. ПУТЬ ЦЕНЫ И СОПРОВОЖДЕНИЕ ===')
    n = len(t)
    for thr in (1.0, 2.0):
        print(f"MFE ≥ {thr}R: {int((t['mfe_r'] >= thr).sum())} из {n}")
    sl = t[t['exit_reason'] == 'SL']
    print(f"стопов {len(sl)}, из них до стопа были в +1R и выше: {int((sl['mfe_r'] >= 1).sum())}; "
          f"безубыток стоял у сделок {list(t.loc[t['breakeven_set'] == True, 'trade_id'])}")
    cost = t['cost_share_pct'] / 100
    be = t['pnl_r'].where(~((t['exit_reason'] == 'SL') & (t['mfe_r'] >= 1.0)), -cost)
    print(f"безубыток при +1R у всех: {be.sum():+.2f}R ({be.mean():+.3f}R/сд)")
    for k in (1.0, 2.0):
        alt = [(k - cs) if m >= k else (-1 - cs) for m, cs in zip(t['mfe_r'], cost)]
        print(f"одна цель {k}R: {sum(alt):+.2f}R ({sum(alt) / n:+.3f}R/сд)")


def market_context(t, px):
    print('\n=== 4. РЫНОК ВОКРУГ ВХОДА ===')
    rows = []
    for _, r in t.iterrows():
        sgn = 1 if r['direction'] == 'LONG' else -1
        e, p = r['open'], r['pair']
        c0 = close_before(px, p, e)
        c24 = close_before(px, p, e - pd.Timedelta(hours=24))
        c96 = close_before(px, p, e - pd.Timedelta(hours=96))
        row = dict(R=r['pnl_r'], move24=(c0 / c24 - 1) * 100 * sgn, move96=(c0 / c96 - 1) * 100 * sgn)
        if r['exit_reason'] == 'SL':
            # Только свечи ПОСЛЕ часа стопа: в самом часе смешаны ход до и после.
            after = px[p][px[p].index > r['close'].floor('h')]
            post = after[after.index <= r['close'] + pd.Timedelta(hours=12)]
            day = after[after.index <= r['close'] + pd.Timedelta(hours=24)]
            risk = abs(r['entry_price'] - r['stop_loss'])
            row['beyond'] = ((post['h'].max() - r['stop_loss']) if sgn < 0 else (r['stop_loss'] - post['l'].min())) / risk
            row['back'] = bool(((r['entry_price'] - post['l'].min()) if sgn < 0
                                else (post['h'].max() - r['entry_price'])) >= 0)
            tp1 = pd.to_numeric(r.get('tp1'), errors='coerce')
            row['tp_after'] = bool(pd.notna(tp1) and ((day['l'].min() <= tp1) if sgn < 0 else (day['h'].max() >= tp1)))
        rows.append(row)
    d = pd.DataFrame(rows)
    chase = d['move24'] > 0
    print(f"вход по ходу последних суток: {int(chase.sum())} из {len(d)} ({d.loc[chase, 'R'].sum():+.2f}R); "
          f"против: {int((~chase).sum())} ({d.loc[~chase, 'R'].sum():+.2f}R)")
    against = d['move96'] < 0
    print(f"вход против хода 4 суток: {int(against.sum())} из {len(d)} ({d.loc[against, 'R'].sum():+.2f}R)")
    s = d[d['beyond'].notna()]
    print(f"после стопа (со следующего часа), 12 ч: дальше стопа на {s['beyond'].median():.2f}R (медиана); "
          f"возврат к входу у {int(s['back'].astype(bool).sum())} из {len(s)}; "
          f"первая цель плана за 24 ч — у {int(s['tp_after'].astype(bool).sum())} из {len(s)}")
    daily = px['BTCUSDT']['c'].resample('1D').last().pct_change() * 100
    print('BTC по суткам, %:', ', '.join(f"{i:%d.%m} {v:+.1f}" for i, v in daily.loc['2026-09-18':].items()))


def bias_skill(c, px):
    print('\n=== 5. НАВЫК НАПРАВЛЕНИЯ (bias разбора) ===')
    rows = []
    for _, r in c[c['bias'].isin(['up', 'down'])].iterrows():
        a, p = r['at'].floor('h'), r['pair']
        if p not in px or a + pd.Timedelta(hours=24) > px[p].index.max():
            continue
        cl = px[p]['c']
        c0, c4, c24 = (cl[cl.index <= a + pd.Timedelta(hours=h)].iloc[-1] for h in (0, 4, 24))
        past = cl[cl.index <= a - pd.Timedelta(hours=24)].iloc[-1]
        s = 1 if r['bias'] == 'up' else -1
        rows.append(dict(bias=r['bias'], f4=(c4 / c0 - 1) * s, f24=(c24 / c0 - 1) * 100 * s,
                         past=(c0 / past - 1) * s, raw24=(c24 / c0 - 1) * 100))
    d = pd.DataFrame(rows)
    print(f"разборов {len(d)}: bias верен на 4 ч в {(d['f4'] > 0).mean() * 100:.0f}%, на 24 ч в {(d['f24'] > 0).mean() * 100:.0f}% "
          f"(up {(d[d.bias == 'up']['f24'] > 0).mean() * 100:.0f}% из {int((d.bias == 'up').sum())}, "
          f"down {(d[d.bias == 'down']['f24'] > 0).mean() * 100:.0f}% из {int((d.bias == 'down').sum())}); "
          f"ход по bias за 24 ч {d['f24'].mean():+.2f}%")
    print(f"«всегда вверх» на тех же моментах: верно {(d['raw24'] > 0).mean() * 100:.0f}%, ход {d['raw24'].mean():+.2f}%")
    print(f"bias совпадает с ходом прошлых суток в {(d['past'] > 0).mean() * 100:.0f}% разборов")


def funnel(c):
    print('\n=== 6. ВОРОНКА И СОБЛЮДЕНИЕ ПРОМТА ===')
    m = c[c['model'].notna()]
    sec = pd.to_numeric(m['seconds'], errors='coerce')
    hours = (c['at'].max() - c['at'].min()).total_seconds() / 3600
    print(f"разборов {len(c)}, с вызовом модели {len(m)} (медиана {sec.median() / 60:.1f} мин, "
          f"{sec.sum() / 3600:.0f} ч модели за {hours:.0f} ч)")
    text = c['raw'].fillna('') + ' ' + c['rejected_raw'].fillna('')
    said_enter = text.str.contains(r'"d"\s*:\s*"enter"', regex=True)
    print(f"модель предложила план: {int(said_enter.sum())}, код принял: {int((c['decision'] == 'enter').sum())}")
    late = c['at'] >= '2026-09-23 06:19'
    print(f"  после 23.09 06:19: предложено {int((said_enter & late).sum())}, принято "
          f"{int(((c['decision'] == 'enter') & late).sum())}")
    print('отказы:', c[c['decision'] == 'skip']['gate'].value_counts().head(12).to_dict())
    ent = c[c['decision'] == 'enter']
    print('p модели у принятых:', pd.to_numeric(ent['p'], errors='coerce').value_counts().sort_index().to_dict())
    when = text[said_enter & late].str.extract(r'"when"\s*:\s*"(\w+)"')[0].value_counts()
    print('условие входа в планах после 23.09 (промт: по умолчанию now):', when.to_dict())
    alt = ent['alt'].fillna('')
    print(f"встречный план пустой или обрывок у {int((alt.str.len() < 12).sum())} из {len(ent)} принятых "
          f"(пример: {alt[alt.str.len() < 12].head(1).tolist()})")


def ev_gate(state, t):
    print('\n=== 7. ВОРОТА «ОЖИДАНИЕ ≤ 0» ===')
    since = [w for w in state if (w.get('at') or '') >= '2026-09-23' and not w.get('gate') and w.get('side')]

    def tp_first(w):
        tp, sl = w.get('tp_hours'), w.get('sl_hours')
        return str(w.get('hit_tp1')) in ('1', 'True') and (sl in (None, '') or (tp not in (None, '') and float(tp) <= float(sl)))
    tpf = sum(tp_first(w) for w in since)
    slf = sum(1 for w in since if w.get('sl_hours') not in (None, '') and not tp_first(w))
    late = t[t['open'] >= '2026-09-23']
    print(f"трекер (касание от момента разбора, налив не нужен): цель раньше стопа {tpf}, стоп раньше {slf} "
          f"-> p ≈ {tpf / max(1, tpf + slf):.2f}")
    print(f"сделки с 23.09: цель {int(late['exit_reason'].str.startswith('TP').sum())} из {len(late)}")


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else '.'
    t, c, state = load(folder)
    px = candles(folder, set(t['pair']) | set(c['pair'].dropna()) | {'BTCUSDT'})
    result(t)
    sides_and_cluster(t)
    path_variants(t)
    market_context(t, px)
    bias_skill(c, px)
    funnel(c)
    ev_gate(state, t)


if __name__ == '__main__':
    main()
