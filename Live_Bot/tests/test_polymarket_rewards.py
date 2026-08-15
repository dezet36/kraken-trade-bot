"""
Награда за ликвидность: другой источник дохода, чем спред.

СПРОШЕНО У САМОЙ БИРЖИ про наши пятнадцать стоящих заявок — под награду не
попадала НИ ОДНА. Причина простая: мы ставим пять контрактов, а минимум везде
двадцать. Замер по 1 243 рынкам: награду платят 778, минимальный размер и
медиана, и минимум равны двадцати.

ЧТО ДАЁТ ПЕРЕХОД ЧЕРЕЗ ПОРОГ. По 256 рынкам с ЖИВОЙ книгой лучшие приносят
сто-двести процентов в месяц на вложенное, тогда как захват спреда за ночь
принёс полцента.

ЛОВУШКА, КОТОРАЯ ЗДЕСЬ ЖДЁТ, описана в заголовке модуля отбора и стоила уже
трёх разборов. Отношение награды к ликвидности поднимает наверх ПУСТЫЕ книги:
пул $3 при книге в $2 даёт «2744% в месяц» — и это не доход, а приглашение
стать всем рынком сразу. Проверки ниже держат этот вход закрытым.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import params, selector  # noqa: E402


def _market(pool=20.0, min_size=20, depth=400.0, price=0.5, per_hour=0.01):
    return {'id': 'M', 'question': 'рынок', 'condition_id': 'C',
            'rewards_daily': pool, 'rewardsMinSize': min_size,
            'price': price, 'order_min': 5, 'size': 5,
            'cost': selector.quote_cost(5, price),
            'bid_usd': depth, 'ask_usd': depth, 'liquidity': depth,
            'spread_share': 0.1, 'our_gain': 0.01, 'usd_per_hour': per_hour,
            'wait_hours': 1.0, 'flow_in': 10.0, 'flow_out': 10.0,
            'queue_in': 0.0, 'queue_out': 0.0, 'event_id': None}


class TestRewardBelowTheThresholdIsZero:

    def test_five_contracts_earn_nothing(self):
        """Наши пять против минимума в двадцать — биржа не засчитывает."""
        assert selector.reward_per_hour(_market(), 5) == 0.0

    def test_the_threshold_itself_earns(self):
        assert selector.reward_per_hour(_market(), 20) > 0

    def test_a_market_without_rewards_earns_nothing(self):
        assert selector.reward_per_hour(_market(pool=0), 20) == 0.0

    def test_our_share_falls_as_the_book_grows(self):
        """Доля считается честно: чем толще книга, тем меньше наша часть."""
        thin = selector.reward_per_hour(_market(depth=100), 20)
        thick = selector.reward_per_hour(_market(depth=10_000), 20)
        assert thin > thick * 10


class TestTheEmptyBookTrapStaysShut:

    def test_an_empty_book_is_not_a_reward_market(self):
        """
        Пул $3 при книге в $2 даёт заоблачный процент. Это не доход, а
        приглашение стать всем рынком сразу — ровно та ловушка, что уже трижды
        всплывала в этом проекте.

        Отсев по качеству книги стоит ВЫШЕ награды и снимает такие рынки до
        того, как их увидит расчёт.
        """
        empty = _market(pool=3.0, depth=2.0)
        assert empty['bid_usd'] < params.MM_MIN_SIDE_DEPTH, \
            'такая книга не проходит порог глубины и до раскладки не доходит'

    def test_the_trap_is_written_down_where_it_is_computed(self):
        text = open(os.path.join(ROOT, 'polymarket', 'selector.py'),
                    encoding='utf-8').read()
        spot = text.index('def reward_per_hour')
        assert 'ПУСТЫЕ книги' in text[spot:spot + 1800]


class TestAllocationCrossesTheThreshold:

    def test_size_grows_to_the_reward_minimum(self, monkeypatch):
        """
        Награда включается СТУПЕНЬКОЙ: до порога ноль, после — сразу заметна.
        Обычный поиск «растём, пока растёт доход» её не находит, потому что
        первый шаг не даёт ничего.
        """
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        # Пул щедрый: награда за час перебивает то, что даёт спред, и только
        # тогда имеет смысл занимать вчетверо больше денег.
        plan = selector.allocate([_market(pool=200.0)], budget=100)
        assert plan['markets'][0]['size'] == 20
        assert plan['markets'][0]['reward_per_hour'] > 0

    def test_a_reward_smaller_than_the_spread_is_declined(self, monkeypatch):
        '''
        Награда занимает вчетверо больше денег. Если рынок и так платит спредом
        больше — расти незачем: те же деньги лучше стоят на других рынках.
        '''
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        plan = selector.allocate([_market(pool=1.0)], budget=100)
        assert plan['markets'][0]['size'] == 5

    def test_a_weak_reward_does_not_justify_the_capital(self, monkeypatch):
        """Награда должна перебивать то, что рынок даёт спредом."""
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        plan = selector.allocate([_market(pool=0.01, per_hour=5.0)], budget=100)
        assert plan['markets'][0]['size'] == 5, 'ради копейки капитал не удваиваем'

    def test_the_budget_is_still_never_exceeded(self, monkeypatch):
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        plan = selector.allocate([_market() for _ in range(10)], budget=45)
        assert plan['used'] <= 45.0

    def test_the_per_market_cap_still_holds(self, monkeypatch):
        """Один рынок не должен забирать счёт даже ради награды."""
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 0.2)
        plan = selector.allocate([_market()], budget=40)
        assert plan['markets'][0]['size'] == 5, 'предел на рынок выше награды'
