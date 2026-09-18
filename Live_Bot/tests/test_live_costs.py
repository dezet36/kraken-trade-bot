"""
Боевая сделка обязана знать, во что обошлась.

ОТКУДА ЭТИ ПРОВЕРКИ. До 30 августа 2026 боевой путь не считал комиссии вообще:
поиск слов «fee» и «funding» по trade_manager, bot и exchange не давал ни
одного совпадения, а итог сделки был грязным. Бумажный двойник за август
показал цену такой слепоты: грязный итог +$34.94, комиссии −$661.03, чистый
−$616.77 — то есть весь убыток это издержки, и единственное, что имело
значение, боевой журнал не записывал.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ. Арифметика издержек, приоритет замеренного над
посчитанным и то, что итог в журнале стал чистым. Проверки СЧИТАЮТ ЧИСЛА, а не
ищут строки в исходнике: сверка с текстом кода ловит переименование, но не
ошибку в формуле, а здесь опасна именно формула.
"""

import csv
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import live_costs


def make_position(entry=100.0, size=10.0, direction='LONG', entry_mode='MARKET',
                  tp_hit=0, remaining=None, held_hours=1.0):
    """Позиция в том виде, в каком её строит trade_manager."""
    opened = datetime(2026, 8, 30, 12, 0, 0)
    return {
        'signal': {'setup': {'type': direction}},
        'params': {
            'position_size': size,
            'stop_loss': entry * (0.98 if direction == 'LONG' else 1.02),
            'tp_targets': [entry * 1.01, entry * 1.02, entry * 1.03],
            'tp_fractions': [0.4, 0.3, 0.3],
        },
        'entry_price': entry,
        'remaining_size': size if remaining is None else remaining,
        'tp_hit': tp_hit,
        'entry_time': opened,
        'exit_time': opened + timedelta(hours=held_hours),
        '_lifecycle': {'entry_mode': entry_mode},
    }


class FakeClient:
    """Биржа, отвечающая тем, чем её зарядили."""

    def __init__(self, fills=None, funding=None, raises=False):
        self.has = {'fetchMyTrades': True, 'fetchFundingHistory': True}
        self._fills = fills or []
        self._funding = funding or []
        self._raises = raises
        self.asked_since = None

    def fetch_my_trades(self, pair, since=None, limit=None):
        if self._raises:
            raise RuntimeError('биржа недоступна')
        self.asked_since = since
        return self._fills

    def fetch_funding_history(self, pair, since=None, limit=None):
        return self._funding


# ── Арифметика оценки ────────────────────────────────────────────────────────

def test_market_entry_charges_taker_on_both_ends():
    """Вход рынком и выход рынком — тейкер дважды."""
    pos = make_position(entry=100.0, size=10.0)
    fees, _ = live_costs.estimate(pos, exit_price=99.0)

    expected = 10 * 100 * config.PAPER_FEE_TAKER + 10 * 99 * config.PAPER_FEE_TAKER
    assert fees == pytest.approx(expected)


def test_limit_entry_is_cheaper_than_market():
    """
    Лимитный вход стоит ставку мейкера, рыночный — тейкера.

    Разница почти втрое, и она не косметическая: именно на ней держится смысл
    отложенных ордеров. Если бы учёт брал одну ставку на оба случая, выгода
    лимитного входа не проявилась бы в журнале никогда.
    """
    market = live_costs.estimate(make_position(entry_mode='MARKET'), 99.0)[0]
    limit = live_costs.estimate(make_position(entry_mode='LIMIT'), 99.0)[0]

    assert limit < market
    # Экономия ровно на разнице ставок по объёму входа.
    assert market - limit == pytest.approx(
        10 * 100 * (config.PAPER_FEE_TAKER - config.PAPER_FEE_MAKER))


def test_unknown_entry_mode_assumes_the_expensive_one():
    """
    Без следа о типе входа считаем тейкера.

    Занизить издержки хуже, чем завысить: заниженные создают видимость
    преимущества, которого нет, и именно так убыточная стратегия выглядит
    рабочей.
    """
    pos = make_position()
    pos['_lifecycle'] = {}
    assert live_costs.estimate(pos, 99.0)[0] == pytest.approx(
        live_costs.estimate(make_position(entry_mode='MARKET'), 99.0)[0])


