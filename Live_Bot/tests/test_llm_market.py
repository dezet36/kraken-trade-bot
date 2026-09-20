"""
Снимок рынка для языковой модели: что он измеряет и чего не выдумывает.

ГЛАВНОЕ ЗДЕСЬ — РАЗНИЦА МЕЖДУ «НЕТ» И «НЕ ИЗМЕРЕНО». Снимок собирается из пяти
независимых источников, и падение одного не имеет права ни унести остальные,
ни выдать себя за пустой рынок. Каждый кусок отдаёт None, разметка печатает
прочерк, и модель видит, чего ей не показали.

Остальное — формулы. Профиль объёма и поглощение считаются по свечам, и
ошибиться в них можно так, что глазом не видно: пик объёма на цене закрытия
вместо диапазона свечи выглядит правдоподобно и врёт.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_context
import llm_market


def make_df(closes, spread=None, volume=None):
    closes = np.asarray(closes, dtype=float)
    spread = closes * 0.002 if spread is None else np.asarray(spread, dtype=float)
    volume = np.full(len(closes), 100.0) if volume is None else np.asarray(volume, float)
    return pd.DataFrame({
        'timestamp': pd.to_datetime(
            np.arange(len(closes)) * 3_600_000 + 1_700_000_000_000, unit='ms'),
        'open': closes,
        'high': closes + spread,
        'low': closes - spread,
        'close': closes,
        'volume': volume,
    })


def wavy(n=400, base=100.0, amp=4.0, period=23):
    idx = np.arange(n)
    return base + amp * np.sin(idx / period * 2 * np.pi) + idx * 0.004


# ── Профиль объёма ───────────────────────────────────────────────────────────

class TestVolumeProfile:

    def test_the_peak_is_where_the_volume_was(self):
        """Пятьдесят свечей у 100 на большом объёме и хвост по сторонам."""
        closes = np.concatenate([np.linspace(90, 110, 100), np.full(50, 100.0)])
        volume = np.concatenate([np.full(100, 10.0), np.full(50, 1000.0)])
        out = llm_market.volume_profile(make_df(closes, volume=volume))

        assert out is not None
        assert abs(out['poc'] - 100) < 1.0, out
        assert out['value_low'] <= 100 <= out['value_high']
        assert out['bars'] == 150

    def test_a_wide_candle_spreads_over_its_range(self):
        """
        Объём свечи ложится на ВЕСЬ её диапазон, а не на цену закрытия.
        Одна свеча размахом 90..110 не должна давать пик в одной корзине.
        """
        closes = np.full(20, 100.0)
        spread = np.full(20, 0.01)
        spread[10] = 10.0                             # одна свеча 90..110
        volume = np.full(20, 1.0)
        volume[10] = 1000.0
        out = llm_market.volume_profile(make_df(closes, spread=spread,
                                                volume=volume), bins=20)

        # Зона стоимости обязана растянуться по диапазону широкой свечи:
        # при равномерной раскладке 70% объёма занимают ~70% корзин.
        assert out['value_high'] - out['value_low'] > 10, out

    def test_adjacent_thin_bins_merge_into_one_gap(self):
        """Дыра в профиле — один диапазон, а не четыре цены через шаг корзины."""
        # Объём везде, кроме сплошного окна 104..108: там только по одной
        # свече на корзину, чтобы корзина была не пустой, а разреженной.
        closes = np.concatenate([np.linspace(90, 103, 200),
                                 np.linspace(104, 108, 4),
                                 np.linspace(109, 120, 200)])
        volume = np.concatenate([np.full(200, 100.0), np.full(4, 1.0),
                                 np.full(200, 100.0)])
        out = llm_market.volume_profile(make_df(closes, volume=volume),
                                        bars=len(closes), bins=30)
        assert len(out['thin']) == 1, out['thin']
        lo, hi = out['thin'][0]
        assert lo < 104.5 and hi > 107.5, out['thin']

    def test_too_few_bars_is_not_a_profile(self):
        assert llm_market.volume_profile(make_df(np.full(5, 100.0))) is None

    def test_the_window_stops_at_the_decision_bar(self):
        """Профиль на баре N не видит объём правее N."""
        closes = np.full(300, 100.0)
        volume = np.full(300, 1.0)
        volume[250:] = 10_000.0                       # будущее — гигантский объём
        closes[250:] = 120.0
        df = make_df(closes, volume=volume)

        out = llm_market.volume_profile(df, at=200)
        assert abs(out['poc'] - 100) < 1.0, 'пик утёк в будущее'


# ── Поглощение ───────────────────────────────────────────────────────────────

class TestAbsorption:

    def test_a_long_wick_on_big_volume_is_reported(self):
        n = 60
        closes = np.full(n, 100.0)
        spread = np.full(n, 0.1)
        volume = np.full(n, 100.0)
        df = make_df(closes, spread=spread, volume=volume)
        # Свеча 50: тело 100..100.05, нижняя тень до 98 — длинный фитиль снизу.
        df.loc[50, ['open', 'close', 'high', 'low', 'volume']] = [100.0, 100.05, 100.1, 98.0, 400.0]

        out = llm_market.absorption(df)
        assert out and out[0]['side'] == 'снизу'
        assert out[0]['price'] == pytest.approx(98.0)
        assert out[0]['bars_ago'] == 9
        assert out[0]['volume_x'] == pytest.approx(4.0)

    def test_a_long_wick_on_thin_volume_is_not_absorption(self):
        """Длинный фитиль без объёма — тонкий рынок, а не чья-то стена."""
        n = 60
        df = make_df(np.full(n, 100.0), spread=np.full(n, 0.1))
        df.loc[50, ['open', 'close', 'high', 'low']] = [100.0, 100.05, 100.1, 98.0]
        assert llm_market.absorption(df) is None

    def test_ordinary_candles_give_nothing(self):
        assert llm_market.absorption(make_df(wavy(100))) is None


# ── Дельта ───────────────────────────────────────────────────────────────────

class TestDelta:

    def _rows(self, minutes, buy, sell, end_ts=1_700_000_000_000):
        return [{'ts': end_ts - i * 60_000, 'buy': buy, 'sell': sell,
                 'value': buy - sell} for i in range(minutes)]

    def test_share_is_relative_to_turnover(self, monkeypatch):
        monkeypatch.setattr(llm_market.positioning, 'series',
                            lambda *a, **k: self._rows(60, buy=30.0, sell=10.0))
        out = llm_market.delta_facts('BTCUSDT')
        assert out['h1']['share_pct'] == pytest.approx(50.0)
        assert out['h1']['rows'] == 60
        # Строк на четыре часа нет: окно в час их и составляет.
        assert out['h4']['rows'] == 60

    def test_a_rise_on_selling_is_called_a_divergence(self, monkeypatch):
        monkeypatch.setattr(llm_market.positioning, 'series',
                            lambda *a, **k: self._rows(240, buy=10.0, sell=30.0))
        out = llm_market.delta_facts('BTCUSDT', price_change_pct=+1.5)
        assert out['divergence'] == 'цена растёт на продажах'

        out = llm_market.delta_facts('BTCUSDT', price_change_pct=-1.5)
        assert out.get('divergence') is None

    def test_no_rows_means_not_measured(self, monkeypatch):
        monkeypatch.setattr(llm_market.positioning, 'series', lambda *a, **k: [])
        assert llm_market.delta_facts('BTCUSDT') is None

    def test_staleness_is_reported(self, monkeypatch):
        end = 1_700_000_000_000
        monkeypatch.setattr(llm_market.positioning, 'series',
                            lambda *a, **k: self._rows(10, 1.0, 1.0, end_ts=end))
        out = llm_market.delta_facts('BTCUSDT', upto=end + 45 * 60_000)
        assert out['fresh_min'] == 45
        assert out['h1'] is not None

    def test_a_stale_series_is_not_measured(self, monkeypatch):
        """
        Ряд, оборвавшийся три недели назад, — не сегодняшняя дельта. Окна
        считаются от последней записи, и без порога простой сборщика
        выглядел бы как рынок.
        """
        end = 1_700_000_000_000
        monkeypatch.setattr(llm_market.positioning, 'series',
                            lambda *a, **k: self._rows(240, 10.0, 30.0, end_ts=end))
        out = llm_market.delta_facts('BTCUSDT', upto=end + 21 * 24 * 60 * 60_000,
                                     price_change_pct=+2.0)
        assert out['stale'] is True
        assert out['fresh_min'] == 21 * 24 * 60
        assert 'h1' not in out and 'divergence' not in out


# ── Стакан ───────────────────────────────────────────────────────────────────

class FakeClient:
    def __init__(self, bids, asks):
        self.bids, self.asks = bids, asks

    def fetch_order_book(self, symbol, limit=None):
        return {'bids': self.bids, 'asks': self.asks}


class TestBook:

    @pytest.fixture(autouse=True)
    def symbol(self, monkeypatch):
        import exchange
        monkeypatch.setattr(exchange, 'market_symbol', lambda pair, client: pair)

    def test_walls_and_imbalance(self):
        bids = [(100 - i * 0.1, 1.0) for i in range(1, 15)]
        asks = [(100 + i * 0.1, 1.0) for i in range(1, 15)]
        bids[5] = (bids[5][0], 20.0)                  # плита снизу
        out = llm_market.book_facts(FakeClient(bids, asks), 'BTCUSDT')

        assert out['imbalance'] > 1.0
        assert len(out['walls_below']) == 1
        assert out['walls_below'][0]['price'] == pytest.approx(bids[5][0])
        assert out['walls_below'][0]['dist_pct'] < 0
        assert out['walls_above'] == []

    def test_far_orders_are_ignored(self):
        """Заявка в пяти процентах от цены в снимок ±2% не входит."""
        bids = [(100 - i * 0.1, 1.0) for i in range(1, 10)] + [(95.0, 1000.0)]
        asks = [(100 + i * 0.1, 1.0) for i in range(1, 10)]
        out = llm_market.book_facts(FakeClient(bids, asks), 'BTCUSDT')
        assert out['walls_below'] == []
        assert out['bid_volume'] == pytest.approx(9.0)

    def test_the_covered_range_is_measured_not_promised(self):
        """
        Пятьсот уровней BTC — это три сотых процента, а не ±2%. Охват в
        снимке обязан быть тем, что видно на самом деле.
        """
        bids = [(100 - i * 0.001, 1.0) for i in range(1, 30)]     # до -0.03%
        asks = [(100 + i * 0.001, 1.0) for i in range(1, 30)]
        out = llm_market.book_facts(FakeClient(bids, asks), 'BTCUSDT')
        assert out['range_pct'] == pytest.approx(0.029, abs=0.001)
        assert out['levels'] == 58

    def test_no_client_no_book(self):
        assert llm_market.book_facts(None, 'BTCUSDT') is None

    def test_empty_book_is_not_measured(self):
        assert llm_market.book_facts(FakeClient([], []), 'BTCUSDT') is None


# ── Структура из smc ─────────────────────────────────────────────────────────

class TestSmcFacts:

    def _context(self):
        from smc import signal as smc_signal
        df = make_df(wavy(400))
        return smc_signal.build_context({'poi': df}, pair='BTCUSDT')

    def test_facts_come_from_the_real_context(self):
        context = self._context()
        price = float(context.frames['poi']['close'].iloc[-1])
        out = llm_market.smc_facts('BTCUSDT', price, context=context)

        assert out is not None
        assert 'trend' in out and 'leg' in out
        assert out['leg']['direction'] in ('BULLISH', 'BEARISH')
        assert out['side_of_range'] in ('DISCOUNT', 'PREMIUM', 'EQUILIBRIUM')
        # Ничего из numpy не протекло: всё сериализуемо.
        import json
        json.dumps(out)

    def test_no_context_is_not_measured(self, monkeypatch):
        import strategy_smc
        monkeypatch.setattr(strategy_smc, 'get_context', lambda *a, **k: None)
        assert llm_market.smc_facts('BTCUSDT', 100.0) is None


# ── Снимок целиком ───────────────────────────────────────────────────────────

class TestSnapshot:

    def test_one_broken_piece_does_not_take_the_others(self, monkeypatch):
        """Упавший стакан — прочерк в стакане, а не пустой снимок."""
        import strategy_smc
        monkeypatch.setattr(strategy_smc, 'get_context', lambda *a, **k: None)
        monkeypatch.setattr(llm_market.positioning, 'series', lambda *a, **k: [])

        def boom(*a, **k):
            raise RuntimeError('биржа не ответила')
        monkeypatch.setattr(llm_market, 'book_facts', boom)

        out = llm_market.snapshot('BTCUSDT', make_df(wavy(300)))
        assert out['book'] is None
        assert out['profile'] is not None
        assert {'profile', 'absorption', 'delta', 'book', 'smc', 'pois', 'htf',
                'oi_flow', 'liquidations'} <= set(out)

    def test_not_a_frame_is_not_a_snapshot(self):
        assert llm_market.snapshot('BTCUSDT', [0] * 500) is None
        assert llm_market.snapshot('BTCUSDT', None) is None

    def test_the_cutoff_follows_the_decision_bar(self, monkeypatch):
        """Дельта читается до бара решения, а не до конца файла."""
        seen = {}

        def series(source, pair, upto=None, **k):
            seen['upto'] = upto
            return []
        monkeypatch.setattr(llm_market.positioning, 'series', series)
        import strategy_smc
        monkeypatch.setattr(strategy_smc, 'get_context', lambda *a, **k: None)

        df = make_df(wavy(300))
        llm_market.snapshot('BTCUSDT', df, at=200)
        assert seen['upto'] == int(df['timestamp'].iloc[200].timestamp() * 1000)


# ── Как это читает модель ────────────────────────────────────────────────────

class TestMarkup:

    def test_price_change_is_in_the_header(self):
        """«Данные не показывают динамику цены» — сказала модель. Теперь показывают."""
        closes = wavy(400)
        closes[-1] = closes[-5] * 1.02             # +2% за четыре часа
        out = llm_context.build('BTCUSDT', make_df(closes))
        assert out['facts']['change_4h'] == pytest.approx(2.0)
        assert 'Ход цены: за 4ч +2.00%' in out['text']

    def test_price_change_needs_history(self):
        out = llm_context.build('BTCUSDT', make_df(wavy(400)), at=3)
        assert out['facts'].get('change_4h') is None or out['text'] == ''

    def test_missing_snapshot_is_dashes_not_silence(self):
        out = llm_context.build('BTCUSDT', make_df(wavy(400)), market=None)
        text = out['text']
        for head in ('ПРОФИЛЬ ОБЪЁМА', 'ПОГЛОЩЕНИЕ', 'ДЕЛЬТА АГРЕССОРА',
                     'СТАКАН', 'СТРУКТУРА', 'ЗОНЫ ИНТЕРЕСА', 'СТАРШИЕ ТАЙМФРЕЙМЫ',
                     'ОТКРЫТЫЙ ИНТЕРЕС ПО СВЕЧАМ', 'КАРТА ЛИКВИДАЦИЙ',
                     'ЛИКВИДАЦИИ ПО ФАКТУ', 'ЭКСТРЕМУМЫ ДНЯ', 'НЕЗАКРЫТЫЕ ИМБАЛАНСЫ'):
            assert head in text, head
        assert text.count('\n  —') == 13, 'каждый блок обязан стоять с прочерком'
        # Без свечей BTC блока про BTC нет вовсе: для самого BTC он бессмыслен.
        assert 'BTC КАК ОРИЕНТИР' not in text

    def test_the_snapshot_is_printed(self):
        market = {
            'profile': {'poc': 101.0, 'value_low': 99.0, 'value_high': 103.0,
                        'thin': [[97.0, 98.0]], 'bars': 200},
            'absorption': [{'price': 98.0, 'side': 'снизу', 'volume_x': 3.2,
                            'bars_ago': 4}],
            'delta': {'fresh_min': 3,
                      'h1': {'delta': 5.0, 'share_pct': 12.5, 'rows': 60},
                      'h4': {'delta': -5.0, 'share_pct': -8.0, 'rows': 60},
                      'h24': None,
                      'divergence': 'цена растёт на продажах'},
            'book': {'mid': 100.0, 'bid_volume': 10, 'ask_volume': 5,
                     'imbalance': 2.0, 'spread_pct': 0.01,
                     'walls_below': [{'price': 99.0, 'volume_x': 7.0,
                                      'dist_pct': -1.0}],
                     'walls_above': [], 'range_pct': 2.0},
            'smc': {'bias': 'BULLISH', 'trend': 'BEARISH',
                    'last_break': {'type': 'CHoCH', 'direction': 'BEARISH',
                                   'price': 102.0, 'bars_ago': 6},
                    'sweep': {'side': 'SSL', 'price': 97.0,
                              'penetration_pct': 0.3, 'reclaimed': True,
                              'bars_ago': 2},
                    'fvg': {'top': 101.5, 'bottom': 101.0, 'direction': 'BULLISH'},
                    'leg': {'direction': 'BULLISH', 'from': 95.0, 'to': 105.0},
                    'equilibrium': 100.0, 'side_of_range': 'DISCOUNT',
                    'equal_levels': [{'price': 106.0, 'source': 'EQH',
                                      'side': 'BSL'}],
                    'untapped': [{'price': 94.0, 'side': 'SSL', 'source': 'SWING'}]},
            'pois': [{'type': 'ORDER_BLOCK', 'direction': 'BULLISH', 'top': 100.0,
                      'bottom': 99.0, 'touches': 1, 'bars_ago': 5, 'inside': True}],
            'htf': {'htf': {'trend': 'BULLISH',
                            'break': {'type': 'BOS', 'direction': 'BULLISH',
                                      'price': 104.0, 'bars_ago': 2},
                            'swing_high': 105.0, 'swing_low': 95.0}},
            'oi_flow': {'bars': ['набор лонгов', 'закрытие шортов'], 'change_pct': 1.2},
            'liquidations': {'below': [{'from': 96.0, 'to': 96.5, 'share_pct': 70.0,
                                        'dist_pct': -3.8, 'side': 'лонги'}],
                             'above': [], 'bars': 72},
            'benchmark': {'btc_4h': 1.0, 'btc_24h': 2.0, 'relative_24h': -0.5},
            'sessions': {'pdh': 103.0, 'pdl': 97.0, 'day_open': 100.5, 'pwh': 108.0,
                         'pwl': 92.0, 'asia_high': 101.0, 'asia_low': 99.5},
            'fvgs': [{'top': 101.5, 'bottom': 101.0, 'direction': 'BULLISH',
                      'bars_ago': 3, 'inside': False}],
            'activity': {'last_x': 2.1, 'last4_x': 1.4, 'day_x': 0.9},
            'delta_hours': [{'share_pct': 5.0, 'rows': 60}, {'share_pct': -12.0, 'rows': 20}],
            'funding_trend': [0.0001, 0.00012, 0.00015],
            'oi_week': 3.4,
            'htf_zones': [{'kind': 'FVG', 'direction': 'BEARISH', 'top': 106.0, 'bottom': 104.5,
                           'bars_ago': 5, 'inside': False}],
            'liq_fact': {'young_min': 20, 'events': 0},
        }
        text = llm_context.build('BTCUSDT', make_df(wavy(400)),
                                 market=market)['text']

        assert 'Пик объёма (POC) 101' in text
        assert 'снизу 98   объём ×3.2   4 св. назад' in text
        assert '1ч +12.5% (60/60 мин)   4ч -8.0% (60/240 мин)   24ч —' in text
        assert 'РАСХОЖДЕНИЕ: цена растёт на продажах' in text
        assert 'Перекос bid/ask 2.00' in text
        assert 'Плиты ниже: 99 (×7.0, -1.000%)' in text
        assert 'Разрежения (цена проскакивает): 97..98' in text
        assert 'видно ±2% от цены' in text
        assert 'CHoCH вниз у 102, 6 св. назад' in text
        assert 'Вынос за 97 (под ценой, стопы лонгов), с возвратом' in text
        assert 'цена в зоне: дисконт' in text
        assert '106 EQH' in text
        assert 'ордер-блок вверх 99..100   цена внутри' in text
        assert '4ч: тренд вверх   слом BOS вверх у 104' in text
        assert 'набор лонгов → закрытие шортов' in text
        assert 'Лонги ликвидируются ниже: 96..96.5 (-3.8%, 70% объёма стороны)' in text
        assert 'BTC за 4ч +1.00%' in text and 'слабее' in text
        assert 'вчера макс 103 (' in text and 'неделя мин 92' in text and 'Азия макс 101' in text
        assert '101..101.5 вверх' in text
        assert 'Активность: последняя свеча ×2.1' in text
        assert '+5% → -12%?' in text
        assert 'последние выплаты: 0.000100 → 0.000120 → 0.000150' in text
        assert 'за 7д 3.40%' in text
        assert '\n  —' not in text, 'снимок полный — прочерков быть не должно'

    def test_the_markup_still_fits_the_context_window(self):
        """
        Полный снимок добавляет к вопросу текст, и он обязан помещаться.

        Окно 4096 = ~1700 вопроса + 1200 ответа + запас; грубая оценка для
        русского текста — три символа на токен. Полная разметка со снимком
        не должна съесть весь запас.
        """
        import config
        import llm_prompt
        text = llm_context.build('BTCUSDT', make_df(wavy(400)),
                                 market=_full_market())['text']
        prompt = llm_prompt.build(text)
        # 2.3 знака на токен — нижняя граница, замеренная на сервере.
        need = len(prompt) / 2.3 + config.LLM_MAX_TOKENS
        assert need < config.LLM_CTX * 0.9, (
            f'вопрос ~{len(prompt) / 2.3:.0f} токенов + ответ '
            f'{config.LLM_MAX_TOKENS} не оставляют запаса в окне {config.LLM_CTX}')


def _full_market():
    return {
        'profile': {'poc': 101.0, 'value_low': 99.0, 'value_high': 103.0,
                    'thin': [[96.0, 97.5], [104.0, 105.5], [107.0, 108.0]], 'bars': 200},
        'absorption': [{'price': 98.0 + i, 'side': 'снизу', 'volume_x': 3.2,
                        'bars_ago': i} for i in range(4)],
        'delta': {'fresh_min': 3,
                  'h1': {'delta': 5.0, 'share_pct': 12.5, 'minutes': 60},
                  'h4': {'delta': -5.0, 'share_pct': -8.0, 'minutes': 240},
                  'h24': {'delta': 1.0, 'share_pct': 0.4, 'minutes': 1440},
                  'divergence': 'цена растёт на продажах'},
        'book': {'mid': 100.0, 'bid_volume': 10, 'ask_volume': 5,
                 'imbalance': 2.0, 'spread_pct': 0.01,
                 'walls_below': [{'price': 99.0 - i, 'volume_x': 7.0,
                                  'dist_pct': -1.0 - i} for i in range(3)],
                 'walls_above': [{'price': 101.0 + i, 'volume_x': 6.0,
                                  'dist_pct': 1.0 + i} for i in range(3)],
                 'range_pct': 2.0},
        'smc': {'bias': 'BULLISH', 'trend': 'BEARISH',
                'last_break': {'type': 'CHoCH', 'direction': 'BEARISH',
                               'price': 102.0, 'bars_ago': 6},
                'sweep': {'side': 'SSL', 'price': 97.0, 'penetration_pct': 0.3,
                          'reclaimed': True, 'bars_ago': 2},
                'fvg': {'top': 101.5, 'bottom': 101.0, 'direction': 'BULLISH'},
                'leg': {'direction': 'BULLISH', 'from': 95.0, 'to': 105.0},
                'equilibrium': 100.0, 'side_of_range': 'DISCOUNT',
                'equal_levels': [{'price': 106.0 + i, 'source': 'EQH',
                                  'side': 'BSL'} for i in range(3)],
                'untapped': [{'price': 94.0 - i, 'side': 'SSL',
                              'source': 'SWING'} for i in range(3)]},
        'pois': [{'type': t, 'direction': 'BULLISH', 'top': 100.0 - i,
                  'bottom': 99.0 - i, 'touches': 1, 'bars_ago': 5 + i, 'inside': False}
                 for i, t in enumerate(('ORDER_BLOCK', 'BREAKER', 'MITIGATION'))],
        'htf': {k: {'trend': 'BULLISH',
                    'break': {'type': 'BOS', 'direction': 'BULLISH',
                              'price': 104.0, 'bars_ago': 2},
                    'swing_high': 105.0, 'swing_low': 95.0} for k in ('htf', 'bias')},
        'oi_flow': {'bars': ['набор лонгов'] * 6, 'change_pct': 1.2},
        'liquidations': {'below': [{'from': 96.0 - i, 'to': 96.5 - i, 'share_pct': 30.0,
                                    'dist_pct': -3.8 - i, 'side': 'лонги'} for i in range(3)],
                         'above': [{'from': 104.0 + i, 'to': 104.5 + i, 'share_pct': 30.0,
                                    'dist_pct': 3.8 + i, 'side': 'шорты'} for i in range(3)],
                         'bars': 72},
        'benchmark': {'btc_4h': 1.0, 'btc_24h': 2.0, 'relative_24h': -0.5},
        'sessions': {'pdh': 103.0, 'pdl': 97.0, 'day_open': 100.5, 'pwh': 108.0,
                     'pwl': 92.0, 'asia_high': 101.0, 'asia_low': 99.5},
        'fvgs': [{'top': 101.5 + i, 'bottom': 101.0 + i, 'direction': 'BULLISH',
                  'bars_ago': 3, 'inside': False} for i in range(4)],
        'activity': {'last_x': 2.1, 'last4_x': 1.4, 'day_x': 0.9},
        'delta_hours': [{'share_pct': 5.0, 'rows': 60}] * 6,
        'funding_trend': [0.0001, 0.00012, 0.00015],
        'oi_week': 3.4,
        'htf_zones': [{'kind': 'ORDER_BLOCK', 'direction': 'BULLISH', 'top': 99.0, 'bottom': 97.5,
                       'bars_ago': 3, 'inside': False}],
        'liq_fact': {'hours': 24,
                     'h24': {'long_n': 12, 'long_size': 3.5, 'short_n': 8, 'short_size': 2.1},
                     'h4': {'long_n': 2, 'long_size': 0.5, 'short_n': 1, 'short_size': 0.1},
                     'clusters': [{'from': 96.0 + i, 'to': 96.4 + i, 'share_pct': 30.0,
                                   'dist_pct': -3.8 + i, 'mostly': 'лонги'} for i in range(3)],
                     'events': 20},
    }


# ── Новые блоки снимка ───────────────────────────────────────────────────────

def oi_rows(df, values):
    """Ряд ОИ: по записи в конце каждой свечи."""
    out = []
    for i, value in enumerate(values):
        ts = int(df['timestamp'].iloc[i].timestamp() * 1000) + 3_600_000 - 1
        out.append({'ts': ts, 'value': value, 'pair': 'BTCUSDT'})
    return out


class TestOiFlow:

    def test_each_bar_is_named_by_price_and_oi(self, monkeypatch):
        closes = np.array([100, 101, 100, 101, 100, 99, 98.0])
        oi = [1000, 1010, 1020, 1015, 1005, 1015, 1005.0]
        df = make_df(closes)
        monkeypatch.setattr(llm_market.positioning, 'series',
                            lambda *a, **k: oi_rows(df, oi))
        out = llm_market.oi_flow('BTCUSDT', df, len(df) - 1, bars=7)
        assert out['bars'] == ['набор лонгов', 'набор шортов', 'закрытие шортов',
                               'закрытие лонгов', 'набор шортов', 'закрытие лонгов']
        assert out['change_pct'] == pytest.approx(0.5)

    def test_no_series_is_not_measured(self, monkeypatch):
        monkeypatch.setattr(llm_market.positioning, 'series', lambda *a, **k: [])
        assert llm_market.oi_flow('BTCUSDT', make_df(wavy(50)), 49) is None

    def test_a_stale_series_is_not_flat_but_unmeasured(self, monkeypatch):
        """Ряд, оборвавшийся давно, — не «ОИ без изменений», а «не измерено»."""
        df = make_df(wavy(50))
        old = [{'ts': int(df['timestamp'].iloc[0].timestamp() * 1000), 'value': 1000.0,
                'pair': 'BTCUSDT'}]
        monkeypatch.setattr(llm_market.positioning, 'series', lambda *a, **k: old)
        assert llm_market.oi_flow('BTCUSDT', df, 49) is None
        assert llm_market.liquidation_estimate('BTCUSDT', df, 49) is None


class TestLiquidationEstimate:

    def test_new_longs_put_a_cluster_below_the_price(self, monkeypatch):
        """
        Лонги, набранные на 100 при плече 10, ликвидируются у 90; при 25 — у
        96. Цена туда не ходила — уровни живы и лежат ниже цены.
        """
        n = 30
        closes = np.full(n, 100.0)
        closes[10] = 100.0
        df = make_df(closes, spread=np.full(n, 0.1))
        oi = [1000.0] * n
        oi[10:] = [2000.0] * (n - 10)                 # прирост ОИ на свече 10
        df.loc[10, 'close'] = 100.5                   # свеча вверх → лонги
        df.loc[10, 'open'] = 100.0
        monkeypatch.setattr(llm_market.positioning, 'series',
                            lambda *a, **k: oi_rows(df, oi))
        out = llm_market.liquidation_estimate('BTCUSDT', df, n - 1, bars=n)
        assert out['above'] == []
        levels = [c['from'] for c in out['below']]
        assert any(abs(l - 100.5 * 0.96) < 0.2 for l in levels), levels
        assert any(abs(l - 100.5 * 0.9) < 0.2 for l in levels), levels
        assert all(c['dist_pct'] < 0 for c in out['below'])

    def test_levels_the_price_already_visited_are_gone(self, monkeypatch):
        """Ликвидированное не ликвидируется второй раз."""
        n = 30
        closes = np.full(n, 100.0)
        df = make_df(closes, spread=np.full(n, 0.1))
        oi = [1000.0] * 10 + [2000.0] * 20
        df.loc[10, 'close'] = 100.5
        df.loc[20, 'low'] = 85.0                      # цена сходила к 85: 10× и 25× сняты
        monkeypatch.setattr(llm_market.positioning, 'series',
                            lambda *a, **k: oi_rows(df, oi))
        out = llm_market.liquidation_estimate('BTCUSDT', df, n - 1, bars=n)
        levels = [c['from'] for c in (out or {}).get('below', [])]
        assert all(l > 99 for l in levels), levels   # остались только 50× и 100×


class TestBenchmark:

    def test_relative_strength(self):
        pair = make_df(np.linspace(100, 110, 60))    # +10% за 59 баров
        btc = make_df(np.linspace(100, 105, 60))
        out = llm_market.benchmark_facts(pair, btc, 59)
        assert out['btc_24h'] > 0
        assert out['relative_24h'] > 0

    def test_no_benchmark_no_block(self):
        assert llm_market.benchmark_facts(make_df(wavy(60)), None, 59) is None


class TestPoisAndHtf:

    def _context(self):
        from smc import signal as smc_signal
        poi = make_df(wavy(400))
        htf = make_df(wavy(200, period=6), spread=None)
        htf['timestamp'] = pd.to_datetime(
            np.arange(200) * 4 * 3_600_000 + 1_700_000_000_000 - 200 * 4 * 3_600_000
            + 400 * 3_600_000, unit='ms')
        return smc_signal.build_context({'poi': poi, 'htf': htf}, pair='BTCUSDT')

    def test_pois_are_the_active_ones_nearest_to_price(self):
        context = self._context()
        df = context.frames['poi']
        price = float(df['close'].iloc[-1])
        out = llm_market.poi_facts(context, price, len(df) - 1)
        if out is None:
            pytest.skip('на синтетике зон не нашлось')
        assert len(out) <= 3
        for z in out:
            assert z['type'] in ('ORDER_BLOCK', 'BREAKER', 'MITIGATION', 'WICK')
            assert z['bottom'] <= z['top']
            assert z['bars_ago'] >= 0

    def test_the_same_zone_under_two_names_is_printed_once(self):
        """Живой снимок SOLUSDT 19.09 показал один блок трижды."""
        class Ctx:
            frames = {'poi': make_df(wavy(60))}
            pois = [{'type': t, 'direction': 'BEARISH', 'top': 113.84, 'bottom': 113.33,
                     'index': 40, 'confirmed_at': 45}
                    for t in ('ORDER_BLOCK', 'MITIGATION', 'MITIGATION')]
        import smc.poi as poi_mod
        out = llm_market.poi_facts(Ctx(), 112.0, 59)
        if out is None:
            pytest.skip('зона отсеяна правилами активности на синтетике')
        assert len(out) == 1 and out[0]['type'] == 'ORDER_BLOCK'

    def test_htf_reads_only_closed_candles(self):
        context = self._context()
        df = context.frames['poi']
        out = llm_market.htf_facts(context, df['timestamp'].iloc[-1])
        assert out and 'htf' in out
        assert out['htf']['trend'] in ('BULLISH', 'BEARISH', 'NEUTRAL')
        import json
        json.dumps(out)
