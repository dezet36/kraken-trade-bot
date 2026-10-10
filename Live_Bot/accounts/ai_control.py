"""
ИИ ведёт торговый счёт — допуск сделок и ведение позиций (10.10.2026).

ЗАЧЕМ. Владелец: «к счёту привязан ИИ: стратегия нашла сетап — ИИ решает,
открывать ли; раз в час смотрит позиции и, если видит разворот, переводит в
безубыток или фиксирует прибыль». Включается переключателем у торгового счёта
(правило `ai_control`); стратегии к счёту подключает владелец. Пока — у счёта
по инструкциям (accounts/manual.py): решения ИИ становятся инструкциями.

ЧТО ДЕЛАЕТ ИИ И ЧТО РЕШАЕТ КОД.
- Допуск: сетап, прошедший решение счёта (стороны, пределы, правила пропа),
  ждёт ответа ИИ «открыть / пропустить». Нет ответа за срок ожидания — сделка
  пропускается (решение владельца 10.10.2026).
- Ведение: раз в час — все открытые позиции одним вопросом; на каждую ИИ
  выбирает одно действие: держать, стоп в безубыток, подтянуть стоп, закрыть
  половину, закрыть всё. Код проверяет каждое действие (validate):
    * стоп только к цене — отодвинуть, добавить к позиции, развернуть нельзя;
    * в безубыток — только когда позиция уже прошла +1R;
    * подтянутый стоп — не ближе 0.25R к цене (иначе это выход шумом);
    * половину — один раз за позицию; одно действие на позицию за обзор.
  Модель недоступна или ответ без смысла — позиции ведутся по правилам
  стратегии, ничего не трогается.

ПОЧЕМУ ТАК ОСТОРОЖНО. По замерам проекта прогнозы модели не лучше монетки
(обзоры 28.09–10.10, опыты A–C), ручное ведение (безубыток, половина на 1R,
трейлинг) на истории не улучшало стратегии, а плюс SMCS и FIB12 — в редких
дальних целях, которые ранняя фиксация режет. Поэтому ИИ ведёт только
торговый счёт, а тестовый счёт стратегии остаётся без него — это контроль:
на тех же сетапах видно, помогает ИИ или вредит (журнал ai_control.jsonl,
research/ai_control_eval.py).

МОДЕЛЬ ОДНА НА СЕРВЕРЕ. Тетрадь спрашивает её сразу после закрытия часа —
допуск ждёт минут 8–58, ведение — 35–55, и только когда сервер свободен (/slots).
Состояние (вопросы, ответы, действия) лежит в книге счёта — переживает
перезапуск бота.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from infra import config
from infra.logger import log

FILE = 'ai_control.jsonl'
GATE_WINDOW = (8, 58)          # минуты часа, когда можно спросить допуск
REVIEW_WINDOW = (35, 55)       # минуты часа для обзора позиций
WAIT_MARKET_MIN = 30           # вход по рынку ждёт ответа не дольше
WAIT_LIMIT_MIN = 120           # лимитная заявка — не дольше
TIMEOUT_S = 12 * 60
BE_MIN_R = 1.0                 # безубыток — только после +1R
TRAIL_GAP_R = 0.25             # подтянутый стоп — не ближе 0.25R к цене
ACTIONS = ('hold', 'be', 'trail', 'half', 'close')

SYSTEM_GATE = """You control a crypto futures trading account. Rule-based strategies propose trades; you see ONE
setup, the market at that moment and the account, and decide whether the account opens it. You cannot change
the entry, stop or target. R = profit in units of the risk (stop distance). Most setups of these strategies
have an average edge near zero, so skip the ones whose context argues against the trade: the move already
exhausted, the crowd and the aggressive flow on the same side as the trade, BTC pushing the other way.
Judge only from the numbers given.
Answer with JSON only, one line:
{"take": "yes" or "no", "conf": 1-5 (5 = very sure), "reason": "at most 20 words, IN RUSSIAN"}"""

SYSTEM_REVIEW = """You manage the open positions of a crypto futures trading account once an hour. For EACH position
choose exactly one action:
- "hold"  - leave it to its plan (stop and target as they are);
- "be"    - move the stop to the entry price (allowed only when the position is at least +1R);
- "trail" - move the stop closer to the price, give the new stop as "stop" (it may only reduce the risk);
- "half"  - close half of the position at market, the rest keeps its plan;
- "close" - close the whole position at market.
R = profit in units of the initial risk. The strategies earn mostly on rare trades that reach far targets, so
closing early or tightening the stop costs money unless the numbers show the move is really reversing. When
unsure - "hold". Judge only from the numbers given.
Answer with JSON only:
{"actions": [{"pair": "XXX", "do": "hold|be|trail|half|close", "stop": number or null, "reason": "at most 20 words, IN RUSSIAN"}]}"""

ACCOUNT = """Account: balance {balance}, open positions {held}, pending orders {pending}{room}.
"""

POSITION = """{n}. {coin} {side} ({strategy}): entry {entry}, now {price} = {r_now:+.2f}R; best so far {mfe:+.2f}R,
worst {mae:+.2f}R; open {hours:.0f} h of max {max_h}; stop {stop} ({stop_r:+.2f}R){be}; target {target} ({tgt_r:+.2f}R){half}.
{market}"""

_worker = {'thread': None}
_lock = threading.Lock()


# ── Включение ───────────────────────────────────────────────────────────────

def enabled(rules):
    """ИИ ведёт этот счёт: правило включено и счёт по инструкциям (пока только он)."""
    return bool(rules.get('ai_control')) and rules.get('kind') == 'manual' \
        and bool(getattr(config, 'LLM_SERVER_URL', ''))


def wait_ms(strategy, setup, order):
    """Сколько сетап ждёт ответа ИИ: вход по рынку — 30 мин, лимит — 2 ч."""
    from strategies import strategy_profile
    from execution import core
    market = strategy_profile.fills_through_market(strategy) and not core.stop_entry(order)
    return int((WAIT_MARKET_MIN if market else WAIT_LIMIT_MIN) * 60_000)


# ── Числа позиции и ограничители ────────────────────────────────────────────

def _sign(pos):
    return 1 if pos['direction'] == 'LONG' else -1


def unit(pos):
    return abs(pos['entry_price'] - pos['initial_stop']) or 1e-12


def r_at(pos, price):
    return _sign(pos) * (price - pos['entry_price']) / unit(pos)


def validate(pos, act, price):
    """
    Действие ИИ по позиции -> (что сделать, значение, None) или (None, None, причина).
    что: 'stop' (значение — новый стоп), 'half' (доля), 'close', 'hold'.
    """
    do = str(act.get('do') or '').lower()
    if do not in ACTIONS:
        return None, None, f'неизвестное действие «{do}»'
    if do == 'hold':
        return 'hold', None, None
    d = _sign(pos)
    stop = pos['stop_loss']
    if do == 'be':
        if r_at(pos, price) < BE_MIN_R:
            return None, None, f'в безубыток только после +{BE_MIN_R:g}R (сейчас {r_at(pos, price):+.2f}R)'
        new = pos['entry_price']
        if (new - stop) * d <= 0:
            return None, None, 'стоп уже в безубытке или ближе'
        return 'stop', new, None
    if do == 'trail':
        try:
            new = float(act.get('stop'))
        except (TypeError, ValueError):
            return None, None, 'не назван новый стоп'
        if (new - stop) * d <= 0:
            return None, None, f'новый стоп {new:g} не ближе к цене, чем нынешний {stop:g}'
        if (price - new) * d < TRAIL_GAP_R * unit(pos):
            return None, None, f'новый стоп ближе {TRAIL_GAP_R:g}R к цене — это выход шумом'
        return 'stop', new, None
    if do == 'half':
        if pos.get('ai_half'):
            return None, None, 'половина уже закрыта'
        return 'half', 0.5, None
    return 'close', None, None


# ── Вопросы и ответы ────────────────────────────────────────────────────────

def _money(x):
    return f'${float(x):,.0f}'


def account_line(book, rules, now):
    from accounts import books
    space = books.room(rules, book, now)
    room = f', can still lose {_money(max(space[0], 0))} before {space[1]}' if space else ''
    return ACCOUNT.format(balance=_money(book['balance']), held=len(book['positions']),
                          pending=len(book['pending']), room=room)


def gate_question(strategy, setup, book, rules, now):
    """Вопрос допуска: сетап и рынок (как вердикт в тени) + состояние счёта."""
    from strategies.llm import llm_shadow
    text, record = llm_shadow.question(strategy, setup.get('trading_pair'), setup,
                                       now=now / 1000, price=setup.get('market_price'))
    return text + '\n' + account_line(book, rules, now), record


def review_question(book, rules, now):
    """Обзор всех открытых позиций одним вопросом. -> (текст, [пары])."""
    from strategies.llm import llm_shadow
    from strategies import strategy_profile
    lines, pairs = [], []
    for n, (pair, pos) in enumerate(sorted(book['positions'].items()), 1):
        price = float(pos.get('last_price') or pos['entry_price'])
        target = pos['targets'][pos['tp_hit']] if pos['tp_hit'] < len(pos['targets']) else pos['targets'][-1]
        max_h = pos.get('max_hold_hours') or strategy_profile.max_hold_hours(pos['strategy'])
        market, _ = llm_shadow.market_text(pair)
        lines.append(POSITION.format(
            n=n, coin=pair.replace('USDT', ''), side=pos['direction'], strategy=pos['strategy'],
            entry=f"{pos['entry_price']:.6g}", price=f'{price:.6g}', r_now=r_at(pos, price),
            mfe=r_at(pos, pos['mfe_price']), mae=r_at(pos, pos['mae_price']),
            hours=(now - pos['opened_ts']) / 3_600_000, max_h=f'{float(max_h):.0f} h' if max_h else 'no limit',
            stop=f"{pos['stop_loss']:.6g}", stop_r=r_at(pos, pos['stop_loss']),
            be=', already at breakeven' if pos.get('breakeven_set') else '',
            target=f'{target:.6g}', tgt_r=r_at(pos, target),
            half=', half already closed' if pos.get('ai_half') else '', market=market))
        pairs.append(pair)
    stamp = datetime.fromtimestamp(now / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M')
    return (f'It is {stamp} UTC. ' + account_line(book, rules, now) + 'Open positions:\n'
            + '\n'.join(lines)), pairs


_JSON = re.compile(r'\{.*\}', re.S)


def parse_review(text, pairs):
    """Ответ обзора -> {пара: {'do', 'stop', 'reason'}} (только известные пары)."""
    found = _JSON.search(text or '')
    if not found:
        return None
    try:
        raw = json.loads(found.group(0))
    except Exception:                                  # noqa: BLE001
        return None
    by_coin = {p.replace('USDT', ''): p for p in pairs}
    out = {}
    for act in raw.get('actions') or []:
        coin = str(act.get('pair') or '').upper().replace('USDT', '').strip()
        pair = by_coin.get(coin)
        if pair and pair not in out:
            out[pair] = {'do': str(act.get('do') or 'hold').lower(), 'stop': act.get('stop'),
                         'reason': str(act.get('reason') or '')[:300]}
    return out


def ask(system, text, max_tokens):
    """Вопрос серверу модели без мысли. -> (ответ, секунды)."""
    from infra import llm_server
    raw = (f'<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{text}<|im_end|>\n'
           f'<|im_start|>assistant\n<think>\n\n</think>\n\n')
    started = time.time()
    out = llm_server._completion({'prompt': raw, 'n_predict': max_tokens, 'temperature': 0.2, 'top_k': 20,
                                  'top_p': 0.9, 'stop': ['<|im_end|>'], 'cache_prompt': True}, TIMEOUT_S)
    return out.get('content') or '', round(time.time() - started, 1)


def journal(record):
    path = os.path.join(config.DATA_DIR, FILE)
    try:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
    except Exception as exc:                           # noqa: BLE001
        log(f'   ИИ-контроль: журнал не записан ({exc})')


# ── Очередь к модели (фоновый поток, состояние — в книгах счетов) ────────────

def _minute(now_ms=None):
    return datetime.fromtimestamp((now_ms or time.time() * 1000) / 1000, timezone.utc).minute


def next_job(now_ms=None):
    """Что спросить у модели сейчас: ('gate', счёт, пара) | ('review', счёт, None) | None."""
    from accounts import books
    now = now_ms or books.now_ms()
    minute = _minute(now)
    rules_by = books.accounts('manual')
    with books.lock:
        state = books.state()
        if GATE_WINDOW[0] <= minute < GATE_WINDOW[1]:
            waiting = [(item['asked_ts'], code, pair) for code, rules in rules_by.items() if enabled(rules)
                       for pair, item in (state.get(code) or {}).get('ai_wait', {}).items()
                       if item.get('answer') is None]
            if waiting:
                _, code, pair = min(waiting)
                return 'gate', code, pair
        if REVIEW_WINDOW[0] <= minute < REVIEW_WINDOW[1]:
            hour = now // 3_600_000
            for code, rules in sorted(rules_by.items()):
                book = state.get(code)
                if enabled(rules) and book and book['positions'] and book.get('ai_review_hour') != hour:
                    return 'review', code, None
    return None


def run_job(job, ask_fn=ask, now_ms=None):
    """Один вопрос модели и запись ответа в книгу счёта (без применения)."""
    from accounts import books
    kind, code, pair = job
    now = now_ms or books.now_ms()
    rules = books.accounts('manual').get(code)
    with books.lock:
        book = books.state().get(code)
        if not rules or not book:
            return
        if kind == 'gate':
            item = book.get('ai_wait', {}).get(pair)
            if not item or item.get('answer') is not None:
                return
            question = item['question']
        else:
            question, pairs = review_question(book, rules, now)
            book['ai_review_hour'] = now // 3_600_000
            books.save()
    system, limit = (SYSTEM_GATE, 120) if kind == 'gate' else (SYSTEM_REVIEW, 90 + 70 * len(pairs))
    try:
        text, seconds = ask_fn(system, question, limit)
    except Exception as exc:                           # noqa: BLE001
        log(f'   ИИ-контроль [{code}]: модель не ответила ({exc})')
        journal({'at': books.iso(now), 'account': code, 'kind': kind, 'pair': pair, 'error': str(exc)[:200]})
        return
    with books.lock:
        book = books.state().get(code)
        if not book:
            return
        if kind == 'gate':
            from strategies.llm import llm_shadow
            item = book.get('ai_wait', {}).get(pair)
            if item is None:
                return
            verdict = llm_shadow.parse(text) or {'take': False, 'conf': None,
                                                 'reason': 'ответ без решения — пропуск'}
            item['answer'] = verdict
            item['answered_ts'] = now
            journal({'at': books.iso(now), 'account': code, 'kind': 'gate', 'pair': pair,
                     'strategy': item['strategy'], 'seconds': seconds, **verdict, 'answer_raw': text[:300]})
        else:
            parsed = parse_review(text, pairs)
            book['ai_actions'] = [{'pair': p, **a, 'ts': now, 'applied': False}
                                  for p, a in (parsed or {}).items()]
            journal({'at': books.iso(now), 'account': code, 'kind': 'review', 'seconds': seconds,
                     'actions': parsed, 'answer_raw': text[:600] if parsed is None else ''})
        books.save()


def server_idle():
    from strategies.llm import llm_shadow
    return llm_shadow.server_idle()


def _serve():
    while True:
        try:
            job = next_job()
            if job and server_idle():
                run_job(job)
                continue
        except Exception as exc:                       # noqa: BLE001
            log(f'   ИИ-контроль: сбой очереди ({exc})')
        time.sleep(30)


def start():
    """Поток очереди — один на процесс; зовёт цикл бота, когда у счёта включён ИИ."""
    with _lock:
        t = _worker['thread']
        if t is None or not t.is_alive():
            _worker['thread'] = threading.Thread(target=_serve, name='ai-control', daemon=True)
            _worker['thread'].start()


def export_text():
    """Промт ИИ-контроля целиком — для docs/Промт_ИИ_контроль_счёта.txt."""
    return ('Промт ИИ-контроля торгового счёта — экспорт из Live_Bot/accounts/ai_control.py.\n'
            'Промт меняется только вместе с этим файлом: совпадение держит tests/test_ai_control.py.\n'
            'Пересобрать (из Live_Bot): python -m accounts.ai_control export\n\n'
            'Модель без мысли (<think></think> в подсказке), temperature 0.2.\n\n'
            f'=== ДОПУСК: СИСТЕМА ===\n{SYSTEM_GATE}\n\n'
            '=== ДОПУСК: ВОПРОС — как вердикт в тени (docs/Промт_ИИ_тень.txt) + строка счёта ===\n'
            f'{ACCOUNT}\n'
            f'=== ВЕДЕНИЕ: СИСТЕМА ===\n{SYSTEM_REVIEW}\n\n'
            f'=== ВЕДЕНИЕ: ПОЗИЦИЯ (по каждой) ===\n{POSITION}\n')


EXPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                      'docs', 'Промт_ИИ_контроль_счёта.txt')


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['export']:
        with open(EXPORT, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(export_text())
        print(EXPORT)