def test_partial_takes_add_maker_fees():
    """Отработавшие цели закрываются биржевым лимитом и тоже стоят денег."""
    plain = live_costs.estimate(make_position(tp_hit=0, remaining=10.0), 99.0)[0]
    after_tp = live_costs.estimate(
        make_position(tp_hit=1, remaining=6.0), 99.0)[0]

    # Доля 0.4 от размера 10 ушла по цене 101 по ставке мейкера.
    partial = 10 * 0.4 * 101 * config.PAPER_FEE_MAKER
    # А остаток на выходе стал меньше на ту же долю.
    exit_saved = 10 * 0.4 * 99 * config.PAPER_FEE_TAKER
    assert after_tp - plain == pytest.approx(partial - exit_saved)


def test_last_target_is_not_charged_as_partial():
    """
    Последняя цель уходит через полное закрытие, а не как частичная.

    Иначе её объём посчитали бы дважды: один раз лимитом, второй — в остатке.
    """
    pos = make_position(tp_hit=3, remaining=3.0)
    fees, _ = live_costs.estimate(pos, 103.0)

    entry_fee = 10 * 100 * config.PAPER_FEE_TAKER
    partials = (10 * 0.4 * 101 + 10 * 0.3 * 102) * config.PAPER_FEE_MAKER
    exit_fee = 3 * 103 * config.PAPER_FEE_TAKER
    assert fees == pytest.approx(entry_fee + partials + exit_fee)


def test_no_funding_before_the_first_period():
    """Сделка короче восьми часов фандинга не платит."""
    assert live_costs.estimate(make_position(held_hours=7.9), 99.0)[1] == 0.0


def test_funding_hits_long_and_pays_short():
    """
    Длинная сторона платит, короткая получает — при положительной ставке.

    Знак важнее величины: перепутав его, учёт превратит издержку в доход и
    стратегия, держащая позиции сутками, получит незаслуженную фору.
    """
    long_funding = live_costs.estimate(
        make_position(direction='LONG', held_hours=9), 99.0)[1]
    short_funding = live_costs.estimate(
        make_position(direction='SHORT', held_hours=9), 99.0)[1]

    assert long_funding > 0
    assert short_funding == pytest.approx(-long_funding)


def test_broken_position_costs_nothing_rather_than_crashing():
    """Позиция без цены и размера не должна ронять закрытие сделки."""
    pos = make_position()
    pos['entry_price'] = 0
    assert live_costs.estimate(pos, 99.0) == (0.0, 0.0)


# ── Замеренное важнее посчитанного ───────────────────────────────────────────

def test_exchange_answer_wins_over_the_estimate():
    """
    Когда биржа сказала, сколько удержала, — берём её число.

    Наш тариф это модель, а удержание — факт. Модель ошибается на скидках,
    на разных тарифах пар и на изменении условий, о котором нам не сообщат.
    """
    client = FakeClient(fills=[{'fee': {'cost': 0.4}}, {'fee': {'cost': 0.35}}])
    out = live_costs.settle(make_position(), 99.0, client, 'BTCUSDT')

    assert out['fees_usd'] == pytest.approx(0.75)
    assert out['fees_source'] == 'exchange'


def test_costs_are_positive_whichever_sign_the_exchange_uses():
    """Биржи пишут удержание то со знаком минус, то без — издержка одна."""
    client = FakeClient(fills=[{'fee': {'cost': -0.4}}, {'fee': {'cost': 0.35}}])
    assert live_costs.settle(
        make_position(), 99.0, client, 'BTCUSDT')['fees_usd'] == pytest.approx(0.75)


def test_fee_under_the_plural_key_is_also_counted():
    """У части бирж комиссия лежит в fees списком, а не в fee словарём."""
    client = FakeClient(fills=[{'fees': [{'cost': 0.6}]}])
    out = live_costs.settle(make_position(), 99.0, client, 'BTCUSDT')
    assert out['fees_usd'] == pytest.approx(0.6)
    assert out['fees_source'] == 'exchange'


def test_unreachable_exchange_falls_back_to_the_estimate():
    """Сбой биржи оставляет сделку с оценкой, а не с нулём издержек."""
    out = live_costs.settle(make_position(), 99.0, FakeClient(raises=True), 'BTCUSDT')

    assert out['fees_source'] == 'estimate'
    assert out['fees_usd'] == pytest.approx(
        round(live_costs.estimate(make_position(), 99.0)[0], 4))


def test_empty_fill_history_is_not_trusted_as_zero():
    """
    Пустой ответ об исполнениях — это отказ, а не бесплатная сделка.

    Исполнений не может не быть: вход состоялся. Приняв пустоту за ноль, учёт
    записал бы нулевые издержки и вернул бы ровно ту слепоту, ради устранения
    которой всё это писалось.
    """
    out = live_costs.settle(make_position(), 99.0, FakeClient(fills=[]), 'BTCUSDT')

    assert out['fees_source'] == 'estimate'
    assert out['fees_usd'] > 0


