"""
ПОЗИЦИИ, О КОТОРЫХ БОТ НЕ ЗНАЛ.

Управлять можно только тем, что видишь: наклон котировки, разгрузка по сроку,
предел вложенного — всё считается по нашим книгам. Позиция, которой в них нет,
не разгружается никогда и держит деньги до самого разрешения рынка.

Замерено на живом счёте:

    биржа держит   16 позиций на $38.77
    бот знает о    14
    неизвестны      6 позиций на $11.69

Среди неизвестных были две ПОЛНЫЕ ПАРЫ «ДА+НЕТ» на $10 — то есть закрытые
круги, из которых просто не вынули деньги. При свободных $1.45 на счету.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import engine, mm  # noqa: E402

CATALOGUE = {
    'YES': {'question': 'рынок', 'token_no': 'NO', 'tick': 0.01},
    'NO': {'question': 'рынок', 'token_no': 'YES', 'tick': 0.01},
}


def _maker(tmp_path):
    return engine.PaperMaker(bankroll=100,
                             state_path=os.path.join(str(tmp_path), 's.json'))


def _exchange(monkeypatch, rows):
    monkeypatch.setattr(mm.executor, 'exchange_positions', lambda: rows)


class TestUnknownPositionsAreTakenOver:

    def test_a_plain_holding_is_adopted(self, tmp_path, monkeypatch):
        _exchange(monkeypatch, {'YES': {'size': 5.0, 'avg_price': 0.20,
                                        'value': 1.0, 'question': 'рынок'}})
        maker = _maker(tmp_path)
        got = mm.adopt_exchange_positions(maker, CATALOGUE)
        assert len(got) == 1
        assert maker.state['books']['YES']['position'] == pytest.approx(5.0)
        assert maker.state['books']['YES']['avg_cost'] == pytest.approx(0.20)

    def test_the_twin_becomes_a_minus_on_our_side(self, tmp_path, monkeypatch):
        """
        Биржа считает по токенам и всегда в плюс: держим пять «НЕТ». Мы считаем
        по рынку и со знаком: минус пять «ДА». Записав ответ как есть, мы завели
        бы вторую позицию на том же рынке и посчитали бы вложенное вдвое.
        """
        maker = _maker(tmp_path)
        maker._slot('YES')                       # рынок знаком, позиции нет
        _exchange(monkeypatch, {'NO': {'size': 5.0, 'avg_price': 0.80,
                                       'value': 4.0, 'question': 'рынок'}})
        mm.adopt_exchange_positions(maker, CATALOGUE)
        assert maker.state['books']['YES']['position'] == pytest.approx(-5.0)
        # Средняя цена переводится в нашу сторону: «НЕТ» по 0.80 — это «ДА»
        # по 0.20, иначе порог «не продавать ниже себестоимости» врал бы.
        assert maker.state['books']['YES']['avg_cost'] == pytest.approx(0.20)

    def test_a_known_position_is_left_alone(self, tmp_path, monkeypatch):
        """
        У известной позиции своя средняя цена и свой зафиксированный итог.
        Переписывать их ответом биржи значило бы потерять историю.
        """
        maker = _maker(tmp_path)
        slot = maker._slot('YES')
        slot['position'] = 3.0
        slot['avg_cost'] = 0.11
        slot['realized'] = 0.42
        _exchange(monkeypatch, {'YES': {'size': 5.0, 'avg_price': 0.20,
                                        'value': 1.0, 'question': 'рынок'}})
        assert mm.adopt_exchange_positions(maker, CATALOGUE) == []
        assert maker.state['books']['YES']['position'] == pytest.approx(3.0)
        assert maker.state['books']['YES']['realized'] == pytest.approx(0.42)

    def test_the_hold_clock_starts_ticking(self, tmp_path, monkeypatch):
        """
        Когда позиция открылась на самом деле, узнать неоткуда. Держать её
        вечно только потому, что метки нет, — худший из вариантов.
        """
        _exchange(monkeypatch, {'YES': {'size': 5.0, 'avg_price': 0.20,
                                        'value': 1.0, 'question': 'рынок'}})
        maker = _maker(tmp_path)
        mm.adopt_exchange_positions(maker, CATALOGUE)
        assert maker.state['books']['YES']['opened_ts']

    def test_silence_from_the_exchange_changes_nothing(self, tmp_path,
                                                       monkeypatch):
        """Не спросили — не выдумываем. Пустой ответ и обрыв здесь равны."""
        maker = _maker(tmp_path)
        for answer in (None, {}):
            _exchange(monkeypatch, answer)
            assert mm.adopt_exchange_positions(maker, CATALOGUE) == []
        assert not maker.state['books']

    def test_adopted_positions_survive_a_restart(self, tmp_path, monkeypatch):
        _exchange(monkeypatch, {'YES': {'size': 5.0, 'avg_price': 0.20,
                                        'value': 1.0, 'question': 'рынок'}})
        path = os.path.join(str(tmp_path), 's.json')
        first = engine.PaperMaker(bankroll=100, state_path=path)
        mm.adopt_exchange_positions(first, CATALOGUE)
        again = engine.PaperMaker(bankroll=100, state_path=path)
        assert again.state['books']['YES']['position'] == pytest.approx(5.0)


class TestThePlanIsBuiltOnRealMoney:
    """
    ПЛАН СТРОИЛСЯ НА СОРОК ДОЛЛАРОВ, КОГДА НА СЧЁТЕ БЫЛО ПОЛТОРА.

    Замерено на живом счёте: двадцать пять котировок в плане, одиннадцать на
    бирже, три отказа «не хватает денег» и десять заявок, которые даже не
    отправлялись. Снаружи это выглядит как зависший бот, а на деле план просто
    описывал деньги, которых нет.
    """

    def _wallet(self, monkeypatch, live, free):
        monkeypatch.setattr(mm.wallet, 'live_enabled', lambda: live)
        monkeypatch.setattr(mm.wallet, 'configured', lambda: True)
        monkeypatch.setattr(mm.wallet, 'balance', lambda: free)

    def test_live_planning_stops_at_the_free_balance(self, monkeypatch):
        self._wallet(monkeypatch, True, 12.0)
        assert mm._spendable(40.0) == 12.0

    def test_a_full_account_plans_the_whole_budget(self, monkeypatch):
        self._wallet(monkeypatch, True, 500.0)
        assert mm._spendable(40.0) == 40.0

    def test_paper_mode_ignores_the_exchange(self, monkeypatch):
        """В бумаге весь смысл в том, чтобы проверить раскладку на заданном."""
        self._wallet(monkeypatch, False, 1.45)
        assert mm._spendable(40.0) == 40.0

    def test_an_empty_account_still_plans_one_order(self, monkeypatch):
        """
        План из нуля рынков не сообщает ничего. План из одного честно
        показывает, на что хватает.
        """
        self._wallet(monkeypatch, True, 0.0)
        assert mm._spendable(40.0) == mm.params.MM_MIN_ORDER_SIZE

    def test_a_silent_exchange_falls_back_to_the_setting(self, monkeypatch):
        monkeypatch.setattr(mm.wallet, 'live_enabled', lambda: True)
        monkeypatch.setattr(mm.wallet, 'configured', lambda: True)
        monkeypatch.setattr(mm.wallet, 'balance', lambda: None)
        assert mm._spendable(40.0) == 40.0


class TestTheExposureCapScalesWithTheAccount:
    """
    ЗДЕСЬ СТОЯЛО ПЯТЬСОТ ДОЛЛАРОВ ПРИ СЧЁТЕ В СОРОК.

    Предел, вдвенадцатеро превышающий счёт, не срабатывает никогда — и не
    сработал: весь счёт тихо перетёк в запас. Свободных денег $1.45, в позициях
    $38.77, шестнадцать рынков. Бот перестал торговать не потому, что сломался,
    а потому что ему стало не на что.
    """

    def test_the_cap_is_below_the_account(self):
        assert mm.params.MM_MAX_EXPOSURE_USD < mm.params.bankroll_for('MM')

    def test_a_quarter_stays_free_at_the_cap(self):
        share = mm.params.MM_MAX_EXPOSURE_USD / mm.params.bankroll_for('MM')
        assert share == pytest.approx(mm.params.MM_MAX_EXPOSURE_SHARE, abs=0.01)
