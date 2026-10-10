"""
Вердикт ИИ по сетапам других стратегий — в тени (10.10.2026).

ЗАЧЕМ. Владелец спросил, может ли ИИ анализировать сетапы остальных стратегий.
На истории модель сетапы не различала (опыты A–C, docs/ИИ_замечания_на_
проверку.md), а её прогнозы BTC и списки «под наблюдением» в обзорах 28.09–10.10
сбывались как монетка (67 обзоров). Проверить вживую это можно, ничего не
ломая: на каждый сетап, по которому тестовый счёт поставил заявку, модель
говорит «брал бы / не брал бы» с причиной — и только. Вердикт пишется в
bot_data/llm_shadow.jsonl, на сделки, счета и другие стратегии не влияет.
Через ~100 вердиктов с исходом research/ai_shadow_eval.py сравнит сделки,
которые модель взяла бы, с теми, которые пропустила бы.

ЧЕСТНОСТЬ ЗАМЕРА — СНИМОК НА МОМЕНТ СЕТАПА. Вопрос собирается сразу, в цикле
бота: сетап и последний закрытый час рынка (признаки тетради — тот же расчёт,
что в обзоре). Модель отвечает позже, но видит только то, что было известно
при постановке заявки; иначе вердикт подглядывал бы в будущее.

МОДЕЛЬ ОДНА НА СЕРВЕРЕ, И ТЕТРАДЬ ВАЖНЕЕ. Решение тетради уходит модели сразу
после закрытия часа и должно успеть за 30 минут; обзор рынка раз в 4 часа
занимает её до 25 минут. Поэтому вердикты ждут в своей очереди и спрашиваются
только во второй половине часа (минуты WINDOW) и только когда сервер модели
свободен (/slots). Вопрос короткий (~700 токенов): ответ за 1.5–3 минуты.

Чужих пакетов модуль не импортирует: сетап приходит готовым словарём из цикла
бота (общий договор сигнала), рынок — из снимка тетради (свой пакет).
"""

import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone

from infra import config
from infra.logger import log

NAME = 'LLM'                     # свои сделки ИИ вердикта не получают
FILE = 'llm_shadow.jsonl'
WINDOW = (32, 52)                # минуты часа, когда вопрос можно отправить
TIMEOUT_S = 15 * 60              # дольше — модель занята или зависла, вердикт пропущен
MAX_AGE_S = 24 * 3600            # вопрос старше суток не задаётся
QUEUE_MAX = 60                   # очередь длиннее — старые вопросы выбрасываются
SNAPSHOT_MAX_AGE_S = 2 * 3600    # рынок тетради старше — вопрос без строк рынка

SYSTEM = """You review trade setups of a crypto futures bot. Rule-based strategies propose trades; you see ONE
setup and the market at that moment, and say whether YOU would take it. You cannot change the entry, stop or
target, and your answer does not open or cancel anything - it is recorded and later compared with the outcome.
R = profit in units of the risk (stop distance). Most setups of these strategies have an average edge near
zero, so a good reviewer skips the ones whose context argues against the trade: the move already exhausted,
the crowd and the aggressive flow on the same side as the trade, BTC pushing the other way, a stop inside
normal noise. Judge only from the numbers given.
Answer with JSON only, one line:
{"take": "yes" or "no", "conf": 1-5 (5 = very sure), "reason": "at most 20 words, IN RUSSIAN"}"""

QUESTION = """It is {time} UTC. Strategy: {title}. Setup: {side} {coin}.
Strategy's own description of the setup (Russian): {why}
Entry {entry} ({where}); stop {stop_pct} from entry; target {target_pct}
from entry = {rr} R.
{market}"""

