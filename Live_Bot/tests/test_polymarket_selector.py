"""
Отбор рынков для котирования. Здесь живёт защита от главной ловушки разбора.

ЛОВУШКА ВСПЛЫВАЛА ТРИЖДЫ, И ВСЯКИЙ РАЗ ПО-НОВОМУ. Как только рынок отбирался
по одному числу, наверх поднимались ПУСТЫЕ книги:

    по награде к ликвидности   бидов на $2,  асков на $15,     спред 0.889
    по относительному спреду   бидов на $0,  асков на $11 849, спред 200%
    по ширине спреда           бид 0.14 против аска 0.70 — никто не котирует
                               теснее не по щедрости, а потому что исход неясен

Второй случай особенно поучителен. Дешёвый лонгшот НИКТО не хочет покупать по
два цента, зато многие хотят продать. Наш бид оказался бы единственным: нас
исполнят немедленно, и мы получим ровно тот хвост дешёвых позиций, на котором
разобранный кошелёк держит переоценку -$8 564 при 22% позиций в плюсе.

Отсюда все проверки ниже: отбор смотрит НАСТОЯЩИЙ стакан, требует денег с обеих
сторон и меряет глубину в долларах, а не в контрактах.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import params, selector  # noqa: E402


def _market(daily=10.0, liquidity=5000.0, price=0.5, spread=0.02,
            volume=100_000, order_min=5):
    return {
        'id': 'M', 'question': 'вопрос', 'conditionId': 'C',
        'clobTokenIds': json.dumps(['TOKEN']),
        'outcomePrices': json.dumps([str(price), str(round(1 - price, 4))]),
        'clobRewards': [{'rewardsDailyRate': daily}],
        'rewardsMinSize': 20, 'rewardsMaxSpread': 4.5,
        'liquidity': liquidity, 'spread': spread, 'volume': volume,
        'orderMinSize': order_min, 'orderPriceMinTickSize': 0.01,
        'endDate': '2027-01-01T00:00:00Z',
    }


class TestQuoteCost:

    def test_both_sides_together_equal_the_size(self):
        """
        Двусторонняя котировка стоит РОВНО размер, при любой цене.

        Покупка берёт p за контракт, продажа — (1-p): продавать надо то, чего у
        нас нет. В сумме единица. Отсюда и вся арифметика малого счёта: пять
        контрактов стоят $5, и сотня долларов — это двадцать рынков, а не пять.
        """
        for price in (0.02, 0.1, 0.5, 0.9):
            assert selector.quote_cost(5, price) == pytest.approx(5.0)


class TestScanLooksAtTheRealBook:

    def _scan(self, monkeypatch, markets, budget=100, book=None,
              trades=None):
        pages = [markets, []]
        monkeypatch.setattr(selector.client, '_get',
                            lambda url: pages.pop(0) if pages else [])
        monkeypatch.setattr(selector.params, 'PAUSE', 0)
        # Стакан подставляется: отбор смотрит настоящую книгу, и без неё ни
        # один рынок не проходит — именно этого мы и добивались.
        # Спред в ТРИ тика: ровно столько нужно, чтобы шагнуть внутрь с обеих
        # сторон и что-то оставить себе. При двух тиках отбор откажет — и это
        # проверяется отдельно ниже.
        default = {'bids': [(0.49, 2000.0)], 'asks': [(0.52, 2000.0)]}
        monkeypatch.setattr(selector.book_mod, 'fetch_many',
                            lambda tokens: {str(t): (book or default)
                                            for t in tokens})
        # ЛЕНТА ПОДСТАВЛЯЕТСЯ ТОЖЕ: отбор считает время ожидания, а без ленты
        # поток равен нулю и ждать пришлось бы вечно. Даём поток по обеим
        # сторонам, чтобы проверялись именно фильтры стакана, а не отсутствие
        # сделок.
        now = int(time.time())
        flow = ([{'price': 0.49, 'size': 500.0, 'side': 'SELL',
                  'asset': 'TOKEN', 'ts': now - 600},
                 {'price': 0.52, 'size': 500.0, 'side': 'BUY',
                  'asset': 'TOKEN', 'ts': now - 300}] if trades is None
                else trades)
        monkeypatch.setattr(selector.book_mod, 'tape',
                            lambda cid, limit=500: list(flow))
        return selector.scan(budget=budget, pages=2)

    def test_two_sided_book_is_accepted(self, monkeypatch):
        rows = self._scan(monkeypatch, [_market()])
        assert len(rows) == 1
        # Размер считается от ПОТОКА, поэтому на живом рынке он больше пятёрки.
        # Стоимость по-прежнему равна размеру: двусторонняя котировка берёт p
        # за покупку и (1-p) за продажу, в сумме единицу при любой цене.
        assert rows[0]['size'] >= 5
        assert rows[0]['cost'] == pytest.approx(rows[0]['size'])

    def test_quiet_market_falls_back_to_the_exchange_minimum(self, monkeypatch):
        """Слабый поток не даёт ставить меньше, чем разрешает биржа."""
        now = int(time.time())
        thin = [{'price': 0.49, 'size': 1.0, 'side': 'SELL',
                 'asset': 'TOKEN', 'ts': now - 3600},
                {'price': 0.52, 'size': 1.0, 'side': 'BUY',
                 'asset': 'TOKEN', 'ts': now - 1800}]
        rows = self._scan(monkeypatch, [_market()], trades=thin)
        if rows:
            assert rows[0]['size'] == 5

    def test_empty_bid_side_is_rejected(self, monkeypatch):
        """
        Односторонний стакан отсеивается, каким бы заманчивым ни выглядел.

        Дешёвый лонгшот с бидами на ноль и асками на одиннадцать тысяч — не
        возможность: наш бид окажется единственным, его снимут немедленно, и
        останется хвост, идущий к нулю.
        """
        one_sided = {'bids': [(0.02, 100.0)], 'asks': [(0.30, 400_000.0)]}
        assert self._scan(monkeypatch, [_market()], book=one_sided) == []

    def test_depth_is_measured_in_money_not_contracts(self, monkeypatch):
        """Тысяча контрактов по 0.002 — это два доллара, а не глубина."""
        thin = {'bids': [(0.002, 1000.0)], 'asks': [(0.004, 1000.0)]}
        assert self._scan(monkeypatch, [_market()], book=thin) == []

    def test_market_without_rewards_is_still_taken(self, monkeypatch):
        """
        Награда НЕ обязательна: доход даёт спред, награда идёт сверх.

        Прежняя версия брала только рынки с наградой и оставляла пять штук на
        сотню долларов. Разобранный кошелёк живёт не этим: 1 495 сделок в сутки
        на разнице цен.
        """
        assert len(self._scan(monkeypatch, [_market(daily=0.0)])) == 1

    def test_market_without_turnover_is_rejected(self, monkeypatch):
        """Стакан можно нарисовать; оборот — нет."""
        assert self._scan(monkeypatch, [_market(volume=10)]) == []

    def test_market_near_resolution_is_rejected(self, monkeypatch):
        """
        Рынок накануне разрешения не котируем — это самый крупный риск затеи.

        Мейкер зарабатывает полтора цента на контракте, а разрешение делает
        контракт нулём или единицей ЦЕЛИКОМ. Запас в пять контрактов по 0.20,
        застигнутый разрешением не в ту сторону, стоит доллар — тринадцать
        удачных кругов. Наклон против запаса разгружает за часы, а перед самым
        разрешением разгружать уже не у кого: ликвидность уходит первой.
        """
        soon = datetime.now(timezone.utc) + timedelta(hours=5)
        market = dict(_market(), endDate=soon.strftime('%Y-%m-%dT%H:%M:%SZ'))
        assert self._scan(monkeypatch, [market]) == []

    def test_market_far_from_resolution_is_kept(self, monkeypatch):
        far = datetime.now(timezone.utc) + timedelta(days=90)
        market = dict(_market(), endDate=far.strftime('%Y-%m-%dT%H:%M:%SZ'))
        assert len(self._scan(monkeypatch, [market])) == 1

    def test_missing_date_does_not_silently_drop_the_market(self):
        """Отсутствие даты — не «ноль часов», иначе отсеялось бы всё подряд."""
        assert selector._hours_left(None) == float('inf')
        assert selector._hours_left('не дата') == float('inf')

    def test_too_narrow_spread_is_rejected(self, monkeypatch):
        """Спред в один тик не покроет даже проскальзывания."""
        narrow = {'bids': [(0.4999, 5000.0)], 'asks': [(0.5001, 5000.0)]}
        assert self._scan(monkeypatch, [_market()], book=narrow) == []

    def test_two_tick_spread_is_rejected_because_we_cannot_step_inside(
            self, monkeypatch):
        """
        Двух тиков мало: шаг внутрь с обеих сторон сводит цены вместе.

        Условие здесь ТО ЖЕ, что в стратегии, и это намеренно. Пока его тут не
        было, отбор обещал 90 рынков и $450 вложений, а котировалось 22:
        стратегия отказывалась вставать в конец очереди на остальных 68. План
        расходился с делом ровно вчетверо.
        """
        two_ticks = {'bids': [(0.49, 2000.0)], 'asks': [(0.51, 2000.0)]}
        assert self._scan(monkeypatch, [_market()], book=two_ticks) == []

    def test_too_wide_spread_is_rejected(self, monkeypatch):
        """
        Слишком широкий спред — тоже отказ, и это не осторожность.

        Бид 0.14 против аска 0.70 означает, что никто не котирует теснее не по
        щедрости, а потому что исход неясен. Обе наши заявки исполнятся там
        только тогда, когда кому-то станет ясно, куда идёт цена.
        """
        wide = {'bids': [(0.14, 3000.0)], 'asks': [(0.70, 3000.0)]}
        assert self._scan(monkeypatch, [_market()], book=wide) == []


class TestAllocationSpreadsRisk:

    def _rows(self, count, cost_each=5.0, spread_share=0.1):
        return [{'id': str(i), 'cost': cost_each, 'spread_share': spread_share,
                 'rewards_daily': 0.0, 'liquidity': 1000.0,
                 'question': f'рынок {i}'} for i in range(count)]

    def test_budget_spreads_over_many_markets(self, monkeypatch):
        """
        Сотня долларов при пяти контрактах — это двадцать рынков.

        Ровно то, чего требовала исходная задача: работать на многих мелких
        событиях, а не на нескольких крупных.
        """
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 0.34)
        plan = selector.allocate(self._rows(50, cost_each=5.0), budget=100)
        assert len(plan['markets']) == 20
        assert plan['used'] == pytest.approx(100.0)

    def test_one_market_never_takes_the_whole_budget(self, monkeypatch):
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 0.34)
        plan = selector.allocate(self._rows(1, cost_each=100.0), budget=100)
        assert plan['markets'] == []

    def test_budget_is_never_exceeded(self, monkeypatch):
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        plan = selector.allocate(self._rows(40, cost_each=30.0), budget=100)
        assert plan['used'] <= 100.0

    def test_ceiling_is_reported_as_a_ceiling(self, monkeypatch):
        """
        Потолок показывается числом и называется потолком.

        Он предполагает, что обе стороны исполнились по нашим ценам и ни разу
        не против нас. Назвать его ожиданием значило бы выдать арифметику за
        обещание — ни того, ни другого мы пока не наблюдали.
        """
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        plan = selector.allocate(self._rows(20, cost_each=5.0, spread_share=0.1),
                                 budget=100)
        # Половина спреда на вложенное: 0.1 / 2 × $100 = $5 за полный оборот.
        assert plan['ceiling_per_round_usd'] == pytest.approx(5.0)
        assert 'expected' not in str(plan), 'слово «ожидаемый» здесь неуместно'


class TestWaitTimeDecides:
    """
    Время ожидания решает, а не ширина спреда. Исправление собственной ошибки.

    Однотиковые рынки отбрасывались на том основании, что впереди стоит 152
    контракта по медиане. Длина мерилась на МЕДЛЕННЫХ рынках и переносилась на
    все. Замер по 53 рынкам: у шести ожидание меньше часа при медиане 23
    минуты, и почти все они однотиковые. «Will Alexandria Ocasio-Cortez win...»
    — очередь 9 контрактов при потоке 530 в час, то есть ОДНА МИНУТА.

    Спред узок там именно потому, что торгуют, и там же очередь рассасывается.
    """

    def _book(self, bid=0.49, ask=0.50, size=100.0):
        return {'bids': [(bid, size)], 'asks': [(ask, size)]}

    def _rows(self, book, flow_size, tick=0.01):
        """
        Цены ленты берутся ИЗ СТАКАНА, а не назначаются числом.

        Первая версия ставила 0.49 и 0.50 при биде 0.40: продажа по 0.49 нашу
        заявку на 0.40 не исполняет, и поток выходил нулевым. Ошибка была в
        фикстуре, а не в коде — но нашлась она только потому, что код ответил
        «ждать вечно» вместо того, чтобы промолчать.
        """
        now = int(time.time())
        top = selector.book_mod.top(book)
        return ([{'token_id': 'T', 'condition_id': 'C', 'tick': tick,
                  'size': 5.0, 'top': top}],
                [{'price': top['bid'], 'size': flow_size, 'side': 'SELL',
                  'asset': 'T', 'ts': now - 3600},
                 {'price': top['ask'], 'size': flow_size, 'side': 'BUY',
                  'asset': 'T', 'ts': now - 1800}])

    def test_fast_market_with_a_queue_is_kept(self, monkeypatch):
        """Очередь в 100 контрактов при потоке 1000 в час — это шесть минут."""
        book = self._book(size=100.0)
        rows, tape = self._rows(book, flow_size=1000.0)
        monkeypatch.setattr(selector.book_mod, 'tape',
                            lambda cid, limit=500: tape)
        got = selector.measure_wait(rows, {'T': book})
        assert got[0]['wait_hours'] < 1.0
        assert not got[0]['step_inside'], 'один тик — встаём в очередь'

    def test_slow_market_with_the_same_queue_is_rejected(self, monkeypatch):
        """
        Та же очередь при потоке в 2 контракта в час — это сутки.

        Один и тот же стакан даёт противоположный ответ в зависимости от
        потока. Именно поэтому мерить длину очереди в отрыве от скорости
        бессмысленно.
        """
        book = self._book(size=100.0)
        rows, tape = self._rows(book, flow_size=2.0)
        monkeypatch.setattr(selector.book_mod, 'tape',
                            lambda cid, limit=500: tape)
        got = selector.measure_wait(rows, {'T': book})
        assert got[0]['wait_hours'] > params.MM_MAX_WAIT_HOURS

    def test_wide_spread_steps_inside_and_waits_only_for_itself(self, monkeypatch):
        """Шагнув внутрь, ждём только собственный размер: очередь нулевая."""
        book = {'bids': [(0.40, 5000.0)], 'asks': [(0.45, 5000.0)]}
        rows, tape = self._rows(book, flow_size=500.0)
        monkeypatch.setattr(selector.book_mod, 'tape',
                            lambda cid, limit=500: tape)
        got = selector.measure_wait(rows, {'T': book})
        assert got[0]['step_inside'] is True
        assert got[0]['wait_hours'] < 0.1, 'чужая очередь нас не задерживает'

    def test_market_without_flow_waits_forever(self, monkeypatch):
        monkeypatch.setattr(selector.book_mod, 'tape', lambda cid, limit=500: [])
        book = self._book()
        rows, _ = self._rows(book, flow_size=0.0)
        got = selector.measure_wait(rows, {'T': book})
        assert got[0]['wait_hours'] == float('inf')


class TestBalanceIsMeasuredInContracts:
    """
    Перекос сторон меряется в КОНТРАКТАХ. В деньгах он врёт по построению.

    При равной глубине в контрактах бид на цене 0.90 стоит $900, а аск $100:
    книга выглядит перекошенной девятикратно, будучи идеально симметричной.
    Так отбраковывался каждый рынок дороже 0.80 и дешевле 0.20 — независимо от
    того, что в нём происходит.

    Глубина при этом по-прежнему считается в деньгах, и это не противоречие:
    глубина спрашивает «сколько стоит эта сторона», перекос — «одинаково ли
    готовы покупать и продавать».
    """

    def _scan(self, monkeypatch, book):
        pages = [[_market()], []]
        monkeypatch.setattr(selector.client, '_get',
                            lambda url: pages.pop(0) if pages else [])
        monkeypatch.setattr(selector.params, 'PAUSE', 0)
        monkeypatch.setattr(selector.book_mod, 'fetch_many',
                            lambda tokens: {str(t): book for t in tokens})
        now = int(time.time())
        top = selector.book_mod.top(book)
        monkeypatch.setattr(selector.book_mod, 'tape', lambda cid, limit=500: [
            {'price': top['bid'], 'size': 5000.0, 'side': 'SELL',
             'asset': 'TOKEN', 'ts': now - 3600},
            {'price': top['ask'], 'size': 5000.0, 'side': 'BUY',
             'asset': 'TOKEN', 'ts': now - 1800}])
        return selector.scan(budget=100, pages=2)

    def test_expensive_symmetric_book_is_kept(self, monkeypatch):
        """
        Дорогой рынок с СИММЕТРИЧНОЙ книгой обязан проходить.

        Прежде он отбраковывался всегда: $900 против $100 при равных
        контрактах. Так терялись лучшие рынки — один из них ждал исполнения
        семь минут и давал $0.25 в час.
        """
        book = {'bids': [(0.88, 2000.0)], 'asks': [(0.91, 2000.0)]}
        assert len(self._scan(monkeypatch, book)) == 1

    def test_truly_one_sided_book_is_still_rejected(self, monkeypatch):
        """Настоящий перекос — в контрактах — по-прежнему отсекается."""
        book = {'bids': [(0.88, 20.0)], 'asks': [(0.91, 20000.0)]}
        assert self._scan(monkeypatch, book) == []


class TestCheapSpreadIsNotRejectedForBeingRelativelyNarrow:

    def test_high_priced_market_with_small_relative_spread_is_kept(
            self, monkeypatch):
        """
        Спред в восемь тиков при цене 0.80 — это 1% от неё, и прежде рынок
        отсеивался как «узкий». Решать должно время ожидания, а не проценты.
        """
        book = {'bids': [(0.80, 3000.0)], 'asks': [(0.88, 3000.0)]}
        pages = [[_market()], []]
        monkeypatch.setattr(selector.client, '_get',
                            lambda url: pages.pop(0) if pages else [])
        monkeypatch.setattr(selector.params, 'PAUSE', 0)
        monkeypatch.setattr(selector.book_mod, 'fetch_many',
                            lambda tokens: {str(t): book for t in tokens})
        now = int(time.time())
        monkeypatch.setattr(selector.book_mod, 'tape', lambda cid, limit=500: [
            {'price': 0.80, 'size': 9000.0, 'side': 'SELL',
             'asset': 'TOKEN', 'ts': now - 3600},
            {'price': 0.88, 'size': 9000.0, 'side': 'BUY',
             'asset': 'TOKEN', 'ts': now - 1800}])
        assert len(selector.scan(budget=100, pages=2)) == 1


class TestBothTokensCount:
    """
    Сделки ОБОИХ токенов приводятся к нашей стороне.

    ЗДЕСЬ ПРЯТАЛАСЬ САМАЯ КРУПНАЯ ОШИБКА ЗАМЕРА. У бинарного рынка два токена,
    и покупка «нет» по цене q экономически есть продажа «да» по цене (1-q).
    Считая сделки только своего токена, отбор видел меньшую часть потока:

        «Will Nigel Farage win at least 80%» — 25 продаж «да» в нашем счёте
        против 363 настоящих, потому что 338 покупок «нет» не считались вовсе.

    Из-за этого 276 рынков из 327 объявлялись «без потока с одной стороны».
    Отбор считал мёртвыми ровно те рынки, где торговля шла через встречный
    токен, — и выбрасывал их.
    """

    def _t(self, asset, side, price, size=10.0):
        return {'asset': asset, 'side': side, 'price': price, 'size': size,
                'ts': 1}

    def test_buying_no_is_selling_yes(self):
        got = selector.as_yes([self._t('NO', 'BUY', 0.70)], 'YES', 'NO')
        assert len(got) == 1
        assert got[0]['side'] == 'SELL'
        assert got[0]['price'] == pytest.approx(0.30)

    def test_selling_no_is_buying_yes(self):
        got = selector.as_yes([self._t('NO', 'SELL', 0.75)], 'YES', 'NO')
        assert got[0]['side'] == 'BUY'
        assert got[0]['price'] == pytest.approx(0.25)

    def test_our_own_token_passes_through_untouched(self):
        row = self._t('YES', 'BUY', 0.30)
        got = selector.as_yes([row], 'YES', 'NO')
        assert got[0]['side'] == 'BUY' and got[0]['price'] == 0.30

    def test_size_is_not_mirrored(self):
        """Контракт «нет» и контракт «да» — одна и та же единица."""
        got = selector.as_yes([self._t('NO', 'BUY', 0.70, size=42.0)],
                              'YES', 'NO')
        assert got[0]['size'] == 42.0

    def test_foreign_asset_is_ignored(self):
        assert selector.as_yes([self._t('ЧУЖОЙ', 'BUY', 0.5)], 'YES', 'NO') == []

    def test_market_without_a_second_token_still_works(self):
        got = selector.as_yes([self._t('YES', 'BUY', 0.3),
                               self._t('NO', 'BUY', 0.7)], 'YES', None)
        assert len(got) == 1, 'без второго токена считаем только свой'

    def test_one_way_flow_becomes_two_way_after_the_fix(self):
        """
        Ровно тот случай, из-за которого рынки признавались мёртвыми: по
        нашему токену идут только продажи, а покупки — через встречный.
        """
        trades = [self._t('YES', 'SELL', 0.30), self._t('YES', 'SELL', 0.30),
                  self._t('NO', 'SELL', 0.68), self._t('NO', 'SELL', 0.68)]
        got = selector.as_yes(trades, 'YES', 'NO')
        assert {t['side'] for t in got} == {'BUY', 'SELL'}


class TestWaitFollowsTheSize:
    """
    Ожидание пересчитывается под ОКОНЧАТЕЛЬНЫЙ размер заявки.

    Ошибка, пойманная на себе: размер поднимался с 5 до 34 контрактов по
    потоку, а ожидание оставалось посчитанным от пятёрки. Доход выходил
    завышенным ровно во столько же раз — получалось, что 34 контракта
    исполняются за две минуты при потоке 68 контрактов в час.

    Потолок дохода после этой правки упал с $3 202 в месяц до правдоподобного.
    Число было красивым и неверным.
    """

    def _row(self, size, flow=100.0, queue=0.0, gain=0.02):
        return {'size': size, 'flow_in': flow, 'flow_out': flow,
                'queue_in': queue, 'queue_out': queue, 'our_gain': gain}

    def test_bigger_order_waits_longer(self):
        small = selector._recompute_wait(self._row(5))['wait_hours']
        big = selector._recompute_wait(self._row(50))['wait_hours']
        assert big > small

    def test_ten_times_the_size_is_not_ten_times_the_income(self):
        """
        Удесятерив заявку, мы не удесятеряем доход: ждать придётся дольше.
        Именно это и терялось, когда время не пересчитывалось.
        """
        small = selector._recompute_wait(self._row(5))['usd_per_hour']
        big = selector._recompute_wait(self._row(50))['usd_per_hour']
        assert big < small * 10

    def test_wait_matches_the_flow_it_needs(self):
        """Пятьдесят контрактов при потоке сто в час — полчаса на сторону."""
        got = selector._recompute_wait(self._row(50, flow=100.0))
        assert got['wait_hours'] == pytest.approx(1.0)

    def test_no_flow_means_no_income(self):
        got = selector._recompute_wait(self._row(5, flow=0.0))
        assert got['wait_hours'] == float('inf')
        assert got['usd_per_hour'] == 0.0
