"""
Имя зоны входа считается по месту лимита, а не объявляется литералом.

ОТКУДА ЭТО. Разбор 193 сделок FIBO с сервера за 5–29 августа 2026: зона B
нарисована на ВСЕХ графиках, а входов в ней ноль. Выглядело как свойство
рынка — «туда цена не доходит». На деле в сигнале стояло

    'trigger': {'zone': 'Zone_A', ...}

литералом, и зона B не могла появиться никогда.

Мёртвыми от этого были сразу трое:

  * статистика в trade_manager отбирает сделки по `zone == 'Zone_B'` — список
    не наполнялся ни разу;
  * Telegram показывал по зоне значок и описание, и ветка 🅱️ была
    недостижима, а `else` выдавала «глубокая коррекция 78.6–88.6%» за любой
    вход, зоной B не являющийся;
  * панель обещала сравнение зоны A с зоной B, которого не существовало.

ПОВЕДЕНИЕ НЕ МЕНЯЕТСЯ. Имя зоны нигде не решает, торговать ли и по какой
цене: место входа задаёт ENTRY_RETRACE. При значении по умолчанию 0.5 вход
попадает в зону A — то есть литерал совпадал с истиной, но по неверной
причине. Поставив 0.8, человек окажется в зоне B, и теперь отчёты об этом
скажут.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import strategy                                            # noqa: E402


def _zones(kind):
    start, end = (100.0, 200.0) if kind == 'LONG' else (200.0, 100.0)
    setup = {'type': kind, 'start_price': start, 'end_price': end,
             'size': abs(end - start)}
    return setup, *strategy.get_zones(setup)


def _price_at(setup, depth):
    """Цена на глубине отката `depth` от конца импульса."""
    if setup['type'] == 'LONG':
        return setup['end_price'] - setup['size'] * depth
    return setup['end_price'] + setup['size'] * depth


class TestTheNameFollowsThePrice:

    def test_the_default_depth_lands_in_zone_a(self):
        """
        ENTRY_RETRACE = 0.5, зона A это откат 38.2–61.8%. Совпадение с прежним
        литералом — и оно объясняет, почему подмена так долго не замечалась.
        """
        import config
        for kind in ('LONG', 'SHORT'):
            setup, za, zb = _zones(kind)
            price = _price_at(setup, config.ENTRY_RETRACE)
            assert strategy.entry_zone_name(price, za, zb) == 'Zone_A', kind

    def test_a_deep_retrace_is_zone_b(self):
        """РАДИ ЭТОГО ВСЁ: зона B наконец достижима."""
        for kind in ('LONG', 'SHORT'):
            setup, za, zb = _zones(kind)
            assert strategy.entry_zone_name(_price_at(setup, 0.83), za, zb) == 'Zone_B', kind

    def test_between_the_zones_is_neither(self):
        """
        Откат 70% — глубже зоны A, мельче зоны B. Прежняя ветка `else` в
        Telegram называла такое зоной B и врала.
        """
        for kind in ('LONG', 'SHORT'):
            setup, za, zb = _zones(kind)
            assert strategy.entry_zone_name(_price_at(setup, 0.70), za, zb) == 'Zone_MID', kind

    def test_shallower_than_zone_a_is_neither(self):
        for kind in ('LONG', 'SHORT'):
            setup, za, zb = _zones(kind)
            assert strategy.entry_zone_name(_price_at(setup, 0.30), za, zb) == 'Zone_MID', kind

    def test_beyond_zone_b_is_neither(self):
        for kind in ('LONG', 'SHORT'):
            setup, za, zb = _zones(kind)
            assert strategy.entry_zone_name(_price_at(setup, 0.95), za, zb) == 'Zone_MID', kind

    def test_the_borders_belong_to_their_zone(self):
        for kind in ('LONG', 'SHORT'):
            setup, za, zb = _zones(kind)
            for zone in (za, zb):
                for edge in (zone['bottom'], zone['top']):
                    assert strategy.entry_zone_name(edge, za, zb) == zone['name'], (kind, edge)

    def test_long_and_short_are_symmetric(self):
        """Зеркальные сетапы обязаны называться одинаково на равной глубине."""
        for depth in (0.30, 0.50, 0.70, 0.83, 0.95):
            names = set()
            for kind in ('LONG', 'SHORT'):
                setup, za, zb = _zones(kind)
                names.add(strategy.entry_zone_name(_price_at(setup, depth), za, zb))
            assert len(names) == 1, (depth, names)

    def test_a_missing_zone_is_not_a_crash(self):
        setup, za, zb = _zones('LONG')
        assert strategy.entry_zone_name(_price_at(setup, 0.5), za, None) == 'Zone_A'
        assert strategy.entry_zone_name(_price_at(setup, 0.83), None, None) == 'Zone_MID'


class TestTheSignalStoppedDeclaringIt:

    SRC = open(os.path.join(ROOT, 'strategy.py'), encoding='utf-8').read()

    @staticmethod
    def _code_only():
        """
        Только код, без описаний и комментариев.

        Иначе проверка ловит СОБСТВЕННОЕ объяснение: в docstring у
        entry_zone_name прежний литерал приведён дословно — там ему и место,
        он объясняет, что именно было не так.
        """
        import re
        src = open(os.path.join(ROOT, 'strategy.py'), encoding='utf-8').read()
        src = re.sub(r'"""[\s\S]*?"""', '', src)
        return '\n'.join(l for l in src.splitlines()
                         if not l.strip().startswith('#'))

    def test_the_literal_is_gone(self):
        """РОВНО ТОТ ДЕФЕКТ."""
        assert "'zone': 'Zone_A'" not in self._code_only(), (
            'зона снова объявляется литералом — статистика по зоне B опять '
            'не сможет наполниться')

    def test_the_signal_computes_it(self):
        spot = self.SRC.index("'entry_type': 'ZONE_LIMIT'")
        assert 'entry_zone_name(' in self.SRC[max(0, spot - 300):spot]


class TestTelegramStoppedGuessing:

    SRC = open(os.path.join(ROOT, 'telegram_notify.py'), encoding='utf-8').read()

    def test_the_icon_has_a_third_case(self):
        """
        Было «A или B»: всё, что не A, получало 🅱️. Теперь между зонами свой
        значок.
        """
        assert '"🅰️" if trigger["zone"] == "Zone_A" else "🅱️"' not in self.SRC
        assert '"Zone_A": "🅰️"' in self.SRC and '"Zone_B": "🅱️"' in self.SRC

    def test_the_description_names_zone_b_only_for_zone_b(self):
        spot = self.SRC.index('Глубокая коррекция')
        before = self.SRC[max(0, spot - 200):spot]
        assert "== 'Zone_B'" in before, (
            'описание зоны B снова показывается по ветке else — то есть всем, '
            'кто не зона A')

    def test_the_middle_shows_a_number_instead_of_a_name(self):
        assert 'между зонами' in self.SRC