def test_missing_client_still_produces_costs():
    """Без клиента биржи остаётся оценка — но не пустота."""
    out = live_costs.settle(make_position(), 99.0, None, 'BTCUSDT')
    assert out['fees_source'] == 'estimate'
    assert out['fees_usd'] > 0


def test_funding_from_exchange_flips_sign_to_expense():
    """
    Списание у биржи отрицательное, издержка — положительная.

    Не перевернув знак, учёт вычел бы отрицательное число и УВЕЛИЧИЛ итог на
    величину фандинга.
    """
    client = FakeClient(fills=[{'fee': {'cost': 0.5}}],
                        funding=[{'amount': -0.2}, {'amount': -0.1}])
    out = live_costs.settle(make_position(held_hours=20), 99.0, client, 'BTCUSDT')
    assert out['funding_usd'] == pytest.approx(0.3)


def test_history_is_requested_from_before_the_entry():
    """
    Окно запроса начинается РАНЬШЕ входа.

    Биржа отдаёт исполнения строго позже since, а исполнение входа приходится
    ровно на время входа — запросив с той же секунды, мы потеряли бы самую
    крупную комиссию сделки.
    """
    pos = make_position()
    client = FakeClient(fills=[{'fee': {'cost': 0.5}}])
    live_costs.settle(pos, 99.0, client, 'BTCUSDT')
    assert client.asked_since < int(pos['entry_time'].timestamp() * 1000)


# ── Журнал ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def journal(tmp_path, monkeypatch):
    import trade_journal
    monkeypatch.setattr(trade_journal, 'JOURNAL_FILE', str(tmp_path / 'trades.csv'))
    monkeypatch.setattr(trade_journal, 'DETAIL_JSONL', str(tmp_path / 'detail.jsonl'))
    monkeypatch.setattr(trade_journal, 'COUNTER_FILE',
                        str(tmp_path / 'counter.txt'), raising=False)
    return trade_journal


def opened_position(journal_module, entry=100.0, size=10.0, risk=20.0):
    signal = {
        'trading_pair': 'BTCUSDT',
        'strategy': 'FIBO',
        'htf_trend': 'UP',
        'setup': {'type': 'LONG', 'size': 5.0,
                  'start_price': 95.0, 'end_price': 100.0},
        'trigger': {'zone': 'Zone_A'},
        'params': {'entry': entry, 'stop_loss': 98.0,
                   'take_profit_1': 102.0, 'take_profit_2': 104.0,
                   'rr': 2.0, 'risk_amount': risk, 'position_size': size,
                   'leverage': 10},
        'atr_pct': 1.35,
    }
    position = {'signal': {'setup': signal['setup']}, 'params': signal['params'],
                'remaining_size': size, 'tp_hit': 0,
                'entry_time': datetime(2026, 8, 30, 12, 0, 0),
                '_lifecycle': {'entry_mode': 'LIMIT'}}
    journal_module.open_trade(position, signal, balance_before=1000.0)
    return position


def test_journal_records_net_result_and_its_parts(journal):
    """
    В журнал уходит чистый итог, а рядом — из чего он сложился.

    Одного чистого числа мало: без грязного итога и суммы издержек нельзя
    ответить, стратегия не работает или её съедают комиссии. Разбор августа
    упёрся ровно в этот вопрос.
    """
    position = opened_position(journal)
    costs = {'fees_usd': 1.2, 'funding_usd': 0.3, 'fees_source': 'exchange'}
    row = journal.close_trade(position, 101.0, 'TP1', pnl_usd=8.5,
                              balance_after=1008.5, import_config=config,
                              gross_pnl=10.0, costs=costs)

    assert row['pnl_usd'] == 8.5
    assert row['gross_pnl_usd'] == 10.0
    assert row['fees_usd'] == 1.2
    assert row['funding_usd'] == 0.3
    assert row['fees_source'] == 'exchange'


def test_journal_records_result_in_risk_units(journal):
    """
    Итог в R. Доллары у BTC и SHIB значат разное, R — одно и то же.

    Без этой колонки сделки по разным парам и с разным плечом нельзя сложить
    в один ряд, а именно так считается всё: и матожидание, и его погрешность.
    """
    position = opened_position(journal, risk=20.0)
    row = journal.close_trade(position, 101.0, 'TP1', pnl_usd=10.0,
                              balance_after=1010.0, import_config=config,
                              gross_pnl=11.5,
                              costs={'fees_usd': 1.5, 'funding_usd': 0.0,
                                     'fees_source': 'estimate'})
    assert row['pnl_r'] == pytest.approx(0.5)


