"""
Эталон бумажного брокера — страховка реорганизации (docs/Архитектура_модули_
2026-10-08.md, этап 0; этапы 2 и 5 переносят деньги в счета и логику заявок в
общее ядро исполнения).

Сценарий детерминирован: 14 суток 5-минутных свечей по трём парам (случайное
блуждание с фиксированным зерном и сменой режима), цикл «как у бота» раз в час;
в заданные часы ставятся сигналы ВСЕХ семи стратегий — лимит на откате, вход
по рынку (лимит за рынком), стоп-заявка уровней, три цели SMC, безубыток на B
ФИБО, срок удержания ИИ, снятие за B у FIB12; издержки бумаги — настоящие.
Итог каждой сделки, снятые заявки, отказы и балансы сравниваются с
tests/golden/broker_replay.json.

Намеренное изменение исполнения — переписать эталон явно:
    GOLDEN_UPDATE=1 python -m pytest tests/test_golden_broker.py
и объяснить в коммите, что и почему изменилось в сделках.
"""

import csv
import json
import os
import sys

import numpy as np
import pytest
from _modules import forget, remember  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

GOLDEN = os.path.join(HERE, 'golden', 'broker_replay.json')
BAR = 5 * 60 * 1000
T0 = 1_700_000_000_000 - 1_700_000_000_000 % (3600 * 1000)
N = 14 * 288
PAIRS = {'BTCUSDT': (60_000.0, 1), 'ETHUSDT': (3_000.0, 2), 'SOLUSDT': (150.0, 3)}
STRATEGIES = ('FIBO', 'SMC', 'LEVELS', 'RSIBB', 'LLM', 'SMCS', 'FIB12')


def series(base, seed):
    rng = np.random.default_rng(seed)
    drift = np.where((np.arange(N) // 576) % 2 == 0, 0.00008, -0.00008)
    close = base * np.exp(np.cumsum(drift + rng.normal(0, 0.0018, N)))
    open_ = np.r_[base, close[:-1]]
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0008, N)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0008, N)))
    vol = rng.uniform(10, 100, N)
    return [[T0 + i * BAR, float(open_[i]), float(high[i]), float(low[i]), float(close[i]), float(vol[i])]
            for i in range(N)]


class ReplayClient:
    """Отдаёт только свечи, закрытые к «сейчас» сценария, — как биржа."""

    def __init__(self, clock):
        self.clock = clock
        self.candles = {p: series(base, seed) for p, (base, seed) in PAIRS.items()}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        now = self.clock()
        rows = [c for c in self.candles.get(symbol, []) if c[0] + BAR <= now]
        if since is not None:
            rows = [c for c in rows if c[0] >= since]
        return rows[:limit] if limit else rows

    def fetch_funding_rate(self, symbol):
        raise RuntimeError('ставка недоступна')

    def price(self, pair):
        rows = self.fetch_ohlcv(pair, '5m')
        return rows[-1][4]


