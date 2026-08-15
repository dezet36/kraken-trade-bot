"""
Награда за ликвидность проверена отправкой настоящих заявок.

ДВА ОПЫТА НА ЖИВОМ СЧЁТЕ, и второй всё объяснил.

    200 контрактов по лучшей цене, рынок про Путина     scoring = False
     20 контрактов в 2.5 цента от середины              scoring = TRUE

        рынок: Will the Republican Party control the House after 2026
        пул $11/сут, минимум 20 контрактов, допуск 4.5 цента
        стакан 0.120/0.130, середина 0.1250, поставили 0.100

Значит требований у биржи два: размер не меньше rewardsMinSize и цена в
пределах rewardsMaxSpread от СЕРЕДИНЫ рынка. Наши пять контрактов не проходят
по размеру никогда, сколько бы мы ни стояли — и потому за всё время работы под
награду не попала ни одна заявка из пятнадцати.

И вторая сторона той же котировки была отвергнута НАШЕЙ ЖЕ проверкой:
«размер $17.00 выше потолка $13.60». Мы сами закрывали себе единственный
проверенный источник дохода.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polymarket import executor, params, selector  # noqa: E402


class TestRewardNeedsMoneyNotAnExemption:
    """
    Первым побуждением было подвинуть потолок заявки, отвергший наградную
    сторону («$17.00 выше потолка $13.60»). Это была бы ошибка: при бюджете в
    сорок долларов двадцать контрактов есть половина счёта в одном рынке, а
    потолок написан ровно против такого.

    Правильный вывод другой: награда требует не поблажки в проверке, а денег.
    """

    def test_forty_dollars_cannot_afford_a_reward_order(self, monkeypatch):
        monkeypatch.delenv('PM_MAX_ORDER_USD', raising=False)
        monkeypatch.setattr(params, 'bankroll_for', lambda _: 40.0)
        assert executor.max_order_usd() < params.MM_REWARD_MIN_SIZE

    def test_a_bigger_budget_admits_it(self, monkeypatch):
        """Доля в треть открывает наградный размер примерно с шестидесяти."""
        monkeypatch.delenv('PM_MAX_ORDER_USD', raising=False)
        monkeypatch.setattr(params, 'bankroll_for', lambda _: 100.0)
        assert executor.max_order_usd() >= params.MM_REWARD_MIN_SIZE

    def test_the_protection_is_not_weakened(self, monkeypatch):
        """Одна заявка не должна съедать счёт целиком ни при каком бюджете."""
        monkeypatch.delenv('PM_MAX_ORDER_USD', raising=False)
        for budget in (20.0, 40.0, 100.0, 500.0):
            monkeypatch.setattr(params, 'bankroll_for', lambda _, b=budget: b)
            if budget > params.MM_MIN_ORDER_SIZE / 0.34:
                assert executor.max_order_usd() < budget

    def test_an_explicit_setting_still_wins(self, monkeypatch):
        monkeypatch.setenv('PM_MAX_ORDER_USD', '3')
        assert executor.max_order_usd() == 3.0

    def test_the_reward_size_matches_what_the_exchange_asked(self):
        """Двадцать — минимум, на котором биржа ответила scoring=True."""
        assert params.MM_REWARD_MIN_SIZE == 20


class TestRewardBeatsAModelEstimate:
    """
    Доход от спреда — ОЦЕНКА модели, и она пока не сбылась: за сутки четыре
    круга и минус тридцать центов при обещанных сотнях в месяц. Награда
    проверена отправкой и платится независимо от того, закроется круг или нет.
    """

    def _market(self, pool, per_hour, depth=400.0, price=0.5):
        return {'id': 'M', 'question': 'рынок', 'condition_id': 'C',
                'rewards_daily': pool, 'rewardsMinSize': 20,
                'price': price, 'order_min': 5, 'size': 5,
                'cost': selector.quote_cost(5, price),
                'bid_usd': depth, 'ask_usd': depth, 'liquidity': depth,
                'spread_share': 0.1, 'our_gain': 0.01,
                'usd_per_hour': per_hour, 'wait_hours': 1.0,
                'flow_in': 10.0, 'flow_out': 10.0, 'queue_in': 0.0,
                'queue_out': 0.0, 'event_id': None}

    def test_a_modest_reward_now_wins_over_the_estimate(self, monkeypatch):
        """
        Прежде награда должна была перебить ВСЮ модельную оценку и потому не
        срабатывала никогда. Теперь ей достаточно перебить её долю.
        """
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        plan = selector.allocate([self._market(pool=40.0, per_hour=0.06)],
                                 budget=100)
        assert plan['markets'][0]['size'] == 20
        assert plan['markets'][0]['reward_per_hour'] > 0

    def test_a_worthless_reward_is_still_declined(self, monkeypatch):
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        plan = selector.allocate([self._market(pool=0.01, per_hour=5.0)],
                                 budget=100)
        assert plan['markets'][0]['size'] == 5

    def test_the_preference_is_a_named_number(self):
        """Доля названа в настройке, а не спрятана в коде."""
        assert 0 < params.MM_REWARD_PREFER <= 1
        text = open(os.path.join(ROOT, 'polymarket', 'selector.py'),
                    encoding='utf-8').read()
        assert 'params.MM_REWARD_PREFER' in text

    def test_the_budget_and_the_cap_still_hold(self, monkeypatch):
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 0.2)
        plan = selector.allocate([self._market(pool=40.0, per_hour=0.06)],
                                 budget=40)
        assert plan['markets'][0]['size'] == 5, 'предел на рынок выше награды'
