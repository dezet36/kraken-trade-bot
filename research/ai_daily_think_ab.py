"""
Анализ дня: с мыслью или без — сравнение на одних вопросах
(docs/ИИ_замечания_на_проверку.md, п. 88а; правило записано до данных).

Запуск на сервере, из /opt/kraken/code/Live_Bot, с каталогом данных бота:
  BOT_DATA_DIR=/opt/kraken/bot_data python ../research/ai_daily_think_ab.py import 2026-10-15 BTCUSDT ETHUSDT
  BOT_DATA_DIR=/opt/kraken/bot_data python ../research/ai_daily_think_ab.py collect SOLUSDT XRPUSDT
  BOT_DATA_DIR=/opt/kraken/bot_data python ../research/ai_daily_think_ab.py run      # фоном, часами
  BOT_DATA_DIR=/opt/kraken/bot_data python ../research/ai_daily_think_ab.py report

import — вопрос и ответ боевого разбора дня (это и есть «без мысли» на том же
вопросе); collect — вопрос сейчас (llm_daily.collect); run — по одному запросу
в час, в минуты 5–12, при свободном сервере, кроме часов разбора дня (бой
важнее); report — проверки кода и тексты парами для чтения.

Модель та же и тот же llm_daily.ask_model: без мысли — n_predict до 3300, с
мыслью — <think> открыт, до 6500 на мысль и ответ. Результаты —
research/results/ai_daily_ab/ (свой каталог, чужого не трогает).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Live_Bot'))

OUT = os.path.join(HERE, 'results', 'ai_daily_ab')
os.makedirs(OUT, exist_ok=True)

from infra import logger  # noqa: E402
logger.LOG_FILE = os.path.join(OUT, 'log.txt')       # не в журнал бота: это не бот

from strategies.llm import llm_daily, llm_shadow  # noqa: E402

PAIRS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT')
# Задания: на каждый вопрос — оба варианта; «без мысли» у BTC, ETH и SOL уже есть из боя.
JOBS = [(p, t) for p in PAIRS for t in (False, True)]
WINDOW = (5, 20)          # минуты начала: тетрадь спрашивает в начале часа; с мыслью ответ ~60 мин
SKIP_FALLBACK = {3, 4, 5}


def skip_hours():
    """Часы боевого разбора дня по настройкам DAILY (бой важнее прогона); нет настроек — запасные."""
    try:
        from accounts import settings_store
        cfg = settings_store.load(force=True).get(settings_store.DAILY) or {}
        if cfg.get('enabled') and cfg.get('pairs'):
            return set(llm_daily.slot_hours(list(cfg['pairs'])[:llm_daily.MAX_PAIRS], cfg.get('hour_utc', 6)).values())
    except Exception:                                  # noqa: BLE001
        pass
    return SKIP_FALLBACK


SECTIONS = ('MARKET REGIME', 'HTF STRUCTURE', 'EMOTIONAL STATE', 'POSITIONING', 'LIQUIDITY MAP',
            'ALGO BEHAVIOR', 'NARRATIVE CONTEXT', 'CONTINUATION VS EXHAUSTION', 'BULLISH SCENARIO',
            'BEARISH SCENARIO', 'INVALIDATION LEVELS', 'HIGH-PROBABILITY LIQUIDITY TARGETS', 'SCORING',
            'EXECUTION QUALITY', 'RISK CONDITIONS', 'FINAL PROBABILISTIC CONCLUSION')


def qpath(pair):
    return os.path.join(OUT, f'q_{pair}.txt')


def rpath(pair, think):
    return os.path.join(OUT, f'{pair}_{"think" if think else "nothink"}.json')


def save(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def now():
    return datetime.now(timezone.utc)


# ── Вопросы ─────────────────────────────────────────────────────────────────

def do_import(day, pairs):
    for pair in pairs:
        rec = load(llm_daily.report_path(day, pair))
        if not rec or not rec.get('answer'):
            print(pair, 'нет боевого разбора', (rec or {}).get('error', ''))
            continue
        with open(qpath(pair), 'w', encoding='utf-8') as fh:
            fh.write(rec['question'])
        save(rpath(pair, False), {'pair': pair, 'think': False, 'source': f'бой {day}', 'seconds': rec.get('seconds'),
                                  'answer': rec['answer'], 'thought': rec.get('thought', ''),
                                  'meta': rec.get('meta') or {}, 'at': rec.get('built_at')})
        print(pair, 'вопрос и ответ без мысли — из боевого разбора', day)


def do_collect(pairs):
    for pair in pairs:
        question, price = llm_daily.collect(pair)
        if question is None:
            print(pair, 'вопрос не собран:', price)
            continue
        with open(qpath(pair), 'w', encoding='utf-8') as fh:
            fh.write(question)
        print(pair, 'вопрос собран, знаков', len(question))


# ── Прогон ──────────────────────────────────────────────────────────────────

def pending():
    return [(p, t) for p, t in JOBS if os.path.exists(qpath(p)) and load(rpath(p, t)) is None]


def do_run():
    last_hour = None
    while True:
        todo = pending()
        if not todo:
            print('всё готово'); return
        t = now()
        ok = (WINDOW[0] <= t.minute < WINDOW[1] and t.hour not in skip_hours()
              and (t.date(), t.hour) != last_hour and llm_shadow.server_idle())
        if not ok:
            time.sleep(30)
            continue
        pair, think = todo[0]
        last_hour = (t.date(), t.hour)
        question = open(qpath(pair), encoding='utf-8').read()
        print(f'{t:%H:%M} {pair} {"с мыслью" if think else "без мысли"} — спрашиваю', flush=True)
        try:
            answer, seconds, sent, meta = llm_daily.ask_model(question, think=think)
            save(rpath(pair, think), {'pair': pair, 'think': think, 'source': 'прогон', 'seconds': seconds,
                                      'answer': answer, 'thought': meta.pop('thought', ''), 'meta': meta,
                                      'question_cut': sent != question, 'at': t.isoformat(timespec='seconds')})
            print(f'   готово за {seconds / 60:.0f} мин, токенов {meta.get("tokens_predicted")}, '
                  f'остановка {meta.get("stop_type")}', flush=True)
        except Exception as exc:                           # noqa: BLE001
            save(rpath(pair, think), {'pair': pair, 'think': think, 'error': str(exc)[:300],
                                      'at': t.isoformat(timespec='seconds')})
            print('   сбой:', exc, flush=True)


# ── Проверки кода ───────────────────────────────────────────────────────────

def numbers(text):
    return [float(x.replace(',', '.')) for x in re.findall(r'\d+(?:[.,]\d+)?', text or '')]


def check(rec, question):
    """Нарушения ответа (п. 88а). -> словарь."""
    answer = rec.get('answer') or ''
    meta = rec.get('meta') or {}
    parsed = llm_daily.parse(answer)
    found = [name for i, name in enumerate(SECTIONS, start=1)
             if re.search(rf'(?m)^\W*{i}\)\s*\**\s*{re.escape(name)}', answer)]
    truncated = bool(meta.get('truncated')) or parsed is None or SECTIONS[-1] not in found
    qnums = [q for q in numbers(question) if q > 0]
    invented = 0
    prices = 0
    for s in (parsed or {}).get('setups') or []:
        for value in [s.get('entry'), s.get('stop')] + list(s.get('targets') or []):
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            prices += 1
            if not any(abs(value - q) / q <= 0.0015 for q in qnums):
                invented += 1
    wrong = sum(1 for row in llm_daily.rr_table(parsed) if row['why'])
    bad = 0
    p = parsed or {}
    try:
        if p.get('bull') is not None and p.get('bear') is not None and float(p['bull']) + float(p['bear']) != 10:
            bad += 1
        if p.get('p_bull') is not None and p.get('p_bear') is not None:
            if abs(float(p['p_bull']) + float(p['p_bear']) - 100) > 0.5:
                bad += 1
            if p.get('bull') is not None and p.get('bear') is not None:
                a, b = float(p['bull']) - float(p['bear']), float(p['p_bull']) - float(p['p_bear'])
                if a * b < 0:
                    bad += 1
    except (TypeError, ValueError):
        bad += 1
    missing = len(SECTIONS) - len(found)
    return {'truncated': truncated, 'sections': len(found), 'prices': prices, 'invented': invented,
            'wrong_side': wrong, 'inconsistent': bad, 'violations': invented + wrong + bad + missing,
            'minutes': round((rec.get('seconds') or 0) / 60), 'tokens': meta.get('tokens_predicted'),
            'thought_chars': len(rec.get('thought') or '')}


def do_report():
    lines = ['Анализ дня: с мыслью / без (п. 88а) — проверки кода', '']
    head = f"{'пара':<9}{'вариант':<11}{'обрезан':>8}{'разделов':>9}{'цен':>5}{'выдум.':>7}{'не та сторона':>14}" \
           f"{'несогл.':>8}{'нарушений':>10}{'мин':>5}{'токенов':>8}"
    lines.append(head)
    totals = {False: [0, 0, 0], True: [0, 0, 0]}     # нарушения, обрезанных, ответов
    pairs_md = ['# Анализ дня: с мыслью и без — тексты парами', '']
    for pair in dict.fromkeys(p for p, _ in JOBS):
        if not os.path.exists(qpath(pair)):
            continue
        question = open(qpath(pair), encoding='utf-8').read()
        pairs_md += [f'## {pair}', '']
        for think in (False, True):
            rec = load(rpath(pair, think))
            label = 'с мыслью' if think else 'без мысли'
            if rec is None or rec.get('error'):
                lines.append(f"{pair:<9}{label:<11}  {'нет ответа' if rec is None else 'сбой: ' + rec['error'][:60]}")
                continue
            c = check(rec, question)
            totals[think][0] += c['violations']
            totals[think][1] += int(c['truncated'])
            totals[think][2] += 1
            lines.append(f"{pair:<9}{label:<11}{'да' if c['truncated'] else 'нет':>8}{c['sections']:>9}{c['prices']:>5}"
                         f"{c['invented']:>7}{c['wrong_side']:>14}{c['inconsistent']:>8}{c['violations']:>10}"
                         f"{c['minutes']:>5}{str(c['tokens'] or '—'):>8}")
            pairs_md += [f'### {label} ({rec.get("source", "")}, {c["minutes"]} мин)', '', rec.get('answer') or '', '']
    lines += ['', f"Итого без мысли: нарушений {totals[False][0]}, обрезано {totals[False][1]} из {totals[False][2]}",
              f"Итого с мыслью:  нарушений {totals[True][0]}, обрезано {totals[True][1]} из {totals[True][2]}", '',
              'Правило (п. 88а): мысль — в бой, если с мыслью ничего не обрезано, нарушений меньше хотя бы на 2 '
              'и владелец выбирает тексты с мыслью.']
    text = '\n'.join(lines)
    with open(os.path.join(OUT, 'report.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')
    with open(os.path.join(OUT, 'pairs.md'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(pairs_md))
    print(text)


if __name__ == '__main__':
    cmd, args = (sys.argv[1:2] or ['report'])[0], sys.argv[2:]
    if cmd == 'import':
        do_import(args[0], args[1:])
    elif cmd == 'collect':
        do_collect(args)
    elif cmd == 'run':
        do_run()
    else:
        do_report()
