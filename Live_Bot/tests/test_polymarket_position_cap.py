"""
Потолок запаса на один рынок считается в ДЕНЬГАХ.

ВОПРОС, С КОТОРОГО ВСЁ НАЧАЛОСЬ: «если цена ушла далеко от точки входа,
позиции будут в сильном минусе?» Ответ — да, и защищает от этого только предел
на размер одной ставки.

А он не работал. В настройке стояло триста контрактов, и при цене 0.20 это
шестьдесят долларов — ПОЛТОРА СЧЁТА в сорок. Предел, превышающий счёт, не
связывает никогда: ровно та же ошибка, что была у предела вложенного, где
стояло $500 при счёте $40.

Контракт стоит от цента до доллара, поэтому один и тот же потолок в контрактах
означает разные деньги на разных рынках. Рискуем мы деньгами — их и
ограничиваем.

ЗАМЕР, РАДИ КОТОРОГО ЭТО ВАЖНО. Из шестнадцати живых позиций две ушли дальше
предела убытка: 0.637 → 0.474 (−26%) и 0.063 → 0.049 (−22%). Первая одна дала
$0.82 убытка при общей переоценке $2.01 — сорок процентов всей просадки сделала
ОДНА позиция.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import params, strategy  # noqa: E402


def _cap_lots(price, bankroll=40.0):
    """Тот же счёт, что делает торговый цикл перед вызовом стратегии."""
    cap_usd = bankroll * params.MM_MAX_MARKET_SHARE
    return min(cap_usd / max(price, 0.01), params.MM_MAX_POSITION)


class TestTheCapIsMoneyNotContracts:

    def test_a_cheap_market_allows_more_contracts(self):
        """При цене 0.05 те же деньги покупают вдесятеро больше штук."""
        assert _cap_lots(0.05) > _cap_lots(0.50) * 9

    def test_the_money_at_risk_is_the_same_everywhere(self):
        """Смысл потолка в том, что рискуем мы одинаково на любом рынке."""
        for price in (0.05, 0.20, 0.50, 0.90):
            assert _cap_lots(price) * price == pytest.approx(
                40.0 * params.MM_MAX_MARKET_SHARE, rel=0.01)

    def test_the_old_setting_did_not_bind_at_all(self):
        """
        Триста контрактов по 0.20 — это $60 при счёте $40. Проверка записывает
        сам факт: настройка допускала полтора счёта в одном рынке.
        """
        assert params.MM_MAX_POSITION * 0.20 > 40.0

    def test_the_absolute_ceiling_still_applies(self):
        """На большом счёте потолок в контрактах остаётся последней границей."""
        assert _cap_lots(0.01, bankroll=1_000_000) == params.MM_MAX_POSITION


class TestTheCapStopsBuying:

    def _quote(self, position, cap):
        top = {'bid': 0.19, 'ask': 0.21, 'mid': 0.20,
               'bid_size': 100, 'ask_size': 100}
        return strategy.desired_quote(
            top, {'tick': 0.01, 'order_min': 5, 'size': 5, 'step_ticks': 0},
            position=position, max_position=cap, avg_cost=0.20)

    def test_a_full_market_only_sells(self):
        cap = _cap_lots(0.20)
        assert self._quote(cap, cap)['only'] == 'ask'

    def test_below_the_cap_both_sides_may_stand(self):
        """
        Маленький запас не закрывает вход. Берём заведомо меньше и потолка, и
        порога наклона: полнота меряется своим размером, а не потолком рынка.
        """
        cap = _cap_lots(0.20)
        small = min(1.0, 5 * params.MM_SKEW_FULL_AT / 2)
        assert self._quote(small, cap)['only'] != 'ask'

    def test_the_cap_holds_a_third_of_the_account(self):
        """
        Даже упёршись в потолок на КАЖДОМ рынке, один рынок берёт треть счёта.
        Это и есть ответ на «весь бюджет в паре сделок».
        """
        cap = _cap_lots(0.20)
        assert cap * 0.20 == pytest.approx(40.0 / 3, rel=0.05)
