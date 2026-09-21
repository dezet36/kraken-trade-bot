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
import llm_grammar
import llm_prompt
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


def _completion(body, timeout):
    """POST /completion с именованными поломками. -> словарь ответа сервера."""
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(f'{url()}/completion', data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
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
            _raise('модель зависла', f'llama-server не ответил за {timeout // 60} мин')
        _raise('модель недоступна', f'llama-server не отвечает: {reason}')
    except TimeoutError:
        _raise('модель зависла', f'llama-server не ответил за {timeout // 60} мин')


THINK_CLOSE = '</think>' + chr(10) + chr(10)


def ask(prompt, grammar=None, max_tokens=None, timeout=None):
    """
    Один вопрос серверу — в две фазы. -> (текст ответа, статистика).

    ФАЗА МЫСЛИ — без грамматики, с пределом LLM_THINK_TOKENS и стоп-словом
    «</think>». ФАЗА ОТВЕТА — грамматика ответа, подсказка продолжена
    мыслью и «</think>»; кэш сервера держит и вопрос, и мысль, так что
    вторая фаза читает только несколько токенов.

    ЗАЧЕМ ДВЕ. Предел мысли в знаках стоял в грамматике, а llama-server с
    MTP-черновиком не продвигает грамматику на принятых черновых токенах:
    при 77% принятых мысль ETH 21.09.2026 прошла 6000 знаков при пределе
    2400, съела все 3000 токенов ответа, и JSON не случился — «ответ
    обрезан», 37 минут. Предел в токенах на стороне сервера обойти нельзя.
    Мысль, не закрывшаяся сама, закрывается здесь — ответ будет всегда.

    Бросает RuntimeError с llm_gate: «модель недоступна», «модель зависла»,
    «модель упала».
    """
    limit = int(max_tokens or config.LLM_MAX_TOKENS)
    timeout = timeout or CALL_TIMEOUT_SEC
    temperature = float(getattr(config, 'LLM_TEMPERATURE', 0.3))
    answer_grammar, think_open = (llm_grammar.open_thinking(grammar, llm_prompt.THINK_SEED)
                                  if grammar else (grammar, ''))
    if think_open:
        answer_grammar = llm_grammar.answer_only(grammar)
    text = _chatml(prompt + ('\n' + config.LLM_THINK_TAG if config.LLM_THINK_TAG else '')) + think_open
    # Вопрос уходит токенами, а не строкой: так префикс прогрева и начало
    # вопроса совпадают гарантированно, а не «обычно».
    prompt_ids = None
    try:
        prompt_ids = _tokenize(text)
        _warm_prefix(text, prompt_ids)
    except Exception as exc:                       # noqa: BLE001
        log(f'   модель: токенизация не удалась, шлю строкой — {exc}')
        prompt_ids = None
    started = time.time()

    # ── Фаза 1: мысль ──────────────────────────────────────────────────────
    thought, thought_ids, thought_tokens, think_finish = '', [], 0, ''
    think_limit = int(getattr(config, 'LLM_THINK_TOKENS', 0) or 0)
    if think_open and think_limit > 0:
        out1 = _completion({
            'prompt': prompt_ids if prompt_ids else text,
            'n_predict': min(think_limit, limit),
            'temperature': temperature,
            'cache_prompt': True,
            'stop': ['</think>', '<|im_end|>'],
            'return_tokens': True,
        }, timeout)
        thought = out1.get('content') or ''
        thought_ids = list(out1.get('tokens') or [])
        t1 = out1.get('timings') or {}
        thought_tokens = int(t1.get('predicted_n') or out1.get('tokens_predicted') or 0)
        think_finish = 'stop' if (out1.get('stopping_word') or '') == '</think>' else 'length'
        if think_finish == 'length':
            log(f'   модель: мысль закрыта по пределу {think_limit} ток.')

    # ── Фаза 2: ответ ──────────────────────────────────────────────────────
    if think_open:
        tail = THINK_CLOSE
        if prompt_ids and thought_ids:
            try:
                answer_prompt = prompt_ids + thought_ids + _tokenize(tail)
            except Exception:                      # noqa: BLE001
                answer_prompt = text + thought + tail
        else:
            answer_prompt = text + thought + tail
    else:
        answer_prompt = prompt_ids if prompt_ids else text
    body = {
        'prompt': answer_prompt,
        'n_predict': max(min(limit, 256), limit - thought_tokens),
        'temperature': temperature,
        'cache_prompt': True,
        'stop': ['<|im_end|>'],
    }
    if answer_grammar:
        body['grammar'] = answer_grammar
    out = _completion(body, timeout)
    spent = time.time() - started

    content = out.get('content') or ''
    answer = (think_open + thought + THINK_CLOSE + content) if think_open else content
    timings = out.get('timings') or {}
    # Что сервер действительно считал — timings.prompt_n. tokens_evaluated,
    # вопреки имени, — это весь вопрос, а tokens_cached — размер кэша после
    # ответа: первая версия брала первое и печатала «7962 вход, 8690 из
    # кэша», вторая — второе и печатала «0 из кэша» при 3 000 из кэша.
    evaluated = int(timings.get('prompt_n') or out.get('tokens_evaluated') or 0)
    answer_tokens = int(timings.get('predicted_n') or out.get('tokens_predicted') or 0) + thought_tokens
    total = len(prompt_ids) if prompt_ids else evaluated
    cached = max(0, total - evaluated) if not think_open else max(0, total - (evaluated - min(evaluated, len(thought_ids) + 3)))
    finish = 'length' if out.get('truncated') or (out.get('stop_type') == 'limit') else 'stop'
    stats = {
        'prompt_tokens': total,
        'answer_tokens': answer_tokens,
        'thought_tokens': thought_tokens,
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
    log(f'   модель: {stats["prompt_tokens"]} вход ({cached} из кэша), {answer_tokens} выход'
        + (f' (мысль {thought_tokens}{", по пределу" if think_finish == "length" else ""})' if think_open else '')
        + f' за {spent:.1f} с = {stats["tok_s"]} ток/с'
        + (f', черновик принят {stats["draft_accepted"]}/{stats["draft_n"]}' if stats['draft_n'] else '')
        + (' — упёрлось в предел' if finish == 'length' else ''))
    return answer, stats
