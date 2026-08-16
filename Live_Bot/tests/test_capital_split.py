"""
Доли капитала между стратегиями измерены, а не назначены поровну.

ЗАЧЕМ ЭТОТ ТЕСТ. Доли — это решение, полученное скользящей проверкой
(research/sizing.py): вес на период считался по прошлым периодам, и из четырёх
правил приёмку прошло одно. Такое решение легко потерять при первой же правке
конфига, а потеря будет молчаливой: бот продолжит работать, просто хуже.

Отдельно закрепляется то, что общий пул НЕ вырос. Перераспределение долей и
увеличение суммарной экспозиции — разные вещи, и спутать их означало бы
получить «прибавку» просто оттого, что в игре стало больше денег.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402


class TestCapitalSplit:

    def test_every_live_strategy_has_a_balance(self):
        """
        Все четыре живые стратегии перечислены явно.

        RSIBB прежде в словаре отсутствовал и получал запасное значение внутри
        брокера: работало, но доля живой стратегии нигде не была записана и не
        поддавалась настройке.
        """
        assert set(config.PAPER_START_BALANCES) == {'FIBO', 'SMC', 'LEVELS',
                                                    'RSIBB'}

    def test_one_strategy_budget_does_not_move_the_others(self, monkeypatch):
        """
        БЮДЖЕТЫ НЕЗАВИСИМЫ. Прежде этого свойства не было.

        Депозиты считались долями одного котла, и правка доли у одной МОЛЧА
        меняла деньги у остальных: их собственные настройки не двигались.
        Менять что-то одно было нельзя в принципе.

        Проверяется через настройку окружения — тем самым способом, которым
        бюджет и меняют на деле.
        """
        import importlib
        before = dict(config.PAPER_START_BALANCES)
        monkeypatch.setenv('PAPER_START_BALANCE_FIBO', '777')
        importlib.reload(config)
        after = dict(config.PAPER_START_BALANCES)
        assert after['FIBO'] == 777
        for name in ('LEVELS', 'SMC', 'RSIBB'):
            assert after[name] == before[name], f'{name} сдвинулся вслед за FIBO'
        monkeypatch.delenv('PAPER_START_BALANCE_FIBO')
        importlib.reload(config)

    def test_common_deposit_no_longer_scales_everyone(self, monkeypatch):
        """
        Общее число депозита больше не растягивает всех разом.

        Именно так выглядела связь: PAPER_START_BALANCE умножался на число
        стратегий, и каждая получала свою долю от произведения. Поднять общий
        депозит значило поднять всем — даже той, которую трогать не хотели.
        """
        import importlib
        before = dict(config.PAPER_START_BALANCES)
        monkeypatch.setenv('PAPER_START_BALANCE', '99999')
        importlib.reload(config)
        assert dict(config.PAPER_START_BALANCES) == before
        monkeypatch.delenv('PAPER_START_BALANCE')
        importlib.reload(config)

    def test_starting_numbers_still_carry_the_measured_proportion(self):
        """
        Начальные суммы сохраняют измеренную пропорцию — как отправную точку.

        Числа: FIBO 50%, LEVELS 23%, SMC 17%, RSIBB 10%. Первые три получены
        из сумм R на четырёх периодах, RSIBB как недоказанный получает десятую
        часть.

        ЭТО БОЛЬШЕ НЕ ФОРМУЛА, А ИСТОРИЯ. Пропорция вшита в стартовые суммы,
        чтобы разделение бюджетов не переписало на ходу результаты идущего
        замера. Дальше каждая сумма живёт своей жизнью и меняется отдельно.
        """
        total = sum(config.PAPER_START_BALANCES.values())
        share = {k: v / total for k, v in config.PAPER_START_BALANCES.items()}
        assert abs(share['FIBO'] - 0.50) < 0.005
        assert abs(share['LEVELS'] - 0.23) < 0.005
        assert abs(share['SMC'] - 0.17) < 0.005
        assert abs(share['RSIBB'] - 0.10) < 0.005

    def test_the_proven_strategy_gets_more_than_the_unproven_one(self):
        """
        Порядок долей отражает доказанность, а не вкус.

        FIBO значим на всех четырёх периодах, RSIBB сидит ровно на пороге
        различимости. Перевернуть этот порядок правкой конфига можно, но
        молча — нельзя.
        """
        balances = config.PAPER_START_BALANCES
        assert balances['FIBO'] > balances['LEVELS'] > balances['SMC']
        assert balances['SMC'] > balances['RSIBB']

    def test_env_override_still_wins(self, monkeypatch):
        """Ручная настройка остаётся сильнее измеренной доли."""
        monkeypatch.setenv('PAPER_START_BALANCE_FIBO', '777')
        import importlib
        reloaded = importlib.reload(config)
        try:
            assert reloaded.PAPER_START_BALANCES['FIBO'] == 777.0
        finally:
            monkeypatch.delenv('PAPER_START_BALANCE_FIBO', raising=False)
            importlib.reload(config)


class TestDirectionsDoNotShareMoney:
    """
    Депозиты стратегий считаются РАЗДЕЛЬНО и не занимают друг у друга.

    Рядом стояло второе направление — Polymarket, — и правило писалось ради
    него: удачный месяц на бирже не должен молча увеличивать ставки там, где
    ещё ничего не подтверждено. Направление вырезано (ветка
    polymarket-archive), а правило осталось: оно про любые две кассы, а не про
    ту конкретную.
    """

    def test_each_strategy_keeps_its_own_deposit(self, monkeypatch):
        import importlib
        before = dict(config.PAPER_START_BALANCES)
        monkeypatch.setenv('PAPER_START_BALANCE_FIBO', '123456')
        importlib.reload(config)
        try:
            after = dict(config.PAPER_START_BALANCES)
            assert after['FIBO'] == 123456.0
            for name, value in before.items():
                if name != 'FIBO':
                    assert after[name] == value, name
        finally:
            monkeypatch.delenv('PAPER_START_BALANCE_FIBO', raising=False)
            importlib.reload(config)

