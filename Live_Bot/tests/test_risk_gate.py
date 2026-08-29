"""
Пределы портфеля живут в ОДНОМ месте, и оба пути их зовут.

ОТКУДА ЭТО. Правила стояли в двух почти одинаковых методах —
`PaperBroker._portfolio_room` и `TradeManager._portfolio_room`, — и держать их
в согласии полагалось вручную.

Так это и сломалось. Коммит 256f242 («Дневной стоп-кран, пауза, история
настроек и календарь по дням») добавил предел дневного убытка: тронул
paper_broker.py, dashboard.py, settings_store.py, dashboard.html и тесты — а
trade_manager.py не тронул вовсе. С 6 августа 2026 предел существовал в
настройках, показывался в панели, работал на бумаге и НЕ ПРОВЕРЯЛСЯ на живых
деньгах.

Хуже: панель в боевом режиме спрашивала дневной итог через hasattr, метода не
находила и показывала ноль. Предохранителя не было, а приборная доска
показывала «сегодня $0.00» — не потому что не потеряли, а потому что не
спросили.

Ни один тест этого не заметил, потому что замечать было нечем: никто не
сверял копии. Здесь сверяют.
"""

import inspect
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import risk_gate                                            # noqa: E402


class TestBothPathsCallTheSameGate:
    """
    Главная проверка файла. Всё остальное проверяет правила; эта — что правила
    применяются ОБА раза.
    """

    def _source(self, module, method):
        path = os.path.join(ROOT, f'{module}.py')
        text = open(path, encoding='utf-8').read()
        start = text.index(f'    def {method}(')
        end = text.index('\n    def ', start + 10)
        return text[start:end]

    def test_paper_calls_the_gate(self):
        body = self._source('paper_broker', '_portfolio_room')
        assert 'risk_gate.check(' in body

    def test_live_calls_the_gate(self):
        body = self._source('trade_manager', '_portfolio_room')
        assert 'risk_gate.check(' in body, (
            'боевой путь снова считает пределы сам — именно так из него '
            'выпал дневной стоп-кран')

    def test_neither_keeps_its_own_copy_of_the_rules(self):
        """
        Своя арифметика предела в вызывающем коде — это начало новой копии.
        Числа собирать можно, решать — нет.

        Смотрим только КОД: в строке документации метод вправе объяснять, что
        он делает, и слово «предел» там законно.
        """
        import re
        for module in ('paper_broker', 'trade_manager'):
            body = self._source(module, '_portfolio_room')
            code = re.sub(r'""".*?"""', '', body, flags=re.S)
            assert 'return False' not in code, (
                f'{module} сам решает отказать — правило разъезжается')
            for word in ('предел портфеля', 'дневной предел', 'занято'):
                assert word not in code, (
                    f'{module} формулирует отказ сам: {word}')

    def test_both_pass_every_limit_to_the_gate(self):
        """
        Забыть один аргумент — то же самое, что забыть проверку: gate получит
        ноль и решит, что предел выключен.
        """
        for module in ('paper_broker', 'trade_manager'):
            body = self._source(module, '_portfolio_room')
            for field in ('max_positions=', 'risk_limit_pct=', 'day_limit_pct=',
                          'risk_used=', 'deposit=', 'adding=', 'slots_used='):
                assert field in body, f'{module} не передаёт {field}'

    def test_both_can_answer_what_was_lost_today(self):
        """
        Дневной предел без дневного итога не работает. У боевого пути метода
        не было — отсюда и ноль в панели.
        """
        import paper_broker
        import trade_manager
        assert hasattr(paper_broker.PaperBroker, 'daily_result')
        assert hasattr(trade_manager.LiveTradeManager, 'daily_result'), (
            'панель спрашивает дневной итог через hasattr: без метода она '
            'молча покажет ноль вместо реальных потерь')


