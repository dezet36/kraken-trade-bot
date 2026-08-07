"""
Описания стратегий на дашборде не должны расходиться с кодом.

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
    Разметка дашборда.

    Путь считается ЗДЕСЬ, а не константой модуля, и это не стилистика.
    conftest уводит во временный каталог любую заглавную строковую константу,
    указывающую на боевой Live_Bot, — так он ловит утечки записи в настоящие
    данные. Константа PAGE попадала под то же правило, и тест искал страницу
    во временной папке. Защита права; подстраивается тест.
    """
    page = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'dashboard.html')
    return open(page, encoding='utf-8').read()


def guide_block(key):
    """Кусок описания одной стратегии из объекта GUIDE."""
    text = page_text()
    start = text.index('const GUIDE = {')
    block = text[start:text.index('\nconst PERIODS', start)]
    at = block.index(f'{key}: {{')
    # Следующая стратегия начинается со своего ключа в начале строки.
    rest = block[at:]
    nxt = re.search(r'\n  [A-Z]+: \{', rest[1:])
    return rest[:nxt.start() + 1] if nxt else rest


class TestEveryStrategyIsDescribed:
    def test_guide_covers_all_trading_strategies(self):
        import sys
        sys.path.insert(0, BOT)
        import settings_store

        text = page_text()
        for name in settings_store.STRATEGIES:
            assert f'{name}: {{' in text, (
                f'у стратегии {name} нет описания в GUIDE. Кнопка «?» на её '
                f'карточке не появится, и человек решит, что описаний нет.')

    def test_each_guide_has_all_four_parts(self):
        import sys
        sys.path.insert(0, BOT)
        import settings_store

        for name in settings_store.STRATEGIES:
            block = guide_block(name)
            for field in ('tf:', 'lead:', 'steps:', 'fact:'):
                assert field in block, f'{name}: нет поля {field}'
            # Четыре шага: сетап, вход, стоп/выход — меньше означает, что
            # описание не отвечает на вопрос «где точки входа и выхода».
            assert block.count("['") >= 3, f'{name}: слишком мало шагов'


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
        from rsibb import params

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
        from levels import params

        block = guide_block('LEVELS')
        assert_quoted(block, params.MIN_TOUCHES, 'касаний для уровня')
        assert_quoted(block, params.RECLAIM_BARS, 'свечей на возврат')
        assert_quoted(block, params.VOLUME_RATIO, 'объём на возврате')
        assert_quoted(block, params.MIN_TARGET_R, 'минимальная цель')
        assert_quoted(block, params.PIERCE_ATR, 'глубина прокола')

    def test_fibo_numbers(self):
        import sys
        sys.path.insert(0, BOT)
        import config

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
        from smc import params

        block = guide_block('SMC')
        assert_quoted(block, params.MIN_CONFLUENCE_SCORE, 'вес подтверждений')
        assert_quoted(block, params.MIN_RR, 'минимальное отношение к риску')


class TestWindowBehaves:
    """
    Окно должно закрываться тремя способами. Открытое случайно и не
    закрывающееся окно чинится только перезагрузкой страницы.
    """

    def test_all_three_ways_to_close_exist(self):
        text = page_text()
        assert 'sheet-close' in text, 'нет крестика'
        assert "event.key === 'Escape'" in text, 'не закрывается по Esc'
        assert 'event.target === back' in text, 'не закрывается щелчком по фону'

    def test_focus_returns_to_the_opener(self):
        """Иначе читающий с клавиатуры после закрытия окажется в начале страницы."""
        text = page_text()
        assert 'GUIDE_OPENER' in text and 'GUIDE_OPENER.focus()' in text

    def test_dialog_is_marked_for_screen_readers(self):
        text = page_text()
        assert 'role="dialog"' in text and 'aria-modal="true"' in text

    def test_body_scrolls_only_vertically(self):
        """
        Горизонтальная перемотка внутри окна — то же неудобство, которое уже
        вычищали со страницы целиком.
        """
        text = page_text()
        assert 'overflow-x: hidden' in text.split('.sheet-body')[1][:120]
