"""Повтор SMCS на окне бота: 4ч Bybit, боевой smcs.core.evaluate на каждом
закрытом баре, исход по 5м (вход — open первой 5м после закрытия бара, стоп
раньше цели внутри бара, цель 8R от закрытия бара слома, 30 сут).

Сверка «бот = стенд»: список сломов сравнивается с журналом бота
(journalctl … | grep "BOS 4ч") и paper_trades.csv. 07.10.2026 совпало 16/16;
бот не берёт COTI/BICO (фильтр объёма liquid_pairs) и вторую позицию на
паре (как sim.one_at_a_time стенда).

С 09.10.2026 (реорганизация, этап 9) рядом с прежней моделью исхода — исход
ТЕМ ЖЕ КОДОМ, что торгует: сигнал строит адаптер бота
(strategy_smcs._to_bot_signal, цена — открытие первой 5м после закрытия
бара), решение счёта и жизнь сделки — accounts/replay (ядро исполнения,
комиссии и проскальзывание бумаги, срок удержания и кулдаун стратегии),
шаг цикла 5 минут. Колонка R_bot — это и есть итог бота на этих свечах.

    python research/smcs_live_replay.py [начало, по умолчанию 2026-10-02 04:00]
"""
import os, sys, time
import requests
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Live_Bot'))
import logger  # noqa: E402
logger.log = lambda *a, **k: None               # строка на каждый шаг прогона — лишняя
from strategies.smcs import adapter as strategy_smcs  # noqa: E402
from accounts import books, replay  # noqa: E402
from strategies.smcs import core, params  # noqa: E402

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
bot_setups, bot_candles = [], {}
for p in PAIRS:
    d4 = kline(p, '240', int((START - pd.Timedelta(hours=4 * 520)).timestamp() * 1000),
               int(NOW.timestamp() * 1000))
    d4 = d4[d4.time + pd.Timedelta(hours=4) <= NOW].reset_index(drop=True)   # только закрытые
    m5 = kline(p, '5', int(START.timestamp() * 1000), int(NOW.timestamp() * 1000))
    bot_candles[p] = m5[['ts', 'o', 'h', 'l', 'c', 'v']].astype(float).values.tolist()
    bot_candles[p] = [[int(r[0])] + r[1:] for r in bot_candles[p]]
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
        # Сигнал бота: тот же адаптер, цена рынка — открытие первой 5м.
        window = pd.DataFrame({'timestamp': w.time, 'open': w.o, 'high': w.h, 'low': w.l, 'close': w.c})
        sig = strategy_smcs._to_bot_signal(s, p, window.reset_index(drop=True), float(entry))
        bot_setups.append((int(close_t.timestamp() * 1000) + 60_000, 'SMCS', books.setup_copy(sig)))
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

# Исход тем же кодом, что торгует (accounts/replay): шаг цикла бота — 5 минут.
bot = replay.run(bot_setups, bot_candles, step_ms=5 * 60_000,
                 until=int(NOW.timestamp() * 1000))
r_bot = {}
for t in bot['trades']:
    opened = pd.Timestamp(t['opened_at'])
    r_bot[(t['pair'], opened)] = (t['pnl_r'], t['exit_reason'])
book = bot['books'].get('SMCS') or {}


def bot_result(row):
    """Сделка бота по этому слому: открыта в течение часа после закрытия бара."""
    close_t = pd.Timestamp(f"{NOW.year}-{row.bar_close}", tz='UTC')
    for (pair, opened), (r, why) in r_bot.items():
        if pair == row.pair and close_t <= opened <= close_t + pd.Timedelta(hours=1):
            return pd.Series({'R_bot': r, 'out_bot': why})
    pos = (book.get('positions') or {}).get(row.pair)
    if pos and close_t <= pd.Timestamp(pos['opened_at']) <= close_t + pd.Timedelta(hours=1):
        return pd.Series({'R_bot': None, 'out_bot': 'OPEN'})
    return pd.Series({'R_bot': None, 'out_bot': 'не взят'})


df = pd.concat([df.reset_index(drop=True), df.apply(bot_result, axis=1).reset_index(drop=True)], axis=1)
pd.set_option('display.width', 200)
print(df.to_string(index=False))
print('\nбот (accounts/replay): закрыто', len(bot['trades']), 'sumR',
      round(sum(t['pnl_r'] for t in bot['trades']), 2), 'открыто', len(book.get('positions') or {}),
      'отказы', (book.get('counts') or {}).get('refused'))
closed = df[df.out != 'OPEN']
print('\nclosed', len(closed), 'sumR', round(closed.R.sum(), 2), 'open', (df.out == 'OPEN').sum(),
      'open markR', round(df[df.out == 'OPEN'].R.sum(), 2))
