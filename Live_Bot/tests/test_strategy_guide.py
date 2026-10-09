"""
Описания стратегий на панели не должны расходиться с кодом.

С 09.10.2026 описания — control/panel/js/guide.js (лист «Как торгует» на
странице стратегии), до того — объект GUIDE в dashboard.html.

ЗАЧЕМ. Описание, разошедшееся с кодом, хуже отсутствующего: по нему принимают
решения, а проверить его нечем. Человек читает «вход на 50% отката», меняет
параметр на 0.618 и месяц удивляется, почему сделок стало больше.

Числа в описаниях взяты из фактических параметров, и здесь проверяется, что
они там и остались. Проверяются не все — только те, что определяют сетап и
названы в описании цифрой: их изменение меняет смысл текста.

Второе, что проверяется, — полнота: у каждой торгующей стратегии описание
должно быть. Четвёртая стратегия, добавленная без описания, оставила бы на
карточке кнопку, открывающую пустоту, — или, хуже, не оставила бы ничего, и
человек решил бы, что описаний в приложении нет вовсе.
"""

import os
import re

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def page_text():
    """
    Описания стратегий панели (js/guide.js).

    Путь считается ЗДЕСЬ, а не константой модуля, и это не стилистика.
    conftest уводит во временный каталог любую заглавную строковую константу,
    указывающую на боевой Live_Bot, — так он ловит утечки записи в настоящие
    данные. Константа PAGE попадала под то же правило, и тест искал страницу
    во временной папке. Защита права; подстраивается тест.
    """
    page = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'control', 'panel', 'js', 'guide.js')
    return open(page, encoding='utf-8').read()


def guide_block(key):
    """Кусок описания одной стратегии из объекта GUIDE."""
    text = page_text()
    start = text.index('export const GUIDE = {')
    block = text[start:]
    at = block.index(f'{key}: {{')
    # Следующая стратегия начинается со своего ключа в начале строки.
    rest = block[at:]
    nxt = re.search(r'\n  [A-Z]+: \{', rest[1:])
    return rest[:nxt.start() + 1] if nxt else rest


class TestEveryStrategyIsDescribed:
    def test_guide_covers_all_trading_strategies(self):
        import sys
        sys.path.insert(0, BOT)
        settings_store = __import__('importlib').import_module('accounts.settings_store')

        text = page_text()
        for name in settings_store.STRATEGIES:
            assert f'{name}: {{' in text, (
                f'у стратегии {name} нет описания в GUIDE. Кнопка «?» на её '
                f'карточке не появится, и человек решит, что описаний нет.')

    def test_each_guide_has_all_four_parts(self):
        import sys
        sys.path.insert(0, BOT)
        settings_store = __import__('importlib').import_module('accounts.settings_store')

        for name in settings_store.STRATEGIES:
            block = guide_block(name)
            for field in ('tf:', 'lead:', 'steps:', 'fact:'):
                assert field in block, f'{name}: нет поля {field}'
            # Четыре шага: сетап, вход, стоп/выход — меньше означает, что
            # описание не отвечает на вопрос «где точки входа и выхода».
            assert block.count("['") >= 3, f'{name}: слишком мало шагов'

    def test_every_guide_says_where_the_stop_is_and_that_it_does_not_move(self):
        """
        Правило проекта (CLAUDE.md, «Стоп»): стоп стоит там, где ломается
        идея, минимум — фильтр. Описание каждой стратегии обязано это
        говорить, иначе оператор ждёт «стоп на 0.8%» и не понимает отказов
        «стоп теснее минимума».
        """
        import sys
        sys.path.insert(0, BOT)
        settings_store = __import__('importlib').import_module('accounts.settings_store')
        for name in settings_store.STRATEGIES:
            block = guide_block(name)
            assert "['Где стоп'" in block, f'{name}: описание не говорит, где стоп'
            stop_row = block[block.index("['Где стоп'"):]
            stop_row = stop_row[:stop_row.index('`],') + 3]
            assert 'не берётся' in stop_row or 'отклоняется' in stop_row, (
                f'{name}: описание не говорит, что тесный стоп — отказ, а не сдвиг')


def quoted_numbers(block):
    """Числа, названные в описании как <code>…</code>, в виде чисел."""
    out = set()
    for raw in re.findall(r'<code>([\d.]+)%?</code>', block):
        try:
            out.add(float(raw))
        except ValueError:
            continue
    return out


