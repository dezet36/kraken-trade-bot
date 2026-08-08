"""
На графике SMC видно не только ГДЕ вход, но и ПОЧЕМУ эта сторона.

ЗАЧЕМ ЭТИ ПРОВЕРКИ. Разметка сделки собирается из полей сигнала, и связь эта
держится на совпадении имён: ядро кладёт `structure`, стратегия перекладывает в
`structure_level`, брокер читает его при сборке графика. Порвись любое звено —
ничего не упадёт, просто на картинке молча станет одной линией меньше. Ровно так
и вышло с ордер-блоком: поля в сигнале были, разметка не собиралась, и заметили
это спустя недели по жалобе «не вижу, от чего строилась структура».

Пробитый структурный уровень и снятая ликвидность — это и есть ответ на вопрос
«почему вообще лонг». Ордер-блок отвечает только на «где вход».
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import glossary  # noqa: E402
from paper_broker import PaperBroker  # noqa: E402


def _signal(**smc):
    base = {
        'poi_type': 'ORDER_BLOCK', 'poi_top': 0.1912, 'poi_bottom': 0.1899,
        'fvg_top': 0.1925, 'fvg_bottom': 0.1918,
        'structure_type': 'CHOCH', 'structure_level': 0.1886,
        'sweep_side': 'SSL', 'sweep_price': 0.1874,
    }
    base.update(smc)
    return {
        'setup': {'type': 'LONG', 'start_price': 0.180, 'end_price': 0.195,
                  'start_time': '2026-08-05T04:00:00Z',
                  'end_time': '2026-08-05T11:00:00Z'},
        'smc': base,
    }


def _labels(geometry, key):
    return [item.get('label', '') for item in geometry.get(key) or []]


def test_order_block_is_the_main_zone():
    """Ордер-блок рисуется выделенной зоной: в него и входим."""
    geo = PaperBroker._geometry('SMC', _signal())
    main = [b for b in geo['bands'] if b['main']]
    assert len(main) == 1
    assert main[0]['label'] == 'ордер-блок'
    assert main[0]['bottom'] == 0.1899 and main[0]['top'] == 0.1912


def test_imbalance_is_drawn_but_not_as_the_entry_zone():
    """Имбаланс — контекст отбора, а не место входа, и выделяться не должен."""
    geo = PaperBroker._geometry('SMC', _signal())
    fvg = [b for b in geo['bands'] if 'имбаланс' in b['label']]
    assert len(fvg) == 1
    assert fvg[0]['main'] is False


def test_structure_break_reaches_the_chart():
    """Пробитый уровень попадает на график с человеческой подписью."""
    geo = PaperBroker._geometry('SMC', _signal())
    hits = [ln for ln in geo['lines'] if ln['price'] == 0.1886]
    assert hits, 'уровень слома структуры не дошёл до разметки'
    assert 'CHoCH' in hits[0]['label']


def test_swept_liquidity_reaches_the_chart():
    """Снятая ликвидность тоже: без неё непонятно, что вынесли перед входом."""
    geo = PaperBroker._geometry('SMC', _signal())
    hits = [ln for ln in geo['lines'] if ln['price'] == 0.1874]
    assert hits, 'снятая ликвидность не дошла до разметки'
    assert 'минимум' in hits[0]['label']


def test_missing_structure_does_not_invent_lines():
    """
    Сетап без слома и без снятия рисуется без этих линий, а не с нулевыми.

    Ноль — это цена, и линия на нуле сжала бы весь график в полоску у верхнего
    края. Проверка дешёвая, а ошибка такого рода делает картинку нечитаемой
    целиком, а не портит одну деталь.
    """
    geo = PaperBroker._geometry('SMC', _signal(
        structure_type=None, structure_level=None,
        sweep_side=None, sweep_price=None))
    prices = [ln['price'] for ln in geo['lines']]
    assert 0 not in prices
    assert all(p > 0 for p in prices)
    # Импульс остаётся: он не зависит от структуры.
    assert len(geo['lines']) == 2


def test_terms_are_translated_not_left_as_jargon():
    """Подписи читаются без знания жаргона, аббревиатура остаётся в скобках."""
    assert glossary.structure_event('BOS') == 'слом структуры (BOS)'
    assert glossary.structure_event('CHOCH') == 'смена характера (CHoCH)'
    assert glossary.liquidity_side('BSL') == 'ликвидность над максимумами'
    assert glossary.liquidity_side('SSL') == 'ликвидность под минимумами'
    # Незнакомое имя возвращается как есть, а не превращается в прочерк:
    # молча потерянный тип хуже непереведённого.
    assert glossary.structure_event('WHAT') == 'WHAT'


def test_sweep_price_is_read_from_the_field_the_core_actually_fills():
    """
    Цена снятия берётся из `level`, а не из `price`.

    ЭТА ПРОВЕРКА ЕСТЬ ПОТОМУ, ЧТО ОШИБКА УЖЕ БЫЛА СДЕЛАНА. Снятие ликвидности
    несёт и `level`, и вложенный `pool` со своим `price`. Обращение к `price`
    на верхнем уровне возвращает None молча: сторона в подписи есть, линии нет.
    Тест на выдуманном словаре этого не ловил — он проверял форму, которую сам
    же и придумал. Поэтому здесь пиннится ИСХОДНИК ядра: пока в нём есть
    'level', стратегия обязана читать именно его.
    """
    core = os.path.join(ROOT, 'smc', 'liquidity.py')
    with open(core, encoding='utf-8') as handle:
        text = handle.read()
    assert "'level': level," in text, 'ядро больше не кладёт level — проверить связь'
    assert "'extreme': float(extreme)," in text

    adapter = os.path.join(ROOT, 'strategy_smc.py')
    with open(adapter, encoding='utf-8') as handle:
        text = handle.read()
    assert "'sweep_price': (setup.get('sweep') or {}).get('level')" in text
    assert "'sweep_price': (setup.get('sweep') or {}).get('price')" not in text


def test_both_chart_surfaces_share_one_geometry():
    """
    Картинка для Telegram берёт разметку из общего модуля, а не свою.

    Две реализации одной разметки разошлись бы при первой правке, и на двух
    графиках одной сделки оказались бы разные зоны. В этом проекте такое уже
    случалось у стратегии уровней и стоило месяца недостоверных наблюдений.
    """
    import setup_geometry

    broker = os.path.join(ROOT, 'paper_broker.py')
    with open(broker, encoding='utf-8') as handle:
        assert 'setup_geometry.build(strategy, signal)' in handle.read()

    png = os.path.join(ROOT, 'chart_generator.py')
    with open(png, encoding='utf-8') as handle:
        text = handle.read()
    assert 'setup_geometry.build(' in text

    # Модуль отдаёт одно и то же обоим — проверяем на живом вызове.
    geo = setup_geometry.build('SMC', _signal())
    assert geo == PaperBroker._geometry('SMC', _signal())


def test_telegram_chart_recognises_every_strategy():
    """
    Имя стратегии выводится по разделу сигнала: в самом сигнале его нет.

    Ошибиться здесь означало бы собрать разметку не той стратегии — молча и
    правдоподобно, потому что форма разметки у всех одинаковая.
    """
    import chart_generator as cg

    assert cg._strategy_of({'smc': {'poi_top': 1}}) == 'SMC'
    assert cg._strategy_of({'levels': {'level': 1}}) == 'LEVELS'
    assert cg._strategy_of({'rsibb': {'upper': 1}}) == 'RSIBB'
    assert cg._strategy_of({'zone_a': {'top': 1}}) == 'FIBO'
    # Незнакомый сигнал не должен выдавать себя за чужую стратегию: пустая
    # строка даёт пустую разметку, то есть прежний график без зон.
    assert cg._strategy_of({'params': {}}) == ''
    assert setup_geometry_bands_empty()


def setup_geometry_bands_empty():
    """Пустое имя стратегии даёт пустую разметку, а не падение."""
    import setup_geometry
    geo = setup_geometry.build('', {'setup': {}})
    return not geo['bands'] and not geo['lines']


def test_core_carries_the_structure_event_into_the_setup():
    """
    Ядро отдаёт событие слома в сетап, а не гасит его внутри подсчёта.

    Проверяется по исходнику: собрать полноценный контекст SMC в тесте дорого,
    а сломать связь можно одной правкой возвращаемого кортежа.
    """
    path = os.path.join(ROOT, 'smc', 'signal.py')
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    assert "'structure': structure_break," in text
    assert 'return factors, score, recent_break' in text
    assert 'factors, score, structure_break = self._confluence(' in text
