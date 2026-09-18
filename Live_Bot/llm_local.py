"""
Локальная модель: загрузка один раз, вызов с грамматикой, отсутствие — не сбой.

В ПРОЦЕССЕ, А НЕ ОТДЕЛЬНЫМ СЕРВЕРОМ. Напрашивалось поднять Ollama или
llama-server и ходить в него по HTTP. Так делать здесь нельзя: у этого бота
уже был тупик с конфликтом портов и история с двумя одновременно работавшими
копиями, забиравшими одни сигналы дважды. Второй демон с портом — те же грабли
в третий раз. llama_cpp загружается прямо в процесс бота.

ОТСУТСТВИЕ МОДЕЛИ — ЗАКОННОЕ СОСТОЯНИЕ, А НЕ ПОЛОМКА. Разработка идёт на
машине, где ни файла модели, ни собранной llama_cpp нет и не будет: там восемь
гигабайт памяти и видеокарта 2011 года. Бот обязан работать там как прежде,
просто без пятой стратегии. Поэтому импорт ленивый, а available() отвечает
честным «нет» вместо исключения.

ЗАГРУЗКА ОДИН РАЗ. Файл на пять гигабайт читается с диска сорок секунд.
Загружать его на каждый сетап — значит тратить эти сорок секунд по десять раз
в день и держать процессор занятым тогда, когда он нужен торговому циклу.

ЗАМОК НА ВЫЗОВЕ. Наблюдение после выхода ходит в своём потоке, и однажды оно
уже ломало общий код неожиданным вторым входом. llama_cpp на параллельные
вызовы одного контекста не рассчитан: второй вызов испортил бы состояние
первого молча, без исключения.
"""

import os
import threading
import time

import config
from logger import log

_model = None
_lock = threading.Lock()
_failed = False          # уже пробовали и не смогли — второй раз не пытаемся


def model_path():
    """Путь к файлу модели. Пусто — стратегия выключена."""
    return (getattr(config, 'LLM_MODEL_PATH', '') or '').strip()


def available():
    """
    Можно ли вообще спрашивать модель.

    Проверяется ДО сборки разметки: незачем считать уровни и грамматику для
    вызова, которого не будет.
    """
    if _failed or not model_path():
        return False
    return os.path.exists(model_path())


def _load():
    """Загружает модель. Возвращает её или None, если не вышло."""
    global _model, _failed
    if _model is not None:
        return _model
    if _failed or not available():
        return None
    try:
        from llama_cpp import Llama
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ llama_cpp недоступна — стратегия LLM выключена ({exc})')
        _failed = True
        return None

    started = time.time()
    try:
        _model = Llama(model_path=model_path(),
                       n_ctx=config.LLM_CTX,
                       n_threads=config.LLM_THREADS,
                       verbose=False)
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ Модель не загрузилась — стратегия LLM выключена ({exc})')
        _failed = True
        return None
    log(f'🧠 Модель загружена за {time.time() - started:.1f} с: '
        f'{os.path.basename(model_path())}, потоков {config.LLM_THREADS}')
    return _model


def ask(prompt, grammar=None, max_tokens=None):
    """
    Один вопрос модели. Возвращает текст ответа.

    Бросает RuntimeError, если модель недоступна: вызывающий (llm_decide)
    ловит это и превращает в именованный отказ. Молча возвращать пустоту
    нельзя — пустой ответ неотличим от отказа модели входить.

    grammar — текст GBNF. Без неё модель вольна ответить чем угодно, и это
    допустимо только при отладке.
    """
    llm = _load()
    if llm is None:
        raise RuntimeError('модель недоступна')

    limit = max_tokens or config.LLM_MAX_TOKENS
    compiled = None
    if grammar:
        from llama_cpp import LlamaGrammar
        compiled = LlamaGrammar.from_string(grammar, verbose=False)

    # /no_think выключает режим размышления Qwen3. На процессоре без
    # видеокарты он стоит минут: шестьсот токенов рассуждения при трёх токенах
    # в секунду — это три минуты на один сетап. Проверки в llm_decide всё
    # равно пересчитывают за моделью, поэтому длинная цепочка рассуждений тут
    # окупается хуже, чем время, которое она отнимает у торгового цикла.
    text = prompt + ('\n' + config.LLM_THINK_TAG if config.LLM_THINK_TAG else '')

    with _lock:
        started = time.time()
        out = llm.create_chat_completion(
            messages=[{'role': 'user', 'content': text}],
            grammar=compiled,
            max_tokens=limit,
            temperature=config.LLM_TEMPERATURE,
        )
        spent = time.time() - started

    answer = (out.get('choices') or [{}])[0].get('message', {}).get('content', '')
    usage = out.get('usage') or {}
    log(f'   модель: {usage.get("prompt_tokens", 0)} вход, '
        f'{usage.get("completion_tokens", 0)} выход за {spent:.1f} с')
    return answer


def last_stats():
    """Заглушка для будущего: счётчики вызовов уходят в журнал сделки."""
    return {'model': os.path.basename(model_path()) if model_path() else ''}


def unload():
    """Освобождает память. Нужен при остановке и в проверках."""
    global _model, _failed
    with _lock:
        _model = None
        _failed = False
