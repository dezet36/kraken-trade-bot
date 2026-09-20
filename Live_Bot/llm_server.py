"""
Модель в llama-server: HTTP-клиент вместо llama-cpp-python.

ЗАЧЕМ. Единственный способ ускорить модель на этом процессоре — MTP
(multi-token prediction: модель предсказывает два токена за проход, и на
CPU, где каждый токен — чтение весов, это до ×1.5–2). llama-cpp-python
0.3.35 MTP не умеет и спекулятивную генерацию через черновик по промту тоже
роняет (требует логиты на все позиции — 7 ГБ). llama-server из llama.cpp
умеет: `--spec-type draft-mtp`. Грамматика GBNF там поддерживается в теле
запроса, кэш префикса (`cache_prompt`) — тоже.

ЧТО ОСТАЁТСЯ ПРЕЖНИМ. Точка входа одна — llm_local.ask(prompt, grammar,
max_tokens); он идёт сюда, когда в настройках задан LLM_SERVER_URL. Имена
поломок те же, что у рабочего процесса (llm_worker): «модель недоступна» —
сервер не отвечает; «модель зависла» — не ответил за отведённое время;
«модель упала» — ответил ошибкой. Статистика последнего вызова — тем же
словарём (_last в llm_local), панель и журнал разницы не видят.

ФОРМАТ ВОПРОСА. /completion с готовым ChatML-текстом, а не
/v1/chat/completions: шаблон чата Qwen3 сам подставляет пустой блок
<think>, и грамматика, ждущая «<think>» первым токеном, с ним разошлась бы.
Здесь текст собирается руками так же, как его собирала llama-cpp-python.
"""

import json
import os
import time
import urllib.error
import urllib.request

import config
from logger import log

# Сколько ждём ответа: как у рабочего процесса — разбор с мыслью до 45 минут.
CALL_TIMEOUT_SEC = int(os.getenv('LLM_CALL_TIMEOUT_MIN', 45)) * 60


def url():
    return (getattr(config, 'LLM_SERVER_URL', '') or '').rstrip('/')


def enabled():
    return bool(url())


def _chatml(prompt):
    return f'<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n'


def _raise(gate, text):
    error = RuntimeError(text)
    error.llm_gate = gate
    raise error


