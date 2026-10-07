"""Повтор SMCS на окне бота: 4ч Bybit, боевой smcs.core.evaluate на каждом
закрытом баре, исход по 5м (вход — open первой 5м после закрытия бара, стоп
раньше цели внутри бара, цель 8R от закрытия бара слома, 30 сут).

Сверка «бот = стенд»: список сломов сравнивается с журналом бота
(journalctl … | grep "BOS 4ч") и paper_trades.csv. 07.10.2026 совпало 16/16;
бот не берёт COTI/BICO (фильтр объёма liquid_pairs) и вторую позицию на
паре (как sim.one_at_a_time стенда).

    python research/smcs_live_replay.py [начало, по умолчанию 2026-10-02 04:00]
"""
import os, sys, time
import requests
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Live_Bot'))
from smcs import core, params  # noqa: E402

PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT', 'ADAUSDT',
         'AVAXUSDT', 'LINKUSDT', 'LTCUSDT', 'ZECUSDT', 'SUIUSDT', 'ARBUSDT', 'DOTUSDT',
         'XLMUSDT', 'SHIB1000USDT', 'NEARUSDT', 'UNIUSDT', 'AAVEUSDT', 'COTIUSDT', 'BICOUSDT']
START = pd.Timestamp(sys.argv[1] if len(sys.argv) > 1 else '2026-10-02 04:00', tz='UTC')   # бар 04–08 закрылся в 08:00 — первый после выкатки
NOW = pd.Timestamp.now(tz='UTC')
FEE_TAKER = 0.055 / 100


def kline(sym, interval, start_ms, end_ms):
    out = []
    end = end_ms
    while True:
        for attempt in range(5):        # 10006 (частота) и 504 CloudFront — подождать и повторить
            try:
                rows = requests.get('https://api.bybit.com/v5/market/kline', params=dict(
                    category='linear', symbol=sym, interval=interval, end=end, limit=1000),
                    timeout=20).json()['result']['list']
                break
            except (ValueError, KeyError, requests.RequestException):
                time.sleep(2 + 3 * attempt)
        else:
            raise RuntimeError(f'{sym} {interval}: Bybit не отдал свечи')
        if not rows:
            break
        out += rows
        oldest = int(rows[-1][0])
        if oldest <= start_ms or len(rows) < 1000:
            break
        end = oldest - 1
        time.sleep(0.1)
    df = pd.DataFrame(out, columns=['ts', 'o', 'h', 'l', 'c', 'v', 't']).astype(float)
    df = df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    df = df[df.ts >= start_ms]
    df['time'] = pd.to_datetime(df.ts, unit='ms', utc=True)
    return df.reset_index(drop=True)


signals = []
for p in PAIRS:
    d4 = kline(p, '240', int((START - pd.Timedelta(hours=4 * 520)).timestamp() * 1000),
               int(NOW.timestamp() * 1000))
    d4 = d4[d4.time + pd.Timedelta(hours=4) <= NOW].reset_index(drop=True)   # только закрытые
    m5 = kline(p, '5', int(START.timestamp() * 1000), int(NOW.timestamp() * 1000))
    for i in range(len(d4)):
        if d4.time[i] < START:
            continue
        w = d4.iloc[max(0, i + 1 - params.HISTORY_BARS):i + 1]
        s, why = core.evaluate(w.o.tolist(), w.h.tolist(), w.l.tolist(), w.c.tolist(),
                               k=params.SWING_K, atr_period=params.ATR_PERIOD,
                               stop_buffer_atr=params.STOP_BUFFER_ATR,
                               target_r=params.TARGET_R, bos_only=params.BOS_ONLY)
        if not s:
            continue
        close_t = d4.time[i] + pd.Timedelta(hours=4)
        after = m5[m5.time >= close_t].reset_index(drop=True)
        if after.empty:
            continue
        entry = after.o[0]
        dd = s['dir']
        risk_ref = (s['ref'] - s['stop']) * dd
        out, exit_px, exit_t = 'OPEN', after.c.iloc[-1], None
        for j in range(len(after)):
            hi, lo = after.h[j], after.l[j]
            if (lo <= s['stop'] if dd == 1 else hi >= s['stop']):
                out, exit_px, exit_t = 'SL', s['stop'], after.time[j]; break
            if (hi >= s['target'] if dd == 1 else lo <= s['target']):
                out, exit_px, exit_t = 'TP', s['target'], after.time[j]; break
        risk_fill = (entry - s['stop']) * dd
        r = ((exit_px - entry) * dd - FEE_TAKER * (entry + exit_px)) / risk_fill
        signals.append(dict(pair=p, bar_close=str(close_t)[5:16], dir=s['direction'],
                            ref=s['ref'], stop=round(s['stop'], 6), stop_pct=round(s['stop_pct'], 2),
                            entry=entry, out=out, exit_t=str(exit_t)[5:16] if exit_t is not None else '',
                            R=round(r, 3)))
    time.sleep(0.2)

df = pd.DataFrame(signals).sort_values(['bar_close', 'pair'])
pd.set_option('display.width', 200)
print(df.to_string(index=False))
closed = df[df.out != 'OPEN']
print('\nclosed', len(closed), 'sumR', round(closed.R.sum(), 2), 'open', (df.out == 'OPEN').sum(),
      'open markR', round(df[df.out == 'OPEN'].R.sum(), 2))