def make_signal(strategy, pair, side, p):
    """Сигнал в форме, которую отдаёт адаптер стратегии (без риска — он у счёта)."""
    d = 1 if side == 'LONG' else -1
    sig = {'trading_pair': pair, 'strategy': strategy, 'htf_trend': 'BULLISH' if d == 1 else 'BEARISH',
           'score': 1.0, 'why': f'эталон {strategy}', 'market_price': p}
    tight = strategy == 'FIBO-тесный'
    if tight:
        strategy = sig['strategy'] = 'FIBO'
    if strategy == 'FIBO':
        size = (0.01 if tight else 0.08) * p        # тесный — отказ по пределу издержек
        b = p * (1 + d * 0.003)
        entry, stop = b - d * 0.5 * size, b - d * 0.896 * size
        # Уровень снятия заявки объявляет сама ФИБО (88.6% отката, этап 5) —
        # прежде брокер считал его по имени стратегии той же формулой.
        config = __import__('importlib').import_module('infra.config')
        inval = b - size * config.ZONE_B_TOP if d == 1 else b + size * config.ZONE_B_TOP
        targets, fr, extra = [b + d * 0.25 * size], [1.0], {'be_level': b, 'breakeven_after_tp': True,
                                                            'pending_invalidation': inval}
        setup = {'type': side, 'start_price': b - d * size, 'end_price': b, 'size': size}
        trig = 'ZONE_LIMIT'
    elif strategy == 'SMC':
        entry = p * (1 - d * 0.01)
        stop = entry * (1 - d * 0.03)
        targets, fr, extra = [entry * (1 + d * k) for k in (0.03, 0.05, 0.08)], [0.25, 0.25, 0.5], {}
        setup, trig = {'type': side, 'start_price': stop, 'end_price': entry, 'size': abs(entry - stop)}, 'LIMIT'
    elif strategy == 'LEVELS':
        entry = p * (1 + d * 0.004)
        stop = p * (1 - d * 0.015)
        targets, fr, extra = [entry * (1 + d * 0.035)], [1.0], {}
        setup, trig = {'type': side, 'start_price': stop, 'end_price': entry, 'size': abs(entry - stop)}, 'MARKET'
    elif strategy == 'RSIBB':
        entry = p * (1 - d * 0.004)
        stop = entry * (1 - d * 0.025)
        targets, fr, extra = [entry * (1 + d * 0.012)], [1.0], {}
        setup, trig = {'type': side, 'start_price': stop, 'end_price': entry, 'size': abs(entry - stop)}, 'LIMIT'
    elif strategy == 'LLM':
        entry = p * (1 + d * 0.001)
        stop = p * (1 - d * 0.03)
        targets, fr, extra = [p * (1 + d * 0.6)], [1.0], {'max_hold_hours': 24}
        setup, trig = {'type': side, 'start_price': stop, 'end_price': entry, 'size': abs(entry - stop)}, 'LIMIT'
    elif strategy == 'SMCS':
        entry = p * (1 + d * 0.003)
        stop = p * (1 - d * 0.05)
        targets, fr, extra = [p + d * 8 * abs(p - stop)], [1.0], {'max_hold_hours': 720}
        setup, trig = {'type': side, 'start_price': stop, 'end_price': p, 'size': abs(p - stop)}, 'LIMIT'
    else:   # FIB12
        entry = p * (1 - d * 0.01)
        stop = p * (1 - d * 0.04)
        b = p * (1 + d * 0.01)
        targets, fr, extra = [p * (1 + d * 0.06)], [1.0], {'cancel_beyond': b, 'max_hold_hours': 720}
        setup, trig = {'type': side, 'start_price': stop, 'end_price': b, 'size': abs(b - stop)}, 'LIMIT'
    params = {'entry': entry, 'stop_loss': stop, 'take_profit_1': targets[0], 'take_profit_2': targets[-1],
              'tp_targets': targets, 'tp_fractions': fr, 'be_level': None, 'breakeven_after_tp': False,
              'max_same_direction': 0, 'rr': abs(targets[-1] - entry) / abs(entry - stop),
              'sl_distance': abs(entry - stop), 'invalidation': stop}
    params.update(extra)
    sig.update({'setup': setup, 'params': params, 'trigger': {'zone': 'эталон', 'entry_type': trig,
                                                              'trigger_price': entry}, 'zone': 'эталон'})
    return sig


def schedule():
    """(час, стратегия, пара, сторона) — по 6 сигналов на стратегию."""
    out = []
    pairs = list(PAIRS)
    for k, strategy in enumerate(STRATEGIES):
        for j in range(6):
            hour = 3 + k * 5 + j * 47
            out.append((hour, strategy, pairs[(k + j) % 3], 'LONG' if (k + j) % 2 == 0 else 'SHORT'))
    out.append((301, 'FIBO-тесный', 'ETHUSDT', 'LONG'))
    return sorted(out)


# Модули, которые эталон загружает заново под свой каталог данных и свои часы.
LOADED = ('infra.config', 'accounts.settings_store', 'execution.paper_broker', 'control.dashboard', 'execution.shadow', 'execution.setup_journal',
          'execution.refused', 'execution.follow_up', 'execution.trade_journal')


