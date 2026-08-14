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

import pytest  # noqa: E402

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


# ── Три стратегии на общем контракте ─────────────────────────────────────────

from polymarket import base, runner  # noqa: E402
from polymarket.longshot import LongshotSignal  # noqa: E402
from polymarket.signal_crypto import (CryptoSignal, digital_probability,  # noqa: E402
                                      parse_market, touch_probability)
from polymarket.weather import WeatherSignal  # noqa: E402


class TestThreeStrategies:

    def test_all_three_declare_a_validation_status(self):
        """
        Ни одна стратегия не может существовать без статуса проверки.

        Это не формальность: статус читается кодом перед выпуском на реальные
        деньги, и отсутствующий означал бы «непроверенная торгует как
        проверенная».
        """
        for factory in (CryptoSignal, LongshotSignal, WeatherSignal):
            module = factory()
            assert module.name and module.name != 'без имени'
            assert module.validation.status in (base.PASSED, base.FAILED,
                                                base.UNTESTED)
            assert len(module.validation.note) > 40, 'причина обязана быть'

    def test_none_of_them_may_touch_real_money_yet(self):
        """Ни одна из трёх не прошла валидацию — значит живых денег нет."""
        for factory in (CryptoSignal, LongshotSignal, WeatherSignal):
            assert factory().allows_real_money() is False

    def test_budget_of_one_strategy_does_not_touch_the_others(self, monkeypatch):
        """
        БЮДЖЕТЫ НЕЗАВИСИМЫ. Это главное свойство, и раньше его не было.

        Долями было так: WEATHER 0.30, CRYPTO 0.20, LONGSHOT 0.05, MM 0.45 — в
        сумме единица. Поднять маркет-мейкингу до 0.60 означало отобрать 0.15 у
        остальных, причём МОЛЧА: цифры в их настройках не менялись, а денег
        становилось меньше. Стратегии были связаны через знаменатель, которого
        никто не трогал.

        Теперь бюджет — просто число долларов, и правка одного не двигает
        соседей ни на цент.
        """
        before = {name: params.bankroll_for(name) for name in params.BUDGET}
        monkeypatch.setitem(params.BUDGET, 'MM', before['MM'] * 7 + 1000)
        after = {name: params.bankroll_for(name) for name in params.BUDGET}
        for name in before:
            if name == 'MM':
                continue
            assert after[name] == before[name], f'{name} сдвинулся вслед за MM'

    def test_budgets_are_dollars_not_shares(self):
        """Сумма бюджетов ничего не ограничивает и никуда не нормируется."""
        assert params.bankroll_for('MM') > 1.0, 'доллары, а не доля единицы'
        assert params.budget_total() == sum(params.BUDGET.values())

    def test_unknown_strategy_gets_nothing(self):
        """
        Опечатка в имени не должна открывать доступ ко всем деньгам.
        Молчаливая щедрость опаснее отказа.
        """
        assert params.bankroll_for('НЕТ ТАКОЙ') == 0.0
        assert params.bankroll_for('') == 0.0

    def test_both_market_making_schemes_have_separate_wallets(self):
        """
        У двусторонней и односторонней схем РАЗНЫЕ кошельки.

        Прежде обе спрашивали bankroll_for('MM'). Запущенные вместе, они обе
        считали, что располагают всей суммой, и планировали потратить её каждая
        целиком — то есть вдвое больше, чем есть на счету.
        """
        assert 'MM' in params.BUDGET and 'ONESIDE' in params.BUDGET
        text = open(os.path.join(ROOT, 'polymarket', 'oneside_run.py'),
                    encoding='utf-8').read()
        assert "bankroll_for('ONESIDE')" in text
        assert "bankroll_for('MM')" not in text.replace('#', '\n#').split('\n#')[0]

    def test_zero_budget_means_no_trading_not_leftovers(self, monkeypatch):
        """Ноль означает «не торгует», а не «получает остаток от других»."""
        monkeypatch.setitem(params.BUDGET, 'ONESIDE', 0.0)
        assert params.bankroll_for('ONESIDE') == 0.0
        assert params.bankroll_for('MM') > 0.0, 'сосед не пострадал'

    def test_second_argument_is_ignored_on_purpose(self):
        """
        Общий счёт больше ни на что не влияет, и вызовы с ним это подтверждают.

        Принимать параметр и не использовать честнее, чем принимать и делать
        вид, будто он на что-то влияет: старые вызовы не падают, но и не
        получают власти над бюджетом.
        """
        assert params.bankroll_for('MM', 999_999) == params.bankroll_for('MM')

    def test_risk_input_shape_is_identical_across_modules(self):
        """
        Слой риска получает ОДИН формат от всех трёх.

        Разные ключи у разных модулей — самый тихий способ получить разные
        лимиты там, где задуманы одинаковые.
        """
        sample = base.SignalResult(0.7, 0.5, 0.9, ['src'],
                                   market={'id': 'X', 'feeType': 'weather_fees'})
        keys = set(sample.as_risk_input('WEATHER'))
        assert {'strategy', 'price', 'model', 'confidence', 'cost',
                'liquidity', 'market', 'event', 'category'} <= keys


