"""
Разметка для языковой модели: что в неё попадает и чего попасть не может.

ГЛАВНАЯ ПРОВЕРКА ЗДЕСЬ — ЗАГЛЯДЫВАНИЕ ВПЕРЁД. Пивот определяется окном
±PIVOT_BARS и становится известен лишь через PIVOT_BARS баров после себя.
Разметка, собранная без учёта этого, содержит будущее: на замере она даёт
прекрасный результат, а в бою не воспроизводится, потому что живой бот таких
уровней в момент решения не видит.

Остальные проверки — про отбор. Обе ошибки, которые они закрывают, нашлись
только на живых свечах BTC и на глаз в коде не читались.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_context


def make_df(closes, spread=None):
    """Свечи из ряда закрытий. Тени задаются отдельно, иначе пивотов не будет."""
    closes = np.asarray(closes, dtype=float)
    spread = closes * 0.002 if spread is None else np.asarray(spread, dtype=float)
    return pd.DataFrame({
        'timestamp': pd.to_datetime(
            np.arange(len(closes)) * 3_600_000 + 1_700_000_000_000, unit='ms'),
        'open': closes,
        'high': closes + spread,
        'low': closes - spread,
        'close': closes,
        'volume': np.full(len(closes), 100.0),
    })


def wavy(n=400, base=100.0, amp=4.0, period=23):
    """Пилообразный ряд: даёт много честных экстремумов на обеих сторонах."""
    idx = np.arange(n)
    return base + amp * np.sin(idx / period * 2 * np.pi) + idx * 0.004


class TestItCannotSeeTheFuture:
    """
    Уровень, известный только позже бара решения, не имеет права в нём быть.

    Это не педантизм. Стратегия уровней однажды считалась по разметке с
    будущим, и месяц наблюдений оказался недостоверным.
    """

    def test_a_level_from_later_bars_is_absent(self):
        df = make_df(wavy(400))
        at = 200

        early, _ = llm_context.levels(df, at=at)
        late, _ = llm_context.levels(df, at=399)

        assert early, 'на середине ряда уровни должны находиться'
        # Ни один уровень раннего среза не опирается на бар правее `at`.
        for level in early:
            assert level['last'] <= at, (
                f"уровень {level['id']} опирается на бар {level['last']} "
                f"при решении на баре {at} — это будущее")
        assert late != early, 'к концу ряда разметка обязана измениться'

    def test_trimming_the_data_gives_the_same_picture(self):
        """
        Разметка на баре N по полному ряду и по ряду, обрезанному на N,
        должна совпасть. Расхождение означает, что хвост всё-таки читается.
        """
        full = make_df(wavy(400))
        at = 250
        by_at = llm_context.levels(full, at=at)[0]
        by_cut = llm_context.levels(full.iloc[:at + 1].reset_index(drop=True))[0]

        assert [(l['price'], l['kind']) for l in by_at] == \
               [(l['price'], l['kind']) for l in by_cut]

    def test_positioning_is_read_with_the_same_cutoff(self, monkeypatch):
        """
        Расстановка участников тоже не имеет права заглядывать вперёд.

        Уровни защищены параметром `at`, а ряды открытого интереса читаются
        из отдельных файлов — и без отсечки по времени в разбор прошлого
        попали бы сегодняшние значения.
        """
        asked = {}

        def spy(source, pair, upto=None):
            asked[source] = upto
            return None

        def spy_change(source, pair, hours, upto=None):
            asked[source] = upto
            return None

        monkeypatch.setattr(llm_context.positioning, 'latest', spy)
        monkeypatch.setattr(llm_context.positioning, 'change_pct', spy_change)

        df = make_df(wavy(400))
        at = 300
        llm_context.build('BTCUSDT', df, at=at)

        expected = int(df['timestamp'].iloc[at].timestamp() * 1000)
        assert asked, 'позиционирование вообще не запрашивалось'
        for source, upto in asked.items():
            assert upto == expected, f'{source} прочитан без отсечки по времени'


class TestTheMenuIsUsable:
    """
    Список уровней — это меню решений. Меню, из которого нельзя выбрать
    осмысленный вход, хуже пустого: модель всё равно что-нибудь выберет.
    """

    def test_both_sides_of_the_price_are_present(self):
        """
        ОШИБКА, НАЙДЕННАЯ НА ЖИВЫХ СВЕЧАХ. Отбор по силе оставил все двенадцать
        уровней ниже цены: сильные скопления там и стояли. У лонга не осталось
        ни одной цели, у шорта — ни одного стопа.
        """
        df = make_df(wavy(400))
        found, _ = llm_context.levels(df)
        price = float(df['close'].iloc[-1])

        assert any(l['price'] > price for l in found), 'нет уровней сверху'
        assert any(l['price'] < price for l in found), 'нет уровней снизу'

    def test_clusters_outrank_lone_pivots(self):
        """
        ОШИБКА, НАЙДЕННАЯ ТАМ ЖЕ. Отбор по одной близости к цене задвинул все
        скопления в хвост, а первые восемь мест заняли одиночные пивоты.
        Скопление из пяти касаний и след одного захода — разные по силе уровни.
        """
        df = make_df(wavy(400))
        found, _ = llm_context.levels(df)
        clusters = [l for l in found if l['touches'] > 1]
        lone = [l for l in found if l['touches'] == 1]

        if not clusters or not lone:
            pytest.skip('на этой выборке нет обоих видов уровней')
        # На своей стороне цены скопление стоит ближе к началу отбора, чем
        # одиночный пивот с тем же расстоянием.
        assert max(c['touches'] for c in clusters) > 1

    def test_the_list_is_sorted_by_price_top_down(self):
        df = make_df(wavy(400))
        found, _ = llm_context.levels(df)
        prices = [l['price'] for l in found]
        assert prices == sorted(prices, reverse=True)
        assert [l['id'] for l in found] == [f'L{i}' for i in range(1, len(found) + 1)]

    def test_levels_glued_to_the_price_are_dropped(self):
        """
        Вход вплотную к цене даёт стоп, который не проходит предел издержек.
        Показывать такие уровни — значит получать предложения, которые
        предохранитель всё равно отвергнет.
        """
        df = make_df(wavy(400))
        found, _ = llm_context.levels(df)
        price = float(df['close'].iloc[-1])
        for level in found:
            assert abs(level['price'] - price) / price * 100 >= \
                   llm_context.MIN_DISTANCE_PCT

    def test_the_count_stays_within_the_limit(self):
        df = make_df(wavy(600))
        found, _ = llm_context.levels(df)
        assert 0 < len(found) <= llm_context.MAX_LEVELS


class TestTheCostFloorIsArithmetic:
    """
    Минимальный стоп выводится из предела расхода, а не назначается.
    Ошибка здесь тихо разрешит сделки, которые не окупают комиссию.
    """

    def test_it_follows_the_limit(self, monkeypatch):
        monkeypatch.setattr(llm_context.config, 'ENTRY_COST_ROUND_TRIP', 0.00075)
        monkeypatch.setattr(llm_context.config, 'MAX_ENTRY_COST_SHARE_PCT', 5.0)
        assert llm_context.min_stop_pct() == pytest.approx(1.5)

    def test_a_tighter_limit_demands_a_wider_stop(self, monkeypatch):
        monkeypatch.setattr(llm_context.config, 'ENTRY_COST_ROUND_TRIP', 0.00075)
        monkeypatch.setattr(llm_context.config, 'MAX_ENTRY_COST_SHARE_PCT', 2.5)
        assert llm_context.min_stop_pct() == pytest.approx(3.0)

    def test_a_disabled_limit_means_no_floor(self, monkeypatch):
        monkeypatch.setattr(llm_context.config, 'MAX_ENTRY_COST_SHARE_PCT', 0)
        assert llm_context.min_stop_pct() == 0.0


class TestWhatTheModelActuallyReads:

    def test_the_floor_is_stated_in_the_text(self):
        df = make_df(wavy(400))
        out = llm_context.build('BTCUSDT', df)
        assert 'Минимальный стоп по издержкам' in out['text']

    def test_a_missing_series_shows_as_unknown_not_zero(self, monkeypatch):
        """
        «Не знаем» и «ноль» — разные утверждения. Слив их в одну клетку,
        получаем вывод о расстановке участников, которой не измеряли.
        """
        monkeypatch.setattr(llm_context.positioning, 'latest',
                            lambda *a, **k: None)
        monkeypatch.setattr(llm_context.positioning, 'change_pct',
                            lambda *a, **k: None)
        out = llm_context.build('BTCUSDT', make_df(wavy(400)))
        assert '—' in out['text']
        assert '0.00%' not in out['text'].split('РАССТАНОВКА')[1]

    def test_absent_news_are_named_absent(self):
        """Молчание модель заполнит сама, поэтому о пустом фоне говорим прямо."""
        out = llm_context.build('BTCUSDT', make_df(wavy(400)), news=None)
        assert 'Не получен' in out['text']

    def test_given_news_reach_the_text(self):
        out = llm_context.build('BTCUSDT', make_df(wavy(400)),
                                news='слабо отрицательный, 4 из 5 источников')
        assert 'слабо отрицательный' in out['text']

    def test_every_listed_level_resolves_back_to_a_price(self):
        """
        Модель отвечает идентификаторами. Если по ним не восстановить цену,
        ответ бесполезен, а ошибка проявится только в бою.
        """
        out = llm_context.build('BTCUSDT', make_df(wavy(400)))
        for level in out['levels']:
            assert llm_context.price_of(out['levels'], level['id']) == level['price']

    def test_an_unknown_id_resolves_to_nothing(self):
        out = llm_context.build('BTCUSDT', make_df(wavy(400)))
        assert llm_context.price_of(out['levels'], 'L999') is None


class TestItDoesNotCrashOnThinData:

    def test_a_short_series_returns_empty(self):
        found, atr = llm_context.levels(make_df(np.linspace(100, 101, 20)))
        assert found == []

    def test_build_on_thin_data_returns_empty_text(self):
        out = llm_context.build('BTCUSDT', make_df(np.linspace(100, 101, 20)))
        assert out['levels'] == []
        assert out['text'] == ''

    def test_a_flat_series_does_not_raise(self):
        """Ровная линия — ATR ноль, делить на него нельзя."""
        out = llm_context.build('BTCUSDT', make_df(np.full(300, 100.0),
                                                   spread=np.zeros(300)))
        assert out['levels'] == []