class TestTheDailyStop:

    def test_it_holds_when_the_day_is_bad(self):
        stopped, why = risk_gate.daily_stop_hit(day_pnl=-500, day_pct=-5.0,
                                                limit_pct=3.0)
        assert stopped and '3.00%' in why

    def test_a_good_day_never_stops(self):
        assert risk_gate.daily_stop_hit(500, 5.0, 3.0)[0] is False

    def test_a_small_loss_does_not_stop(self):
        assert risk_gate.daily_stop_hit(-100, -1.0, 3.0)[0] is False

    def test_exactly_at_the_limit_stops(self):
        """Граница включительно: «предел 3%» значит, что 3% — уже предел."""
        assert risk_gate.daily_stop_hit(-300, -3.0, 3.0)[0] is True

    def test_zero_limit_means_off(self):
        assert risk_gate.daily_stop_hit(-9999, -99.0, 0)[0] is False

    def test_the_stop_outranks_everything_else(self):
        """
        Дневной стоп проверяется первым: он говорит «сегодня больше не
        торгуем», и проверять после него что-то ещё бессмысленно.
        """
        ok, why = risk_gate.check(
            slots_used=0, max_positions=100, risk_used=0, deposit=10000,
            adding=50, risk_limit_pct=50, day_pnl=-500, day_pct=-5.0,
            day_limit_pct=3.0)
        assert not ok and 'дневной' in why


class TestTheDayIsCountedFromTheJournal:

    ROWS = [
        {'close_time': '2026-08-20T10:00:00+00:00', 'pnl_usd': '-100'},
        {'close_time': '2026-08-20T14:00:00+00:00', 'pnl_usd': '-50'},
        {'close_time': '2026-08-19T23:00:00+00:00', 'pnl_usd': '-9000'},
    ]

    def test_only_today_counts(self):
        pnl, pct = risk_gate.day_result(self.ROWS, 10000, '2026-08-20')
        assert pnl == -150 and pct == -1.5

    def test_yesterday_is_not_dragged_in(self):
        pnl, _ = risk_gate.day_result(self.ROWS, 10000, '2026-08-19')
        assert pnl == -9000

    def test_broken_numbers_do_not_crash_the_limit(self):
        rows = [{'close_time': '2026-08-20T10:00:00+00:00', 'pnl_usd': '—'}]
        assert risk_gate.day_result(rows, 10000, '2026-08-20') == (0.0, 0.0)

    def test_an_empty_journal_is_a_quiet_zero(self):
        assert risk_gate.day_result([], 10000, '2026-08-20') == (0.0, 0.0)

    def test_no_deposit_does_not_divide_by_zero(self):
        assert risk_gate.day_result(self.ROWS, 0, '2026-08-20')[1] == 0.0


class TestThePortfolioLimits:

    BASE = dict(slots_used=0, max_positions=0, risk_used=0.0, deposit=10000.0,
                adding=50.0, risk_limit_pct=0.0, day_pnl=0.0, day_pct=0.0,
                day_limit_pct=0.0)

    def test_everything_off_lets_the_trade_through(self):
        assert risk_gate.check(**self.BASE)[0] is True

    def test_position_count_holds(self):
        ok, why = risk_gate.check(**{**self.BASE, 'slots_used': 5,
                                     'max_positions': 5})
        assert not ok and '5/5' in why

    def test_one_slot_short_passes(self):
        assert risk_gate.check(**{**self.BASE, 'slots_used': 4,
                                  'max_positions': 5})[0] is True

    def test_risk_limit_counts_the_new_trade(self):
        """
        Предел смотрит на состояние ПОСЛЕ сделки. Иначе последняя сделка
        всегда проходит, и предел превышается ровно на её размер.
        """
        ok, why = risk_gate.check(**{**self.BASE, 'risk_used': 480.0,
                                     'adding': 50.0, 'risk_limit_pct': 5.0})
        assert not ok and '5.3%' in why

    def test_it_passes_when_the_trade_fits(self):
        assert risk_gate.check(**{**self.BASE, 'risk_used': 400.0,
                                  'adding': 50.0, 'risk_limit_pct': 5.0})[0] is True

    def test_zero_deposit_does_not_divide_by_zero(self):
        assert risk_gate.check(**{**self.BASE, 'deposit': 0.0,
                                  'risk_limit_pct': 5.0})[0] is True


