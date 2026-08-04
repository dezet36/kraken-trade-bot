"""
Юнит-тесты ядра SMC.

Проверяют инварианты, поломка которых не видна глазом в логах бота, но
уничтожает результат: подглядывание в будущее, разъезд таймфреймов,
неверная разметка структуры.

Запуск:  python -m pytest Live_Bot/tests -q
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smc import fib, imbalance, liquidity, signal, structure, swings  # noqa: E402

T0 = pd.Timestamp('2026-01-01', tz='UTC')


def build_zigzag(pivots, legs=4, freq='h'):
    """
    Свечи зигзагом через заданные экстремумы.

    Экстремум сидит в ТЕНИ разворотной свечи — как в реальном развороте:
    цена прокалывает уровень и закрывается обратно. Если положить экстремум
    в close, следующая свеча откроется ровно на нём, и строгий фрактал
    перестанет детектироваться (это артефакт синтетики, не рынка).
    """
    rows = []
    price = pivots[0]

    def push(o, h, l, c):
        rows.append({
            'timestamp': T0 + pd.Timedelta(**{{'h': 'hours', 'D': 'days'}[freq]: len(rows)}),
            'open': o, 'high': h, 'low': l, 'close': c, 'volume': 100.0,
        })

    for target in pivots[1:]:
        up = target > price
        span = abs(target - price)
        approach = target - 0.03 * span if up else target + 0.03 * span
        for j in range(1, legs + 1):
            nxt = price + (approach - price) * j / legs
            push(price, max(price, nxt), min(price, nxt), nxt)
            price = nxt
        pullback = target - 0.10 * span if up else target + 0.10 * span
        push(price, max(price, target, pullback), min(price, target, pullback), pullback)
        price = pullback

    return pd.DataFrame(rows)


UPTREND = [100, 120, 110, 138, 126, 155, 141, 112]


# ── Свинги ───────────────────────────────────────────────────────────────────
class TestSwings:
    def test_finds_exact_pivots(self):
        df = build_zigzag(UPTREND)
        highs, lows = swings.find_swings(df, n=2)

        assert [round(h['price']) for h in highs] == [120, 138, 155]
        assert [round(l['price']) for l in lows] == [110, 126]

    def test_confirmed_at_lags_by_n(self):
        """Свинг известен только через N свечей — иначе бот видит будущее."""
        df = build_zigzag(UPTREND)
        for n in (1, 2, 3):
            highs, lows = swings.find_swings(df, n=n)
            for swing in [*highs, *lows]:
                assert swing['confirmed_at'] == swing['index'] + n

    def test_visible_swings_never_leak_future(self):
        df = build_zigzag(UPTREND)
        highs, lows = swings.find_swings(df, n=2)
        allswings = highs + lows

        for at in range(len(df)):
            for swing in swings.visible_swings(allswings, at):
                assert swing['confirmed_at'] <= at

    def test_merge_keeps_every_swing(self):
        """
        Регрессия на дефект, найденный аудитом ядра.

        Раньше два хая подряд без лоя между ними схлопывались в один — более
        высокий. Решение принималось по БУДУЩИМ данным: чтобы узнать, что
        второй хай выше, надо дожить до второго хая. А в момент между ними
        первый был настоящим уровнем, и живой бот торговал бы от него.

        Замер на 900 свечах показал цену этой «оптимизации»: удалялось 64
        свинга из 257, и ТРИ события слома структуры бэктест терял целиком.
        Order-блоки привязаны к сломам — значит бэктест не видел сделок,
        которые бой увидит.
        """
        highs = [
            {'index': 5, 'price': 100.0, 'kind': 'high', 'confirmed_at': 7, 'time': T0},
            {'index': 9, 'price': 105.0, 'kind': 'high', 'confirmed_at': 11, 'time': T0},
        ]
        lows = [{'index': 15, 'price': 90.0, 'kind': 'low', 'confirmed_at': 17, 'time': T0}]

        merged = swings.merge_swings(highs, lows)

        assert len(merged) == 3
        assert [s['index'] for s in merged] == [5, 9, 15]

    def test_structure_on_truncated_history_matches_full(self):
        """
        Главный инвариант ядра: структура на свече i не зависит от того, что
        было ПОСЛЕ i. Иначе бэктест меряет не ту стратегию, которая торгует.
        """
        df = build_zigzag([100, 118, 108, 132, 121, 147, 133, 160, 112], legs=3)

        full = structure.build_structure(df)
        for i in range(20, len(df), 4):
            cut = structure.build_structure(df.iloc[:i + 1].reset_index(drop=True))

            seen_full = [(p['index'], p['kind']) for p in structure.visible_points(full, i)]
            seen_live = [(p['index'], p['kind']) for p in structure.visible_points(cut, i)]
            assert seen_full == seen_live, f'свинги разошлись на свече {i}'

            events_full = [(e['index'], e['type']) for e in full['events'] if e['index'] <= i]
            events_live = [(e['index'], e['type']) for e in cut['events']]
            assert events_full == events_live, f'события разошлись на свече {i}'


# ── Структура ────────────────────────────────────────────────────────────────
class TestStructure:
    def test_labels_uptrend(self):
        df = build_zigzag(UPTREND)
        st = structure.build_structure(df)
        labels = [p['label'] for p in st['points'] if p['label']]

        assert labels[:3] == ['HH', 'HL', 'HH']

    def test_break_of_confirmed_hl_is_trend_break(self):
        """
        §2.2: обновление подтверждённого HL — слом бычьего тренда.
        Ожидаем CHoCH вниз, а не BOS.
        """
        df = build_zigzag(UPTREND)
        st = structure.build_structure(df)

        bearish = [e for e in st['events'] if e['direction'] == 'BEARISH']
        assert bearish, 'слом бычьего тренда не найден'
        assert bearish[-1]['type'] == 'CHOCH'

    def test_continuation_is_bos(self):
        df = build_zigzag(UPTREND)
        st = structure.build_structure(df)

        bullish = [e for e in st['events'] if e['direction'] == 'BULLISH']
        # Первый пробой вверх из NEUTRAL — смена характера, дальнейшие — BOS
        assert bullish[0]['type'] == 'CHOCH'
        assert any(e['type'] == 'BOS' for e in bullish[1:])

    def test_state_at_is_monotonic_in_time(self):
        """state_at на свече i не должен зависеть от данных после i."""
        df = build_zigzag(UPTREND)
        st = structure.build_structure(df)

        for event in st['events']:
            before = structure.state_at(st, event['index'] - 1)
            at = structure.state_at(st, event['index'])
            if before['last_event'] is not None:
                assert before['last_event']['index'] < event['index']
            assert at['last_event']['index'] == event['index']

    def test_last_leg_matches_trend(self):
        df = build_zigzag(UPTREND)
        st = structure.build_structure(df)
        leg = structure.last_leg(st)

        assert leg is not None
        assert leg['direction'] == 'BULLISH'
        assert leg['start']['kind'] == 'low' and leg['end']['kind'] == 'high'
        assert leg['size'] == pytest.approx(abs(leg['end']['price'] - leg['start']['price']))


# ── Фибоначчи ────────────────────────────────────────────────────────────────
class TestFib:
    @staticmethod
    def _leg(direction='BULLISH'):
        if direction == 'BULLISH':
            start, end = 100.0, 200.0
        else:
            start, end = 200.0, 100.0
        return {
            'direction': direction,
            'start': {'price': start, 'index': 0},
            'end': {'price': end, 'index': 10},
            'size': abs(end - start),
        }

    def test_retracement_bullish(self):
        leg = self._leg('BULLISH')
        assert fib.retracement(leg, 0.0) == pytest.approx(200.0)
        assert fib.retracement(leg, 0.5) == pytest.approx(150.0)
        assert fib.retracement(leg, 1.0) == pytest.approx(100.0)

    def test_retracement_bearish_mirrors(self):
        leg = self._leg('BEARISH')
        assert fib.retracement(leg, 0.5) == pytest.approx(150.0)
        assert fib.retracement(leg, 1.0) == pytest.approx(200.0)

    def test_discount_and_premium(self):
        """§10.1: покупаем со скидкой, продаём с наценкой."""
        leg = self._leg('BULLISH')
        assert fib.market_side(120.0, leg) == fib.DISCOUNT
        assert fib.market_side(180.0, leg) == fib.PREMIUM

        assert fib.is_valid_side(120.0, leg, 'BULLISH')
        assert not fib.is_valid_side(180.0, leg, 'BULLISH')

    def test_ote_inside_deep_retracement(self):
        leg = self._leg('BULLISH')
        bottom, top = fib.ote_zone(leg)
        # OTE = 0.62-0.79 коррекции, то есть цены 121..138 при ноге 100->200
        assert bottom == pytest.approx(121.0)
        assert top == pytest.approx(138.0)
        assert fib.in_ote(130.0, leg)

    def test_invalidation_at_886(self):
        leg = self._leg('BULLISH')
        assert fib.invalidation_level(leg) == pytest.approx(111.4)
        assert fib.is_invalidated(110.0, leg)
        assert not fib.is_invalidated(115.0, leg)

    def test_targets_are_beyond_entry(self):
        leg = self._leg('BULLISH')
        targets = fib.targets(leg, entry=150.0)
        assert targets and all(t > 150.0 for t in targets)
        assert targets == sorted(targets)

    def test_law_of_effort(self):
        """§10.3: коррекция должна идти дольше импульса."""
        leg = self._leg('BULLISH')   # импульс занял 10 свечей
        assert fib.law_of_effort(leg, correction_bars=15)
        assert not fib.law_of_effort(leg, correction_bars=5)


# ── Имбаланс ─────────────────────────────────────────────────────────────────
class TestImbalance:
    @staticmethod
    def _df(rows):
        return pd.DataFrame([
            {'timestamp': T0 + pd.Timedelta(hours=i), 'open': o, 'high': h,
             'low': l, 'close': c, 'volume': 1.0}
            for i, (o, h, l, c) in enumerate(rows)
        ])

    def test_detects_bullish_gap(self):
        # high[0]=101 < low[2]=105 -> бычий гэп 101..105
        df = self._df([(100, 101, 99, 100), (103, 106, 102, 105), (106, 110, 105, 109)])
        gaps = imbalance.find_fvg(df, min_size_pct=0.0)

        assert len(gaps) == 1
        assert gaps[0]['direction'] == 'BULLISH'
        assert gaps[0]['bottom'] == pytest.approx(101)
        assert gaps[0]['top'] == pytest.approx(105)

    def test_detects_bearish_gap(self):
        df = self._df([(110, 111, 109, 110), (107, 108, 104, 105), (104, 105, 100, 101)])
        gaps = imbalance.find_fvg(df, min_size_pct=0.0)

        assert len(gaps) == 1
        assert gaps[0]['direction'] == 'BEARISH'

    def test_full_fill_reaches_one(self):
        """§4.2: FF — возврат к максимуму первой свечи для бычьего гэпа."""
        df = self._df([
            (100, 101, 99, 100), (103, 106, 102, 105), (106, 110, 105, 109),
            (109, 110, 100, 101),          # провал обратно ниже 101
        ])
        gaps = imbalance.find_fvg(df, min_size_pct=0.0)
        assert imbalance.fill_state(df, gaps[0], at_index=3) == pytest.approx(1.0)

    def test_no_fill_before_gap_forms(self):
        df = self._df([(100, 101, 99, 100), (103, 106, 102, 105), (106, 110, 105, 109)])
        gaps = imbalance.find_fvg(df, min_size_pct=0.0)
        assert imbalance.fill_state(df, gaps[0], at_index=2) == 0.0


# ── Ликвидность ──────────────────────────────────────────────────────────────
class TestLiquidity:
    def test_equal_highs_detected(self):
        points = [
            {'index': 10, 'price': 100.0, 'kind': 'high', 'confirmed_at': 12},
            {'index': 20, 'price': 100.05, 'kind': 'high', 'confirmed_at': 22},
        ]
        clusters = liquidity.find_equal_levels(points, 'high', tolerance_pct=0.01)

        assert len(clusters) == 1
        assert clusters[0]['type'] == 'EQH'
        assert clusters[0]['count'] == 2

    def test_distant_highs_not_equal(self):
        points = [
            {'index': 10, 'price': 100.0, 'kind': 'high', 'confirmed_at': 12},
            {'index': 20, 'price': 130.0, 'kind': 'high', 'confirmed_at': 22},
        ]
        assert liquidity.find_equal_levels(points, 'high', tolerance_pct=0.01) == []

    def test_reference_levels_use_previous_period(self):
        """PDH обязан быть максимумом ПРОШЛОГО дня, иначе это подглядывание."""
        rows = []
        for day in range(3):
            for hour in range(24):
                base = 100 + day * 10
                rows.append({
                    'timestamp': T0 + pd.Timedelta(days=day, hours=hour),
                    'open': base, 'high': base + 5, 'low': base - 5,
                    'close': base, 'volume': 1.0,
                })
        df = pd.DataFrame(rows)

        levels = liquidity.build_reference_levels(df)
        # Второй день: PDH = максимум первого дня = 105
        assert levels['pdh'].iloc[24] == pytest.approx(105)
        # Третий день: PDH = максимум второго = 115
        assert levels['pdh'].iloc[48] == pytest.approx(115)
        # Первый день прошлого не имеет
        assert pd.isna(levels['pdh'].iloc[0])


# ── Связь таймфреймов (регрессия на реальный баг) ─────────────────────────────
class TestTimeframeAlignment:
    def test_to_ns_normalises_millisecond_resolution(self):
        """
        Регрессия: pandas хранит datetime64 в разрешении, в котором колонка
        была создана. to_datetime(unit='ms') даёт datetime64[ms], и наивный
        .astype('int64') вернёт МИЛЛИСЕКУНДЫ, тогда как Timestamp.value —
        наносекунды. Из-за этого align_index всегда возвращал последнюю
        свечу, и bias читался из конца истории.
        """
        ms_series = pd.to_datetime(pd.Series([1748563200000]), unit='ms', utc=True)
        ns_series = pd.to_datetime(pd.Series(['2025-05-30 00:00:00']), utc=True)

        assert signal.to_ns(ms_series)[0] == signal.to_ns(ns_series)[0]
        assert signal.to_ns(ms_series)[0] == 1748563200000000000

    def test_align_index_picks_last_closed_bar(self):
        daily = pd.DataFrame({
            'timestamp': pd.to_datetime(
                [1748563200000, 1748649600000, 1748736000000], unit='ms', utc=True),
            'open': [1.0, 2.0, 3.0], 'high': [1, 2, 3],
            'low': [1, 2, 3], 'close': [1, 2, 3], 'volume': [1, 1, 1],
        })

        # Момент внутри второго дня -> должен вернуть индекс 1, не 2
        mid_day_two = pd.Timestamp('2025-05-31 12:00', tz='UTC')
        assert signal.align_index(daily, mid_day_two) == 1

        # Ровно на открытии третьего -> индекс 2
        assert signal.align_index(daily, pd.Timestamp('2025-06-01 00:00', tz='UTC')) == 2

        # До начала истории -> -1
        assert signal.align_index(daily, pd.Timestamp('2020-01-01', tz='UTC')) == -1

    def test_duration_excludes_still_forming_bar(self):
        """
        Регрессия: свеча старшего ТФ доступна только ПОСЛЕ закрытия.

        Дневная свеча текущего дня имеет метку 00:00 — она меньше текущего
        момента, и наивная проверка «метка <= сейчас» её принимает. Но её
        high/low/close агрегируют весь день вперёд, поэтому bias, считанный
        по ней, знает будущее.
        """
        day_ns = 24 * 3600 * 1_000_000_000
        daily = pd.DataFrame({
            'timestamp': pd.to_datetime(
                ['2026-01-01', '2026-01-02', '2026-01-03'], utc=True),
            'open': [1.0, 2.0, 3.0], 'high': [1, 2, 3],
            'low': [1, 2, 3], 'close': [1, 2, 3], 'volume': [1, 1, 1],
        })

        midday_third = pd.Timestamp('2026-01-03 12:00', tz='UTC')

        # Без длительности берётся ещё не закрытая свеча третьего дня
        assert signal.align_index(daily, midday_third) == 2
        # С длительностью — последняя ЗАКРЫТАЯ, то есть второй день
        assert signal.align_index(daily, midday_third, duration_ns=day_ns) == 1

        # Ровно в момент закрытия третьего дня он уже доступен
        closed = pd.Timestamp('2026-01-04 00:00', tz='UTC')
        assert signal.align_index(daily, closed, duration_ns=day_ns) == 2

    def test_bar_duration_is_robust_to_gaps(self):
        """Пропуск свечей не должен искажать оценку таймфрейма."""
        stamps = pd.to_datetime(
            ['2026-01-01 00:00', '2026-01-01 01:00', '2026-01-01 02:00',
             '2026-01-01 09:00',   # разрыв: биржа не отдала свечи
             '2026-01-01 10:00', '2026-01-01 11:00'], utc=True)
        df = pd.DataFrame({
            'timestamp': stamps, 'open': 1.0, 'high': 1.0,
            'low': 1.0, 'close': 1.0, 'volume': 1.0,
        })

        assert signal.bar_duration_ns(df) == 3600 * 1_000_000_000

    def test_align_index_never_returns_future_bar(self):
        hourly = build_zigzag(UPTREND)
        daily = (hourly.set_index('timestamp')
                 .resample('1D')
                 .agg({'open': 'first', 'high': 'max', 'low': 'min',
                       'close': 'last', 'volume': 'sum'})
                 .dropna().reset_index())

        for i in range(len(hourly)):
            ts = hourly['timestamp'].iloc[i]
            idx = signal.align_index(daily, ts)
            if idx >= 0:
                assert daily['timestamp'].iloc[idx] <= ts
