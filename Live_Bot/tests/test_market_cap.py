"""
Рынок в целом: USDT.D, BTC.D, TOTAL2 по методике TradingView своими руками.

Формула: капа = цена × предложение, TOTAL — сумма по топ-монетам,
USDT.D = капа USDT ÷ TOTAL. Цены — Bybit, предложение — снимок раз в час.
Возраст данных — часть результата: старое становится прочерком, а не
последним известным числом.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config           # noqa: E402
import market_cap       # noqa: E402

DAY = 86_400_000

COINS = [
    {'id': 'bitcoin', 'symbol': 'BTC', 'supply': 20_000_000, 'price': 80_000},
    {'id': 'ethereum', 'symbol': 'ETH', 'supply': 120_000_000, 'price': 2_600},
    {'id': 'tether', 'symbol': 'USDT', 'supply': 170_000_000_000, 'price': 1.0},
    {'id': 'solana', 'symbol': 'SOL', 'supply': 500_000_000, 'price': 110},
    {'id': 'obscure', 'symbol': 'OBS', 'supply': 1_000_000_000, 'price': 0.5, 'volume_pct': 3.0},   # нет на Bybit, но торгуется
    {'id': 'fund', 'symbol': 'BUIDL', 'supply': 2_000_000_000, 'price': 1.0, 'volume_pct': 0.01},   # фонд: рынка нет
]
TICKERS = {
    'BTCUSDT': {'last': 81_000, 'pct24': 1.0},
    'ETHUSDT': {'last': 2_700, 'pct24': 2.0},
    'SOLUSDT': {'last': 100, 'pct24': -3.0},
}


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(market_cap, '_supply_cache', None)
    monkeypatch.setattr(market_cap, '_last_collect', 0.0)


class TestTheFormulaIsTradingViews:
    def test_caps_are_price_times_supply_with_bybit_prices_first(self):
        row = market_cap.compute(COINS, TICKERS)
        btc = 81_000 * 20_000_000
        eth = 2_700 * 120_000_000
        usdt = 170_000_000_000
        sol = 100 * 500_000_000
        obs = 0.5 * 1_000_000_000                   # цена из снимка — на Bybit пары нет
        total = btc + eth + usdt + sol + obs
        assert row['total'] == pytest.approx(total)
        assert row['usdt_d'] == pytest.approx(usdt / total * 100)
        assert row['btc_d'] == pytest.approx(btc / total * 100)
        assert row['total2'] == pytest.approx(total - btc)
        assert row['total3'] == pytest.approx(total - btc - eth)
        assert row['priced_on_bybit'] == 3 and row['coins'] == 5, 'фонд без рынка не считается'

    def test_untraded_assets_are_left_out_like_tradingview_does(self):
        """
        FIGR_HELOC, BUIDL, USYC и прочие фонды в топ-125 CoinGecko раздували
        TOTAL на 68 млрд против TradingView. Нет рынка на Bybit и оборот
        меньше 0.5% капы — актив не торгуется и в сумму не входит.
        """
        with_fund = market_cap.compute(COINS, TICKERS)
        without = market_cap.compute([c for c in COINS if c['symbol'] != 'BUIDL'], TICKERS)
        assert with_fund['total'] == pytest.approx(without['total'])
        assert market_cap.tradable({'symbol': 'LEO', 'volume_pct': 0.05}, None) is False
        assert market_cap.tradable({'symbol': 'ZEC', 'volume_pct': 8.0}, None) is True
        assert market_cap.tradable({'symbol': 'SOL', 'volume_pct': 0.0}, {'last': 1}) is True

    def test_usdt_itself_is_one_dollar(self):
        """У USDT нет пары к USDT — цена 1, как индекс USDTUSD у TradingView."""
        row = market_cap.compute(COINS, {**TICKERS, 'USDTUSDT': {'last': 5.0, 'pct24': 0}})
        assert row['usdt_cap'] == pytest.approx(170_000_000_000)

    def test_breadth_counts_only_coins_priced_on_bybit(self):
        row = market_cap.compute(COINS, TICKERS)
        assert row['counted_24h'] == 3 and row['up_24h'] == 2   # BTC и ETH в плюсе, SOL в минусе

    def test_without_btc_or_usdt_there_is_no_answer(self):
        assert market_cap.compute([c for c in COINS if c['symbol'] != 'USDT'], TICKERS) is None


class TestSupplyIsRefreshedHourlyAndSurvivesOutages:
    def test_fetched_once_per_hour(self):
        calls = []

        def fetch():
            calls.append(1)
            return COINS
        market_cap.supply(now=1000, fetch=fetch)
        market_cap.supply(now=1000 + 1800, fetch=fetch)
        assert len(calls) == 1, 'в пределах часа — кэш'
        market_cap.supply(now=1000 + 3601, fetch=fetch)
        assert len(calls) == 2

    def test_an_outage_keeps_the_last_snapshot(self):
        market_cap.supply(now=1000, fetch=lambda: COINS)

        def boom():
            raise RuntimeError('CoinGecko 429')
        got = market_cap.supply(now=1000 + 7200, fetch=boom)
        assert got['coins'] == COINS and got['at'] == 1000

    def test_the_snapshot_is_on_disk_for_the_next_start(self, monkeypatch):
        market_cap.supply(now=1000, fetch=lambda: COINS)
        monkeypatch.setattr(market_cap, '_supply_cache', None)
        got = market_cap.supply(now=1500, fetch=lambda: (_ for _ in ()).throw(AssertionError('не звать')))
        assert got['coins'] == COINS


class FakeClient:
    def __init__(self, tickers=TICKERS):
        self.tickers = tickers

    def publicGetV5MarketTickers(self, params):
        assert params == {'category': 'spot'}
        return {'result': {'list': [
            {'symbol': sym, 'lastPrice': str(t['last']), 'price24hPcnt': str(t['pct24'] / 100)}
            for sym, t in self.tickers.items()]}}


def _seed(points):
    """points: [(ts_ms, usdt_d, btc_d, total)] -> ряд на диске."""
    with open(market_cap.store_path(), 'w', encoding='utf-8') as fh:
        for ts, usdt_d, btc_d, total in points:
            row = {'ts': ts, 'usdt_d': usdt_d, 'btc_d': btc_d, 'total': total,
                   'total2': total * (1 - btc_d / 100), 'total3': 0, 'usdt_cap': 0,
                   'coins': 125, 'priced_on_bybit': 110, 'up_24h': 40, 'counted_24h': 110,
                   'supply_at': ts - 600_000}
            fh.write(json.dumps(row) + chr(10))


class TestCollectionWritesTheSeries:
    def test_a_point_per_cycle_with_the_supply_stamp(self):
        market_cap.supply(now=1000, fetch=lambda: COINS)
        row = market_cap.collect(FakeClient(), now=1300)
        assert row['ts'] == 1_300_000 and row['supply_at'] == 1_000_000
        rows = market_cap.series(upto=2_000_000)
        assert len(rows) == 1 and rows[0]['usdt_d'] == pytest.approx(row['usdt_d'])

    def test_not_more_often_than_a_cycle(self):
        market_cap.supply(now=1000, fetch=lambda: COINS)
        assert market_cap.collect_if_due(FakeClient(), now=1000) is not None
        assert market_cap.collect_if_due(FakeClient(), now=1100) is None
        assert market_cap.collect_if_due(FakeClient(), now=1000 + 300) is not None

    def test_a_failing_exchange_is_logged_not_raised(self):
        class Broken:
            def publicGetV5MarketTickers(self, params):
                raise RuntimeError('10006')
        market_cap.supply(now=1000, fetch=lambda: COINS)
        assert market_cap.collect_if_due(Broken(), now=1000) is None

    def test_stale_supply_skips_the_point(self, monkeypatch):
        market_cap.supply(now=1000, fetch=lambda: COINS)
        monkeypatch.setattr(market_cap, '_fetch_supply',
                            lambda: (_ for _ in ()).throw(RuntimeError('CoinGecko лежит')))
        assert market_cap.collect(FakeClient(), now=1000 + 3 * 86400) is None


class TestFactsCarryChangesAndAge:
    def test_changes_over_a_day_and_a_week_and_the_streak(self):
        now = 10 * DAY + 5 * 3_600_000                  # 05:00 UTC десятого дня
        points = []
        # USDT.D падал шесть дней (4.3 → 4.0), потом три дня растёт по 0.1 п.п.
        for d in range(9, -1, -1):
            ts = now - d * DAY
            usdt_d = 4.0 + (0.1 * (3 - d) if d <= 3 else 0.05 * (d - 3))
            points.append((ts, usdt_d, 58.0 + 0.01 * d, 3.0e12 + d * 1e9))
        _seed(points)
        m = market_cap.facts(upto=now)
        assert m['usdt_d'] == pytest.approx(4.3)
        assert m['usdt_d_24h'] == pytest.approx(0.1)
        assert m['usdt_d_7d'] == pytest.approx(4.3 - 4.2)
        assert m['btc_d_24h'] == pytest.approx(-0.01)
        assert m['usdt_d_streak_days'] == 3
        assert m['total2_24h'] is not None and m['btc_24h'] is not None
        assert m['stale'] is False and m['age_min'] == 0

    def test_an_old_series_is_marked_stale(self):
        _seed([(1_000_000_000, 4.0, 58.0, 3e12)])
        m = market_cap.facts(upto=1_000_000_000 + 3 * 3_600_000)
        assert m['stale'] is True and m['age_min'] == pytest.approx(180)

    def test_no_series_means_none(self):
        assert market_cap.facts(upto=5) is None


class TestTheMarkupShowsItHonestly:
    def test_fresh_facts_become_three_lines(self):
        import llm_context
        m = {'usdt_d': 4.82, 'btc_d': 58.1, 'usdt_d_24h': 0.31, 'usdt_d_7d': 0.9, 'btc_d_24h': 0.4,
             'usdt_d_streak_days': 3, 'total2_24h': -2.3, 'btc_24h': -0.8, 'up_24h': 31, 'counted_24h': 118,
             'age_min': 2, 'supply_age_min': 34, 'stale': False}
        lines = llm_context._macro_lines(m)
        text = chr(10).join(lines)
        assert 'USDT.D 4.82%' in text and '+0.31 п.п.' in text and 'растёт 3-й день' in text
        assert 'BTC.D 58.10%' in text and 'альты слабее BTC' in text and '31 из 118' in text
        assert 'цены 2 мин назад' in text and 'предложение монет 0.6 ч назад' in text

    def test_stale_facts_are_a_dash_with_the_age(self):
        import llm_context
        lines = llm_context._macro_lines({'usdt_d': 4.8, 'btc_d': 58, 'age_min': 190, 'stale': True})
        assert len(lines) == 1 and 'не измерено' in lines[0] and '190 мин' in lines[0]

    def test_nothing_is_a_dash(self):
        import llm_context
        assert llm_context._macro_lines(None) == [llm_context._NONE]

    def test_the_block_is_in_the_market_text_and_the_prompt_defines_it(self):
        import llm_context
        import llm_prompt
        text = chr(10).join(llm_context.market_lines({'macro': None}, 100.0))
        assert 'РЫНОК В ЦЕЛОМ' in text
        assert 'USDT.D' in llm_prompt.definitions()


class TestTheSnapshotSeesTheCurrentHour:
    def test_points_after_the_candle_open_are_included(self, monkeypatch):
        """
        Отсечка снимка — открытие последней свечи; точки ряда за текущий час
        позже неё. С отсечкой «не позже открытия» блок был пустым при живом
        ряде: первый разбор после выкатки 21.09.2026 его не получил.
        """
        import time
        import llm_market
        now = int(time.time() * 1000)
        candle_open = now - now % 3_600_000
        _seed([(now - 60_000, 6.4, 59.4, 2.86e12)])          # точка минуту назад
        assert llm_market._macro_facts(candle_open) is not None
        assert llm_market._macro_facts(candle_open)['usdt_d'] == pytest.approx(6.4)

    def test_a_past_candle_does_not_see_the_future(self):
        import llm_market
        _seed([(10 * DAY + 5 * 3_600_000, 6.4, 59.4, 2.86e12)])   # точка в 05:00
        assert llm_market._macro_facts(10 * DAY + 3 * 3_600_000) is None   # разбор свечи 03:00
        assert llm_market._macro_facts(10 * DAY + 4 * 3_600_000) is not None   # свеча 04:00 закрывается в 05:00