class TestCryptoMath:

    def test_touch_probability_exceeds_terminal(self):
        """
        Вероятность КОСНУТЬСЯ выше вероятности закрыться за барьером.

        Спутав их, мы объявили бы половину рынков недооценёнными. Проверено
        симуляцией: для 100→90 при воле 0.6 на неделю формула даёт 0.216
        против 0.110 у цифрового варианта.
        """
        years = 7 / 365
        touch = touch_probability(100, 90, 0.60, years, 'down')
        terminal = digital_probability(100, 90, 0.60, years, 'down')
        assert touch > terminal * 1.7
        assert 0.20 < touch < 0.23, f'ждали ~0.216, вышло {touch}'
        assert 0.10 < terminal < 0.12

    def test_touch_is_certain_when_barrier_already_passed(self):
        assert touch_probability(100, 110, 0.5, 0.1, 'down') == 1.0
        assert touch_probability(100, 90, 0.5, 0.1, 'up') == 1.0

    def test_probabilities_stay_inside_zero_and_one(self):
        for sigma in (0.1, 0.6, 2.0):
            for days in (1, 30, 365):
                p = touch_probability(100, 80, sigma, days / 365, 'down')
                assert 0.0 <= p <= 1.0

    def test_parser_separates_touch_from_terminal(self):
        touch = parse_market('Will XRP dip to $0.90 August 3-9?')
        assert touch['kind'] == 'touch' and touch['direction'] == 'down'
        assert touch['symbol'] == 'XRP' and abs(touch['strike'] - 0.90) < 1e-9

        term = parse_market('Will Bitcoin be above $120,000 on June 30?')
        assert term['kind'] == 'terminal' and term['direction'] == 'up'
        assert term['strike'] == 120_000

    def test_parser_refuses_markets_it_cannot_price(self):
        """
        Похожий по виду вопрос без доступного спота — не считается.

        «Will Glean's valuation hit $10B» выглядит как ценовой рынок, но ни
        спота, ни волатильности для него этим способом не достать, и
        посчитанная вероятность была бы выдумкой.
        """
        assert parse_market("Will Glean's valuation hit $10B by December 31?") is None
        assert parse_market('Will NVIDIA be the largest company?') is None
        assert parse_market('Will the highest temperature be 34°C?') is None

    def test_confidence_falls_with_horizon(self):
        """На длинном сроке риск-нейтральная мера расходится с реальной."""
        def fake_vol(symbol):
            return {'spot': 100.0, 'sigma_annual': 0.6, 'bars': 500}

        module = CryptoSignal(vol_source=fake_vol)
        # Даты СЧИТАЮТСЯ ОТ СЕГОДНЯ, а не вписаны числом: прошитая дата
        # однажды становится прошлым, срок обращается в ноль, и тест падает не
        # потому, что код сломался.
        import time as _time
        soon = _time.strftime('%Y-%m-%dT00:00:00Z',
                              _time.gmtime(_time.time() + 3 * 86400))
        later = _time.strftime('%Y-%m-%dT00:00:00Z',
                               _time.gmtime(_time.time() + 300 * 86400))
        near = {'question': 'Will Bitcoin be above $110 on X?',
                'endDate': soon,
                'outcomePrices': json.dumps(['0.4', '0.6']),
                'feeType': 'crypto_fees_v2', 'liquidity': 5000}
        far = dict(near, endDate=later)
        results = module.scan([near, far])
        assert len(results) == 2
        assert results[0].confidence > results[1].confidence


