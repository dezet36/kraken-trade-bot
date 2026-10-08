"""
Эталон решений всех стратегий — страховка реорганизации (docs/Архитектура_модули_
2026-10-08.md, этап 0).

ЗАЧЕМ. tests/test_strategy_isolation_behaviour.py сравнивает отпечатки решений
ВНУТРИ одного запуска (правка параметров одной стратегии не сдвигает других).
Перенос кода по модулям — другое: нужно, чтобы НОВАЯ версия кода принимала те же
решения, что СТАРАЯ. Поэтому отпечатки записаны в файл (tests/golden/
decisions.json) и сравниваются с ним.

Намеренное изменение поведения стратегии — переписать эталон явно:
    GOLDEN_UPDATE=1 python -m pytest tests/test_golden_decisions.py
и объяснить в коммите, чьи решения и почему изменились.
"""

import hashlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_strategy_isolation_behaviour as iso  # noqa: E402

GOLDEN = os.path.join(HERE, 'golden', 'decisions.json')
REAL = os.path.join(HERE, 'data', 'golden_market_1h.json')


def markets():
    """Рынки эталона: синтетический (как в тесте изоляции) и два настоящих —
    ETH и SOL, 2500 часов Bybit 2026-03…07 (на синтетике уровни, Боллинджер и
    старая ФИБО почти не находят сетапов)."""
    import pandas as pd
    out = {'синтетика': iso.market()}
    with open(REAL, encoding='utf-8') as fh:
        real = json.load(fh)
    for pair, g in real.items():
        out[pair] = pd.DataFrame({
            'timestamp': pd.to_datetime(g['t'], unit='s', utc=True),
            'open': g['o'], 'high': g['h'], 'low': g['l'], 'close': g['c'], 'volume': g['v']})
    return out


def fibo_decisions(df):
    """Старая ФИБО: analyze_market на скользящем окне часа. Решение
    сканера по тренду 4ч и толпе — отдельно (не меняется при переносе файлов
    отдельно от этого), здесь — геометрия сетапа."""
    import strategy
    # Ручка «минимальный стоп» — настройка стратегии (strategies/settings.py,
    # с этапа 2; до него — settings_store). Денег стратегия не знает.
    knobs = strategy.settings
    orig = knobs.min_stop_pct
    knobs.min_stop_pct = lambda name: 0.008      # доля
    try:
        out = []
        for i in range(120, len(df), 5):
            window = df.iloc[i - 68:i].reset_index(drop=True)
            sig = strategy.analyze_market(window, None, 'TEST')
            if not sig:
                out.append(None)
                continue
            p = sig['params']
            out.append((sig['setup']['type'], round(p['entry'], 8), round(p['stop_loss'], 8),
                        round(p['take_profit_1'], 8), round(p['be_level'] or 0, 8)))
        return iso._fp(out)
    finally:
        knobs.min_stop_pct = orig


def levels_every_hour(df):
    """Уровни на КАЖДОМ часе: читатель теста изоляции идёт шагом 17 баров и на
    этих рынках не застаёт ни одного сетапа."""
    import numpy as np
    from levels import core
    high, low, close, volume = (df[c].to_numpy(dtype=float) for c in ('high', 'low', 'close', 'volume'))
    lv = core.build_levels(high, low)
    a = core.atr(high, low, close)
    out = []
    for i in range(150, len(df)):
        setup, reason = core.evaluate(high, low, close, volume, i, levels=lv, atr_values=a)
        if setup is not None:
            out.append((i, {k: (round(float(v), 8) if isinstance(v, (int, float, np.floating)) else str(v))
                            for k, v in setup.items()}))
    return iso._fp(out)


def rsibb_every_hour(df):
    import numpy as np
    from rsibb import core
    ind = core.indicators(*(df[c].to_numpy(dtype=float) for c in ('open', 'high', 'low', 'close')))
    out = []
    for i in range(150, len(df)):
        setup, _ = core.evaluate(ind, i)
        if setup is not None:
            trade = core.build_trade(setup)
            out.append((i, str(setup.get('direction')), None if trade is None else
                        {k: round(float(v), 8) for k, v in trade.items() if isinstance(v, (int, float, np.floating))}))
    return iso._fp(out)


def readers():
    out = dict(iso.READERS)
    out['FIBO'] = fibo_decisions
    out['LEVELS (каждый час)'] = levels_every_hour
    out['RSIBB (каждый час)'] = rsibb_every_hour
    return out


def fingerprints():
    got = {}
    for market, df in markets().items():
        for name, fn in sorted(readers().items()):
            got[f'{name} @ {market}'] = hashlib.sha256(fn(df).encode('utf-8')).hexdigest()
    return got


def test_decisions_match_golden():
    got = fingerprints()
    if os.getenv('GOLDEN_UPDATE') == '1' or not os.path.exists(GOLDEN):
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        with open(GOLDEN, 'w', encoding='utf-8') as fh:
            json.dump(got, fh, indent=1, sort_keys=True, ensure_ascii=False)
        pytest.skip('эталон решений записан заново')
    with open(GOLDEN, encoding='utf-8') as fh:
        want = json.load(fh)
    changed = sorted(n for n in set(got) | set(want) if got.get(n) != want.get(n))
    assert not changed, f'решения изменились у: {changed} (эталон tests/golden/decisions.json)'