def test_journal_records_cost_share_of_risk(journal):
    """
    Доля риска, съеденная комиссиями, зависит только от тесноты стопа.

    Она известна ЗАРАНЕЕ, при входе, и записывается ради проверки на новых
    данных догадки, что дешёвые входы прибыльнее дорогих: на августовской
    выборке разница была, но порог выбирался из семи опробованных, и с
    поправкой на перебор p поднимался с 0.012 до ≈0.08.
    """
    position = opened_position(journal, entry=100.0)     # стоп 98 -> дистанция 2
    row = journal.close_trade(position, 101.0, 'TP1', pnl_usd=10.0,
                              balance_after=1010.0, import_config=config,
                              gross_pnl=11.5,
                              costs={'fees_usd': 1.5, 'funding_usd': 0.0,
                                     'fees_source': 'estimate'})

    expected = 100.0 / 2.0 * config.ENTRY_COST_ROUND_TRIP * 100
    assert row['cost_share_pct'] == pytest.approx(round(expected, 2))


def test_journal_keeps_entry_conditions(journal):
    """Обстановка на момент решения: размах свечей и час суток."""
    position = opened_position(journal)
    row = journal.close_trade(position, 101.0, 'TP1', pnl_usd=1.0,
                              balance_after=1001.0, import_config=config)
    assert row['atr_pct'] == 1.35
    assert 0 <= row['hour_utc'] <= 23


def test_journal_survives_a_call_without_costs(journal):
    """Старый вызов без издержек должен писать строку, а не падать."""
    position = opened_position(journal)
    row = journal.close_trade(position, 101.0, 'TP1', pnl_usd=1.0,
                              balance_after=1001.0, import_config=config)
    assert row is not None
    assert 'fees_usd' not in row or row.get('fees_usd') in (None, '')


def test_extremes_and_their_moments_reach_the_csv(journal):
    """
    Насколько цена уходила и КОГДА — обе величины должны дойти до CSV.

    Величины считались и раньше, но терялись: DictWriter получает
    extrasaction='ignore' и молча выбрасывал всё, чего нет в COLUMNS. В
    trades_detail.jsonl они были, в CSV нет — а панель читает CSV.
    """
    position = opened_position(journal)
    position['mfe_price'] = 103.0
    position['mae_price'] = 99.0
    position['mfe_ts'] = position['entry_time'] + timedelta(minutes=45)
    position['mae_ts'] = position['entry_time'] + timedelta(minutes=12)

    journal.close_trade(position, 101.0, 'TP1', pnl_usd=1.0,
                        balance_after=1001.0, import_config=config)

    with open(journal.JOURNAL_FILE, encoding='utf-8-sig', newline='') as fh:
        saved = list(csv.DictReader(fh))[0]

    assert float(saved['mfe_r']) == pytest.approx(1.5)    # +3 при стопе 2
    assert float(saved['mae_r']) == pytest.approx(-0.5)
    assert int(saved['mfe_min']) == 45
    assert int(saved['mae_min']) == 12


def test_new_columns_do_not_shift_old_rows(journal):
    """
    Дописанная колонка не имеет права сдвинуть значения прежних строк.

    Шапка CSV пишется один раз, строки — всегда по текущему COLUMNS. Без
    переноса шапки значения съезжают влево, и в колонке «результат»
    оказывается баланс. Файл при этом открывается и выглядит правдоподобно.
    """
    old_columns = ['trade_id', 'mode', 'open_time', 'pair', 'result']
    with open(journal.JOURNAL_FILE, 'w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=old_columns)
        writer.writeheader()
        writer.writerow({'trade_id': 1, 'mode': 'LIVE',
                         'open_time': '2026-08-01T10:00:00',
                         'pair': 'ETHUSDT', 'result': 'WIN'})

    position = opened_position(journal)
    journal.close_trade(position, 101.0, 'TP1', pnl_usd=1.0,
                        balance_after=1001.0, import_config=config)

    with open(journal.JOURNAL_FILE, encoding='utf-8-sig', newline='') as fh:
        rows = list(csv.DictReader(fh))

    assert len(rows) == 2
    # Старая строка сохранила СВОИ значения в своих колонках.
    assert rows[0]['pair'] == 'ETHUSDT'
    assert rows[0]['result'] == 'WIN'
    assert rows[0]['fees_usd'] == ''
    # Новая строка встала под теми же именами.
    assert rows[1]['pair'] == 'BTCUSDT'