class TestRunner:

    def test_unvalidated_proposals_are_marked_paper_only(self, monkeypatch,
                                                         tmp_path):
        """
        Пометка ставится в оркестраторе, а не в каждом модуле.

        Забыть её в одном из трёх — вопрос времени, а последствие — реальная
        сделка по непроверенной стратегии.
        """
        from polymarket import store as store_mod

        monkeypatch.setattr(store_mod, 'SNAPSHOTS', str(tmp_path / 's.jsonl'))
        monkeypatch.setattr(store_mod, 'DECISIONS', str(tmp_path / 'd.jsonl'))

        class Fake(base.SignalModule):
            name = 'WEATHER'
            validation = base.Validation(base.UNTESTED, 'проверка не проводилась')

            def scan(self, markets):
                return [base.SignalResult(
                    0.85, 0.60, 1.0, ['test'],
                    market={'id': 'M', 'feeType': 'weather_fees',
                            'question': 'вопрос', 'events': [{'id': 'E'}]},
                    cost=0.02, liquidity=50_000)]

        monkeypatch.setattr(runner, 'collect_markets', lambda *a, **k: [{'id': 'M'}])
        proposals = runner.run_once(total_bankroll=10_000,
                                    modules=[(Fake, ('weather_fees',))])
        assert len(proposals) == 1
        assert proposals[0]['decision'].details['paper_only'] is True
        assert proposals[0]['decision'].reason.startswith('ТОЛЬКО БУМАГА')
        # Отказы и предложения обязаны попадать в журнал решений.
        assert store_mod.read(store_mod.DECISIONS)


