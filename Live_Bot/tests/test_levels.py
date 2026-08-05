"""
Проверка стратегии уровней на сценариях с ЗАРАНЕЕ ИЗВЕСТНЫМ ответом.

Свечи здесь строятся руками так, чтобы правильный ответ был очевиден до
запуска. На реальных данных так проверить нельзя — там неизвестно, каким
ответ должен быть.

Главное, что защищается, — подтверждение входа. Замер показал, что без него
стратегия даёт -1104 R вместо +138 R: вход происходит и когда уровень
устоял, и когда его прошли насквозь. Если это правило однажды сломается,
результат превратится в подбрасывание монеты, и по доходности это будет
заметно далеко не сразу.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from levels import core, params  # noqa: E402


def series(rows):
    """rows: (high, low, close, volume) -> четыре массива."""
    arr = np.array(rows, dtype=float)
    return arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]


def flat(n, price=100.0, vol=1.0, spread=0.3):
    """Ровный фон: цена колеблется вокруг уровня, объём средний."""
    return [(price + spread, price - spread, price, vol) for _ in range(n)]


def touch(price, depth):
    """Свеча, которая опускается к цене и отскакивает."""
    return (price + 0.3, price - depth, price, 1.0)


class TestУровни:
    def test_dva_kasaniya_odnoy_ceny_dayut_uroven(self):
        rows = flat(20)
        rows[5] = touch(100.0, 2.0)      # низ 98.0
        rows[15] = touch(100.0, 2.0)     # низ 98.0
        high, low, close, _ = series(rows)
        levels = core.build_levels(high, low, tolerance_pct=0.5, min_touches=2)
        assert any(abs(lv['price'] - 98.0) < 0.2 for lv in levels)

    def test_odno_kasanie_urovnem_ne_yavlyaetsya(self):
        rows = flat(20)
        rows[10] = touch(100.0, 2.0)
        high, low, close, _ = series(rows)
        levels = core.build_levels(high, low, tolerance_pct=0.5, min_touches=2)
        assert not any(abs(lv['price'] - 98.0) < 0.2 for lv in levels)

    def test_uroven_izvesten_ne_ranshe_podtverzhdeniya_ekstremuma(self):
        """
        Экстремум подтверждается через PIVOT_N баров. Уровень, известный
        раньше, означал бы знание будущего — самая дорогая ошибка бэктеста.
        """
        rows = flat(30)
        rows[5] = touch(100.0, 2.0)
        rows[20] = touch(100.0, 2.0)
        high, low, close, _ = series(rows)
        levels = core.build_levels(high, low, tolerance_pct=0.5, min_touches=2)
        target = [lv for lv in levels if abs(lv['price'] - 98.0) < 0.2]
        assert target, 'уровень не построился'
        assert target[0]['known_at'] >= 20 + params.PIVOT_N


class TestПодтверждение:
    def test_prokol_s_vozvratom_podtverzhdaet(self):
        high, low, close, _ = series([
            (100.5, 99.5, 100.0, 1.0),
            (100.2, 97.0, 97.5, 1.0),     # прокол уровня 98
            (99.5, 97.4, 98.6, 1.0),      # возврат выше 98
        ])
        found = core.find_reclaim(high, low, close, 0, 98.0, core.LONG, 1.0)
        assert found is not None
        index, extreme = found
        assert index == 2
        assert extreme == pytest.approx(97.0)

    def test_proboy_bez_vozvrata_ne_podtverzhdaet(self):
        """Цена ушла за уровень и не вернулась — это пробой, а не отбой."""
        high, low, close, _ = series([
            (100.5, 99.5, 100.0, 1.0),
            (100.2, 97.0, 97.5, 1.0),
            (97.6, 96.0, 96.2, 1.0),
            (96.5, 95.0, 95.1, 1.0),
        ])
        assert core.find_reclaim(high, low, close, 0, 98.0, core.LONG, 1.0) is None

    def test_kasanie_bez_prokola_ne_podtverzhdaet(self):
        """Цена не дошла до уровня — подтверждать нечего."""
        high, low, close, _ = series([
            (100.5, 99.5, 100.0, 1.0),
            (100.2, 98.5, 99.0, 1.0),
            (100.0, 98.8, 99.5, 1.0),
        ])
        assert core.find_reclaim(high, low, close, 0, 98.0, core.LONG, 1.0) is None

    def test_vozvrat_pozzhe_okna_ne_schitaetsya(self):
        rows = [(100.5, 99.5, 100.0, 1.0), (100.2, 97.0, 97.5, 1.0)]
        rows += [(97.8, 97.0, 97.2, 1.0)] * (params.RECLAIM_BARS + 2)
        rows += [(99.0, 97.5, 98.8, 1.0)]           # возврат, но слишком поздно
        high, low, close, _ = series(rows)
        assert core.find_reclaim(high, low, close, 0, 98.0, core.LONG, 1.0) is None


class TestСетап:
    def _scene(self, reclaim_volume):
        """Уровень 98, подход сверху, прокол и возврат на заданном объёме."""
        rows = flat(80, price=100.0, vol=1.0)
        rows[20] = touch(100.0, 2.0)
        rows[40] = touch(100.0, 2.0)
        # Цена подходит к уровню. Расстояние до него должно быть меньше
        # TRIGGER_ATR * ATR (здесь ATR ~0.67), иначе сетап не рассматривается.
        rows[75] = (98.7, 98.3, 98.4, 1.0)
        rows[76] = (98.6, 97.0, 97.4, 1.0)                  # прокол
        rows[77] = (99.0, 97.3, 98.7, reclaim_volume)       # возврат
        return series(rows)

    def test_setap_sobiraetsya_na_obeme(self):
        high, low, close, volume = self._scene(reclaim_volume=5.0)
        setup, reason = core.evaluate(high, low, close, volume, 75)
        assert setup is not None, reason
        assert setup['direction'] == core.LONG
        assert setup['stop_loss'] < setup['entry'] < setup['target']
        assert setup['rr'] > 0

    def test_bez_obema_setapa_net(self):
        """Уровень, который никто не защищает объёмом, не торгуется."""
        high, low, close, volume = self._scene(reclaim_volume=1.0)
        setup, reason = core.evaluate(high, low, close, volume, 75)
        assert setup is None
        assert 'объём' in reason

    def test_stop_za_ekstremumom_prokola(self):
        """
        Стоп обязан стоять ЗА точкой прокола, а не за уровнем: именно у
        уровня собирают стопы, и стоп там выбивается шумом.
        """
        high, low, close, volume = self._scene(reclaim_volume=5.0)
        setup, _ = core.evaluate(high, low, close, volume, 75)
        assert setup['stop_loss'] < setup['pierce_extreme'] + 1e-9

    def test_stop_ne_tonshe_minimuma(self):
        """
        На тонком стопе издержки съедают половину риска. Минимум — часть
        стратегии, а не перестраховка: без него замер давал -99% на всех
        конфигурациях.
        """
        high, low, close, volume = self._scene(reclaim_volume=5.0)
        setup, _ = core.evaluate(high, low, close, volume, 75)
        floor = setup['entry'] * params.MIN_STOP_PCT / 100
        assert setup['sl_distance'] >= floor - 1e-9

    def test_prichina_otkaza_vozvrashchaetsya_vsegda(self):
        high, low, close, volume = series(flat(80))
        setup, reason = core.evaluate(high, low, close, volume, 75)
        assert setup is None
        assert reason and isinstance(reason, str)

    def test_malo_istorii_ne_padaet(self):
        high, low, close, volume = series(flat(10))
        setup, reason = core.evaluate(high, low, close, volume, 5)
        assert setup is None
        assert 'истории' in reason


class TestАдаптер:
    def test_signal_v_formate_bota(self, monkeypatch):
        import strategy_levels

        rows = flat(80, price=100.0, vol=1.0)
        rows[20] = touch(100.0, 2.0)
        rows[40] = touch(100.0, 2.0)
        rows[75] = (98.7, 98.3, 98.4, 1.0)
        rows[76] = (98.6, 97.0, 97.4, 1.0)
        rows[77] = (99.0, 97.3, 98.7, 5.0)
        high, low, close, volume = series(rows)

        setup, reason = core.evaluate(high, low, close, volume, 75)
        assert setup is not None, reason

        signal = strategy_levels._to_bot_signal(setup, 'BTCUSDT', 10000.0, None)
        params_ = signal['params']
        assert params_['entry'] == setup['entry']
        assert params_['stop_loss'] == setup['stop_loss']
        assert params_['tp_targets'] == [setup['target']]
        assert params_['tp_fractions'] == [1.0]
        # Безубыток выключен: замер показал вред у всех трёх стратегий
        assert params_['be_level'] is None
        assert params_['breakeven_after_tp'] is False
        assert params_['position_size'] > 0
        assert 'уровня' in signal['why']