def health(timeout=5):
    """Жив ли сервер и загружена ли модель. Словарь /health или None."""
    try:
        with urllib.request.urlopen(f'{url()}/health', timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:                              # noqa: BLE001
        return None


def props():
    """Свойства сервера: имя модели, окно, потоки. Пусто, если не ответил."""
    try:
        with urllib.request.urlopen(f'{url()}/props', timeout=5) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:                              # noqa: BLE001
        return {}


# Разделитель между системным промтом и разметкой — тот же, что ставит
# llm_prompt.build. По нему ищется общее начало вопроса.
PREFIX_SEP = chr(10) + '-' * 40 + chr(10)


def _tokenize(text, timeout=60):
    data = json.dumps({'content': text}).encode('utf-8')
    req = urllib.request.Request(f'{url()}/tokenize', data=data,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return list(json.loads(resp.read().decode('utf-8')).get('tokens') or [])


def _warm_prefix(full_text, full_ids):
    """
    Прогревает кэш общим началом вопроса (системный промт) — отдельным
    запросом без генерации.

    ЗАЧЕМ. У Qwen3.6 часть слоёв рекуррентная (Gated DeltaNet): состояние
    нельзя откатить на произвольную позицию, поэтому llama-server
    переиспользует кэш только когда сохранённый промт целиком совпадает с
    началом нового. После разбора ETH в кэше лежит «промт+разметка ETH+ответ»,
    и вопрос про XRP расходится с ним на 4500-м токене — сервер считал всё
    заново: 7961 токен, 13 минут (20.09.2026, task 101). Запрос ровно с
    префиксом и n_predict=0 оставляет состояние на границе, и следующий
    вопрос продолжает с неё. Если префикс уже в кэше, прогрев стоит секунду.

    Возвращает число токенов префикса или 0, если прогрев не удался.
    """
    head, sep, _ = full_text.partition(PREFIX_SEP)
    if not sep:
        return 0
    try:
        prefix_ids = _tokenize(head + sep)
    except Exception:                              # noqa: BLE001
        return 0
    n = len(prefix_ids)
    if n < 64 or full_ids[:n] != prefix_ids:
        return 0                                   # граница легла не на токен
    body = {'prompt': prefix_ids, 'n_predict': 0, 'cache_prompt': True}
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(f'{url()}/completion', data=data,
                                 headers={'Content-Type': 'application/json'})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_SEC) as resp:
            resp.read()
    except Exception as exc:                       # noqa: BLE001
        log(f'   модель: прогрев префикса не удался — {exc}')
        return 0
    spent = time.time() - started
    if spent > 5:
        log(f'   модель: префикс вопроса ({n} ток.) посчитан заново за {spent:.0f} с')
    return n


def ask(prompt, grammar=None, max_tokens=None, timeout=None):
    """
    Один вопрос серверу. -> (текст ответа, статистика) как у llm_worker.ask.

    Бросает RuntimeError с llm_gate: «модель недоступна», «модель зависла»,
    «модель упала».
    """
    limit = int(max_tokens or config.LLM_MAX_TOKENS)
    text = _chatml(prompt + ('\n' + config.LLM_THINK_TAG if config.LLM_THINK_TAG else ''))
    # Вопрос уходит токенами, а не строкой: так префикс прогрева и начало
    # вопроса совпадают гарантированно, а не «обычно».
    prompt_ids = None
    try:
        prompt_ids = _tokenize(text)
        _warm_prefix(text, prompt_ids)
    except Exception as exc:                       # noqa: BLE001
        log(f'   модель: токенизация не удалась, шлю строкой — {exc}')
        prompt_ids = None
    body = {
        'prompt': prompt_ids if prompt_ids else text,
        'n_predict': limit,
        'temperature': float(getattr(config, 'LLM_TEMPERATURE', 0.3)),
        'cache_prompt': True,
        'stop': ['<|im_end|>'],
    }
    if grammar:
        body['grammar'] = grammar
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(f'{url()}/completion', data=data,
                                 headers={'Content-Type': 'application/json'})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout or CALL_TIMEOUT_SEC) as resp:
            out = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = exc.read().decode('utf-8')[:300]
        except Exception:                          # noqa: BLE001
            pass
        _raise('модель упала', f'llama-server ответил {exc.code}: {detail}')
    except urllib.error.URLError as exc:
        reason = getattr(exc, 'reason', exc)
        if isinstance(reason, TimeoutError) or 'timed out' in str(reason):
            _raise('модель зависла', f'llama-server не ответил за {(timeout or CALL_TIMEOUT_SEC) // 60} мин')
        _raise('модель недоступна', f'llama-server не отвечает: {reason}')
    except TimeoutError:
        _raise('модель зависла', f'llama-server не ответил за {(timeout or CALL_TIMEOUT_SEC) // 60} мин')
    spent = time.time() - started

    answer = out.get('content') or ''
    timings = out.get('timings') or {}
    evaluated = int(out.get('tokens_evaluated') or timings.get('prompt_n') or 0)
    answer_tokens = int(out.get('tokens_predicted') or timings.get('predicted_n') or 0)
    # Вопрос целиком — сколько токенов ушло; из кэша — сколько из них сервер
    # не пересчитывал. tokens_cached сервера — это размер кэша после ответа,
    # не то же самое (первая версия печатала «7962 вход, 8690 из кэша»).
    total = len(prompt_ids) if prompt_ids else evaluated
    cached = max(0, total - evaluated)
    finish = 'length' if out.get('truncated') or (out.get('stop_type') == 'limit') else 'stop'
    stats = {
        'prompt_tokens': total,
        'answer_tokens': answer_tokens,
        'limit': limit,
        'ctx': int(out.get('n_ctx') or getattr(config, 'LLM_CTX', 0)),
        'seconds': round(spent, 1),
        'finish': finish,
        'at': time.time(),
        'cached_tokens': cached,
        'tok_s': round(float(timings.get('predicted_per_second') or 0), 2),
        'draft_accepted': int(timings.get('draft_n_accepted') or 0),
        'draft_n': int(timings.get('draft_n') or 0),
    }
    log(f'   модель: {stats["prompt_tokens"]} вход ({cached} из кэша), {answer_tokens} выход '
        f'за {spent:.1f} с = {stats["tok_s"]} ток/с'
        + (f', черновик принят {stats["draft_accepted"]}/{stats["draft_n"]}' if stats['draft_n'] else '')
        + (' — упёрлось в предел' if finish == 'length' else ''))
    return answer, stats
