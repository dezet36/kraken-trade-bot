"""
Статистика считается по сделкам, прожитым целиком, — и не подгоняется.

ОТКУДА ЭТО. В журнале появилась колонка data_gap_min: сколько минут жизни
сделки прошло без свечей. Панель про неё не знала и считала винрейт вместе со
сделками, посчитанными по неполным данным.

НО ГЛАВНОЕ ЗДЕСЬ — ОШИБКА, КОТОРУЮ ЧУТЬ НЕ ЗАКРЕПИЛИ. Первая версия фильтра
считала годными только те сделки, у которых gap_min == 0, то есть выбрасывала
и «дыра была», и «неизвестно». Под второе попадают сделки СТАРШЕ самой колонки
— прожитые целиком, просто записанные раньше.

В нашем журнале таких оказалось ровно две, и обе убыточные. Винрейт от такого
фильтра поднимался с 45% до 56% — не потому, что стал честнее, а потому, что
из выборки пропали два минуса. Подгонка, которая льстит, опаснее шума.

Отсутствие отметки — не свидетельство чистоты, но и не свидетельство порчи.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dashboard                                            # noqa: E402


def trade(pnl, gap=0, risk=50.0):
    return {'pnl': pnl, 'pnl_r': pnl / risk, 'risk': risk, 'gap_min': gap,
            'fees': 0.0, 'funding': 0.0}


class TestOnlyAMeasuredGapDisqualifies:

    def test_a_whole_trade_counts(self):
        assert dashboard.is_measured(trade(100, gap=0))

    def test_a_trade_with_a_hole_does_not(self):
        assert not dashboard.is_measured(trade(100, gap=193 * 60))

    def test_even_a_small_hole_does_not(self):
        assert not dashboard.is_measured(trade(100, gap=6))

    def test_a_trade_older_than_the_column_still_counts(self):
        """
        РОВНО ТА ОШИБКА. Нет отметки — значит неизвестно, а не «испорчена».
        Выбросив такие, мы убрали бы из выборки два реальных минуса и
        улучшили винрейт подгонкой.
        """
        assert dashboard.is_measured(trade(-55, gap=None)), (
            'сделка старше колонки выброшена из статистики — так убыток '
            'исчезает из отчёта, а винрейт растёт сам собой')


class TestTheSummaryDropsOnlyWhatItShould:

    def test_holes_are_excluded_from_the_count(self):
        got = dashboard._summarise([trade(100), trade(-50),
                                    trade(-50, gap=600)])
        assert got['trades'] == 2 and got['skipped'] == 1

    def test_the_excluded_are_named_not_hidden(self):
        """
        Молча уменьшить число сделок значит заменить одну неверную картину
        другой. Сколько отброшено — часть ответа.
        """
        got = dashboard._summarise([trade(100), trade(-50, gap=600)])
        assert got['skipped'] == 1 and got['trades_all'] == 2

    def test_nothing_to_drop_reports_zero(self):
        got = dashboard._summarise([trade(100), trade(-50)])
        assert got['skipped'] == 0

    def test_a_dropped_loss_does_not_flatter_the_winrate(self):
        """
        Проверка смысла: отбрасывание работает в обе стороны и не превращается
        в способ убрать неудобные сделки.
        """
        with_hole = dashboard._summarise([trade(100), trade(-50, gap=600)])
        without = dashboard._summarise([trade(100), trade(-50)])
        assert with_hole['winrate'] == 100.0     # дыра честно исключена
        assert without['winrate'] == 50.0        # а старая сделка — нет

    def test_old_trades_keep_dragging_the_winrate_down(self):
        """Две убыточные без отметки обязаны остаться в счёте."""
        got = dashboard._summarise([trade(100), trade(-55, gap=None),
                                    trade(-55, gap=None)])
        assert got['trades'] == 3 and got['skipped'] == 0
        assert got['winrate'] == round(1 / 3 * 100, 1)


class TestThePanelCarriesTheField:

    SRC = open(os.path.join(ROOT, 'dashboard.py'), encoding='utf-8').read()
    HTML = open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()

    def test_the_journal_field_reaches_the_payload(self):
        assert "'gap_min'" in self.SRC and 'data_gap_min' in self.SRC

    def test_the_trade_list_marks_it(self):
        assert 'дыра в данных' in self.HTML

    def test_the_count_of_skipped_is_shown(self):
        assert 'skipped' in self.HTML, (
            'отброшенные сделки посчитаны, но человеку о них не сказали')
