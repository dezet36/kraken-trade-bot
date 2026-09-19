"""
Модель в отдельном процессе: её падение не роняет бота.

ЗАЧЕМ. 19 сентября 2026 ошибка в грамматике (форма `(0, 2)` вместо `{0,2}`)
роняла llama.cpp на каждом разборе. Это не исключение Python — это abort в
нативном коде, и он убивает ВЕСЬ процесс: поток «сбоку» здесь не защита. Бот
перезапускался systemd раз в десять минут, панель между перезапусками
отвечала «нет связи», а четыре другие стратегии теряли по циклу на каждом
падении.

КАК. Модель живёт в дочернем процессе; бот шлёт ему вопрос по трубе и ждёт
ответ с пределом времени. Умер дочерний — бот получает именованный отказ
«модель упала» и в следующий раз поднимает новый. Завис — убивается по
пределу, отказ «модель зависла». Торговля остальных стратегий этого не
замечает вовсе.

ЦЕНА. Загрузка модели — в дочернем процессе, один раз на его жизнь; файл
через mmap, и страницы остаются в кэше системы между перезапусками, поэтому
второй подъём быстрее первого. Обмен по трубе — килобайты текста, это
ничто рядом с десятью минутами счёта.

ПОЧЕМУ НЕ HTTP-СЕРВЕР. Тот же довод, что в llm_local: у бота уже была история
с двумя копиями и конфликтом портов. Труба между родителем и ребёнком не
имеет порта, не переживает родителя и не может быть занята кем-то ещё.
"""

import multiprocessing as mp
import os
import threading
import time

import config
from logger import log

# Сколько ждём ответа, прежде чем считать модель зависшей. Разбор с критиком
# укладывается в двадцать минут; тридцать — с запасом на загрузку модели.
CALL_TIMEOUT_SEC = int(os.getenv('LLM_CALL_TIMEOUT_MIN', 30)) * 60

_lock = threading.Lock()
_process = None
_conn = None
_impl = 'llm_local:ask_in_process'        # что зовём в дочернем процессе
_crashes = 0


def _resolve(dotted):
    module_name, func_name = dotted.split(':')
    module = __import__(module_name, fromlist=[func_name])
    return getattr(module, func_name)


def _serve(conn, impl):
    """
    Цикл дочернего процесса: вопрос — ответ, до закрытия трубы.

    Ошибка модели уходит родителю как данные, с именем отказа, а не как
    исключение: труба исключений не носит.
    """
    try:
        ask = _resolve(impl)
    except Exception as exc:                           # noqa: BLE001
        conn.send({'ok': False, 'error': f'модель не импортируется: {exc}',
                   'gate': 'модель недоступна'})
        return
    while True:
        try:
            request = conn.recv()
        except (EOFError, OSError):
            return
        if request is None:
            return
        try:
            answer = ask(request['prompt'], request.get('grammar'),
                         request.get('max_tokens'))
            stats = {}
            try:
                import llm_local
                stats = llm_local.last_stats()
            except Exception:                          # noqa: BLE001
                pass
            conn.send({'ok': True, 'answer': answer, 'stats': stats})
        except Exception as exc:                       # noqa: BLE001
            conn.send({'ok': False, 'error': str(exc)[:500],
                       'gate': getattr(exc, 'llm_gate', '')})


def _start():
    """Поднимает дочерний процесс. Под замком вызывающего."""
    global _process, _conn
    ctx = mp.get_context('spawn')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_serve, args=(child, _impl),
                          name='llm-worker', daemon=True)
    process.start()
    child.close()
    _process, _conn = process, parent
    log(f'🧠 модель: отдельный процесс запущен (pid {process.pid})')


def _stop(reason=''):
    global _process, _conn
    if _process is not None:
        try:
            if _conn is not None:
                _conn.send(None)
        except Exception:                              # noqa: BLE001
            pass
        try:
            _process.join(3)
            if _process.is_alive():
                _process.kill()
                _process.join(3)
        except Exception:                              # noqa: BLE001
            pass
        if reason:
            log(f'🧠 модель: процесс остановлен — {reason}')
    _process, _conn = None, None


def alive():
    with _lock:
        return _process is not None and _process.is_alive()


def ask(prompt, grammar=None, max_tokens=None, timeout=None):
    """
    Вопрос модели через дочерний процесс. Возвращает (ответ, статистика).

    Бросает RuntimeError с атрибутом llm_gate: «модель упала», «модель
    зависла» или то, что назвал сам дочерний процесс.
    """
    global _crashes
    timeout = timeout or CALL_TIMEOUT_SEC
    with _lock:
        if _process is None or not _process.is_alive():
            _stop()
            _start()
        conn, process = _conn, _process
        try:
            conn.send({'prompt': prompt, 'grammar': grammar, 'max_tokens': max_tokens})
        except Exception as exc:                       # noqa: BLE001
            _stop('труба не принимает вопрос')
            error = RuntimeError(f'модель упала до вопроса: {exc}')
            error.llm_gate = 'модель упала'
            raise error

        deadline = time.time() + timeout
        while True:
            if conn.poll(1.0):
                break
            if not process.is_alive():
                _crashes += 1
                _stop()
                error = RuntimeError(
                    f'процесс модели умер во время разбора (код {process.exitcode}); '
                    f'падений за запуск: {_crashes}')
                error.llm_gate = 'модель упала'
                raise error
            if time.time() > deadline:
                _stop('нет ответа дольше предела')
                error = RuntimeError(f'модель не ответила за {timeout // 60} мин')
                error.llm_gate = 'модель зависла'
                raise error
        try:
            reply = conn.recv()
        except (EOFError, OSError) as exc:
            _crashes += 1
            _stop()
            error = RuntimeError(f'процесс модели оборвал ответ: {exc}')
            error.llm_gate = 'модель упала'
            raise error

    if not reply.get('ok'):
        error = RuntimeError(reply.get('error') or 'модель не ответила')
        error.llm_gate = reply.get('gate') or 'модель недоступна'
        raise error
    return reply['answer'], reply.get('stats') or {}


def stop():
    """Останавливает дочерний процесс — при выключении бота и в проверках."""
    with _lock:
        _stop('остановка')


def crashes():
    return _crashes
