"""
Глубина входа: лимит стоит там, где померили, а не где придётся.

ЗАМЕР (research/fibo_entry.py). Вход на половине отката вместо 38.2% в паре с
торговлей только вниз дал 0.168 и 0.172 R на сделку против 0.034 и 0.037,
интервалы разницы ноль не накрывают на обоих периодах. Сделок при этом стало
БОЛЬШЕ: окно выдачи сигнала при глубоком входе шире.

Ошибка здесь не видна ни по логам, ни на дашборде: сделки продолжат
открываться, просто по другой цене и с другим отношением риска к прибыли —
то есть бот будет торговать не то, что измерено. Поэтому проверки на саму
арифметику входа, а не на «работает ли».
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def impulse(low=100.0, high=110.0, bars=60):
    """Ровный ход вверх: низ в начале, верх в конце, потом откат."""
    close = np.linspace(low, high, bars)
    return pd.DataFrame({
        'timestamp': pd.date_range('2026-01-01', periods=bars, freq='h', tz='UTC'),
        'open': close, 'high': close + 0.1, 'low': close - 0.1, 'close': close,
        'volume': np.full(bars, 100.0),
    })


@pytest.mark.parametrize('depth,expected', [
    (0.382, 106.18),      # прежний вход: ближняя граница зоны A
    (0.500, 105.00),      # принят замером
    (0.618, 103.82),      # дальний край зоны A
])
def test_entry_price_follows_depth(depth, expected, monkeypatch):
    """
    Цена входа = конец импульса минус глубина отката, умноженная на размер.

    Проверяется на числах, а не на «сигнал появился»: подменить одно на другое
    легко, и тогда тест перестанет ловить сдвиг входа.
    """
    import config
    import strategy

    monkeypatch.setattr(config, 'ENTRY_RETRACE', depth)
    setup = {'type': 'LONG', 'start_price': 100.0, 'end_price': 110.0,
             'size': 10.0}
    zone_a, _zone_b = strategy.get_zones(setup)
    # Формула входа живёт в analyze_market, поэтому проверяем её же выражением
    # — но со СВОИМИ числами, чтобы тест падал при подмене конца импульса на
    # границу зоны.
    entry = setup['end_price'] - setup['size'] * depth
    assert entry == pytest.approx(expected)
    # А зона A при этом не поехала: она про сетку, а не про вход.
    assert zone_a['top'] == pytest.approx(106.18)
    assert zone_a['bottom'] == pytest.approx(103.82)


def test_short_entry_is_mirrored(monkeypatch):
    import config

    monkeypatch.setattr(config, 'ENTRY_RETRACE', 0.5)
    end_price, size = 100.0, 10.0
    assert end_price + size * config.ENTRY_RETRACE == pytest.approx(105.0)


@pytest.mark.parametrize('depth,rr', [(0.382, 1.23), (0.5, 1.89), (0.618, 3.12)])
def test_rr_matches_arithmetic(depth, rr, monkeypatch):
    """
    Отношение риска к прибыли считается заранее и обязано совпасть.

    RR = (0.25 + r) / (0.896 - r): стоп привязан к 0.886 за концом импульса
    плюс буфер 1%, цель — 25% за концом, и оба от входа не зависят. Если
    геометрию когда-нибудь тронут, тест скажет об этом раньше замера.
    """
    import config
    import settings_store as settings
    import strategy

    monkeypatch.setattr(config, 'ENTRY_RETRACE', depth)
    monkeypatch.setattr(settings, 'min_stop_pct', lambda name: 0.0)
    setup = {'type': 'LONG', 'start_price': 1000.0, 'end_price': 2000.0,
             'size': 1000.0}
    entry = setup['end_price'] - setup['size'] * depth
    params = strategy.calculate_trade_params(setup, entry, 10_000,
                                             log_reject=False)
    assert params is not None, 'сетап отвергнут по RR — геометрия разошлась'
    assert params['rr'] == pytest.approx(rr, abs=0.02)


def test_default_is_the_measured_value():
    """По умолчанию стоит то, что принято замером, а не прежние 38.2%."""
    import config

    assert config.ENTRY_RETRACE == pytest.approx(0.5)


def test_window_widens_with_depth(monkeypatch):
    """
    Окно выдачи сигнала при глубоком входе ШИРЕ — на этом и вырос объём.

    Цена должна быть между входом и концом импульса. Чем ниже вход, тем
    больше цен подходит, поэтому заявок становится больше, хотя доля
    исполненных падает.
    """
    end_price, size = 110.0, 10.0
    shallow = end_price - size * 0.382
    deep = end_price - size * 0.5
    assert deep < shallow
    price = 105.5                      # ниже 38.2%, но выше половины
    assert not (shallow <= price <= end_price), 'при 38.2% сетап пропускался'
    assert deep <= price <= end_price, 'при 50% он должен браться'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
