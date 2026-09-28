"""
Живая оценка ИИ-трейдера по тетради (28.09.2026): вклад модели и её анализ.

    python research/ai_notebook_live_eval.py <папка с данными сервера>

В папке: llm_notebook_log.jsonl (решения: сигналы, выбор модели, выбор
механики), llm_notebook_reviews.jsonl (обзоры с JSON ожиданий). Свечи — Binance
1 ч (как у тетради). Считает:
  1. исход каждого сигнала по правилам его закономерности (вход на открытии
     следующего часа, стоп — доля суточного размаха, выход по сроку) — и сумму
     R: модель, механика, все сигналы подряд;
  2. обзоры: верно ли модель назвала ход BTC на сутки, и чем кончились
     монеты из её «списка наблюдения» за сутки в названную сторону;
  3. исполнение (п. 73): задержка входа от закрытия часа, опоздавшие ответы,
     ошибки модели; закрытые сделки тетради из paper_trades.jsonl — их R
     против R того же сигнала по правилам стенда (разница — цена живого входа).
Судить после ≥ 50 решений (п. 69).
"""
import json
import os
import sys
import urllib.request

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_model_trader_bt as B                        # noqa: E402

H = 3_600_000
FEE, SLIP = 0.00055, 0.0003
BINANCE = {'SHIB1000USDT': '1000SHIBUSDT'}
NOTEBOOK_START = pd.Timestamp('2026-09-28 09:43', tz='UTC')     # LLM_MODE=notebook на сервере


def klines(pair, start_ms, end_ms):
    sym = BINANCE.get(pair, pair)
    url = (f'https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1h'
           f'&startTime={start_ms}&endTime={end_ms}&limit=1500')
    rows = json.loads(urllib.request.urlopen(url, timeout=30).read())
    df = pd.DataFrame(rows, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'x1', 'qv', 'n', 'tb', 'tbq', 'x2'])
    df = df[['ts', 'o', 'h', 'l', 'c']].astype(float)
    df.index = pd.to_datetime(df['ts'].astype('int64'), unit='ms', utc=True)
    return df


def outcome(alert, hour):
    """R сигнала: вход на открытии часа после решения, стоп или срок."""
    pat = B.PATTERNS[alert['pattern']]
    sgn = 1.0 if pat['side'] == 'long' else -1.0
    t = pd.Timestamp(hour)
    start = int((t + pd.Timedelta(hours=1)).timestamp() * 1000)
    df = klines(alert['pair'], start, start + (pat['hold'] + 1) * H)
    if len(df) < pat['hold']:
        return None                                    # серия ещё не закончилась
    entry = df['o'].iloc[0] * (1 + sgn * SLIP)
    dist = pat['stop'] * alert['atr_d'] / 100.0
    stop = entry * (1 - sgn * dist)
    exit_px = None
    for _, row in df.iloc[:pat['hold']].iterrows():
        if (sgn > 0 and row['l'] <= stop) or (sgn < 0 and row['h'] >= stop):
            exit_px = stop * (1 - sgn * SLIP)
            break
    if exit_px is None:
        exit_px = df['c'].iloc[pat['hold'] - 1] * (1 - sgn * SLIP)
    return (sgn * (exit_px / entry - 1) - 2 * FEE) / dist


def decisions(folder):
    path = os.path.join(folder, 'llm_notebook_log.jsonl')
    rows = [json.loads(x) for x in open(path, encoding='utf-8') if x.strip()]
    got = {'модель': [], 'механика': [], 'все сигналы': []}
    p1_by_score = {'счёт ≥ 3': [], 'счёт ≤ 2': []}          # п. 71: держатся ли флаги P1 вживую
    for r in rows:
        res = {}
        for a in r['alerts']:
            try:
                res[a['pair']] = outcome(a, r['hour'])
            except Exception as exc:                  # noqa: BLE001
                print('  нет свечей', a['pair'], exc)
            if a.get('pattern') == 'P1' and a.get('score') is not None and res.get(a['pair']) is not None:
                p1_by_score['счёт ≥ 3' if a['score'] >= 3 else 'счёт ≤ 2'].append(res[a['pair']])
        res = {p: v for p, v in res.items() if v is not None}
        got['все сигналы'] += list(res.values())
        got['механика'] += [res[p] for p in r.get('mechanical') or [] if p in res]
        got['модель'] += [res[p] for p in (r.get('picks') or []) if p in res]
    print(f'решений: {len(rows)}')
    for name, rs in list(got.items()) + [(f'P1, {k}', v) for k, v in p1_by_score.items()]:
        rs = np.array(rs)
        if len(rs):
            print(f'  {name:12s} {len(rs):4d} сд  R {rs.mean():+.3f}  сумма {rs.sum():+.1f}R  плюс {np.mean(rs > 0) * 100:.0f}%')
    n_p1 = sum(len(v) for v in p1_by_score.values())
    if n_p1:
        print(f'  п. 71: сигналов P1 со счётом {n_p1} (проверка знака — после 40)')


