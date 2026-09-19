"""
Подделка модели для дочернего процесса llm_worker в проверках.

Живёт отдельным модулем, потому что spawn импортирует цель по имени: функция
из тела проверки в другой процесс не переедет.
"""

import os
import time


def ask(prompt, grammar=None, max_tokens=None):
    if prompt == 'crash':
        os._exit(3)                                # abort нативного кода, как у llama.cpp
    if prompt == 'hang':
        time.sleep(30)
    if prompt == 'error':
        error = RuntimeError('окно контекста 4096 мало')
        error.llm_gate = 'окно контекста мало'
        raise error
    return f'ответ на {prompt!r} (pid {os.getpid()})'