@pytest.fixture()
def own_modules():
    """
    После эталона его модули убираются из памяти. Иначе следующая проверка
    получала брокера с подменёнными часами (_now_ms) и файлом состояния
    эталона: «термостат» видел дневную просадку 85% от чужих депозитов и
    отказывал test_signal_contract (нашлось 08.10.2026, на этапе 5; в полном
    прогоне это маскировали проверки между ними, перезагружавшие брокер).
    """
    yield
    for module in LOADED:
        forget(module, None)


def run(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('TRADING_MODE', 'PAPER')
    monkeypatch.setenv('PAPER_FUNDING', 'false')
    for name in STRATEGIES:
        monkeypatch.setenv(f'PAPER_START_BALANCE_{name}', '10000')
    for module in LOADED:
        forget(module, None)
    pb = __import__('importlib').import_module('execution.paper_broker')
    settings_store = __import__('importlib').import_module('accounts.settings_store')
    settings_store.SETTINGS_FILE = str(tmp_path / 'runtime_settings.json')
    settings_store._cache = None
    settings_store._mtime = None
    state = {'now': T0}
    pb._now_ms = lambda: state['now']
    client = ReplayClient(lambda: state['now'])
    broker = pb.PaperBroker(client, strategies=STRATEGIES)
    plan = schedule()
    opened = []
    for hour in range(1, N // 12):
        state['now'] = T0 + hour * 3600 * 1000 + 30_000
        broker.update()
        for h, strategy, pair, side in plan:
            if h == hour:
                sig = make_signal(strategy, pair, side, client.price(pair))
                opened.append((hour, strategy, pair, side, bool(broker.open(sig['strategy'], sig))))
    return broker, opened


def r(x):
    try:
        return round(float(x), 6)
    except (TypeError, ValueError):
        return x


TRADE_FIELDS = ('strategy', 'pair', 'direction', 'open_time', 'close_time', 'entry_price', 'exit_price',
                'exit_reason', 'tps_hit', 'position_size', 'risk_usd', 'fees_usd', 'funding_usd', 'pnl_usd',
                'pnl_r', 'duration_min', 'breakeven_set', 'mfe_r', 'mae_r')


def outcome(tmp_path, broker, opened):
    trades = []
    path = tmp_path / 'paper_trades.jsonl'
    if path.exists():
        for line in open(path, encoding='utf-8'):
            row = json.loads(line)
            trades.append({k: r(row.get(k)) for k in TRADE_FIELDS})
    dropped = []
    path = tmp_path / 'orders_dropped.csv'
    if path.exists():
        dropped = [(row['strategy'], row['pair'], row['direction'], row['reason'])
                   for row in csv.DictReader(open(path, encoding='utf-8'))]
    refused = []
    path = tmp_path / 'refused.csv'
    if path.exists():
        refused = [(row['strategy'], row['pair'], row.get('gate', ''))
                   for row in csv.DictReader(open(path, encoding='utf-8'))]
    return {'opened': opened, 'trades': trades, 'dropped': dropped, 'refused': refused,
            'balances': {s: r(broker.balance(s)) for s in STRATEGIES},
            'open_positions': {s: sorted(broker.positions(s)) for s in STRATEGIES},
            'pending': {s: sorted(broker.pending(s)) for s in STRATEGIES}}


def test_broker_replay_matches_golden(tmp_path, monkeypatch, own_modules):
    broker, opened = run(tmp_path, monkeypatch)
    got = json.loads(json.dumps(outcome(tmp_path, broker, opened), ensure_ascii=False))
    if os.getenv('GOLDEN_UPDATE') == '1' or not os.path.exists(GOLDEN):
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        with open(GOLDEN, 'w', encoding='utf-8') as fh:
            json.dump(got, fh, indent=1, ensure_ascii=False)
        pytest.skip('эталон брокера записан заново')
    with open(GOLDEN, encoding='utf-8') as fh:
        want = json.load(fh)
    for key in ('opened', 'dropped', 'refused', 'balances', 'open_positions', 'pending'):
        assert got[key] == want[key], f'{key} отличается от эталона'
    assert len(got['trades']) == len(want['trades']), 'число сделок отличается от эталона'
    for g, w in zip(got['trades'], want['trades']):
        assert g == w, f'сделка отличается от эталона:\n было {w}\n стало {g}'
