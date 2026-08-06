"""
Сборка сигнала из кандидата: каждая стратегия получает СВОЙ сетап.

ЗАЧЕМ ЭТОТ ФАЙЛ. Здесь была подмена, которую не видно ни по логам, ни по
дашборду. _build_signal разбирал случаи как «если SMC — так, ИНАЧЕ — фибо».
Стратегия уровней не подходила ни под одно условие, попадала в ветку фибо, её
готовый сигнал выбрасывался, и на тех же свечах заново искался импульс
Фибоначчи. Результат помечался именем LEVELS и уходил в журнал как обычная
сделка стратегии уровней.

Заметить это можно было только сверив цену входа с уровнем — чего никто не
делает, глядя на список сделок. Поэтому проверка живёт тестом.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _levels_candidate():
    """Кандидат сканера уровней: сигнал уже готов, свечей для фибо нет."""
    return {
        'pair': 'BTCUSDT',
        'signal': {
            'trading_pair': 'BTCUSDT',
            'setup': {'type': 'SHORT', 'start_price': 100.0, 'end_price': 95.0},
            'params': {'entry': 97.0, 'stop_loss': 101.0,
                       'take_profit_1': 90.0, 'rr': 1.75},
            'levels': {'level': 100.0, 'touches': 3, 'volume_ratio': 2.1,
                       'mirror': False},
            'zone': 'LEVEL', 'htf_trend': 'NEUTRAL', 'score': 21.0,
            'why': 'SHORT от уровня 100.0',
        },
        'score': 21.0, 'rr': 1.75, 'poi_type': 'LEVEL', 'df_1h': None,
    }


def test_levels_keeps_its_own_signal():
    import bot

    candidate = _levels_candidate()
    original = candidate['signal']
    signal, _df = bot._build_signal(candidate, 'LEVELS', 10_000)

    assert signal is not None, 'сигнал уровней потерян'
    # Тот же объект, а не пересобранный кем-то другим.
    assert signal is original
    assert signal['strategy'] == 'LEVELS'
    assert signal['params']['entry'] == 97.0
    assert signal['levels']['touches'] == 3


def test_levels_scan_context_reaches_journal():
    """Контекст «почему открылась» у уровней — свой, а не фибовский."""
    import bot

    signal, _df = bot._build_signal(_levels_candidate(), 'LEVELS', 10_000)
    scan = signal['scan']
    assert scan['touches'] == 3
    assert scan['volume_ratio'] == 2.1
    # Полей фибо-скана здесь быть не должно: они пришли бы из чужой ветки.
    assert 'size_pct' not in scan
    assert 'htf_strength' not in scan


def test_unknown_strategy_is_refused_not_given_to_fibo():
    """
    Неизвестная стратегия получает отказ, а не ветку фибо.

    Это и есть страховка на будущее: четвёртая стратегия, добавленная без
    ветки здесь, должна упереться в явный отказ, а не начать торговать
    сетапы Фибоначчи под своим именем.
    """
    import bot

    candidate = _levels_candidate()
    signal, df = bot._build_signal(candidate, 'BREAKOUT', 10_000)
    assert signal is None
    assert df is None


def test_levels_candidate_without_signal_is_refused():
    import bot

    candidate = _levels_candidate()
    candidate.pop('signal')
    signal, df = bot._build_signal(candidate, 'LEVELS', 10_000)
    assert signal is None
    assert df is None


def test_fibo_still_rebuilds_from_candles():
    """
    Фибо по-прежнему пересчитывает сигнал по свечам.

    У неё сканер отдаёт только кандидата, поэтому ветка обязана звать
    analyze_market — и обязана вернуть None, когда сетапа там нет.
    """
    import bot

    calls = []

    def fake_analyze(df_1h, df_5m, pair, balance):
        calls.append(pair)
        return None

    saved = bot.analyze_market
    bot.analyze_market = fake_analyze
    try:
        signal, df = bot._build_signal(
            {'pair': 'ETHUSDT', 'df_1h': object()}, 'FIBO', 10_000)
    finally:
        bot.analyze_market = saved

    assert calls == ['ETHUSDT']
    assert signal is None and df is None


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
