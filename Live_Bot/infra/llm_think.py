"""
Блок размышления в грамматике ответа и две фазы вопроса — протокол сервиса
модели (этап 10: вынесен из strategies/llm/llm_grammar).

Грамматика с мыслью имеет вид «root ::= think answer | answer». Сервис модели
(llm_server) делит вопрос на две фазы: мысль — без грамматики с пределом в
токенах, ответ — по грамматике без блока мысли. Здесь — только эта механика;
что именно модель думает (первые слова мысли, THINK_SEED) — промт стратегии,
его передаёт вызывающий.
"""


def with_thinking(grammar, think_chars):
    """
    Блок размышления перед ответом: root ::= think answer | answer.

    Внутри мысли — любой текст, кроме последовательности «</think>»; предел
    в знаках — бюджет. Без него грамматика требует JSON с первого токена, и
    режим размышления Qwen3 был несовместим с ней.
    """
    if not think_chars or think_chars <= 0:
        return grammar
    head = 'root     ::='
    assert grammar.startswith(head)
    think = ('root     ::= think answer | answer' + chr(10)
             + 'think    ::= "<think>" tchar{0,' + str(int(think_chars)) + '} "</think>" nl' + chr(10)
             + 'tchar    ::= [^<] | "<" [^/]' + chr(10)
             + 'nl       ::= [ ' + chr(92) + 'n]{0,4}' + chr(10))
    return think + 'answer   ::=' + grammar[len(head):]


THINK_OPEN = '<think>' + chr(10)
_ROOT_OPTIONAL = 'root     ::= think answer | answer'
_THINK_CLOSED = 'think    ::= "<think>" tchar{0,'


def open_thinking(grammar, seed=''):
    """
    Переносит открывающий «<think>» из грамматики в подсказку.

    -> (грамматика без «<think>», хвост подсказки) или (grammar, '') если
    блока мысли в ней нет. seed — первые слова мысли, которые модель
    продолжит (llm_prompt.THINK_SEED); в бюджет tchar они не входят.

    ЗАЧЕМ. llama-server с MTP-черновиком и грамматикой на границе «<think>»
    принимает от черновика «\n\n</think>» — мысль выходит пустой в 100%
    ответов (21.09.2026: 23 разбора подряд, 0 знаков; без черновика или
    без грамматики модель думает). Родной шаблон Qwen в режиме
    размышления сам открывает «<think>\n» в подсказке — делаем так же:
    модель начинает уже внутри мысли, грамматика требует только закрыть её.
    Ответ модели после этого не содержит «<think>», вызывающий приписывает
    THINK_OPEN обратно, чтобы split_thought и журнал ничего не заметили.
    """
    if _ROOT_OPTIONAL not in grammar or _THINK_CLOSED not in grammar:
        return grammar, ''
    grammar = grammar.replace(_ROOT_OPTIONAL, 'root     ::= think answer', 1)
    grammar = grammar.replace(_THINK_CLOSED, 'think    ::= tchar{0,', 1)
    return grammar, THINK_OPEN + (seed or '')


def answer_only(grammar):
    """
    Грамматика без блока мысли: root ::= answer. Для второй фазы вопроса
    в llama-server, когда мысль уже написана и в подсказке закрыта
    «</think>». Без блока мысли — как есть.
    """
    if _ROOT_OPTIONAL not in grammar:
        return grammar
    grammar = grammar.replace(_ROOT_OPTIONAL, 'root     ::= answer', 1)
    keep = [ln for ln in grammar.split(chr(10))
            if not ln.startswith(('think    ::=', 'tchar    ::=', 'nl       ::='))]
    return chr(10).join(keep)