class TestDisabledLimitsAreNamedOutLoud:
    """
    Выключенный предел выглядит настроенным: в поле стоит ноль, и на глаз это
    неотличимо от «ещё не задал». Оба портфельных предела сейчас именно такие.
    """

    def test_all_three_off_are_all_named(self):
        assert len(risk_gate.disabled_limits(0, 0.0, 0.0)) == 3

    def test_nothing_named_when_all_are_set(self):
        assert risk_gate.disabled_limits(10, 5.0, 3.0) == []

    def test_it_names_the_right_one(self):
        off = risk_gate.disabled_limits(10, 0.0, 3.0)
        assert off == ['предел риска портфеля']


class TestMaxExposureIsComputable:
    """
    «Сколько мы можем потерять одновременно» должно быть числом, а не
    рассуждением. Считается по слотам и риску каждой стратегии.
    """

    def test_it_sums_slots_times_risk(self):
        total, unbounded = risk_gate.max_exposure(
            [('FIBO', 20, 0.5), ('SMC', 20, 1.0)])
        assert total == 30.0 and unbounded == []

    def test_a_strategy_without_a_slot_limit_is_unbounded(self):
        """Ноль слотов означает «без предела» — она наберёт сколько найдёт."""
        total, unbounded = risk_gate.max_exposure(
            [('FIBO', 20, 0.5), ('RSIBB', 0, 0.5)])
        assert total == 10.0 and unbounded == ['RSIBB']


class TestSettingsFailureIsLoud:
    """
    Боевой путь глушил ошибку чтения настроек и торговал БЕЗ пределов вовсе.
    Останавливать торговлю из-за неё нельзя — позиции уже в рынке. Но и
    молчать нельзя.
    """

    def test_it_says_something(self, capsys, monkeypatch):
        said = []
        monkeypatch.setattr(risk_gate, 'log', lambda m: said.append(m))
        risk_gate.settings_unavailable(RuntimeError('файл занят'))
        assert said and 'БЕЗ' in said[0]


class TestTrailingCannotDivergeSilently:
    """
    ТО ЖЕ СЕМЕЙСТВО, ЧТО И ДНЕВНОЙ СТОП, ПОЙМАННОЕ ДО ТОГО, КАК СТОИЛО ДЕНЕГ.

    Трейлинг-стоп реализован в боевом пути (TRAIL_AFTER_TP, trail_distance,
    расчёт от markPrice) и не реализован в бумажном вовсе — там на его месте
    константа `'trailing_active': False`.

    Сейчас расхождения нет: в бою он выключен сторожевым значением
    TRAIL_AFTER_TP = 99 («трейлинг не активируется»), и обе стороны ведут
    позицию одинаково. Но стоит поставить туда 1 или 2 — и бумага начнёт
    измерять не то, что торгует бой, молча.

    Это ровно тот способ, которым из боевого пути выпал дневной стоп: правило
    поменяли в одном месте, второе никто не сверил. Здесь сверяют заранее.
    """

    def _live_trailing_enabled(self):
        import config
        # 99 — сторожевое значение: столько целей не бывает, условие
        # `tp_hit >= 99` не выполняется никогда.
        return int(getattr(config, 'TRAIL_AFTER_TP', 99)) < 10

    def _paper_has_trailing(self):
        text = open(os.path.join(ROOT, 'paper_broker.py'), encoding='utf-8').read()
        # Настоящий трейлинг двигает стоп за ценой. Константа
        # 'trailing_active': False в снимке для панели — не реализация.
        return 'trail_distance' in text or 'TRAIL_AFTER_TP' in text

    def test_paper_keeps_up_if_live_starts_trailing(self):
        if not self._live_trailing_enabled():
            return                             # выключен в бою — расхождения нет
        assert self._paper_has_trailing(), (
            'в бою включён трейлинг (TRAIL_AFTER_TP), а бумажный брокер его не '
            'умеет: замер перестанет описывать боевое поведение — ровно так из '
            'боевого пути выпал дневной стоп-кран')

    def test_the_sentinel_is_still_a_sentinel(self):
        """
        Если значение перестанет быть заведомо недостижимым, проверка выше
        начнёт молчать не потому, что всё хорошо.
        """
        import config
        value = int(getattr(config, 'TRAIL_AFTER_TP', 99))
        assert value >= 10 or self._paper_has_trailing(), (
            f'TRAIL_AFTER_TP = {value}: это уже не «выключено», а рабочее '
            f'значение')