MARKET = """Market at the last closed hour ({hour} UTC):
BTC: {btc4}% in 4h, {btc24}% in 24h, {btc7}% in 7d, {btc30}% in 30d. Coins in a liquidation cascade in the last 3h: {breadth}.
{coin}: {c4}% in 4h, {c24}% in 24h, {c7}% in 7d, {c30}% in 30d | vs BTC 24h {rel}% | open interest {oi4}% in 4h,
{oi24}% in 24h | aggressive buying {buy} of 24h volume (0.50 = balance) | funding {fund} bp per 8h (+1.0 = normal) |
retail longs rank {rank} vs own last 30 days (0 = lowest, 1 = highest) | volume {vol}x normal"""

NO_MARKET = 'Market data for this hour is not available - judge from the setup only.'

_jobs = queue.Queue()
_worker = {'thread': None}
_snapshot = {'t': None, 'data': None, 'at': 0.0}
_stats = {'asked': 0, 'answered': 0, 'dropped': 0}


def enabled():
    return bool(getattr(config, 'LLM_SHADOW', True)) and bool(getattr(config, 'LLM_SERVER_URL', ''))


def remember_market(t, data):
    """Снимок рынка последнего закрытого часа — его кладёт тетрадь (llm_notebook._new_hour)."""
    _snapshot.update(t=t, data=data, at=time.time())


# ── Вопрос ──────────────────────────────────────────────────────────────────

def _num(x, fmt):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return '—'
    return '—' if x != x else format(x, fmt)


def _pct(a, b):
    """Расстояние от b до a в процентах от b, со знаком."""
    try:
        return f'{(float(a) / float(b) - 1) * 100:+.2f}%'
    except (TypeError, ValueError, ZeroDivisionError):
        return '—'


def market_text(pair, now=None):
    """Строки рынка из снимка тетради или NO_MARKET (снимка нет, он старый, пары в нём нет)."""
    now = time.time() if now is None else now
    data, t = _snapshot.get('data'), _snapshot.get('t')
    if not data or t is None or now - _snapshot.get('at', 0) > SNAPSHOT_MAX_AGE_S:
        return NO_MARKET, None
    try:
        btc = data['BTCUSDT'][1].loc[t]
        if pair not in data or t not in data[pair][1].index:
            return NO_MARKET, None
        f = data[pair][1].loc[t]
    except Exception:                                  # noqa: BLE001
        return NO_MARKET, None
    hour = t.strftime('%Y-%m-%d %H:%M') if hasattr(t, 'strftime') else str(t)
    text = MARKET.format(
        hour=hour, btc4=_num(btc.get('btc_ret_4h'), '+.1f'), btc24=_num(btc.get('btc_ret_24h'), '+.1f'),
        btc7=_num(btc.get('btc_ret_7d'), '+.1f'), btc30=_num(btc.get('btc_ret_30d'), '+.1f'),
        breadth=_num(btc.get('cascade_count_3h'), '.0f'), coin=pair.replace('USDT', ''),
        c4=_num(f.get('ret_4h'), '+.1f'), c24=_num(f.get('ret_24h'), '+.1f'), c7=_num(f.get('ret_7d'), '+.1f'),
        c30=_num(f.get('ret_30d'), '+.1f'), rel=_num(f.get('rel_24h'), '+.1f'),
        oi4=_num(f.get('oi_chg_4h'), '+.1f'), oi24=_num(f.get('oi_chg_24h'), '+.1f'),
        buy=_num(f.get('taker_24h'), '.2f'), fund=_num(f.get('funding_bp'), '+.2f'),
        rank=_num(f.get('buy_ratio_pct_30d'), '.2f'), vol=_num(f.get('vol_z'), '.1f'))
    return text, hour


def setup_of(signal, price=None):
    """Сетап из сигнала (общий договор): сторона, вход, стоп, первая цель, цена сейчас.

    Цена — из сигнала (market_price) или от цикла бота (закрытие последней свечи
    кандидата); нет ни той, ни другой — None, а не вход: «вход в 0% от цены»
    был бы неправдой."""
    params = signal.get('params') or {}
    targets = params.get('tp_targets') or [params.get('take_profit_1')]
    entry = float(params['entry'])
    price = signal.get('market_price') or price
    return {
        'side': str((signal.get('setup') or {}).get('type') or signal.get('direction') or '').upper(),
        'entry': entry,
        'stop': float(params['stop_loss']),
        'target': float(targets[0]) if targets and targets[0] is not None else None,
        'price': float(price) if price else None,
    }