def assert_quoted(block, value, what):
    """
    Сравнение ЧИСЛЕННОЕ, а не строковое.

    Первая версия сверяла подстроки и споткнулась о `0.30` в описании против
    `0.3` из Python. Расхождением это не было — описание правильное, придирался
    тест.
    """
    numbers = quoted_numbers(block)
    assert any(abs(n - float(value)) < 1e-9 for n in numbers), (
        f'{what}: в коде {value}, а в описании названы {sorted(numbers)}')


class TestNumbersMatchTheCode:
    """
    Цифры в описании — те же, что в параметрах. Расхождение здесь означает,
    что человек читает про одну стратегию, а торгует другая.
    """

    def test_bollinger_numbers(self):
        import sys
        sys.path.insert(0, BOT)
        from strategies.rsibb import params

        block = guide_block('RSIBB')
        assert_quoted(block, params.BB_MULT, 'множитель полос')
        assert_quoted(block, params.BB_PERIOD, 'период полос')
        # Обратное прочтение RSI — суть стратегии, и порог назван словами.
        assert str(int(params.RSI_LOW)) in block
        assert params.RSI_MODE == 'divergence', (
            'описание объясняет ОБРАТНОЕ прочтение RSI, а в коде режим '
            f'{params.RSI_MODE}')

    def test_levels_numbers(self):
        import sys
        sys.path.insert(0, BOT)
        from strategies.levels import params

        block = guide_block('LEVELS')
        assert_quoted(block, params.MIN_TOUCHES, 'касаний для уровня')
        assert_quoted(block, params.RECLAIM_BARS, 'свечей на возврат')
        assert_quoted(block, params.VOLUME_RATIO, 'объём на возврате')
        assert_quoted(block, params.MIN_TARGET_R, 'минимальная цель')
        assert_quoted(block, params.PIERCE_ATR, 'глубина прокола')

    def test_fibo_numbers(self):
        import sys
        sys.path.insert(0, BOT)
        config = __import__('importlib').import_module('infra.config')

        block = guide_block('FIBO')
        assert_quoted(block, config.MAX_IMPULSE_CANDLES, 'длина импульса')
        assert_quoted(block, config.MIN_IMPULSE_VELOCITY, 'скорость импульса')
        assert_quoted(block, config.MIN_IMPULSE_PCT, 'размер импульса')
        # Глубина входа — то, ради чего гонялся отдельный замер.
        assert_quoted(block, round(config.ENTRY_RETRACE * 100),
                      'глубина входа')
        assert_quoted(block, round(config.TP1_LEVEL * 100), 'цель')

    def test_smc_numbers(self):
        import sys
        sys.path.insert(0, BOT)
        from strategies.smc import params

        block = guide_block('SMC')
        assert_quoted(block, params.MIN_CONFLUENCE_SCORE, 'вес подтверждений')
        assert_quoted(block, params.MIN_RR, 'минимальное отношение к риску')


class TestSheetBehaves:
    """
    Лист «Как торгует» (ui.openSheet) закрывается тремя способами: крестиком,
    Esc и щелчком по фону. Открытое случайно и не закрывающееся окно чинится
    только перезагрузкой страницы.
    """

    UI = open(os.path.join(BOT, 'control', 'panel', 'js', 'ui.js'), encoding='utf-8').read()
    CSS = open(os.path.join(BOT, 'control', 'panel', 'app.css'), encoding='utf-8').read()

    def test_all_three_ways_to_close_exist(self):
        assert "class: 'sheet-x'" in self.UI, 'нет крестика'
        assert "addEventListener('cancel'" in self.UI, 'не закрывается по Esc'
        assert 'e.target === dlg' in self.UI, 'не закрывается щелчком по фону'

    def test_focus_returns_to_the_opener(self):
        """Модальный <dialog> сам возвращает фокус туда, откуда его открыли."""
        assert "h('dialog'" in self.UI and 'showModal()' in self.UI and 'dlg.close()' in self.UI

    def test_body_scrolls_only_vertically(self):
        """Горизонтальная перемотка внутри листа — то же неудобство, что на странице."""
        assert 'overflow-x: hidden' in self.CSS.split('.sheet-body')[1][:160]

    def test_the_strategy_page_opens_it(self):
        page = open(os.path.join(BOT, 'control', 'panel', 'js', 'pages', 'strategies.js'), encoding='utf-8').read()
        assert "button('Как торгует'" in page and 'GUIDE[code]' in page
