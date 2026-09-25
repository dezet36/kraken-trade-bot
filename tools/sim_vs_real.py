# -*- coding: utf-8 -*-
"""
Сверка симулятора с жизнью: каждая настоящая сделка ИИ из paper_trades.csv
рядом с её планом из журнала и тем, что насчитал бы симулятор.

25.09.2026: из 17 сделок 9 первых (19–21.09) — ответы старого формата, план
не разбирается; по остальным симулятор совпал в 7 случаях из 9. Запуск на
сервере: /opt/kraken/venv/bin/python tools/sim_vs_real.py
"""
import csv, io, contextlib, datetime
src = open('/opt/kraken/code/tools/plan_factors.py', encoding='utf-8').read()
with contextlib.redirect_stdout(io.StringIO()):
    exec(src[:src.index("\nprint(f'Планов")])
trades = [t for t in csv.DictReader(open('/opt/kraken/bot_data/paper_trades.csv', encoding='utf-8')) if t['strategy'] == 'LLM']
acc = [r for r in rows if r['decision'] == 'enter']
def iso(s): return datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
print('%-17s %-9s %-5s | %-9s %-9s %-9s | %-11s %-7s %-6s | sim: %s' % ('открыта', 'пара', 'сторона', 'вход план', 'вход факт', 'стоп', 'выход', 'R', 'ждал ч', 'исход'))
for t in sorted(trades, key=lambda t: t['open_time']):
    ot = iso(t['open_time'])
    cand = [r for r in acc if r['pair'] == t['pair'] and iso(r['at']) <= ot]
    if not cand:
        print(t['open_time'][:16], t['pair'], 'план не найден'); continue
    r = cand[-1]
    p = plan_of(r.get('raw'), r.get('levels'))
    wait_h = (ot - iso(r['at'])).total_seconds() / 3600
    sim = simulate(r['pair'], r['at'], p) if p else None
    print('%-17s %-9s %-5s | %-9.5g %-9.5g %-9.5g | %-11s %+6.2f %6.1f | %s  (условие %s)' % (
        t['open_time'][:16], t['pair'], t['direction'], float(t['planned_entry'] or 0), float(t['entry_price']),
        float(t['stop_loss']), t['exit_reason'][:11], float(t['pnl_r']), wait_h, sim, p['when'] if p else '-'))