def question(strategy, pair, signal, why='', now=None, title=None, price=None):
    """(вопрос модели, запись-заготовка журнала). Всё — на момент сетапа."""
    now = time.time() if now is None else now
    s = setup_of(signal, price)
    why = why or signal.get('why') or ''
    if title is None:
        try:
            from strategies import registry
            entry = registry.get(strategy)
            title = entry.title if entry else strategy
        except Exception:                              # noqa: BLE001
            title = strategy
    risk = abs(s['entry'] - s['stop'])
    rr = abs(s['target'] - s['entry']) / risk if s['target'] and risk else None
    market, hour = market_text(pair, now)
    text = QUESTION.format(
        time=datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d %H:%M'),
        title=title, side=s['side'], coin=pair.replace('USDT', ''), why=why or '—',
        entry=f"{s['entry']:.6g}",
        where=(f"{_pct(s['entry'], s['price'])} from the current price {s['price']:.6g}" if s['price']
               else 'current price not given'),
        stop_pct=_pct(s['stop'], s['entry']), target_pct=_pct(s['target'], s['entry']) if s['target'] else '—',
        rr=_num(rr, '.2f'), market=market)
    record = {'at': datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='seconds'),
              'strategy': strategy, 'pair': pair, 'direction': s['side'], 'entry': s['entry'],
              'stop': s['stop'], 'target': s['target'], 'price': s['price'], 'rr': rr,
              'market_hour': hour, 'why': why}
    return text, record


# ── Ответ ───────────────────────────────────────────────────────────────────

_JSON = re.compile(r'\{[^{}]*"take"[^{}]*\}', re.S)


def parse(text):
    """Ответ модели -> {'take': True/False, 'conf': 1..5|None, 'reason': str} или None."""
    found = _JSON.findall(text or '')
    if not found:
        return None
    try:
        raw = json.loads(found[-1])
    except Exception:                                  # noqa: BLE001
        return None
    take = str(raw.get('take', '')).strip().lower()
    if take not in ('yes', 'no'):
        return None
    try:
        conf = int(raw.get('conf'))
        conf = conf if 1 <= conf <= 5 else None
    except (TypeError, ValueError):
        conf = None
    return {'take': take == 'yes', 'conf': conf, 'reason': str(raw.get('reason') or '')[:300]}


def ask_model(text, timeout=TIMEOUT_S):
    """Вопрос серверу модели без мысли — как решения тетради. -> (ответ, секунды)."""
    from infra import llm_server
    raw = (f'<|im_start|>system\n{SYSTEM}<|im_end|>\n<|im_start|>user\n{text}<|im_end|>\n'
           f'<|im_start|>assistant\n<think>\n\n</think>\n\n')
    started = time.time()
    out = llm_server._completion({'prompt': raw, 'n_predict': 120, 'temperature': 0.2, 'top_k': 20,
                                  'top_p': 0.9, 'stop': ['<|im_end|>'], 'cache_prompt': True}, timeout)
    return out.get('content') or '', round(time.time() - started, 1)


def server_idle():
    """Свободен ли сервер модели (/slots): занят — тетрадь или обзор, вердикт подождёт."""
    from infra import llm_server
    import urllib.request
    try:
        with urllib.request.urlopen(f'{llm_server.url()}/slots', timeout=5) as resp:
            slots = json.loads(resp.read().decode('utf-8'))
        return not any(s.get('is_processing') for s in slots)
    except Exception:                                  # noqa: BLE001
        return False


def in_window(now=None):
    minute = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).minute
    return WINDOW[0] <= minute < WINDOW[1]


