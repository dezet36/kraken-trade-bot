"""
Издержки входа: считаются верно, ограничиваются и записываются.

ОТКУДА ЭТО. Разбор 364 сделок с сервера за 5–29 августа 2026:

    грязный итог     +34.94 $      стратегии сработали в ноль
    комиссии        -661.03 $      весь убыток — здесь
    фондирование      +9.32 $
    чистый итог     -616.77 $

Две находки, обе про издержки.

ПЕРВАЯ — ошибка учёта. Комиссия выхода списывалась по ставке ТЕЙКЕРА всегда.
Но тейк-профит — это лимитная заявка, лежащая в стакане: её исполняет
встречный рынок, и ставка мейкерская, втрое ниже. Ошибка односторонняя:
завышала расход ровно на прибыльных сделках, потому что убыточные и так
закрываются рынком. На выборке — 128 выходов по цели, переплата $96.59.

ВТОРАЯ — теснота стопа. Доля риска, уходящая в комиссии, равна
ставке_туда-обратно, делённой на дистанцию стопа. У SMC стоп 1.19% при плече
94x — 7% риска уходило до того, как цена сдвинулась. У RSIBB встречались
входы по 12.4%.

ЧЕГО ЗДЕСЬ НЕТ. Предел не утверждает, что тесные стопы проигрывают. На той
выборке порог 3% поднимал ожидание с −0.028R до +0.181R, но перестановочная
проверка показала: настоящей экономией объясняется 8% прибавки, остальное —
отбор по исходу. Порог выбран из семи опробованных, с поправкой на перебор
p≈0.08. Поэтому предел стоит там, где оправдан арифметикой, а гипотеза
проверяется отдельно — по колонке cost_share_pct на НОВЫХ данных.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config                                            # noqa: E402
import risk_gate                                         # noqa: E402
from paper_broker import PaperBroker                     # noqa: E402


class TestTheExitRateFollowsHowWeLeft:

    def test_a_target_is_a_maker(self):
        """Тейк-профит лежит в стакане — его исполняют, а не он."""
        assert PaperBroker._exit_fee_rate('TP1') == config.PAPER_FEE_MAKER
        assert PaperBroker._exit_fee_rate('TP3') == config.PAPER_FEE_MAKER

    def test_every_stop_is_a_taker(self):
        """Стоп, безубыток, тайм-стоп и ручное закрытие уходят рынком."""
        for reason in ('SL', 'BE', 'TIME', 'MANUAL'):
            assert PaperBroker._exit_fee_rate(reason) == config.PAPER_FEE_TAKER, reason

    def test_the_rates_really_differ(self):
        """Если ставки сравняются, проверка выше станет бессмысленной."""
        assert config.PAPER_FEE_MAKER < config.PAPER_FEE_TAKER

    def test_no_exit_charges_taker_unconditionally(self):
        """РОВНО ТОТ ДЕФЕКТ: безусловный тейкер на закрытии."""
        src = open(os.path.join(ROOT, 'paper_broker.py'), encoding='utf-8').read()
        code = re.sub(r'"""[\s\S]*?"""', '', src)
        code = '\n'.join(l for l in code.splitlines() if not l.strip().startswith('#'))
        spot = code.index('def _close(')
        body = code[spot:code.index('\n    def ', spot + 10)]
        assert 'PAPER_FEE_TAKER' not in body, (
            'выход снова считается тейкером независимо от причины')
        assert '_exit_fee_rate(' in body

    def test_a_partial_target_is_a_maker_too(self):
        src = open(os.path.join(ROOT, 'paper_broker.py'), encoding='utf-8').read()
        spot = src.index('def _take_partial')
        body = src[spot:src.index('\n    def ', spot + 10)]
        assert 'PAPER_FEE_MAKER' in body and 'PAPER_FEE_TAKER' not in body


class TestTheCostShareIsArithmetic:

    def test_the_formula_matches_the_derivation(self):
        """
        доля = цена / дистанция × ставка. Проверяем обходом: считаем через
        объём, как оно происходит на самом деле.
        """
        entry, sl_dist, rate, risk = 100.0, 2.0, 0.00075, 50.0
        size = risk / sl_dist
        expect = size * entry * rate / risk
        assert abs(risk_gate.entry_cost_share(entry, sl_dist, rate) - expect) < 1e-12

    def test_a_tighter_stop_costs_more(self):
        wide = risk_gate.entry_cost_share(100.0, 3.0, 0.00075)
        tight = risk_gate.entry_cost_share(100.0, 1.0, 0.00075)
        assert tight > wide
        assert abs(tight / wide - 3.0) < 1e-9, 'втрое теснее — втрое дороже'

    def test_the_measured_strategies_come_out_right(self):
        """Сверка с журналом сервера: стоп 3.10% → 2.4%, стоп 1.19% → 6.3%."""
        assert abs(risk_gate.entry_cost_share(100.0, 3.10, 0.00075) * 100 - 2.42) < 0.05
        assert abs(risk_gate.entry_cost_share(100.0, 1.19, 0.00075) * 100 - 6.30) < 0.05

    def test_a_broken_stop_is_not_a_division(self):
        for bad in (0.0, -1.0):
            assert risk_gate.entry_cost_share(100.0, bad, 0.00075) == 0.0
        assert risk_gate.entry_cost_share(0.0, 1.0, 0.00075) == 0.0


class TestTheLimitRefusesExpensiveEntries:

    def test_an_ordinary_entry_passes(self):
        hi, share, why = risk_gate.cost_too_high(100.0, 3.10, 0.00075, 5.0)
        assert not hi and not why and abs(share - 2.42) < 0.05

    def test_the_smc_style_entry_is_refused(self):
        """Стоп 1.19% — 6.3% риска в комиссиях ещё до движения цены."""
        hi, share, why = risk_gate.cost_too_high(100.0, 1.19, 0.00075, 5.0)
        assert hi and 'слишком дорог' in why and '6.3' in why

    def test_zero_means_no_check(self):
        """Как у остальных пределов: ноль выключает, а не запрещает всё."""
        hi, _, _ = risk_gate.cost_too_high(100.0, 0.01, 0.00075, 0)
        assert not hi

    def test_exactly_at_the_limit_passes(self):
        hi, _, _ = risk_gate.cost_too_high(100.0, 100.0 * 0.00075 / 0.05, 0.00075, 5.0)
        assert not hi, 'предел — это «не больше», а не «строго меньше»'

    def test_the_reason_names_both_numbers(self):
        _, _, why = risk_gate.cost_too_high(100.0, 0.5, 0.00075, 5.0)
        assert '15.0%' in why and '5.0%' in why


class TestBothPathsUseTheOneImplementation:
    """
    Правило, написанное дважды, расходится. Так уже вышло с дневным
    стоп-краном: коммит 256f242 добавил его только в бумажный путь, и три
    недели предел существовал в настройках, но не проверялся на живых деньгах.
    """

    def _entry_block(self, name, anchor):
        src = open(os.path.join(ROOT, name), encoding='utf-8').read()
        spot = src.index(anchor)
        return src[spot:spot + 1600]

    def test_the_paper_path_calls_the_gate(self):
        assert 'risk_gate.cost_too_high(' in self._entry_block(
            'paper_broker.py', 'sl_dist = abs(limit_price - stop)')

    def test_the_live_path_calls_the_gate(self):
        assert 'risk_gate.cost_too_high(' in self._entry_block(
            'trade_manager.py', 'sig_sl_dist = abs(sizing_entry')

    def test_neither_grew_its_own_copy(self):
        for name in ('paper_broker.py', 'trade_manager.py'):
            src = open(os.path.join(ROOT, name), encoding='utf-8').read()
            code = re.sub(r'"""[\s\S]*?"""', '', src)
            code = '\n'.join(l for l in code.splitlines()
                             if not l.strip().startswith('#'))
            assert 'ENTRY_COST_ROUND_TRIP /' not in code, name
            assert code.count('cost_too_high') == 1, f'{name}: проверка не одна'


class TestTheNumberReachesTheJournal:
    """
    Записывать обязательно: без этого гипотезу «дешёвые входы прибыльнее»
    придётся проверять на той же выборке, где она и родилась.
    """

    def test_the_column_exists(self):
        import paper_broker
        assert 'cost_share_pct' in paper_broker.COLUMNS

    def test_it_stands_next_to_the_costs(self):
        import paper_broker
        c = paper_broker.COLUMNS
        assert abs(c.index('cost_share_pct') - c.index('fees_usd')) <= 2

    def test_the_order_carries_it_to_the_position(self):
        src = open(os.path.join(ROOT, 'paper_broker.py'), encoding='utf-8').read()
        assert "'cost_share_pct': round(cost_share, 3)" in src
        assert "'cost_share_pct': order.get('cost_share_pct'" in src
        assert "'cost_share_pct': pos.get('cost_share_pct'" in src

    def test_an_added_column_cannot_shift_old_rows(self):
        """
        Шапка пишется один раз, строки — всегда по текущему COLUMNS. Новая
        колонка в середине сдвинула бы все прежние строки влево, и файл
        читался бы правдоподобно — просто в «комиссии» оказалась бы
        длительность. Перенос шапки обязан существовать и вызываться перед
        записью.
        """
        import paper_broker
        assert hasattr(paper_broker, '_migrate_journal_header')
        src = open(os.path.join(ROOT, 'paper_broker.py'), encoding='utf-8').read()
        spot = src.index('_migrate_journal_header()\n', src.index('def _migrate') + 10)
        assert 'DictWriter' in src[spot:spot + 500]


class TestTheDisabledLimitIsNamed:
    """
    Выключенный предел выглядит настроенным: в поле ноль, и на глаз это
    неотличимо от «ещё не задал». Правило проекта — называть такие поимённо.
    """

    def test_a_zero_limit_is_reported(self):
        off = risk_gate.disabled_limits(5, 7.0, 3.0, 0)
        assert off == ['предел расхода на вход']

    def test_a_set_limit_is_silent(self):
        assert risk_gate.disabled_limits(5, 7.0, 3.0, 5.0) == []

    def test_not_asking_is_not_the_same_as_off(self):
        """
        Прежние вызывающие про этот предел не знают. Молчание для них
        правильнее выдумки, иначе диагностика начнёт ругаться на тех, кто
        просто не спрашивал.
        """
        assert risk_gate.disabled_limits(5, 7.0, 3.0) == []
        assert risk_gate.disabled_limits(5, 7.0, 3.0, None) == []

    def test_it_joins_the_others_and_does_not_replace_them(self):
        off = risk_gate.disabled_limits(0, 0, 0, 0)
        assert len(off) == 4 and 'предел расхода на вход' in off

    def test_diagnostics_asks_about_it(self):
        src = open(os.path.join(ROOT, 'doctor.py'), encoding='utf-8').read()
        spot = src.index('disabled_limits(')
        assert 'MAX_ENTRY_COST_SHARE_PCT' in src[spot:spot + 400]