class TestSigmaUnits:
    """
    Разброс сравнивается в ШИРИНАХ КОРЗИНЫ, а не в градусах.

    Пока предел применялся к градусам, фаренгейтовые рынки проходили отбор
    незаслуженно: корзина там вдвое уже, и то же значение в °C означает вдвое
    более размазанное распределение. Замер по городам вскрыл это тремя
    станциями, где вышло по одной ставке и все три проиграли полностью.
    """

    def test_fahrenheit_sigma_counts_nearly_double(self):
        assert weather.sigma_in_buckets(1.0, 'C') == 1.0
        assert abs(weather.sigma_in_buckets(1.0, 'F') - 1.8) < 1e-9

    def test_new_york_is_refused_like_the_worst_station(self):
        """
        Нью-Йорк с 0.83°C — это 1.49 корзины, то есть уровень Амстердама.

        По градусам он проходил как середина списка. Число взято из обученной
        поправки, а не придумано.
        """
        ny = weather.sigma_in_buckets(0.83, 'F')
        amsterdam = weather.sigma_in_buckets(1.49, 'C')
        assert ny > params.MAX_SIGMA
        assert abs(ny - amsterdam) < 0.05

    def test_good_celsius_cities_still_pass(self):
        for sigma in (0.59, 0.61, 0.67):
            assert weather.sigma_in_buckets(sigma, 'C') <= params.MAX_SIGMA

    def test_gate_uses_bucket_units_not_degrees(self):
        """Отбор в signals() читает единицу корзины, а не только станцию."""
        source = os.path.join(ROOT, 'polymarket', 'weather.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        assert "spread_buckets = sigma_in_buckets(fit['sigma'], bucket['unit'])" in text
        assert "if not fit or fit['sigma'] > params.MAX_SIGMA:" not in text, \
            'старый отбор по градусам обязан быть убран'


class TestBudgetCanFollowTheWallet:
    """
    Сумму можно задать числом, а можно взять с кошелька.

        PM_BUDGET_MM=100      сто долларов, ровно и всегда
        PM_BUDGET_MM=max      всё, что лежит на кошельке
        PM_BUDGET_MM=80%      восемь десятых остатка, две в запасе

    Объяснение возвращается ВСЕГДА, включая отказ: бюджет отделяет «бот
    работает» от «бот стоит», и молчаливый ноль неотличим от поломки — заявок
    нет, ошибок нет, причины нет.
    """

    def test_plain_number_is_taken_as_dollars(self, monkeypatch):
        monkeypatch.setenv('PM_BUDGET_MM', '250')
        amount, why = params.budget_plan('MM', balance=9999)
        assert amount == 250.0
        assert 'числом' in why, 'кошелёк тут ни при чём'

    def test_max_takes_what_is_left_after_the_others(self, monkeypatch):
        """
        Чужие фиксированные бюджеты вычитаются: они уже обещаны.

        Иначе «максимум» означал бы «всё, включая соседское», и две стратегии
        планировали бы одни деньги.
        """
        monkeypatch.setenv('PM_BUDGET_MM', 'max')
        monkeypatch.setitem(params.BUDGET, 'WEATHER', 100.0)
        monkeypatch.setitem(params.BUDGET, 'CRYPTO', 100.0)
        monkeypatch.setitem(params.BUDGET, 'LONGSHOT', 100.0)
        monkeypatch.setitem(params.BUDGET, 'ONESIDE', 0.0)
        amount, why = params.budget_plan('MM', balance=1000.0)
        assert amount == 700.0
        assert 'занято другими' in why

    def test_share_of_the_wallet(self, monkeypatch):
        monkeypatch.setenv('PM_BUDGET_MM', '80%')
        for name in ('WEATHER', 'CRYPTO', 'LONGSHOT', 'ONESIDE'):
            monkeypatch.setitem(params.BUDGET, name, 0.0)
        amount, _ = params.budget_plan('MM', balance=1000.0)
        assert amount == pytest.approx(800.0)

    def test_two_strategies_asking_for_max_are_both_refused(self, monkeypatch):
        """
        Два «максимума» — это двойная трата одних денег.

        Отказываем обоим, а не выбираем одного тайком: тихий выбор означал бы,
        что вторая стратегия торгует на деньги, уже обещанные первой. Ровно эта
        ошибка была, когда обе схемы делили кошелёк.
        """
        monkeypatch.setenv('PM_BUDGET_MM', 'max')
        monkeypatch.setenv('PM_BUDGET_ONESIDE', 'max')
        for name in ('MM', 'ONESIDE'):
            amount, why = params.budget_plan(name, balance=1000.0)
            assert amount == 0.0
            assert 'несколько стратегий' in why

    def test_unknown_balance_is_not_treated_as_zero_or_infinity(self,
                                                                monkeypatch):
        """
        Неизвестный остаток — не ноль и не «сколько-нибудь».

        Кошелёк намеренно различает «ноль» и «не удалось спросить». Различие
        сохраняется: не зная остатка, берём запасную сумму и говорим об этом.
        Без неё бумажный прогон, где кошелька нет вовсе, не запустился бы.
        """
        monkeypatch.setenv('PM_BUDGET_MM', 'max')
        amount, why = params.budget_plan('MM', balance=None)
        assert amount > 0
        assert 'неизвест' in why

    def test_garbage_setting_stops_trading_and_says_why(self, monkeypatch):
        monkeypatch.setenv('PM_BUDGET_MM', 'сто рублей')
        amount, why = params.budget_plan('MM', balance=1000.0)
        assert amount == 0.0
        assert 'не число' in why

    def test_share_outside_reason_is_refused(self, monkeypatch):
        monkeypatch.setenv('PM_BUDGET_MM', '250%')
        assert params.budget_plan('MM', balance=1000.0)[0] == 0.0

    def test_wallet_mode_never_exceeds_the_wallet(self, monkeypatch):
        monkeypatch.setenv('PM_BUDGET_MM', 'max')
        for name in ('WEATHER', 'CRYPTO', 'LONGSHOT', 'ONESIDE'):
            monkeypatch.setitem(params.BUDGET, name, 0.0)
        amount, _ = params.budget_plan('MM', balance=42.0)
        assert amount <= 42.0

    def test_reserved_more_than_the_wallet_leaves_nothing(self, monkeypatch):
        """Если соседям обещано больше, чем есть, максимум равен нулю."""
        monkeypatch.setenv('PM_BUDGET_MM', 'max')
        monkeypatch.setitem(params.BUDGET, 'WEATHER', 5000.0)
        amount, _ = params.budget_plan('MM', balance=100.0)
        assert amount == 0.0
