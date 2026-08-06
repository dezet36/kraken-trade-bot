"""
Касания уровня доезжают от ядра стратегии до разметки графика.

ЗАЧЕМ. У стратегии уровней сетап — это сам уровень, а уровень сделан
касаниями. График сделки начинался за час до входа, подпись обещала
«касаний 3», и проверить это на картинке было нечем: касания случились
раньше и в окно не попадали.

Цепочка длинная — ядро, живая стратегия, разметка сделки, — и рвётся она
молча: пропавшее поле не ошибка, а просто отсутствие кружков на картинке.
Поэтому каждое звено проверяется отдельно.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _series_with_level():
    """
    Ряд с honest-уровнем: цена трижды отбивается от одной цены.

    Строится руками, а не берётся с биржи: тест должен падать от правки
    кода, а не от того, что рынок в тот день вёл себя иначе.
    """
    n = 240
    close = np.full(n, 100.0)
    high = np.full(n, 100.4)
    low = np.full(n, 99.6)
    # Три касания сверху в одну цену, с запасом между ними.
    for i in (30, 90, 150):
        high[i] = 103.0
        close[i] = 101.5
    # Между касаниями цена уходит вниз, иначе пивота не выйдет.
    for i in (45, 105, 165):
        low[i] = 97.0
        close[i] = 98.0
    return high, low, close


def test_core_records_touch_points():
    from levels import core

    high, low, close = _series_with_level()
    levels = core.build_levels(high, low, tolerance_pct=0.5, min_touches=2)
    assert levels, 'уровень не собрался — тест бесполезен, проверь ряд'

    top = max(levels, key=lambda lv: lv['touches'])
    assert top['touches'] == len(top['points']), 'число касаний и список разошлись'
    assert top['first_index'] == min(p['index'] for p in top['points'])
    for point in top['points']:
        assert {'index', 'price', 'kind'} <= set(point)


def test_first_index_is_earliest_not_latest():
    """
    Первое касание — самое раннее, а не последнее.

    Перепутать легко: рядом лежит known_at, и это МАКСИМУМ по членам. Если
    взять его, окно графика начнётся почти у входа и смысл потеряется.
    """
    from levels import core

    high, low, close = _series_with_level()
    levels = core.build_levels(high, low, tolerance_pct=0.5, min_touches=2)
    top = max(levels, key=lambda lv: lv['touches'])
    assert top['first_index'] < top['known_at']


def test_geometry_carries_touches_and_start(monkeypatch, tmp_path):
    """Разметка сделки получает и время первого касания, и сами касания."""
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    import paper_broker as pb

    signal = {
        'setup': {
            'type': 'SHORT',
            'start_price': 100.0, 'end_price': 99.0, 'size': 1.0,
            'start_time': '2026-08-01T03:00:00Z',
            'touches_at': [{'at': '2026-08-01T03:00:00Z', 'price': 100.1},
                           {'at': '2026-08-01T09:00:00Z', 'price': 99.95},
                           {'at': '2026-08-01T15:00:00Z', 'price': 100.05}],
        },
        'levels': {'level': 100.0, 'touches': 3},
    }
    geo = pb.PaperBroker._geometry('LEVELS', signal)

    assert geo['from'] == '2026-08-01T03:00:00Z'
    assert len(geo['touches']) == 3
    assert geo['touches'][0]['price'] == 100.1
    # Линия уровня с подписью тоже должна остаться.
    assert any('уровень' in (line.get('label') or '') for line in geo['lines'])


def test_geometry_without_touches_still_works(monkeypatch, tmp_path):
    """Старые записи без касаний не должны ронять разметку."""
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    import paper_broker as pb

    geo = pb.PaperBroker._geometry(
        'LEVELS', {'setup': {'type': 'LONG'}, 'levels': {'level': 50.0}})
    assert 'touches' not in geo
    assert 'from' not in geo
    assert geo['lines']


def test_live_signal_converts_indices_to_time(monkeypatch, tmp_path):
    """
    Живая стратегия отдаёт ВРЕМЯ, а не номера баров.

    Номер бара, ушедший в журнал числом, через неделю не значит ничего:
    таблица к тому времени другая, и отметка встанет не туда.
    """
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    import strategy_levels

    stamps = pd.date_range('2026-08-01', periods=60, freq='h', tz='UTC')
    df = pd.DataFrame({'timestamp': stamps, 'close': np.full(60, 100.0)})
    setup = {
        'direction': 'SHORT', 'level': 100.0, 'touches': 2, 'mirror': False,
        'entry': 99.5, 'stop_loss': 100.6, 'target': 97.0, 'rr': 2.3,
        'sl_distance': 1.1, 'volume_ratio': 2.0,
        'reclaim_index': 50, 'pierce_index': 48, 'pierce_extreme': 100.8,
        'points': [{'index': 4, 'price': 100.2, 'kind': 'high'},
                   {'index': 33, 'price': 99.9, 'kind': 'high'}],
        'first_index': 4,
    }
    signal = strategy_levels._to_bot_signal(setup, 'BTCUSDT', 10_000, df)

    assert signal['setup']['start_time'] == '2026-08-01T04:00:00Z'
    times = [p['at'] for p in signal['setup']['touches_at']]
    # Бары часовые от полуночи 1 августа: 4-й — 04:00 того же дня,
    # 33-й — 09:00 СЛЕДУЮЩЕГО.
    assert times == ['2026-08-01T04:00:00Z', '2026-08-02T09:00:00Z']


def test_live_signal_survives_bad_index(monkeypatch, tmp_path):
    """Индекс за пределами таблицы не должен ронять сигнал целиком."""
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    import strategy_levels

    stamps = pd.date_range('2026-08-01', periods=10, freq='h', tz='UTC')
    df = pd.DataFrame({'timestamp': stamps, 'close': np.full(10, 100.0)})
    setup = {
        'direction': 'LONG', 'level': 100.0, 'touches': 2, 'mirror': False,
        'entry': 100.5, 'stop_loss': 99.4, 'target': 103.0, 'rr': 2.3,
        'sl_distance': 1.1, 'volume_ratio': 2.0,
        'reclaim_index': 5, 'pierce_index': 4, 'pierce_extreme': 99.2,
        'points': [{'index': 999, 'price': 100.2, 'kind': 'low'},
                   {'index': 3, 'price': 99.9, 'kind': 'low'}],
        'first_index': 999,
    }
    signal = strategy_levels._to_bot_signal(setup, 'BTCUSDT', 10_000, df)
    assert signal['setup']['start_time'] is None
    assert [p['at'] for p in signal['setup']['touches_at']] \
        == ['2026-08-01T03:00:00Z']


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
