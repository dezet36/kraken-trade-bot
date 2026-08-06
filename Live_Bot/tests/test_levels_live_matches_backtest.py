"""
Уровни: замер и бой обязаны принимать одно и то же решение.

ПОЧЕМУ ЭТО НУЖНО ИМЕННО ЗДЕСЬ. Замер (research/levels_backtest.py) зовёт
core.evaluate напрямую и потому НИКОГДА не страдал от дефекта диспетчера:
подобранные параметры относились к настоящей стратегии. Ломался путь ПОСЛЕ
evaluate — сборка сигнала, диспетчер, контекст сделки. Из-за этого месяц
наблюдений измерял не то, что подбиралось, и заметить это по числам было
нельзя: сделки в журнале выглядели обычными.

Проверять поэтому надо не сам evaluate, а ЦЕПОЧКУ от него до готового
сигнала. Здесь она и проверяется: на одних и тех же свечах вызывается
боевой analyze_market и напрямую core.evaluate, а потом сверяются решение,
направление, вход, стоп и цель.

Расхождение здесь означает ровно то же, что и в SMC-версии этой проверки:
подобранные параметры относятся к стратегии, которой в бою нет.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


SUPPORT, RESISTANCE, BASE = 100.0, 112.0, 104.0


def market(bars=180, approach=0.4, volume_on_reclaim=500.0):
    """
    Ряд, СОБРАННЫЙ под условия стратегии, а не случайное блуждание.

    Первая версия этой проверки брала шум с дрейфом, и она проходила — но
    вхолостую: сетапов там не находилось вовсе, и «оба ничего не нашли»
    засчитывалось как совпадение. Поймал это сторожевой тест ниже, и он
    остаётся в файле именно для этого.

    Стратегии нужно совпадение шести условий сразу: уровень с касаниями,
    уровень впереди для цели, подход к уровню с нужной стороны и на нужном
    расстоянии, прокол, возврат закрытием и объём выше среднего. Собрать это
    случайно почти невозможно, поэтому собрано руками.

    Фон намеренно ровный: на шуме вокруг круглой цены строится десяток
    паразитных уровней, они оказываются ближе к цене и вытесняют настоящий.
    """
    close = np.full(bars, BASE)
    high = np.full(bars, BASE + 0.30)
    low = np.full(bars, BASE - 0.30)
    volume = np.full(bars, 100.0)

    # Касания: поддержка снизу, сопротивление сверху — оно же цель сделки.
    for i in (20, 50, 80):
        low[i] = SUPPORT
    for i in (35, 65, 95):
        high[i] = RESISTANCE

    pierce, reclaim = bars - 3, bars - 1
    # Подход к уровню сверху: ближе TRIGGER_ATR и дальше MIN_GAP_ATR.
    for k in range(bars - 12, pierce):
        close[k] = SUPPORT + approach
        high[k] = close[k] + 0.25
        low[k] = close[k] - 0.25
    # Прокол вниз, затем возврат закрытием выше уровня на объёме.
    low[pierce] = SUPPORT - 1.2
    close[pierce] = SUPPORT - 0.6
    high[pierce] = SUPPORT + 0.1
    close[bars - 2] = SUPPORT - 0.3
    high[bars - 2] = SUPPORT + 0.1
    low[bars - 2] = SUPPORT - 0.7
    close[reclaim] = SUPPORT + 0.8
    high[reclaim] = close[reclaim] + 0.3
    low[reclaim] = SUPPORT - 0.1
    volume[reclaim] = volume_on_reclaim

    return pd.DataFrame({
        'timestamp': pd.date_range('2026-01-01', periods=bars, freq='1h', tz='UTC'),
        'open': np.concatenate([[close[0]], close[:-1]]),
        'high': high, 'low': low, 'close': close, 'volume': volume,
    })


def _direct(df):
    """То, что мерит замер: core.evaluate на последней закрытой свече."""
    from levels import core

    high = df['high'].to_numpy(float)
    low = df['low'].to_numpy(float)
    close = df['close'].to_numpy(float)
    volume = df['volume'].to_numpy(float)
    levels = core.build_levels(high, low)
    atr_values = core.atr(high, low, close)
    setup, _reason = core.evaluate(high, low, close, volume, len(close) - 1,
                                   levels=levels, atr_values=atr_values)
    return setup


def _live(df, monkeypatch, tmp_path):
    """То, что торгует бот: боевой analyze_market на тех же свечах."""
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    import strategy_levels

    # Живой путь сам отбрасывает незакрытую свечу, поэтому подаём на одну
    # больше — иначе сравнивались бы решения на РАЗНЫХ последних барах, и
    # тест ловил бы собственную небрежность вместо расхождения кода.
    feed = df.copy()
    extra = feed.iloc[-1:].copy()
    extra['timestamp'] = feed['timestamp'].iloc[-1] + pd.Timedelta('1h')
    feed = pd.concat([feed, extra], ignore_index=True)

    monkeypatch.setattr(strategy_levels, 'fetch_ohlcv',
                        lambda *a, **k: feed)
    strategy_levels._cache.clear()
    return strategy_levels.analyze_market('TESTUSDT', 10_000)


@pytest.mark.parametrize('bars', [170, 180, 200, 240])
def test_same_decision_on_same_bar(bars, monkeypatch, tmp_path):
    df = market(bars=bars)
    setup = _direct(df)
    signal = _live(df, monkeypatch, tmp_path)

    assert (setup is None) == (signal is None), (
        f'решения разошлись на {bars} барах: '
        f'замер {"нашёл" if setup else "не нашёл"} сетап, '
        f'бой {"нашёл" if signal else "не нашёл"}')

    if setup is None:
        return

    assert signal['setup']['type'] == setup['direction']
    params = signal['params']
    assert params['entry'] == pytest.approx(setup['entry'])
    assert params['stop_loss'] == pytest.approx(setup['stop_loss'])
    assert params['take_profit_1'] == pytest.approx(setup['target'])
    assert params['rr'] == pytest.approx(setup['rr'])


def test_at_least_one_setup_found(monkeypatch, tmp_path):
    """
    Хотя бы один сетап на ряду обязан найтись.

    Без этой проверки предыдущая зелёная всегда: «оба ничего не нашли» —
    совпадение, и оно ничего не доказывает.
    """
    found = sum(1 for bars in (170, 180, 200, 240)
                if _direct(market(bars=bars)) is not None)
    assert found == 4, ('ряд перестал порождать сетапы — предыдущая проверка '
                        'стала зелёной вхолостую')


def test_live_signal_carries_full_contract(monkeypatch, tmp_path):
    """
    Найденный сетап доезжает до сигнала целиком.

    Именно здесь и рвалось: evaluate находил сетап, а дальше сигнал был
    неполон и падал на входе. Числа сходились, а сделки не было.
    """
    import strategy_levels

    df = market()
    assert _direct(df) is not None, 'сетапа нет — проверять нечего'
    signal = _live(df, monkeypatch, tmp_path)
    assert signal is not None, 'сетап найден, а сигнала нет — цепочка рвётся'

    for field in ('trading_pair', 'setup', 'params', 'trigger', 'levels', 'why'):
        assert field in signal, f'в сигнале нет {field}'
    assert signal['setup'].get('touches_at') is not None
    assert signal['setup'].get('start_time')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
