"""
Стакан, очередь и модель исполнения. Ядро маркет-мейкинга.

ЧТО ЗДЕСЬ ЗАКРЕПЛЕНО И ПОЧЕМУ ИМЕННО ЭТО. Каждая проверка держит решение,
ошибка в котором НЕ ПАДАЕТ, а тихо превращает замер в обещание:

    порядок заявок в ответе биржи обратный ожидаемому — взяв первую вместо
    последней, мы получили бы чужой спред и решили, что он огромен;

    очередь на нашей цене считается ЦЕЛИКОМ — мы встаём в конец, и поблажка
    здесь пририсовала бы исполнения, которых не будет;

    заявку на покупку исполняет агрессивная ПРОДАЖА — перепутав стороны, мы
    насчитали бы вдвое больше исполнений и вдвое меньше риска.

Всё это не гипотезы: порядок заявок уже дважды оказывался обратным, и оба раза
это ловилось сверкой с полем `spread` самого рынка.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polymarket import book, params  # noqa: E402


def _book():
    """Стакан в том виде, в каком его отдаёт биржа после нормализации."""
    return {'bids': [(0.22, 500.0), (0.21, 1200.0), (0.20, 3000.0)],
            'asks': [(0.23, 400.0), (0.24, 900.0), (0.25, 2500.0)]}


class TestTop:

    def test_best_prices_are_the_inner_ones(self):
        info = book.top(_book())
        assert info['bid'] == 0.22 and info['ask'] == 0.23
        assert abs(info['spread'] - 0.01) < 1e-9
        assert abs(info['mid'] - 0.225) < 1e-9

    def test_one_sided_book_has_no_spread(self):
        """
        Односторонний стакан не притворяется исправным.

        Спред и середину там считать не из чего, и вернуть ноль значило бы
        сказать «спред нулевой» — прямо противоположное правде.
        """
        info = book.top({'bids': [(0.10, 50.0)], 'asks': []})
        assert info['ask'] is None
        assert info['spread'] is None and info['mid'] is None

    def test_empty_book_is_not_a_crash(self):
        assert book.top({'bids': [], 'asks': []})['bid'] is None
        assert book.top(None) is None


class TestQueue:

    def test_our_own_level_counts_entirely(self):
        """
        Встаём в КОНЕЦ очереди на своей цене, а не в начало.

        Поблажка здесь — самый тихий способ нарисовать себе исполнения:
        модель обещала бы фил там, где нас даже не коснулись.
        """
        assert book.depth_ahead(_book(), 'bid', 0.22) == 500.0

    def test_deeper_levels_wait_for_the_better_ones(self):
        """На биде 0.21 перед нами стоит и весь 0.22, и весь 0.21."""
        assert book.depth_ahead(_book(), 'bid', 0.21) == 500.0 + 1200.0

    def test_ask_side_mirrors_the_bid_side(self):
        assert book.depth_ahead(_book(), 'ask', 0.23) == 400.0
        assert book.depth_ahead(_book(), 'ask', 0.24) == 400.0 + 900.0

    def test_price_better_than_the_whole_book_has_no_queue(self):
        """Заявка лучше всех — первая, перед ней никого."""
        assert book.depth_ahead(_book(), 'bid', 0.225) == 0.0
        assert book.depth_ahead(_book(), 'ask', 0.225) == 0.0


class TestQuote:

    def test_steps_inside_when_the_spread_allows(self):
        wide = {'bids': [(0.20, 100.0)], 'asks': [(0.30, 100.0)]}
        q = book.quote(wide, 0.01, step=1, min_size=50)
        assert abs(q['bid'] - 0.21) < 1e-9
        assert abs(q['ask'] - 0.29) < 1e-9

    def test_falls_back_when_the_spread_is_one_tick(self):
        """
        При спреде в один тик встать внутрь НЕЛЬЗЯ.

        Шаг внутрь дал бы заявку, пересекающую рынок, то есть тейкерскую — с
        комиссией и без награды, ровно наоборот замыслу. Возвращаемся к лучшим
        ценам и честно встаём в очередь.

        Это не редкий случай: на наблюдаемых рынках медианный спред и есть один
        тик.
        """
        q = book.quote(_book(), 0.01, step=1, min_size=50)
        assert abs(q['bid'] - 0.22) < 1e-9
        assert abs(q['ask'] - 0.23) < 1e-9

    def test_one_sided_book_gives_no_quote(self):
        assert book.quote({'bids': [(0.2, 10.0)], 'asks': []}, 0.01) is None

    def test_refuses_prices_outside_the_range(self):
        edge = {'bids': [(0.001, 10.0)], 'asks': [(0.999, 10.0)]}
        q = book.quote(edge, 0.001, step=1, min_size=50)
        assert q is None or (q['bid'] > 0 and q['ask'] < 1)


class TestRewardEligibility:

    def _market(self, min_size=20, max_spread=4.5):
        return {'rewardsMinSize': min_size, 'rewardsMaxSpread': max_spread}

    def test_qualifying_quote_passes(self):
        q = {'bid': 0.22, 'ask': 0.23, 'size': 100.0, 'mid': 0.225}
        assert book.rewards_eligible(q, self._market())['eligible'] is True

    def test_too_small_size_is_refused_with_the_reason(self):
        """
        Размер ниже порога рынка — заявка не считается вовсе.

        Найдено первым же прогоном наблюдателя: с общим размером 100 семь
        рынков из двадцати пяти не проходили под награду, то есть мы стояли бы
        в стакане, неся риск, и не получали за это ничего.
        """
        q = {'bid': 0.22, 'ask': 0.23, 'size': 100.0, 'mid': 0.225}
        out = book.rewards_eligible(q, self._market(min_size=200))
        assert out['eligible'] is False
        assert '200' in out['why']

    def test_too_far_from_mid_is_refused(self):
        q = {'bid': 0.10, 'ask': 0.40, 'size': 100.0, 'mid': 0.25}
        assert book.rewards_eligible(q, self._market())['eligible'] is False

    def test_market_without_rewards_is_refused(self):
        q = {'bid': 0.22, 'ask': 0.23, 'size': 100.0, 'mid': 0.225}
        out = book.rewards_eligible(q, {'rewardsMaxSpread': 0})
        assert out['eligible'] is False and 'без награды' in out['why']

    def test_extreme_prices_are_flagged_as_needing_two_sides(self):
        """
        Вне 0.10-0.90 награда идёт ТОЛЬКО за двустороннюю ликвидность.

        Это правило и делает стратегию двусторонней по существу: наибольший
        перевес мейкера над тейкером как раз на дешёвых контрактах, а там
        односторонняя заявка не приносит ничего.
        """
        cheap = {'bid': 0.04, 'ask': 0.06, 'size': 100.0, 'mid': 0.05}
        out = book.rewards_eligible(cheap, self._market())
        assert out['eligible'] is True
        assert out['two_sided_required'] is True

        middle = {'bid': 0.49, 'ask': 0.51, 'size': 100.0, 'mid': 0.50}
        assert book.rewards_eligible(middle, self._market())['two_sided_required'] is False


class TestFillModel:

    def _tape(self, *rows):
        return [{'price': p, 'size': s, 'side': side, 'ts': ts, 'asset': 'T'}
                for p, s, side, ts in rows]

    def test_bid_is_filled_by_aggressive_sells_only(self):
        """
        Нашу покупку исполняет ПРОДАЖА, а не покупка.

        Перепутав стороны, мы насчитали бы вдвое больше исполнений и вдвое
        меньше риска — ошибка, которая делает любую доходность красивой.
        """
        buys = self._tape((0.22, 10_000, 'BUY', 100))
        assert book.would_fill('bid', 0.22, 500, buys)['filled'] is False
        sells = self._tape((0.22, 10_000, 'SELL', 100))
        assert book.would_fill('bid', 0.22, 500, sells)['filled'] is True

    def test_queue_must_be_exceeded_not_merely_reached(self):
        """Ровно столько объёма, сколько стоит перед нами, нас НЕ исполняет."""
        tape = self._tape((0.22, 500, 'SELL', 100))
        assert book.would_fill('bid', 0.22, 500, tape)['filled'] is False
        more = self._tape((0.22, 500, 'SELL', 100), (0.22, 1, 'SELL', 101))
        assert book.would_fill('bid', 0.22, 500, more)['filled'] is True

    def test_trades_at_worse_prices_do_not_reach_us(self):
        """Сделка выше нашего бида нас не касается: до нас не дошли."""
        tape = self._tape((0.23, 10_000, 'SELL', 100))
        assert book.would_fill('bid', 0.22, 0, tape)['filled'] is False

    def test_fill_time_is_the_trade_that_crossed_us(self):
        tape = self._tape((0.22, 300, 'SELL', 100), (0.21, 400, 'SELL', 250))
        out = book.would_fill('bid', 0.22, 500, tape)
        assert out['filled'] is True and out['ts'] == 250

    def test_other_tokens_are_ignored(self):
        """
        Лента события содержит обе стороны рынка.

        Не отсеяв чужой токен, мы засчитали бы себе исполнения от сделок по
        противоположному исходу — то есть по другой бумаге.
        """
        tape = [{'price': 0.22, 'size': 10_000, 'side': 'SELL', 'ts': 100,
                 'asset': 'ДРУГОЙ'}]
        out = book.would_fill('bid', 0.22, 0, tape, token_id='T')
        assert out['filled'] is False

    def test_missing_tape_is_not_a_fill(self):
        """Нет ленты — нет ответа, а не «не исполнилось»."""
        assert book.would_fill('bid', 0.22, 0, None) is None


class TestObserverIsReadOnly:

    def test_no_order_placement_anywhere_in_the_package(self):
        """
        Наблюдатель ничего не отправляет на биржу — по построению.

        Проверяется отсутствием самих средств отправки: ни подписи, ни ключа,
        ни POST. Пока их нет, случайная отправка невозможна независимо от
        настроек.
        """
        for name in ('observer.py', 'book.py', 'client.py'):
            path = os.path.join(ROOT, 'polymarket', name)
            with open(path, encoding='utf-8') as fh:
                text = fh.read()
            assert 'post_order' not in text, name
            assert 'private_key' not in text.lower(), name
            assert 'urlopen(req, data' not in text, name

    def test_quote_size_respects_the_market_threshold(self):
        """Размер котировки поднимается до порога рынка, а не остаётся общим."""
        path = os.path.join(ROOT, 'polymarket', 'observer.py')
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        assert "max(params.MM_QUOTE_SIZE, need)" in text
        assert "market.get('rewardsMinSize')" in text
