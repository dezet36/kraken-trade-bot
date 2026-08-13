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

    def test_total_pool_is_unchanged_by_the_split(self):
        """Перераспределение долей не увеличивает суммарную экспозицию."""
        total = sum(config.PAPER_START_BALANCES.values())
        expected = config.PAPER_START_BALANCE * len(config.PAPER_START_BALANCES)
        assert abs(total - expected) < 1e-6

    def test_shares_follow_the_measured_rule(self):
        """
        Доли соответствуют измеренному правилу «по сумме R».

        Числа: FIBO 50%, LEVELS 23%, SMC 17%, RSIBB 10%. Первые три получены
        из сумм R на четырёх периодах, RSIBB как недоказанный получает десятую
        часть.
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