def reviews(folder):
    path = os.path.join(folder, 'llm_notebook_reviews.jsonl')
    if not os.path.exists(path):
        return
    rows = [json.loads(x) for x in open(path, encoding='utf-8') if x.strip()]
    btc_ok, watch = [], []
    for r in rows:
        view = r.get('view') or {}
        t = pd.Timestamp(r['hour'])
        start = int(t.timestamp() * 1000)
        try:
            btc = klines('BTCUSDT', start, start + 25 * H)
        except Exception:                             # noqa: BLE001
            continue
        if len(btc) < 25:
            continue
        move = btc['c'].iloc[24] / btc['o'].iloc[0] - 1
        call = view.get('btc_24h')
        if call in ('up', 'down'):
            btc_ok.append((move > 0) == (call == 'up'))
        for w in view.get('watch') or []:
            pair = str(w.get('coin', '')).upper().replace('USDT', '') + 'USDT'
            try:
                k = klines(pair, start, start + 25 * H)
                sgn = 1 if w.get('side') == 'long' else -1
                watch.append(sgn * (k['c'].iloc[24] / k['o'].iloc[0] - 1) * 100)
            except Exception:                         # noqa: BLE001
                continue
    print(f'обзоров: {len(rows)}')
    if btc_ok:
        print(f'  ход BTC за сутки угадан: {np.mean(btc_ok) * 100:.0f}% из {len(btc_ok)}')
    if watch:
        w = np.array(watch)
        print(f'  список наблюдения: {len(w)} монет, ход за сутки в названную сторону {w.mean():+.2f}% '
              f'(в плюс {np.mean(w > 0) * 100:.0f}%)')


def _utc(value):
    ts = pd.Timestamp(value)
    return ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')


def execution(folder):
    """Исполнение (п. 73): задержка, опоздания, ошибки; живые сделки против правил стенда."""
    rows = [json.loads(x) for x in open(os.path.join(folder, 'llm_notebook_log.jsonl'), encoding='utf-8')
            if x.strip()]
    delays = np.array([r['delay_min'] for r in rows if r.get('delay_min') is not None], float)
    late = sum(1 for r in rows if r.get('late'))
    errors = sum(1 for r in rows if r.get('error'))
    print(f'исполнение: решений {len(rows)}; задержка входа от закрытия часа — '
          + (f'медиана {np.median(delays):.1f} мин, макс {delays.max():.1f}' if len(delays) else 'нет данных')
          + f'; ответ опоздал {late}, модель не ответила {errors}')
    path = os.path.join(folder, 'paper_trades.jsonl')
    if not os.path.exists(path):
        return
    trades = [json.loads(x) for x in open(path, encoding='utf-8') if x.strip()]
    trades = [t for t in trades if t.get('strategy') == 'LLM' and _utc(t['open_time']) >= NOTEBOOK_START]
    live, rule = [], []
    for t in trades:
        opened = _utc(t['open_time'])
        # Сигнал сделки — та же пара в решении часа, закрывшегося за 0–90 минут до входа.
        for r in rows:
            close = _utc(r['hour']) + pd.Timedelta(hours=1)
            alert = next((a for a in r['alerts'] if a['pair'] == t['pair']), None)
            if alert is None or not close <= opened <= close + pd.Timedelta(minutes=90):
                continue
            try:
                bt = outcome(alert, r['hour'])
            except Exception:                         # noqa: BLE001
                bt = None
            if bt is not None and t.get('pnl_r') is not None:
                live.append(float(t['pnl_r']))
                rule.append(bt)
            break
    if live:
        live, rule = np.array(live), np.array(rule)
        print(f'  закрытых сделок тетради {len(live)}: живой R {live.mean():+.3f}, те же сигналы по правилам стенда '
              f'{rule.mean():+.3f}, разница {live.mean() - rule.mean():+.3f} (цена живого входа и исполнения)')
    else:
        print(f'  закрытых сделок тетради {len(trades)}, сопоставимых с сигналами — нет')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    folder = sys.argv[1] if len(sys.argv) > 1 else '.'
    decisions(folder)
    execution(folder)
    reviews(folder)
