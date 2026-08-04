"""
Проверка разметки режима рынка и множителя риска.

Главное, что здесь защищается, — причинность. Разметка обязана зависеть
только от прошлого: правило, настроенное на порог из будущего, вживую не
воспроизводится, а в бэктесте выглядит прекрасно.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smc import params  # noqa: E402
from smc import regime  # noqa: E402


def straight(n, step=1.0, start=100.0):
    """Ровный ход в одну сторону: ER должен быть равен единице."""
    return start + np.arange(n) * step


def zigzag(n, amp=1.0, start=100.0):
    """Пила без итогового смещения: ER около нуля."""
    return start + amp * (np.arange(n) % 2)


def test_er_ravnyi_edinice_na_pryamom_hode():
    er, moved = regime.efficiency_ratio(straight(60), window=30)
    assert er == pytest.approx(1.0)
    assert moved > 0


def test_er_okolo_nulya_na_pile():
    er, _ = regime.efficiency_ratio(zigzag(60), window=30)
    assert er < 0.1


def test_er_ne_schitaetsya_bez_istorii():
    er, moved = regime.efficiency_ratio(straight(10), window=30)
    assert er is None and moved is None


def test_rezhim_neizvesten_poka_malo_istorii():
    # Данных на ER хватает, на порог — нет: разметки быть не должно.
    closes = straight(60)
    name, er, threshold = regime.classify(closes, window=30, quantile=0.667,
                                          min_history=180)
    assert name is regime.UNKNOWN
    assert er is not None      # сам ER посчитан
    assert threshold is None   # а порога ещё нет
    assert regime.risk_multiplier(name) == 1.0


def test_rost_i_padenie_razlichayutsya_znakom():
    rng = np.random.default_rng(7)
    noise = np.cumsum(rng.normal(0, 1, 400))     # накопление истории ER
    up = np.concatenate([noise, noise[-1] + np.arange(1, 41) * 5.0])
    down = np.concatenate([noise, noise[-1] - np.arange(1, 41) * 5.0])
    name_up, _, _ = regime.classify(up, window=30, quantile=0.667, min_history=180)
    name_down, _, _ = regime.classify(down, window=30, quantile=0.667, min_history=180)
    assert name_up == regime.TREND_UP
    assert name_down == regime.TREND_DOWN


def test_pila_posle_trenda_priznaetsya_bokovikom():
    trend = np.arange(400) * 2.0                       # история сплошь трендовая
    flat = trend[-1] + (np.arange(1, 41) % 2) * 0.5    # затем топтание
    closes = np.concatenate([trend, flat])
    name, er, threshold = regime.classify(closes, window=30, quantile=0.667,
                                          min_history=180)
    assert name == regime.RANGE
    assert er < threshold


def test_porog_ne_zavisit_ot_budushchego():
    """
    Разметка дня не должна меняться от того, что случилось ПОСЛЕ него.

    Это ровно та ошибка, из-за которой разбор пришлось пересчитывать: порог
    брался как квантиль по всему периоду, то есть знал будущее.
    """
    rng = np.random.default_rng(11)
    base = 100 + np.cumsum(rng.normal(0, 1, 500))
    sudden = np.concatenate([base, base[-1] + np.arange(1, 201) * 10.0])

    now = regime.classify(base, window=30, quantile=0.667, min_history=180)
    later = regime.classify(sudden[:len(base)], window=30, quantile=0.667,
                            min_history=180)
    assert now == later


def test_mnozhitel_tolko_umenshaet(monkeypatch):
    monkeypatch.setattr(params, 'REGIME_RISK_SCALE', 2.5)
    assert regime.risk_multiplier(regime.TREND_UP) == 1.0
    monkeypatch.setattr(params, 'REGIME_RISK_SCALE', -1.0)
    assert regime.risk_multiplier(regime.TREND_DOWN) == 0.0


def test_v_bokovike_i_bez_rezhima_razmer_polnyi(monkeypatch):
    monkeypatch.setattr(params, 'REGIME_RISK_SCALE', 0.5)
    assert regime.risk_multiplier(regime.RANGE) == 1.0
    assert regime.risk_multiplier(regime.UNKNOWN) == 1.0
    assert regime.risk_multiplier(regime.TREND_UP) == 0.5


def test_edinica_polnostyu_otklyuchaet_pravilo(monkeypatch):
    monkeypatch.setattr(params, 'REGIME_RISK_SCALE', 1.0)
    for name in (regime.TREND_UP, regime.TREND_DOWN, regime.RANGE, regime.UNKNOWN):
        assert regime.risk_multiplier(name) == 1.0


def test_opisanie_chitaemo():
    text = regime.describe(regime.TREND_DOWN, 0.412, 0.270, 0.5)
    assert 'падение' in text and '0.412' in text and '×0.50' in text
    assert 'неизвестен' in regime.describe(regime.UNKNOWN, None, None, 1.0)
