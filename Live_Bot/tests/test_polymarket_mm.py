"""
Маркет-мейкер: котирование, запас, учёт и ограничения риска.

ЧТО ЗДЕСЬ ЗАКРЕПЛЕНО. Решения, ошибка в которых не падает, а тихо превращает
замер в обещание, и одно решение, ошибка в котором стоит денег:

    наклон против запаса — то единственное, чем наша схема отличается от
    разобранного кейса, где 2 236 позиций дали переоценку -$8 564;

    учёт по средней цене — метод «первым пришёл» на тех же данных показал бы
    вдвое большую прибыль раньше срока;

    очередь запоминается при выставлении — пересчитывая её каждый цикл, мы бы
    вечно «подходили к началу» и рисовали себе исполнения;

    исполнения обрабатываются ДО выставления новых заявок — обратный порядок
    дал бы себе фору в один цикл.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import engine, mm, params, strategy  # noqa: E402


def _top(bid=0.20, ask=0.24, bid_size=100, ask_size=100):
    return {'bid': bid, 'ask': ask, 'mid': (bid + ask) / 2,
            'bid_size': bid_size, 'ask_size': ask_size,
            'spread': round(ask - bid, 6)}


def _market(tick=0.01, min_size=20):
    return {'tick': tick, 'rewardsMinSize': min_size, 'rewardsMaxSpread': 4.5}


class TestFairValue:

    def test_equal_sizes_give_exactly_the_midpoint(self):
        assert abs(strategy.fair_value(_top()) - 0.22) < 1e-12

    def test_bigger_bid_pulls_the_estimate_up(self):
        """
        Большой бид и маленький аск означают давление вверх.

        Вес берётся ПРОТИВОПОЛОЖНЫЙ: цена бида умножается на размер аска. Это
        легко перепутать, и перепутанная микроцена систематически ошибалась бы
        в сторону, обратную давлению.
        """
        pushed = strategy.fair_value(_top(bid_size=900, ask_size=100))
        assert pushed > 0.22

    def test_bigger_ask_pulls_the_estimate_down(self):
        assert strategy.fair_value(_top(bid_size=100, ask_size=900)) < 0.22

    def test_empty_book_side_gives_nothing(self):
        assert strategy.fair_value({'bid': None, 'ask': 0.5}) is None


class TestInventorySkew:

    def test_no_position_no_skew(self):
        assert strategy.inventory_skew(0, 300, 0.04) == 0.0

    def test_long_position_pushes_quotes_down(self):
        """Длинная позиция обязана удешевлять нашу котировку, а не удорожать."""
        assert strategy.inventory_skew(150, 300, 0.04) < 0

    def test_short_position_pushes_quotes_up(self):
        assert strategy.inventory_skew(-150, 300, 0.04) > 0

    def test_skew_never_exceeds_half_the_spread(self):
        """
        Сдвиг ограничен половиной спреда.

        Больший увёл бы котировку за пределы рынка: заявка пересекла бы его и
        стала тейкерской — с комиссией и без награды, ровно наоборот замыслу.
        """
        for position in (300, 3000, -3000):
            assert abs(strategy.inventory_skew(position, 300, 0.04)) <= 0.02 + 1e-12


class TestQuoting:

    def test_flat_quote_steps_inside_the_spread(self):
        """
        Без запаса встаём ВНУТРЬ спреда, а не на лучшую цену.

        Прежде этот тест требовал обратного — встать ровно на 0.20 и 0.24, то
        есть на лучшие цены рынка. Требование было ошибочным и стоило нам всех
        исполнений: вставая на чужую цену, мы попадаем в КОНЕЦ чужой очереди.
        На 4 852 наблюдениях впереди стояло 152 контракта по медиане и больше
        пяти в 96% случаев — заявка на пять контрактов там не исполнится.

        Шаг внутрь создаёт новый уровень, где мы одни, и очередь нулевая.
        """
        q = strategy.desired_quote(_top(), _market(), position=0,
                                   max_position=300)
        assert q['bid'] > 0.20 and q['ask'] < 0.24, 'должны быть внутри рынка'
        assert q['bid'] < q['ask']
        assert q['only'] is None

    def test_narrow_spread_is_declined_instead_of_queued(self):
        """
        При узком спреде рынок ПРОПУСКАЕТСЯ, а не берётся в конец очереди.

        Шагнуть внутрь нельзя — цены сойдутся и заявка станет тейкерской.
        Встать на лучшую цену можно, но это значит держать $5 за очередью из
        152 контрактов, ничего не зарабатывая. При бюджете в сотню долларов
        это двадцатая часть счёта, простаивающая там, где мы заведомо не
        первые. Отказ освобождает её для рынка с нулевой очередью.
        """
        for bid, ask in ((0.20, 0.21), (0.20, 0.22)):
            q = strategy.desired_quote(_top(bid, ask), _market(tick=0.01), 0, 300)
            assert q.get('reason'), 'узкий спред обязан быть назван причиной'
            assert 'очеред' in q['reason']

    def test_long_inventory_lowers_the_ask(self):
        flat = strategy.desired_quote(_top(), _market(), 0, 300)
        long_ = strategy.desired_quote(_top(), _market(), 150, 300)
        assert long_['ask'] < flat['ask']

    def test_full_inventory_quotes_only_the_reducing_side(self):
        """
        Запас на потолке — котируем ТОЛЬКО сокращающую сторону.

        Это единственное место, где котировка становится односторонней, и оно
        сознательное: держать запас выше потолка опаснее, чем потерять награду.
        """
        assert strategy.desired_quote(_top(), _market(), 300, 300)['only'] == 'ask'
        assert strategy.desired_quote(_top(), _market(), -300, 300)['only'] == 'bid'

    def test_prices_land_on_the_exchange_grid(self):
        """Цена вне сетки биржи была бы отвергнута при отправке."""
        q = strategy.desired_quote(_top(0.207, 0.243), _market(tick=0.001), 0, 300)
        for price in (q['bid'], q['ask']):
            assert abs(round(price / 0.001) * 0.001 - price) < 1e-9

    def test_quote_never_crosses_the_market(self):
        for bid, ask in ((0.20, 0.21), (0.20, 0.24), (0.001, 0.002)):
            q = strategy.desired_quote(_top(bid, ask), _market(tick=0.001), 0, 300)
            if q and not q.get('reason'):
                assert q['bid'] < q['ask']

    def test_size_is_the_exchange_minimum_not_the_reward_threshold(self):
        """
        Размер берётся минимальный ДОПУСТИМЫЙ БИРЖЕЙ, а не наградный порог.

        Прежде тест требовал поднимать размер до rewardsMinSize (20-200). Это
        порог НАГРАДЫ, а не торговли, и он разорял малый счёт: на 90 рынках по
        100-200 контрактов выходило около $9 000 обязательств при $450 денег.
        В бумаге это не жгло только потому, что не исполнялось ни разу.

        Награда теперь идёт сверх, а не ведёт отбор, — значит и её порог не
        должен диктовать размер заявки.
        """
        q = strategy.desired_quote(_top(), _market(min_size=200), 0, 300)
        assert q['size'] == 5, 'наградный порог больше не раздувает заявку'

    def test_size_respects_the_exchange_minimum_when_it_is_higher(self):
        """Если биржа требует больше пяти — подчиняемся бирже."""
        market = dict(_market(), order_min=15)
        assert strategy.desired_quote(_top(), market, 0, 300)['size'] == 15


class TestAccounting:

    @pytest.fixture
    def maker(self, tmp_path):
        return engine.PaperMaker(bankroll=1000,
                                 state_path=str(tmp_path / 'state.json'))

    def test_average_cost_not_first_in_first_out(self, maker):
        """
        Учёт по средней цене: он не льстит.

        Купили 100 по 0.20 и 100 по 0.30, продали 100 по 0.28. Средняя даёт
        +3.00; метод «первым пришёл» дал бы +8.00 на тех же данных, то есть
        признал бы прибыль раньше, чем она заработана.
        """
        slot = maker._slot('T')
        maker._apply_fill(slot, 'bid', 0.20, 100)
        maker._apply_fill(slot, 'bid', 0.30, 100)
        assert abs(slot['avg_cost'] - 0.25) < 1e-9
        maker._apply_fill(slot, 'ask', 0.28, 100)
        assert abs(slot['realized'] - 3.0) < 1e-9

    def test_building_a_position_realises_nothing(self, maker):
        """Наращивание не фиксирует результат: покупка сама по себе не прибыль."""
        slot = maker._slot('T')
        maker._apply_fill(slot, 'bid', 0.20, 100)
        maker._apply_fill(slot, 'bid', 0.30, 100)
        assert slot['realized'] == 0.0

    def test_reversal_closes_then_opens(self, maker):
        slot = maker._slot('U')
        maker._apply_fill(slot, 'bid', 0.50, 100)
        maker._apply_fill(slot, 'ask', 0.60, 250)
        assert slot['position'] == -150.0
        assert abs(slot['realized'] - 10.0) < 1e-9
        assert abs(slot['avg_cost'] - 0.60) < 1e-9

    def test_flat_position_resets_the_cost(self, maker):
        slot = maker._slot('T')
        maker._apply_fill(slot, 'bid', 0.20, 100)
        maker._apply_fill(slot, 'ask', 0.25, 100)
        assert slot['position'] == 0.0 and slot['avg_cost'] == 0.0

    def test_inventory_is_marked_at_the_market_not_at_cost(self, maker):
        """
        Запас оценивается по СЕРЕДИНЕ рынка.

        Оценка по цене покупки скрывала бы ровно ту болезнь, ради лечения
        которой всё затевалось: разобранный кошелёк с переоценкой -$8 564 по
        «цене покупки» выглядел бы прибыльным.
        """
        slot = maker._slot('T')
        maker._apply_fill(slot, 'bid', 0.50, 100)
        maker.state['cash'] -= 50.0
        cheap = maker.mark_to_market({'T': 0.10})
        assert cheap['inventory'] == pytest.approx(10.0)
        assert cheap['pnl'] < 0

    def test_state_survives_a_restart(self, tmp_path):
        path = str(tmp_path / 's.json')
        first = engine.PaperMaker(bankroll=1000, state_path=path)
        slot = first._slot('T')
        first._apply_fill(slot, 'bid', 0.20, 100)
        first.save()
        second = engine.PaperMaker(bankroll=1000, state_path=path)
        assert second._slot('T')['position'] == 100.0

    def test_broken_state_file_starts_clean_not_crashes(self, tmp_path):
        path = str(tmp_path / 's.json')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('не json вовсе')
        maker = engine.PaperMaker(bankroll=1000, state_path=path)
        assert maker.state['cash'] == 1000


class TestOrdersAndFills:

    @pytest.fixture
    def maker(self, tmp_path):
        return engine.PaperMaker(bankroll=1000,
                                 state_path=str(tmp_path / 'state.json'))

    def _book(self):
        return {'bids': [(0.20, 500.0)], 'asks': [(0.24, 400.0)]}

    def test_queue_is_captured_at_placement(self, maker):
        """
        Очередь запоминается В МОМЕНТ выставления и не пересчитывается.

        Пересчитывая её каждый цикл, мы бы вечно «подходили к началу» и
        рисовали себе исполнения, которых не будет.
        """
        quote = {'bid': 0.20, 'ask': 0.24, 'size': 100, 'only': None}
        orders, _ = maker.place('T', quote, None, self._book())
        assert orders['bid']['queue'] == 500.0
        assert orders['ask']['queue'] == 400.0

    def test_unchanged_price_keeps_the_original_queue_position(self, maker):
        """Переставлять заявку на ту же цену — значит потерять место в очереди."""
        quote = {'bid': 0.20, 'ask': 0.24, 'size': 100, 'only': None}
        maker.place('T', quote, None, self._book())
        first_ts = maker._slot('T')['orders']['bid']['ts']
        maker.place('T', quote, None, {'bids': [(0.20, 9999.0)],
                                        'asks': [(0.24, 400.0)]})
        assert maker._slot('T')['orders']['bid']['ts'] == first_ts
        assert maker._slot('T')['orders']['bid']['queue'] == 500.0

    def test_one_sided_quote_cancels_the_other_side(self, maker):
        quote = {'bid': 0.20, 'ask': 0.24, 'size': 100, 'only': 'ask'}
        orders, _ = maker.place('T', quote, None, self._book())
        assert orders['bid'] is None and orders['ask'] is not None

    def test_fill_updates_position_and_cash(self, maker):
        quote = {'bid': 0.20, 'ask': 0.24, 'size': 100, 'only': None}
        maker.place('T', quote, None, {'bids': [(0.20, 0.0)],
                                       'asks': [(0.24, 0.0)]})
        tape = [{'price': 0.20, 'size': 50, 'side': 'SELL',
                 'ts': engine._now() + 5, 'asset': 'T'}]
        done = maker.process_fills('T', 'C', tape)
        assert len(done) == 1 and done[0]['side'] == 'bid'
        assert maker._slot('T')['position'] == 100.0
        assert maker.state['cash'] == pytest.approx(1000 - 0.20 * 100)

    def test_filled_order_is_removed(self, maker):
        quote = {'bid': 0.20, 'ask': 0.24, 'size': 100, 'only': None}
        maker.place('T', quote, None, {'bids': [(0.20, 0.0)], 'asks': [(0.24, 0.0)]})
        tape = [{'price': 0.20, 'size': 50, 'side': 'SELL',
                 'ts': engine._now() + 5, 'asset': 'T'}]
        maker.process_fills('T', 'C', tape)
        assert maker._slot('T')['orders']['bid'] is None
        assert maker.process_fills('T', 'C', tape) == []


class TestRiskLimits:

    def test_exposure_counts_both_directions(self, tmp_path):
        """Короткая позиция — тоже вложенные деньги, и по модулю."""
        maker = engine.PaperMaker(bankroll=1000,
                                  state_path=str(tmp_path / 's.json'))
        maker._apply_fill(maker._slot('A'), 'bid', 0.50, 100)
        maker._apply_fill(maker._slot('B'), 'ask', 0.50, 100)
        assert maker.exposure({'A': 0.5, 'B': 0.5}) == pytest.approx(100.0)

    def test_stale_positions_are_reported(self, tmp_path):
        """
        Позиция, висящая дольше срока, перестаёт быть спредом.

        Доход здесь — разница цен, а не исход события. Застрявшая позиция
        превращает мейкера в предсказателя, кем он быть не собирался.
        """
        maker = engine.PaperMaker(bankroll=1000,
                                  state_path=str(tmp_path / 's.json'))
        slot = maker._slot('T')
        maker._apply_fill(slot, 'bid', 0.20, 100)
        slot['opened_ts'] = engine._now() - 100 * 3600
        assert 'T' in maker.stale_positions(hours=24)
        assert maker.stale_positions(hours=1000) == []

    def test_fills_are_processed_before_new_quotes(self):
        """
        Порядок в цикле: сначала исполнения, потом котировки.

        Обратный порядок выставил бы заявку и тут же проверил её по ленте, где
        она физически не могла исполниться, — то есть дал бы себе фору.
        """
        source = os.path.join(ROOT, 'polymarket', 'mm.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        fills_at = text.index('maker.process_fills')
        place_at = text.index('maker.place(')
        assert fills_at < place_at

    def test_no_order_sending_anywhere(self):
        """Живого исполнения нет: ни ключа, ни подписи, ни отправки."""
        for name in ('mm.py', 'engine.py', 'strategy.py'):
            path = os.path.join(ROOT, 'polymarket', name)
            with open(path, encoding='utf-8') as fh:
                text = fh.read().lower()
            assert 'private_key' not in text, name
            assert 'post_order' not in text, name


class TestStaleLiveOrdersAreCancelled:
    """
    Заменяемая живая заявка обязана вернуться на снятие.

    БЕЗ ЭТОГО ЖИВОЙ РЕЖИМ НЕПРИГОДЕН. Старая заявка осталась бы лежать в
    стакане по устаревшей цене: мы бы её не видели, а исполнить нас по ней
    могли — и именно тогда, когда это выгодно встречной стороне. За смену
    часов таких заявок накопились бы сотни, и каждая несла бы полный размер.
    """

    @pytest.fixture
    def maker(self, tmp_path):
        return engine.PaperMaker(bankroll=1000,
                                 state_path=str(tmp_path / 'state.json'))

    def _book(self):
        return {'bids': [(0.20, 500.0)], 'asks': [(0.24, 400.0)]}

    def test_price_change_returns_the_old_order_id(self, maker):
        first = {'bid': 0.20, 'ask': 0.24, 'size': 100, 'only': None}
        orders, replaced = maker.place('T', first, None, self._book())
        assert replaced == []
        orders['bid']['live_id'] = 'ORDER-1'
        second = {'bid': 0.21, 'ask': 0.24, 'size': 100, 'only': None}
        _, replaced = maker.place('T', second, None, self._book())
        assert replaced == ['ORDER-1']

    def test_going_one_sided_returns_the_cancelled_side(self, maker):
        quote = {'bid': 0.20, 'ask': 0.24, 'size': 100, 'only': None}
        orders, _ = maker.place('T', quote, None, self._book())
        orders['bid']['live_id'] = 'ORDER-BID'
        one_sided = dict(quote, only='ask')
        _, replaced = maker.place('T', one_sided, None, self._book())
        assert replaced == ['ORDER-BID']

    def test_unchanged_price_cancels_nothing(self, maker):
        quote = {'bid': 0.20, 'ask': 0.24, 'size': 100, 'only': None}
        orders, _ = maker.place('T', quote, None, self._book())
        orders['bid']['live_id'] = 'ORDER-1'
        _, replaced = maker.place('T', quote, None, self._book())
        assert replaced == []

    def test_cycle_cancels_before_placing(self):
        """
        Снятие идёт ДО выставления нового.

        Обратный порядок оставил бы обе заявки в стакане одновременно: двойной
        размер и двойной риск ровно тогда, когда цена уже сдвинулась.
        """
        source = os.path.join(ROOT, 'polymarket', 'mm.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        cancel_at = text.index('executor.cancel(order_id)')
        place_at = text.index('out = executor.place(')
        assert cancel_at < place_at


class TestSingleInstance:
    """
    Замок на единственный экземпляр. ПОЙМАНО НА СЕБЕ, отсюда и тесты.

    Три процесса разом писали в одно состояние: один со старым кодом в памяти,
    два с новым. Вышла смесь — заявки по 5, 100 и 200 контрактов вперемешку,
    число рынков скакало 46, 90, 48 от цикла к циклу. В бумаге это путаница;
    на бирже это двойные заявки, двойной запас и снятие чужих номеров.
    """

    def test_second_start_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mm.store, 'DIR', str(tmp_path))
        assert mm._single_instance() is True
        # Чужой ЖИВОЙ процесс: подставляем номер, который заведомо жив.
        lock = tmp_path / 'mm.lock'
        lock.write_text('999999999', encoding='utf-8')
        monkeypatch.setattr(mm, '_alive', lambda pid: True)
        assert mm._single_instance() is False

    def test_dead_lock_does_not_block_forever(self, tmp_path, monkeypatch):
        """
        Мёртвый замок обязан сниматься сам.

        Иначе одно аварийное завершение — отключение питания, снятие процесса —
        и бот больше не запустится никогда, причём молча.
        """
        monkeypatch.setattr(mm.store, 'DIR', str(tmp_path))
        (tmp_path / 'mm.lock').write_text('999999999', encoding='utf-8')
        monkeypatch.setattr(mm, '_alive', lambda pid: False)
        assert mm._single_instance() is True

    def test_damaged_lock_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mm.store, 'DIR', str(tmp_path))
        (tmp_path / 'mm.lock').write_text('не число', encoding='utf-8')
        assert mm._single_instance() is True


class TestAdverseSelection:
    """
    Замер сноса цены после исполнения — число, решающее судьбу затеи.

    Спред известен заранее и выглядит щедро: 15% от ставки по медиане. Чего
    нельзя узнать заранее — сколько из него отбирает неблагоприятный отбор.
    Нас исполняют не в случайный момент, а тогда, когда встречной стороне это
    выгодно, то есть когда цена уже пошла против нас. Если снос больше спреда,
    не помогут ни частота, ни число рынков, ни размер счёта.
    """

    @pytest.fixture
    def maker(self, tmp_path):
        return engine.PaperMaker(bankroll=100,
                                 state_path=str(tmp_path / 'state.json'))

    def _fill(self, side='bid', price=0.20, size=5):
        return {'token': 'T', 'side': side, 'price': price, 'size': size}

    def test_price_falling_after_our_buy_counts_against_us(self, maker):
        """Купили, и середина упала — это снос против нас, а не случайность."""
        maker.watch_drift([self._fill('bid')], {'T': 0.20})
        maker.state['drift_pending'][0]['ts'] -= 3600
        got = maker.measure_drift({'T': 0.15})
        assert len(got) == 1
        assert got[0]['gain_per_contract'] < 0
        assert got[0]['gain_usd'] == pytest.approx(-0.25)

    def test_price_rising_after_our_sell_counts_against_us(self, maker):
        maker.watch_drift([self._fill('ask')], {'T': 0.20})
        maker.state['drift_pending'][0]['ts'] -= 3600
        got = maker.measure_drift({'T': 0.25})
        assert got[0]['gain_per_contract'] < 0

    def test_price_rising_after_our_buy_counts_for_us(self, maker):
        maker.watch_drift([self._fill('bid')], {'T': 0.20})
        maker.state['drift_pending'][0]['ts'] -= 3600
        assert maker.measure_drift({'T': 0.25})[0]['gain_per_contract'] > 0

    def test_unripe_measurement_is_not_closed_early(self, maker):
        """
        Замер до срока не закрывается: за минуту снос ещё не проявится, и
        ранний ответ был бы шумом, выданным за результат.
        """
        maker.watch_drift([self._fill()], {'T': 0.20})
        assert maker.measure_drift({'T': 0.10}) == []
        assert len(maker.state['drift_pending']) == 1

    def test_market_without_price_waits_instead_of_being_dropped(self, maker):
        """
        Рынок без цены ЖДЁТ, а не выбрасывается.

        Выбросив его, мы выбросили бы ровно те случаи, где книга опустела после
        нашего исполнения, — то есть худшие из возможных, и замер стал бы
        заведомо оптимистичным.
        """
        maker.watch_drift([self._fill()], {'T': 0.20})
        maker.state['drift_pending'][0]['ts'] -= 3600
        assert maker.measure_drift({}) == []
        assert len(maker.state['drift_pending']) == 1

    def test_fill_without_a_mark_is_not_registered(self, maker):
        maker.watch_drift([self._fill()], {})
        assert maker.state.get('drift_pending', []) == []
