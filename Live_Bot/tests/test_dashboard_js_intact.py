"""
Каждая вызываемая функция панели обязана существовать.

ПОЧЕМУ ЭТА ПРОВЕРКА ПОЯВИЛАСЬ. Вырезая из панели раздел, я удалил вместе с ним
две вспомогательные функции — `pmMoney` и `renderErrors`, — а вызовы их
остались в сводке и в разделе ошибок.

Браузер на таком вызове бросает ReferenceError и прекращает выполнение всего
скрипта. Снаружи это выглядит как «приложение запущено, но там ничего не
работает»: сервер отвечает, бот торгует, страница пуста.

570 тестов при этом проходили. Разметку они проверяли, названия разделов
проверяли, а того, что страница вообще исполнится, — нет.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HTML = open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()

# Ключевые слова языка, встроенные имена и функции CSS: они пишутся как вызов,
# но объявлять их нам не нужно.
NOT_OURS = {
    'async', 'await', 'catch', 'return', 'typeof', 'new', 'delete', 'void',
    'if', 'for', 'while', 'switch', 'function', 'class', 'super', 'import',
    'isNaN', 'isFinite', 'parseInt', 'parseFloat', 'encodeURIComponent',
    'decodeURIComponent', 'setTimeout', 'setInterval', 'clearTimeout',
    'clearInterval', 'fetch', 'alert', 'confirm', 'prompt', 'structuredClone',
    'requestAnimationFrame', 'queueMicrotask',
    # CSS: cubic-bezier, linear-gradient, translateX, scale, blur и прочее
    'bezier', 'gradient', 'translateX', 'translateY', 'translate', 'scale',
    'rotate', 'blur', 'brightness', 'saturate', 'rgba', 'rgb', 'hsl', 'hsla',
    'media', 'minmax', 'repeat', 'calc', 'clamp', 'url', 'var', 'attr',
    'supports', 'keyframes',
}


def _declared():
    names = set(re.findall(r'function\s+([A-Za-z_$][\w$]*)\s*\(', HTML))
    names |= set(re.findall(r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=', HTML))
    # Методы объектов и параметры-функции объявляются иначе; их вызывают
    # через точку, а такие вызовы мы и не считаем.
    return names


def _called():
    # Только вызовы БЕЗ точки перед именем: `foo(` — наше, `a.foo(` — чужое.
    return set(re.findall(r'(?<![\w.$])([a-z][A-Za-z0-9_$]{3,})\s*\(', HTML))


class TestEveryCallHasItsFunction:

    def test_nothing_is_called_out_of_thin_air(self):
        """
        Ровно та ошибка, что сделала панель пустой: функция удалена вместе с
        разделом, а вызовы остались в другом.
        """
        missing = sorted(_called() - _declared() - NOT_OURS)
        assert not missing, f'вызывается, но не объявлено: {missing}'

    def test_the_helpers_that_broke_it_are_present(self):
        """Обе поимённо: они уже один раз пропали и уронили страницу целиком."""
        for name in ('pmMoney', 'renderErrors'):
            assert f'function {name}(' in HTML, name

    def test_the_page_renderers_are_whole(self):
        for name in ('renderSummary', 'buildNav', 'showPage'):
            assert f'function {name}(' in HTML, name


def code_only(script):
    """
    Оставляет от скрипта только код: строки, шаблоны, комментарии и литералы
    регулярных выражений заменяются пустышками.

    ЭТО СДЕЛАНО СКАНЕРОМ, А НЕ РЕГУЛЯРКОЙ, И ПРИЧИНА КОНКРЕТНАЯ. Регулярка
    снимает комментарии отдельным проходом и не знает, где строка: адрес
    'https://example' она обрывает на двойном слэше, после чего кавычка
    остаётся непарной и весь дальнейший подсчёт врёт. Разбирать вложенные
    языки надо по одному символу — здесь ровно это.
    """
    out, i, n = [], 0, len(script)
    prev = ''                    # последний значащий символ: по нему отличаем
    while i < n:                 # деление от начала регулярного выражения
        ch = script[i]

        if ch in '"\'':                                   # обычная строка
            i += 1
            while i < n and script[i] != ch:
                i += 2 if script[i] == '\\' else 1
            i += 1
            out.append('""')
            prev = '"'
        elif ch == '`':                                   # шаблон с ${...}
            i += 1
            depth = 0
            while i < n:
                if script[i] == '\\':
                    i += 2
                    continue
                if script[i] == '{' and script[i - 1] == '$':
                    depth += 1
                elif script[i] == '}' and depth:
                    depth -= 1
                elif script[i] == '`' and not depth:
                    break
                i += 1
            i += 1
            out.append('""')
            prev = '"'
        elif script.startswith('//', i):                  # комментарий строкой
            i = script.find('\n', i)
            i = n if i < 0 else i
        elif script.startswith('/*', i):                  # комментарий блоком
            i = script.find('*/', i)
            i = n if i < 0 else i + 2
        elif ch == '/' and prev not in '' and not (prev.isalnum()
                                                   or prev in ')]_$'):
            i += 1                                        # литерал /.../
            inside = False
            while i < n and (inside or script[i] != '/'):
                if script[i] == '\\':
                    i += 1
                elif script[i] == '[':
                    inside = True
                elif script[i] == ']':
                    inside = False
                elif script[i] == '\n':
                    break
                i += 1
            i += 1
            out.append('0')
            prev = '0'
        else:
            out.append(ch)
            if not ch.isspace():
                prev = ch
            i += 1
    return ''.join(out)


class TestTheScriptIsSyntacticallyWhole:
    """
    Вырезание кусками ломает не только имена. Незакрытая скобка обрывает
    скрипт так же молча, как и потерянная функция.
    """

    CODE = None

    @classmethod
    def setup_class(cls):
        script = HTML[HTML.index('<script'):HTML.rindex('</script>')]
        cls.CODE = code_only(script)

    def test_braces_balance(self):
        assert self.CODE.count('{') == self.CODE.count('}'), 'фигурные не сошлись'

    def test_parens_balance(self):
        assert self.CODE.count('(') == self.CODE.count(')'), 'круглые не сошлись'

    def test_brackets_balance(self):
        assert self.CODE.count('[') == self.CODE.count(']'), 'квадратные не сошлись'

    def test_nesting_never_goes_negative(self):
        """
        Равное число скобок ещё не значит верное: `} … {` сходится по счёту,
        но это уже другой код. Глубина обязана оставаться неотрицательной.
        """
        depth = 0
        for ch in self.CODE:
            depth += (ch == '{') - (ch == '}')
            assert depth >= 0, 'закрывающая скобка раньше открывающей'
        assert depth == 0

    def test_markup_tags_balance(self):
        for tag in ('div', 'section', 'script'):
            assert HTML.count(f'<{tag}') == HTML.count(f'</{tag}>'), tag