# ── Очередь ─────────────────────────────────────────────────────────────────

def _append(record):
    path = os.path.join(config.DATA_DIR, FILE)
    try:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
    except Exception as exc:                           # noqa: BLE001
        log(f'   ИИ в тени: журнал не записан ({exc})')


def answer(job, ask=ask_model, now=None):
    """Один вердикт: вопрос модели -> строка журнала. Возвращает запись."""
    record = dict(job['record'])
    try:
        text, seconds = ask(job['question'])
    except Exception as exc:                           # noqa: BLE001
        record.update(error=f"{getattr(exc, 'llm_gate', '')} {exc}".strip()[:200])
        _append(record)
        log(f"   ИИ в тени: {record['strategy']} {record['pair']} — вердикта нет ({record['error']})")
        return record
    verdict = parse(text)
    record.update(asked_at=datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
                  .isoformat(timespec='seconds'), seconds=seconds, answer=text[:400])
    if verdict is None:
        record['error'] = 'в ответе нет вердикта'
    else:
        record.update(verdict)
    _append(record)
    _stats['answered'] += 1
    log(f"   ИИ в тени: {record['strategy']} {record['pair']} {record['direction']} — "
        + (('брал бы' if verdict['take'] else 'не брал бы') + f" ({verdict['conf']}/5): {verdict['reason']}"
           if verdict else f'ответ без вердикта: {text[:120]}'))
    return record


def _serve():
    while True:
        job = _jobs.get()
        while True:
            if time.time() - job['queued'] > MAX_AGE_S:
                _stats['dropped'] += 1
                _append(dict(job['record'], error='вопрос устарел в очереди'))
                break
            if in_window() and server_idle():
                answer(job)
                break
            time.sleep(30)


def observe(strategy, pair, signal, why='', price=None):
    """
    Сетап, по которому тестовый счёт поставил заявку, — в очередь вердиктов.
    Зовёт цикл бота после broker.open; ничего не ждёт и ничего не бросает.
    """
    if strategy == NAME or not enabled():
        return False
    try:
        text, record = question(strategy, pair, signal, why, price=price)
    except Exception as exc:                           # noqa: BLE001
        log(f'   ИИ в тени: {strategy} {pair} — вопрос не собран ({exc})')
        return False
    while _jobs.qsize() >= QUEUE_MAX:
        try:
            old = _jobs.get_nowait()
            _stats['dropped'] += 1
            _append(dict(old['record'], error='очередь переполнена'))
        except queue.Empty:
            break
    _jobs.put({'question': text, 'record': record, 'queued': time.time()})
    _stats['asked'] += 1
    thread = _worker['thread']
    if thread is None or not thread.is_alive():
        _worker['thread'] = threading.Thread(target=_serve, name='llm-shadow', daemon=True)
        _worker['thread'].start()
    return True


def export_text():
    """Промт вердикта целиком — для docs/Промт_ИИ_тень.txt (промт меняется только с экспортом)."""
    return ('Промт ИИ «вердикт в тени» — экспорт из Live_Bot/strategies/llm/llm_shadow.py.\n'
            'Промт меняется только вместе с этим файлом: совпадение держит tests/test_llm_shadow.py.\n'
            'Пересобрать (из Live_Bot): python -m strategies.llm.llm_shadow export\n\n'
            'Модель без мысли (<think></think> в подсказке), n_predict 120, temperature 0.2.\n\n'
            f'=== СИСТЕМА ===\n{SYSTEM}\n\n=== ВОПРОС ===\n{QUESTION}\n\n'
            f'=== РЫНОК (если есть снимок тетради) ===\n{MARKET}\n\n=== РЫНКА НЕТ ===\n{NO_MARKET}\n')


EXPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))), 'docs', 'Промт_ИИ_тень.txt')


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['export']:
        with open(EXPORT, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(export_text())
        print(EXPORT)
