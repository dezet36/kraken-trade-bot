"""
Polymarket: издержки, разбор корзин, лимиты риска и запрет живой торговли.

ЧТО ИМЕННО ЗДЕСЬ ЗАКРЕПЛЯЕТСЯ. Не «код запускается», а те решения, которые
дались замерами и которые легко потерять при первой же правке: что издержки
считаются от цены входа, что корзина «или ниже» не превращается в «ровно», что
потолок на событие сильнее потолка на корзину и что модуль лонгшотов не может
предложить живую сделку, пока валидация провалена.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polymarket import client, longshot, params, risk, weather  # noqa: E402


# ── Издержки ─────────────────────────────────────────────────────────────────

class TestCosts:

    def test_cheap_buckets_are_ruinous_and_expensive_ones_are_not(self):
        """
        Издержки в долях ВЛОЖЕННОГО падают с ростом цены, и разница огромна.

        Это не арифметическая мелочь, а причина запрета дешёвых корзин: замер
        калибровки намерил 0.224 у корзин по 5 центов и 0.013 у корзин по 95.
        """
        cheap = client.entry_cost(0.05, 0.05)
        rich = client.entry_cost(0.95, 0.05)
        assert 0.20 < cheap < 0.26, f'у дешёвой корзины ждали ~0.22, вышло {cheap}'
        assert 0.010 < rich < 0.016, f'у дорогой ждали ~0.013, вышло {rich}'
        assert cheap > rich * 10

    def test_fee_rate_prefers_the_market_schedule(self):
        """Ставка берётся из расписания рынка, а не из таблицы категорий."""
        market = {'feeType': 'crypto_fees_v2', 'feeSchedule': {'rate': 0.04}}
        assert client.fee_rate(market) == 0.04
        assert client.fee_rate({'feeType': 'crypto_fees_v2'}) == 0.07
        assert client.fee_rate({'feeType': 'что-то новое'}) == params.FEE_DEFAULT

    def test_zero_price_is_infinite_cost_not_a_crash(self):
        """Нулевая цена не делит на ноль, а честно отвечает «нельзя»."""
        assert client.entry_cost(0.0, 0.05) == float('inf')


# ── Разбор корзин ────────────────────────────────────────────────────────────

class TestBuckets:

    def test_open_ended_buckets_are_not_read_as_exact(self):
        """
        «31 или ниже» — не «ровно 31», и это переворачивает ставку.

        Ошибка разбора здесь не падает и выглядит правдоподобно: вероятность
        просто окажется вчетверо меньше настоящей.
        """
        below = weather.parse_bucket('Will the highest temperature in X be 31°C or below on August 9?')
        above = weather.parse_bucket('Will the highest temperature in X be 41°C or higher on August 9?')
        exact = weather.parse_bucket('Will the highest temperature in X be 34°C on August 9?')
        assert below['kind'] == 'below' and below['value'] == 31
        assert above['kind'] == 'above' and above['value'] == 41
        assert exact['kind'] == 'exact' and exact['value'] == 34

    def test_fahrenheit_is_detected(self):
        bucket = weather.parse_bucket('Will the highest temperature in Chicago be 94°F or higher on August 8?')
        assert bucket['unit'] == 'F' and bucket['kind'] == 'above'

    def test_unparsed_question_returns_none(self):
        """Неразобранный вопрос не притворяется обычной корзиной."""
        assert weather.parse_bucket('Will it rain tomorrow?') is None
        assert weather.parse_bucket('') is None
        assert weather.parse_bucket(None) is None

    def test_bucket_probabilities_sum_to_one_over_the_ladder(self):
        """Лестница из закрытых и двух открытых концов даёт полную единицу."""
        buckets = [{'value': 30, 'kind': 'below', 'unit': 'C'}]
        buckets += [{'value': v, 'kind': 'exact', 'unit': 'C'} for v in range(31, 41)]
        buckets += [{'value': 41, 'kind': 'above', 'unit': 'C'}]
        total = sum(weather.bucket_probability(b, 34.6, 0.2, 0.8) for b in buckets)
        assert abs(total - 1.0) < 1e-6, f'сумма по лестнице {total}'

    def test_fahrenheit_converts_spread_too(self):
        """
        При переводе в Фаренгейты переводится И разброс, а не только центр.

        Забыть про разброс — частая ошибка: распределение стало бы вдвое уже,
        и всякая корзина выглядела бы вероятнее, чем есть.
        """
        c = weather.bucket_probability({'value': 34, 'kind': 'exact', 'unit': 'C'},
                                       34.0, 0.0, 1.0)
        f = weather.bucket_probability({'value': 93, 'kind': 'exact', 'unit': 'F'},
                                       34.0, 0.0, 1.0)
        # 34°C = 93.2°F, корзина 93 покрывает 92.5-93.5°F = ширина 1°F = 0.56°C.
        # Она обязана быть УЖЕ градуса Цельсия, то есть менее вероятной.
        assert f < c


# ── Риск ─────────────────────────────────────────────────────────────────────

def _signal(price=0.60, model=0.75, liquidity=50_000, cost=0.02, event='E1'):
    return {'price': price, 'model': model, 'liquidity': liquidity,
            'cost': cost, 'event': event, 'category': 'weather_fees',
            'market': {'id': 'M1'}}


class TestRisk:

    def test_a_good_signal_is_proposed_with_a_size(self):
        decision = risk.RiskManager(bankroll=10_000).evaluate(_signal())
        assert decision.action == 'PROPOSE'
        assert decision.size_usd > 0

    def test_costs_are_subtracted_before_the_threshold(self):
        """
        Порог применяется к ЧИСТОМУ расхождению.

        Валовое расхождение 0.06 при издержках, съедающих его целиком, обязано
        отсеиваться: иначе порог пропускал бы заведомо убыточные ставки.
        """
        signal = _signal(price=0.60, model=0.66, cost=0.5)
        decision = risk.RiskManager(bankroll=10_000).evaluate(signal)
        assert decision.action == 'SKIP'
        assert 'издержки' in decision.reason

    def test_cheap_buckets_are_refused_outright(self):
        decision = risk.RiskManager(bankroll=10_000).evaluate(
            _signal(price=0.02, model=0.30))
        assert decision.action == 'SKIP'
        assert 'вне диапазона' in decision.reason

    def test_thin_book_is_refused(self):
        decision = risk.RiskManager(bankroll=10_000).evaluate(
            _signal(liquidity=100))
        assert decision.action == 'SKIP'
        assert 'ликвидности' in decision.reason

    def test_event_cap_beats_position_cap(self):
        """
        Одиннадцать корзин одного дня — ОДИН исход, а не одиннадцать ставок.

        Без потолка на событие мы получили бы одиннадцатикратную концентрацию,
        называя её диверсификацией.
        """
        manager = risk.RiskManager(bankroll=10_000)
        used = manager.bankroll * params.MAX_EVENT_PCT / 100
        decision = manager.evaluate(
            _signal(), [{'event': 'E1', 'category': 'weather_fees',
                         'size_usd': used}])
        assert decision.action == 'SKIP'
        assert 'событие' in decision.reason

    def test_daily_loss_stop_fires_before_anything_else(self):
        """Аварийная остановка проверяется первой и не зависит от сигнала."""
        manager = risk.RiskManager(bankroll=10_000, day_loss_usd=600)
        decision = manager.evaluate(_signal())
        assert decision.action == 'SKIP'
        assert 'дневной убыток' in decision.reason

    def test_kelly_is_zero_when_the_bet_is_bad(self):
        assert risk.kelly_fraction(0.4, 0.6) == 0.0
        assert risk.kelly_fraction(0.75, 0.6) > 0

    def test_size_never_exceeds_the_position_cap(self):
        """Даже при огромном расхождении потолок на корзину не пробивается."""
        manager = risk.RiskManager(bankroll=10_000)
        decision = manager.evaluate(_signal(price=0.20, model=0.95, cost=0.01))
        cap = 10_000 * params.MAX_POSITION_PCT / 100
        assert decision.size_usd <= cap + 1e-9


# ── Лонгшоты: запрет живой торговли ──────────────────────────────────────────

class TestLongshotIsPaperOnly:

    def test_validation_is_marked_failed(self):
        assert longshot.VALIDATION_PASSED is False
        assert 'не подтверждено' in longshot.VALIDATION_NOTE

    def test_live_trading_is_never_allowed(self, monkeypatch):
        """
        Запрет не зависит от настроек.

        Даже выключив бумажный режим, живую торговлю получить нельзя: пока
        валидация не пройдена, разрешение не выдаётся.
        """
        monkeypatch.setattr(params, 'PAPER', False)
        assert longshot.live_trading_allowed() is False

    def test_proposals_are_marked_paper_only(self):
        signal = {'price': 0.985, 'model': 0.99, 'liquidity': 50_000,
                  'cost': 0.002, 'event': 'E9', 'category': 'politics_fees',
                  'market': {'id': 'M9'}}
        decision = longshot.evaluate(signal, risk.RiskManager(bankroll=10_000))
        assert decision.action != 'EXECUTE'
        if decision.action == 'PROPOSE':
            assert decision.details.get('paper_only') is True
            assert decision.reason.startswith('ТОЛЬКО БУМАГА')

    def test_candidates_use_the_no_side_price(self):
        """Ставка на «No» стоит 1 - p, и издержки считаются от неё."""
        market = {'id': 'M', 'outcomePrices': json.dumps(['0.01', '0.99']),
                  'feeType': 'politics_fees', 'liquidity': 5000,
                  'events': [{'id': 'E'}]}
        rows = longshot.candidates([market])
        assert len(rows) == 1
        assert rows[0]['side'] == 'NO'
        assert abs(rows[0]['price'] - 0.99) < 1e-9
        # Издержки на дорогой стороне малы — единственное, что здесь благополучно.
        assert rows[0]['cost'] < 0.02

    def test_markets_outside_the_spec_range_are_ignored(self):
        market = {'id': 'M', 'outcomePrices': json.dumps(['0.35', '0.65']),
                  'feeType': 'politics_fees', 'events': [{'id': 'E'}]}
        assert longshot.candidates([market]) == []


# ── Ловушки самой площадки ───────────────────────────────────────────────────

class TestApiTraps:

    def test_resolved_query_bounds_the_end_date(self):
        """
        Отбор разрешённых рынков обязан ограничивать дату сверху.

        Без этого сортировка по убыванию выдаёт рынки-заглушки с концом в 2027
        году: закрытые досрочно и без торгов. Первый сбор так добыл 1256 рынков,
        из которых 95% не имели истории цен вовсе.
        """
        source = os.path.join(ROOT, 'polymarket', 'client.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        assert 'end_date_max' in text
        assert 'offset >= 1800' in text, 'шаг по дате обязателен: смещение упирается в 422'

    def test_failed_requests_are_not_treated_as_empty(self):
        """Отказ сети возвращает None, а пустая история — пустой список."""
        source = os.path.join(ROOT, 'polymarket', 'client.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        assert 'if data is None:\n        return None' in text
