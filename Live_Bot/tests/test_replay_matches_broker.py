"""
Прогон стендов (accounts/replay.py) = бумажный брокер — реорганизация, этап 9.

Сценарий эталона брокера (tests/test_golden_broker.py: 14 суток 5-минутных
свечей по трём парам, сигналы стратегий по расписанию) проходит через прогон
стендов, и его сделки сверяются со сделками эталона: те же входы и выходы,
те же причины, тот же итог в R. Стенд, построенный на replay, торгует как
бот — не похожим кодом, а тем же.

ИИ в сравнении нет: на торговый счёт он не ставится (торгуется только на
тесте). Пределы портфеля и термостат — общие у брокера на все стратегии, у
стенда их нет: сетапы, которые они не пустили у брокера, сравнению не
подлежат (в эталоне таких отказов не было, и проверка это держит).
"""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_golden_broker as golden  # noqa: E402

H = 3_600_000
FIELDS = ('pair', 'direction', 'entry_price', 'exit_price', 'exit_reason', 'tps_hit', 'pnl_r')


def setups():
    from accounts import books
    candles = {p: golden.series(base, seed) for p, (base, seed) in golden.PAIRS.items()}
    out = []
    for hour, strategy, pair, side in golden.schedule():
        if strategy == 'LLM':
            continue
        now = golden.T0 + hour * H + 30_000
        price = [c for c in candles[pair] if c[0] + golden.BAR <= now][-1][4]
        sig = golden.make_signal(strategy, pair, side, price)
        out.append((now, sig['strategy'], books.setup_copy(sig)))
    return out, candles


def norm(row, times):
    out = {k: (round(float(row[k]), 6) if isinstance(row.get(k), (int, float)) else row.get(k)) for k in FIELDS}
    out['opened'], out['closed'] = times
    return out


def test_golden_has_no_portfolio_refusals():
    """Сравнение честное, только пока брокер в эталоне не отказывал по
    пределам портфеля и термостату — их у стенда нет."""
    want = json.load(open(golden.GOLDEN, encoding='utf-8'))
    gates = {g for _, _, g in want['refused']}
    assert not gates & {'термостат', 'предел портфеля'}, gates


def test_replay_trades_equal_broker_trades(monkeypatch):
    monkeypatch.setenv('PAPER_FUNDING', 'false')
    config = __import__('importlib').import_module('infra.config')
    monkeypatch.setattr(config, 'PAPER_FUNDING', False)
    from accounts import replay
    plan, candles = setups()
    until = golden.T0 + (golden.N // 12 - 1) * H + 30_000
    result = replay.run(plan, candles, until=until)
    want = json.load(open(golden.GOLDEN, encoding='utf-8'))
    # Сверка не пустая: стопы, тейки, безубыток и три цели SMC.
    assert len(result['trades']) >= 20
    assert {'SL', 'TP1', 'BE', 'TP3'} <= {t['exit_reason'] for t in result['trades']}
    for strategy in sorted({s for _, s, _ in plan}):
        got = [norm(t, (t['opened_at'], t['closed_at'])) for t in result['trades'] if t['strategy'] == strategy]
        exp = [norm(t, (t['open_time'], t['close_time'])) for t in want['trades'] if t['strategy'] == strategy]
        assert got == exp, f'{strategy}: сделки стенда отличаются от брокера'


def test_replay_leaves_the_bot_data_alone(tmp_path):
    """Прогон работает в своём каталоге и возвращает боту его каталог и кэши."""
    config = __import__('importlib').import_module('infra.config')
    from accounts import books, replay
    before = config.DATA_DIR
    plan, candles = setups()
    replay.run(plan[:2], candles, until=plan[1][0] + 2 * H)
    assert config.DATA_DIR == before and books.state() == {}
