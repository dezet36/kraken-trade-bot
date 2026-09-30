"""
ИИ-трейдер по тетради (llm_notebook, LLM_MODE=notebook): признаки общего слоя,
сигналы закономерностей, решение модели, сигнал брокеру, срок позиции.

Тетрадь и признаки обязаны совпадать с историческим прогоном
(research/ai_pattern_lab.py, research/ai_model_trader_bt.py): иначе живая
модель торговала бы не то, что проверялось.
"""

import json
import os
import sys
import threading
from concurrent.futures import Future

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
RESEARCH = os.path.join(os.path.dirname(HERE), 'research')

import config  # noqa: E402
import flow_features  # noqa: E402
import llm_notebook  # noqa: E402

H = 3_600_000
T0 = pd.Timestamp('2026-06-01', tz='UTC')
MIN = 60_000
REAL_SUBMIT = llm_notebook._submit
REAL_MAYBE_REVIEW = llm_notebook._maybe_review


def raw_frame(n=500, seed=0, drop_at=None, drop=0.0):
    """
    Синтетические часы пары. drop_at — час, в который разгрузка ВПЕРВЫЕ проходит
    порог: за час до него цена и ОИ −3% (ниже порога 4.3%), в нём — ещё до drop.
    """
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    oi = 1e6 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    if drop_at is not None:
        rest = (1 - drop) / 0.97
        for arr in (c, oi):
            arr[drop_at - 1:] *= 0.97
            arr[drop_at:] *= rest
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range(T0, periods=n, freq='h')
    v = rng.uniform(900, 1100, n)
    return pd.DataFrame({'o': o, 'h': np.maximum(o, c) * 1.002, 'l': np.minimum(o, c) * 0.998, 'c': c,
                         'v': v, 'qv': v * c, 'trades': rng.integers(900, 1100, n),
                         'tb': v * rng.uniform(0.45, 0.55, n), 'tbq': v * c * 0.5,
                         'buy_ratio': rng.uniform(0.5, 0.8, n), 'oi': oi,
                         'funding': np.full(n, 0.0001)}, index=idx)


def market_with_cascade(n=500, t_idx=450, dumped=('AVAXUSDT', 'ADAUSDT', 'DOTUSDT', 'NEARUSDT', 'SOLUSDT')):
    frames = {}
    for k, p in enumerate(llm_notebook.UNIVERSE):
        frames[p] = raw_frame(n, seed=k, drop_at=t_idx if p in dumped else None, drop=0.07)
    return frames


class Gate:
    def __init__(self, held=()):
        self._held = list(held)

    def has_position_or_order(self, pair):
        return pair in self._held

    def check_cooldown(self, pair):
        return True

    def held(self):
        return list(self._held)


def run_now(fn, *args):
    """Поток тетради без потока: ответ готов сразу, и его забирает тот же цикл."""
    future = Future()
    try:
        future.set_result(fn(*args))
    except Exception as exc:                          # noqa: BLE001
        future.set_exception(exc)
    return future


@pytest.fixture
def notebook_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'LLM_MODE', 'notebook', raising=False)
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    import settings_store
    monkeypatch.setattr(settings_store, 'risk_pct', lambda name: 0.5)
    # Обзор рынка (поток к модели) и Telegram в тестах решений не нужны.
    monkeypatch.setattr(llm_notebook, '_maybe_review', lambda *a, **k: False)
    monkeypatch.setattr(llm_notebook, '_notify_decision', lambda *a, **k: None)
    monkeypatch.setattr(llm_notebook, '_submit', run_now)
    monkeypatch.setattr(llm_notebook, '_asked', [])
    monkeypatch.setattr(llm_notebook, '_health', {'fails': {}, 'last': {}, 'alerted': 0.0})
    import telegram_notify
    monkeypatch.setattr(telegram_notify, 'llm_notebook_health', lambda text: True)
    # Биржи в тестах нет: цена сейчас — 100, если тест не назвал свою.
    monkeypatch.setattr(llm_notebook, '_price_now', lambda pair, client=None: 100.0)


def hour_ms(idx):
    return int((T0 + pd.Timedelta(hours=idx)).timestamp() * 1000)


class TestParityWithResearch:
    def test_features_match_research(self):
        path = os.path.join(RESEARCH, 'ai_pattern_lab.py')
        if not os.path.exists(path):
            pytest.skip('нет research/')
        sys.path.insert(0, RESEARCH)
        import ai_pattern_lab
        btc, coin = raw_frame(900, seed=1), raw_frame(900, seed=2)
        ours = flow_features.features(coin, btc)
        theirs = ai_pattern_lab.features(coin, btc)[ours.columns]
        assert np.allclose(ours.to_numpy(float), theirs.to_numpy(float), equal_nan=True)

    def test_notebook_question_and_patterns_match_the_tested_ones(self):
        if not os.path.exists(os.path.join(RESEARCH, 'ai_model_trader_bt.py')):
            pytest.skip('нет research/')
        sys.path.insert(0, RESEARCH)
        import ai_model_trader_bt as bt
        import ai_question_v34 as v34
        assert llm_notebook.NOTEBOOK == bt.NOTEBOOK
        assert llm_notebook.QUESTION == bt.QUESTION
        assert {k: v for k, v in llm_notebook.PATTERNS.items() if k != 'P4'} == bt.PATTERNS
        assert llm_notebook.PATTERNS['P4'] == v34.P4
        assert llm_notebook.SLOTS == bt.SLOTS
        assert llm_notebook.EXTRA == bt.EXTRA

    def test_live_question_is_the_validated_v34(self):
        """Вживую — ровно тот вопрос и та тетрадь, что прошли проверку п. 77 (research/ai_question_v34.py)."""
        if not os.path.exists(os.path.join(RESEARCH, 'ai_question_v34.py')):
            pytest.skip('нет research/')
        sys.path.insert(0, RESEARCH)
        import ai_question_v34 as v34
        assert llm_notebook.QUESTION == v34.QUESTION_V34
        assert llm_notebook.NOTEBOOK == v34.NOTEBOOK_V34

    def test_question_shows_side_and_btc_30d(self):
        data = llm_notebook.market(market_with_cascade(n=900, t_idx=850))
        t = T0 + pd.Timedelta(hours=850)
        q = llm_notebook.brief(data, t, [('AVAXUSDT', 'P1')], [], 6)
        assert 'in 30d.' in q and 'P1 long  AVAX' in q and '"trade"' in q and 'at most 25 words' in q


class TestNotebookFlags:
    """«Лучше/хуже» тетради флагами (п. 71): те же, что в research/ai_notebook_flags.py, и в журнале решений."""

    def test_flags_match_research(self):
        if not os.path.exists(os.path.join(RESEARCH, 'ai_notebook_flags.py')):
            pytest.skip('нет research/')
        sys.path.insert(0, RESEARCH)
        import ai_notebook_flags as research
        assert llm_notebook.FLAGS == research.FLAGS
        rng = np.random.default_rng(3)
        for _ in range(200):
            f = pd.Series({'cascade_count_3h': rng.integers(0, 12), 'vol_z': rng.uniform(0, 4),
                           'btc_ret_24h': rng.normal(0, 3), 'ret_7d': rng.normal(0, 20), 'funding_bp': rng.normal(0.5, 2),
                           'btc_ret_7d': rng.normal(0, 5), 'buy_ratio_pct_30d': rng.uniform(), 'btc_ret_30d': rng.normal(0, 10)})
            for key in ('P1', 'P2', 'B3'):
                assert llm_notebook.flags_of(f, key)['score'] == research.score(f, key)

    def test_alerts_in_the_decision_log_carry_the_score(self, notebook_mode):
        frames = market_with_cascade()
        llm_notebook.scan(list(llm_notebook.POOL), Gate(), now_ms=hour_ms(451) + 60_000,
                          frames_of=lambda p: frames[p], ask=lambda q: '{"trade": [], "reason": "x"}',
                          price_of=lambda p: 100.0)
        alert = json.loads(open(os.path.join(config.DATA_DIR, 'llm_notebook_log.jsonl'), encoding='utf-8')
                           .readline())['alerts'][0]
        assert isinstance(alert['score'], int) and {'better', 'worse'} <= set(alert)
        assert any(x.startswith('cascade_count_3h') or x.startswith('vol_z') or x.startswith('abs_btc')
                   for x in alert['better'] + alert['worse']) or alert['score'] == 0

    def test_pattern_without_flags_gets_none(self):
        assert llm_notebook.flags_of(pd.Series({'btc_ret_24h': 0.0}), 'X') == {}


class TestAlerts:
    def test_wide_cascade_gives_p1_only_on_the_first_hour(self):
        frames = market_with_cascade()
        data = llm_notebook.market(frames)
        t = T0 + pd.Timedelta(hours=450)
        first = {p for p, k in llm_notebook.alerts_at(data, t) if k == 'P1'}
        assert first == {'AVAXUSDT', 'ADAUSDT', 'DOTUSDT', 'NEARUSDT', 'SOLUSDT'}
        later = {p for p, k in llm_notebook.alerts_at(data, t + pd.Timedelta(hours=1)) if k == 'P1'}
        assert not (later & first)            # серия продолжается — это не новый сигнал

    def test_single_coin_dump_is_not_a_cascade(self):
        frames = market_with_cascade(dumped=('AVAXUSDT',))
        data = llm_notebook.market(frames)
        t = T0 + pd.Timedelta(hours=450)
        assert not [p for p, k in llm_notebook.alerts_at(data, t) if k == 'P1']


class TestScan:
    def test_model_picks_become_broker_signals(self, notebook_mode):
        frames = market_with_cascade()
        asked = []

        def ask(question):
            asked.append(question)
            return '{"buy": ["AVAX", "ADA"], "reason": "broad cascade, high volume"}'

        close = frames['AVAXUSDT'].loc[T0 + pd.Timedelta(hours=450), 'c']
        now = {'AVAXUSDT': close * 1.012, 'ADAUSDT': 0.5}              # за минуты после закрытия цена ушла
        out = llm_notebook.scan(list(llm_notebook.POOL), Gate(held=['BTCUSDT']), now_ms=hour_ms(451) + 60_000,
                                frames_of=lambda p: frames[p], ask=ask, price_of=now.get)
        assert len(asked) == 1 and f'Free position slots: {llm_notebook.SLOTS - 1}' in asked[0] and 'Already holding: BTC' in asked[0]
        assert [c['pair'] for c in out] == ['AVAXUSDT', 'ADAUSDT']
        sig = out[0]['signal']
        p = sig['params']
        f = llm_notebook.market(frames)['AVAXUSDT'][1].loc[T0 + pd.Timedelta(hours=450)]
        price = now['AVAXUSDT']                        # вход — по цене биржи сейчас, не по закрытию часа
        assert sig['market_price'] == pytest.approx(price)
        assert p['entry'] > price                                   # лимит за рынком — вход сразу
        assert p['stop_loss'] == pytest.approx(price * (1 - 1.0 * f['atr_d'] / 100))
        assert p['max_hold_hours'] == 24 and p['tp_fractions'] == [1.0]
        assert p['be_level'] is None and p['breakeven_after_tp'] is False
        assert sig['llm']['mode'] == 'notebook' and sig['llm']['pattern'] == 'P1'

    def test_same_hour_is_decided_once(self, notebook_mode):
        frames = market_with_cascade()
        calls = []
        kw = dict(now_ms=hour_ms(451) + 60_000, frames_of=lambda p: frames[p],
                  ask=lambda q: calls.append(q) or '{"buy": [], "reason": "skip"}')
        llm_notebook.scan(list(llm_notebook.POOL), Gate(), **kw)
        llm_notebook.scan(list(llm_notebook.POOL), Gate(), **kw)
        assert len(calls) == 1

    def test_no_free_slots_no_question(self, notebook_mode):
        frames = market_with_cascade()
        held = ['BTCUSDT', 'ETHUSDT', 'XRPUSDT', 'BNBUSDT', 'LTCUSDT', 'LINKUSDT'][:llm_notebook.SLOTS]
        out = llm_notebook.scan(list(llm_notebook.POOL), Gate(held=held), now_ms=hour_ms(451) + 60_000,
                                frames_of=lambda p: frames[p],
                                ask=lambda q: (_ for _ in ()).throw(AssertionError('мест нет — модель не спрашиваем')))
        assert out == []

    def test_model_failure_skips_the_hour(self, notebook_mode):
        frames = market_with_cascade()

        def broken(question):
            raise RuntimeError('llama-server не отвечает')

        out = llm_notebook.scan(list(llm_notebook.POOL), Gate(), now_ms=hour_ms(451) + 60_000,
                                frames_of=lambda p: frames[p], ask=broken)
        assert out == []
        state = json.load(open(os.path.join(config.DATA_DIR, 'llm_notebook_state.json'), encoding='utf-8'))
        assert 'error' in state

    def test_own_universe_is_traded_not_the_bot_list(self, notebook_mode):
        frames = market_with_cascade()
        out = llm_notebook.scan(['ADAUSDT'], Gate(), now_ms=hour_ms(451) + 60_000,
                                frames_of=lambda p: frames.get(p),
                                ask=lambda q: '{"buy": ["AVAX", "ADA"], "reason": "x"}')
        assert [c['pair'] for c in out] == ['AVAXUSDT', 'ADAUSDT']

    def test_breadth_counts_only_core_pairs(self):
        frames = market_with_cascade(dumped=('AVAXUSDT', 'ADAUSDT', 'DOTUSDT'))
        for k, p in enumerate(llm_notebook.EXTRA[:3]):      # три новые пары тоже обваливаются
            frames[p] = raw_frame(500, seed=100 + k, drop_at=450, drop=0.07)
        data = llm_notebook.market(frames)
        t = T0 + pd.Timedelta(hours=450)
        assert data['BTCUSDT'][1].at[t, 'cascade_count_3h'] == 3        # новые пары широту не добавляют
        assert not [p for p, k in llm_notebook.alerts_at(data, t) if k == 'P1']

    def test_short_squeeze_p4_alert(self):
        """P4 (п. 77): фандинг ≤ −2 bp и доля розничных лонгов в нижних 3% месяца — сигнал на первом часе серии."""
        frames = market_with_cascade(n=900, t_idx=850, dumped=())
        df = frames['FILUSDT']
        df.iloc[848:, df.columns.get_loc('funding')] = -0.0003          # −3 bp: шорты платят лонгам
        df.iloc[848:, df.columns.get_loc('buy_ratio')] = 0.30           # толпа ушла в шорты — минимум месяца
        data = llm_notebook.market(frames)
        t = T0 + pd.Timedelta(hours=848)
        assert ('FILUSDT', 'P4') in llm_notebook.alerts_at(data, t)
        assert ('FILUSDT', 'P4') not in llm_notebook.alerts_at(data, t + pd.Timedelta(hours=1))   # серия идёт
        assert llm_notebook.title('P4') == 'Сжатие шортистов (лонг, 48 ч)'

    def test_switched_off_pattern_gives_no_alerts(self, monkeypatch):
        """P2 выключена владельцем 30.09 (п. 75): модель её сигналов не получает, текст тетради тот же."""
        assert llm_notebook.OFF == ('P2',)
        patterns = dict(llm_notebook.PATTERNS)
        patterns['P2'] = dict(patterns['P2'], conditions=[['ret_4h', '<', -4.2604], ['oi_chg_4h', '<', -4.7381]])
        monkeypatch.setattr(llm_notebook, 'PATTERNS', patterns)
        data = llm_notebook.market(market_with_cascade(dumped=('AVAXUSDT', 'ADAUSDT')))
        t = T0 + pd.Timedelta(hours=450)
        assert not [p for p, k in llm_notebook.alerts_at(data, t) if k == 'P2']
        monkeypatch.setattr(llm_notebook, 'OFF', ())
        assert {p for p, k in llm_notebook.alerts_at(data, t) if k == 'P2'} == {'AVAXUSDT', 'ADAUSDT'}

    def test_core_only_pattern_skips_extra_pairs(self, monkeypatch):
        patterns = {'X': {'label': 'ANY DUMP', 'side': 'long', 'hold': 24, 'stop': 1.0, 'rank': ('ret_4h', 1),
                          'universe': 'core', 'conditions': [['ret_4h', '<', -4.2604], ['oi_chg_4h', '<', -4.7381]]}}
        patterns['P1'] = llm_notebook.PATTERNS['P1']        # market() берёт условие разгрузки у P1
        monkeypatch.setattr(llm_notebook, 'PATTERNS', patterns)
        frames = market_with_cascade(dumped=('AVAXUSDT', 'ADAUSDT'))
        for k, p in enumerate(llm_notebook.EXTRA[:2]):
            frames[p] = raw_frame(500, seed=200 + k, drop_at=450, drop=0.07)
        data = llm_notebook.market(frames)
        got = {p for p, k in llm_notebook.alerts_at(data, T0 + pd.Timedelta(hours=450)) if k == 'X'}
        assert got == {'AVAXUSDT', 'ADAUSDT'}


class Later:
    """Поток тетради под рукой теста: вопрос ждёт, пока тест не «ответит» за модель."""

    def __init__(self):
        self.jobs = []

    def __call__(self, fn, *args):
        future = Future()
        self.jobs.append((future, fn, args))
        return future

    def run(self):
        for future, fn, args in self.jobs:
            try:
                future.set_result(fn(*args))
            except Exception as exc:                  # noqa: BLE001
                future.set_exception(exc)
        self.jobs.clear()


def log_rows():
    path = os.path.join(config.DATA_DIR, 'llm_notebook_log.jsonl')
    return [json.loads(x) for x in open(path, encoding='utf-8')] if os.path.exists(path) else []


class TestModelBesideTheCycle:
    """Цикл бота общий для всех стратегий: модель спрашивается в своём потоке, ответ забирает цикл."""

    def _scan(self, frames, minutes, gate=None, answer='{"buy": ["AVAX"], "reason": "x"}', calls=None,
              price_of=lambda p: 100.0):
        ask = (lambda q: calls.append(q) or answer) if calls is not None else (lambda q: answer)
        return llm_notebook.scan(list(llm_notebook.POOL), gate or Gate(), now_ms=hour_ms(451) + minutes * MIN,
                                 frames_of=lambda p: frames[p], ask=ask, price_of=price_of)

    def test_cycle_does_not_wait_for_the_model(self, notebook_mode, monkeypatch):
        frames, later, calls = market_with_cascade(), Later(), []
        monkeypatch.setattr(llm_notebook, '_submit', later)
        assert self._scan(frames, 4, calls=calls) == []                  # вопрос в очереди, цикл пошёл дальше
        assert len(later.jobs) == 1 and not calls and not log_rows()
        later.run()                                                       # модель ответила между циклами
        out = self._scan(frames, 9, calls=calls)
        assert [c['pair'] for c in out] == ['AVAXUSDT'] and len(calls) == 1
        row = log_rows()[0]
        assert row['delay_min'] == 9.0 and row['entries'] == {'AVAXUSDT': 100.0} and row['picks'] == ['AVAXUSDT']
        assert self._scan(frames, 14, calls=calls) == [] and len(calls) == 1     # час решён один раз

    def test_late_answer_is_not_traded(self, notebook_mode, monkeypatch):
        frames, later = market_with_cascade(), Later()
        monkeypatch.setattr(llm_notebook, '_submit', later)
        self._scan(frames, 4)
        later.run()
        assert self._scan(frames, 45) == []
        row = log_rows()[0]
        assert row['late'] is True and row['picks'] == ['AVAXUSDT'] and row['delay_min'] == 45.0

    def test_slots_and_pairs_are_rechecked_at_entry(self, notebook_mode, monkeypatch):
        frames, later = market_with_cascade(), Later()
        monkeypatch.setattr(llm_notebook, '_submit', later)
        self._scan(frames, 4, answer='{"buy": ["AVAX", "ADA", "DOT"], "reason": "x"}')
        later.run()
        # Пока модель думала, AVAX уже в позиции, а свободное место осталось одно.
        held = ['AVAXUSDT'] + ['BTCUSDT', 'ETHUSDT', 'XRPUSDT', 'BNBUSDT', 'LTCUSDT', 'LINKUSDT'][:llm_notebook.SLOTS - 2]
        out = self._scan(frames, 9, gate=Gate(held=held), answer='{"buy": ["AVAX", "ADA", "DOT"], "reason": "x"}')
        assert [c['pair'] for c in out] == ['ADAUSDT']

    def test_no_exchange_price_no_entry(self, notebook_mode):
        frames = market_with_cascade()
        out = self._scan(frames, 4, answer='{"buy": ["AVAX", "ADA"], "reason": "x"}',
                         price_of={'ADAUSDT': 0.5}.get)
        assert [c['pair'] for c in out] == ['ADAUSDT'] and log_rows()[0]['entries'] == {'ADAUSDT': 0.5}

    def test_trade_question_goes_before_the_review(self, notebook_mode, monkeypatch):
        frames = market_with_cascade(t_idx=451)            # сигналы часа, что закрылся в 20:00, — час обзора
        order = []
        monkeypatch.setattr(llm_notebook, '_submit', lambda fn, *a: order.append(fn.__name__) or Future())
        monkeypatch.setattr(llm_notebook, '_maybe_review', REAL_MAYBE_REVIEW)
        monkeypatch.setattr(llm_notebook, '_review', {'slot': None, 'busy': False})
        llm_notebook.scan(list(llm_notebook.POOL), Gate(), now_ms=hour_ms(452) + 4 * MIN,
                          frames_of=lambda p: frames[p], ask=lambda q: '{"buy": [], "reason": "x"}',
                          price_of=lambda p: 100.0)
        assert order == ['_decide_job', '_run_review']

    def test_real_thread_is_a_daemon_and_does_not_block(self, monkeypatch):
        import queue
        monkeypatch.setattr(llm_notebook, '_queue', queue.Queue())
        monkeypatch.setattr(llm_notebook, '_worker', {'thread': None})
        release = threading.Event()
        future = REAL_SUBMIT(lambda: release.wait(5) and 'ответ')
        assert not future.done()                           # цикл не ждёт модель
        release.set()
        assert future.result(timeout=5) == 'ответ'
        assert llm_notebook._worker['thread'].daemon       # остановка бота не ждёт модель
        failed = REAL_SUBMIT(lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):             # ошибка модели — тому, кто забирает ответ
            failed.result(timeout=5)


class TestExecutionProfile:
    def test_notebook_mode_profile(self, notebook_mode):
        import strategy_profile as sp
        e = llm_notebook.EXECUTION
        assert sp.expiry_hours('LLM') == e.PENDING_ORDER_MAX_HOURS
        assert sp.cooldown_hours('LLM') == e.COOLDOWN_HOURS
        assert sp.cost_limit_pct('LLM') == e.MAX_ENTRY_COST_SHARE_PCT
        assert sp.max_hold_hours('LLM') == e.MAX_POSITION_HOLD_HOURS
        assert sp.fills_through_market('LLM') is True

    def test_routing(self, notebook_mode, monkeypatch):
        import strategy_llm
        monkeypatch.setattr(llm_notebook, 'scan', lambda pairs, gate, client=None, balance=None: ['из тетради'])
        monkeypatch.setattr(strategy_llm.llm_local, 'available',
                            lambda: (_ for _ in ()).throw(AssertionError('режим планов не нужен')))
        assert strategy_llm.scan_for_setups(['BTCUSDT'], Gate()) == ['из тетради']


class TestPositionHold:
    """Брокер: срок позиции из сигнала; без него — срок стратегии, как раньше."""

    @pytest.fixture()
    def broker(self, tmp_path, monkeypatch):
        monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
        monkeypatch.setenv('TRADING_MODE', 'PAPER')
        monkeypatch.setenv('PAPER_FUNDING', 'false')
        for module in ('config', 'paper_broker', 'dashboard', 'shadow', 'setup_journal'):
            sys.modules.pop(module, None)
        import config as cfg
        import paper_broker
        for name, value in (('PAPER_FEE_MAKER', 0.0), ('PAPER_FEE_TAKER', 0.0), ('PAPER_SLIPPAGE_PCT', 0.0),
                            ('LIMIT_ENTRY_OFFSET_PCT', 0.0), ('MAX_POSITION_HOLD_HOURS', 0.0)):
            monkeypatch.setattr(cfg, name, value)

        class Client:
            candles = {}

            def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
                rows = [c for c in self.candles.get(symbol, []) if since is None or c[0] >= since]
                return rows[:limit] if limit else rows

            def fetch_funding_rate(self, symbol):
                raise RuntimeError('нет')

        client = Client()
        yield paper_broker.PaperBroker(client, strategies=('FIBO',)), client, paper_broker
        for module in ('config', 'paper_broker'):
            sys.modules.pop(module, None)

    @staticmethod
    def _signal(hold=None):
        params = {'entry': 100.0, 'stop_loss': 90.0, 'take_profit_1': 130.0, 'take_profit_2': 130.0,
                  'tp_targets': [130.0], 'tp_fractions': [1.0], 'be_level': None, 'breakeven_after_tp': False,
                  'max_same_direction': 0, 'rr': 3.0}
        if hold is not None:
            params['max_hold_hours'] = hold
        return {'trading_pair': 'BTCUSDT', 'strategy': 'FIBO', 'setup': {'type': 'LONG'},
                'trigger': {'zone': 'x'}, 'htf_trend': 'BULLISH', 'params': params, 'scan': {}}

    @staticmethod
    def _run(broker, client, pb):
        start, bar = 1_700_000_000_000, 5 * 60 * 1000
        pb._now_ms = lambda: start
        client.candles['BTCUSDT'] = [[start, 100, 100, 100, 100, 0]]
        pb._now_ms = lambda: start + 2 * bar
        broker.update()
        later = start + 2 * H
        client.candles['BTCUSDT'].append([later, 100, 101, 99, 100, 0])
        pb._now_ms = lambda: later + 2 * bar
        broker.update()

    def test_position_hold_from_signal(self, broker):
        b, client, pb = broker
        pb._now_ms = lambda: 1_700_000_000_000
        assert b.open('FIBO', self._signal(hold=1))
        self._run(b, client, pb)
        rows = pb.read_journal()
        assert rows and rows[0]['exit_reason'] == 'TIME'

    def test_without_the_field_strategy_hold_applies(self, broker):
        b, client, pb = broker
        pb._now_ms = lambda: 1_700_000_000_000
        assert b.open('FIBO', self._signal())
        self._run(b, client, pb)
        assert b.positions('FIBO') and not pb.read_journal()
        assert 'time_exit' not in b.snapshot()['open'][0]            # у обычной позиции карточка прежняя


class TestNotebookSignalThroughTheBroker:
    """
    Путь до конца: сигнал тетради -> bot._build_signal -> бумажный брокер.
    Вход сразу по цене биржи из сигнала (тейкер, проскальзывание против нас),
    стоп — тот, что посчитала тетрадь, без безубытка; выход по сроку закономерности.
    """

    @pytest.fixture()
    def broker(self, tmp_path, monkeypatch):
        monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
        monkeypatch.setenv('TRADING_MODE', 'PAPER')
        monkeypatch.setenv('PAPER_FUNDING', 'false')
        monkeypatch.setattr(llm_notebook.config, 'LLM_MODE', 'notebook', raising=False)
        for module in ('config', 'paper_broker', 'dashboard', 'shadow', 'setup_journal'):
            sys.modules.pop(module, None)
        import config as cfg
        import paper_broker
        for name, value in (('PAPER_FEE_MAKER', 0.0), ('PAPER_FEE_TAKER', 0.0), ('PAPER_SLIPPAGE_PCT', 0.0003),
                            ('MAX_POSITION_HOLD_HOURS', 0.0)):
            monkeypatch.setattr(cfg, name, value)
        import settings_store
        import telegram_notify
        monkeypatch.setattr(settings_store, 'load', lambda: {})           # стороны — обе, риск — общий
        monkeypatch.setattr(telegram_notify, '_send', lambda *a, **k: True)
        monkeypatch.setattr(telegram_notify, '_send_photo', lambda *a, **k: True)

        class Client:
            candles = {}

            def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
                rows = [c for c in self.candles.get(symbol, []) if since is None or c[0] >= since]
                return rows[:limit] if limit else rows

            def fetch_funding_rate(self, symbol):
                raise RuntimeError('нет')

        client = Client()
        yield paper_broker.PaperBroker(client, strategies=('LLM',)), client, paper_broker
        for module in ('config', 'paper_broker'):
            sys.modules.pop(module, None)

    @staticmethod
    def _signal(key, price):
        f = pd.Series({'atr_d': 5.0, 'ret_4h': -5.0, 'ret_24h': -6.0, 'ret_7d': -9.0, 'rel_24h': -2.0,
                       'oi_chg_4h': -6.0, 'oi_chg_24h': 5.0, 'vol_z': 2.0, 'taker_24h': 0.47, 'funding_bp': 0.5,
                       'buy_ratio_pct_30d': 0.6})
        import bot
        candidate = {'pair': 'AVAXUSDT', 'signal': llm_notebook.to_signal('AVAXUSDT', key, f, price, 'x'),
                     'score': 1.0, 'rr': None, 'df_1h': None}
        signal, _ = bot._build_signal(candidate, 'LLM', 10_000)
        assert signal is not None and signal['strategy'] == 'LLM'
        return signal

    START, BAR = 1_700_000_000_000, 5 * 60 * 1000

    def test_long_enters_at_market_and_stops_at_notebook_stop(self, broker):
        b, client, pb = broker
        pb._now_ms = lambda: self.START
        assert b.open('LLM', self._signal('P1', 20.0))
        pos = b.positions('LLM')['AVAXUSDT']
        assert pos['entry_price'] == pytest.approx(20.0 * 1.0003)          # по рынку, не по лимиту 20.02
        assert pos['stop_loss'] == pytest.approx(19.0) and pos['max_hold_hours'] == 24
        assert pos['breakeven_after_tp'] is False and not pos['be_level']
        # Панели — срок выхода, а не заглушка-цель: «цель +20R» читали как «позиция на месяцы».
        pb._now_ms = lambda: self.START + 6 * H
        view = b.snapshot()['open'][0]
        assert view['time_exit'] is True and view['hold_hours'] == 24 and view['time_left_h'] == 18.0
        assert view['progress_time'] == 0.25 and view['exit_by'] == pb._iso(self.START + 24 * H)
        pb._now_ms = lambda: self.START
        client.candles['AVAXUSDT'] = [[self.START + self.BAR, 20.0, 20.1, 18.9, 19.0, 0]]
        pb._now_ms = lambda: self.START + 3 * self.BAR
        b.update()
        rows = pb.read_journal()
        assert rows and rows[0]['exit_reason'] == 'SL'

    def test_short_leaves_by_the_pattern_clock(self, broker):
        b, client, pb = broker
        pb._now_ms = lambda: self.START
        assert b.open('LLM', self._signal('B3', 20.0))
        pos = b.positions('LLM')['AVAXUSDT']
        assert pos['direction'] == 'SHORT' and pos['entry_price'] == pytest.approx(20.0 * (1 - 0.0003))
        assert pos['stop_loss'] == pytest.approx(21.3) and pos['max_hold_hours'] == 48
        later = self.START + 49 * H
        client.candles['AVAXUSDT'] = [[self.START + self.BAR, 20.0, 20.2, 19.8, 19.9, 0],
                                      [later, 19.5, 19.6, 19.4, 19.5, 0]]
        pb._now_ms = lambda: later + 2 * self.BAR
        b.update()
        rows = pb.read_journal()
        assert rows and rows[0]['exit_reason'] == 'TIME'


class TestDecisionLogAndReview:
    def test_decision_is_logged_with_mechanical_choice(self, notebook_mode):
        frames = market_with_cascade()
        llm_notebook.scan(list(llm_notebook.POOL), Gate(held=['BTCUSDT', 'ETHUSDT']), now_ms=hour_ms(451) + 60_000,
                          frames_of=lambda p: frames[p], ask=lambda q: '{"buy": ["DOT"], "reason": "x"}')
        rows = [json.loads(x) for x in open(os.path.join(config.DATA_DIR, 'llm_notebook_log.jsonl'), encoding='utf-8')]
        assert len(rows) == 1
        r = rows[0]
        assert r['free'] == llm_notebook.SLOTS - 2 and r['picks'] == ['DOTUSDT']
        assert len(r['mechanical']) == min(5, llm_notebook.SLOTS - 2)
        assert {a['pattern'] for a in r['alerts']} == {'P1'} and all(a['atr_d'] > 0 for a in r['alerts'])

    def test_review_question_and_parse(self, notebook_mode):
        data = llm_notebook.market(market_with_cascade())
        t = T0 + pd.Timedelta(hours=450)
        q = llm_notebook.review_question(data, t)
        assert 'AVAX' in q and 'IN RUSSIAN' in q and '"regime"' in q
        assert 'PATTERN STATUS NOW' in q and '- P4 «Сжатие шортистов» (long):' in q
        assert 'Your open positions now: none' in q
        assert 'Your open positions now: LINK, XLM' in llm_notebook.review_question(data, t, ['LINKUSDT', 'XLMUSDT'])
        # У каждого числа подпись: голые столбцы модель путала (фандинг за долю покупок).
        row = next(x for x in q.splitlines() if x.startswith('AVAX:'))
        assert 'buy share 0.' in row and 'funding +' in row and 'retail rank ' in row and row.endswith('x')
        assert 'nan' not in q
        text = 'Рынок падает, толпа в лонгах.\nРиск — продолжение разгрузки.\n' \
               '{"regime": "falling", "btc_24h": "down", "watch": [{"coin": "SOL", "side": "long", "why": "x"}]}'
        body, view = llm_notebook.parse_review(text)
        assert body.startswith('Рынок падает') and view['regime'] == 'falling' and view['watch'][0]['coin'] == 'SOL'
        assert llm_notebook.parse_review('без json')[1] is None

    def test_review_runs_every_four_hours_once(self, monkeypatch):
        data = llm_notebook.market(market_with_cascade())
        monkeypatch.setattr(llm_notebook, '_review', {'slot': None, 'busy': False})
        ran = []
        run = lambda q, slot, held: ran.append(slot) or llm_notebook._review.update(busy=False)   # noqa: E731
        t = T0 + pd.Timedelta(hours=451)          # закрытие в 20:00 UTC
        assert llm_notebook._maybe_review(data, t, Gate(), run=run)
        assert not llm_notebook._maybe_review(data, t, Gate(), run=run)          # тот же час — один раз
        assert not llm_notebook._maybe_review(data, t + pd.Timedelta(hours=1), Gate(), run=run)   # 21:00 — не час обзора
        assert len(ran) == 1


def status_data(coins=None, market=None):
    """Один час признаков по всем парам: спокойный рынок, BTC за 30 дней +6.5%; coins — {пара: {признак: число}}."""
    base = {'ret_4h': 0.1, 'oi_chg_4h': 0.2, 'rel_24h': 0.0, 'oi_chg_24h': 0.5, 'vol_z': 1.0, 'funding_bp': 1.0,
            'buy_ratio_pct_30d': 0.5, 'btc_ret_4h': 0.1, 'btc_ret_24h': 0.3, 'btc_ret_7d': -2.0, 'btc_ret_30d': 6.5,
            'cascade_count_3h': 0.0}
    data = {p: (None, pd.DataFrame([{**base, **(market or {}), **(coins or {}).get(p, {})}], index=[T0]))
            for p in llm_notebook.UNIVERSE}
    return data, T0


def status_line(data, t, key):
    return next(x for x in llm_notebook.pattern_status(data, t).splitlines() if x.startswith(f'- {key} '))


class TestPatternStatus:
    """
    Обзор: что с закономерностями сейчас — считает код. Обзоры 29–30.09 обещали шорты «Продажи на
    отскоке» при BTC за 30 дней в плюсе: условия модель выводила из таблицы сама.
    """

    def test_market_wide_condition_blocks_the_pattern(self):
        data, t = status_data()
        b3 = status_line(data, t, 'B3')
        assert b3.startswith('- B3 «Продажа на отскоке» (short): CANNOT trigger this hour')
        assert 'BTC 30d +6.5% (needs < 0%)' in b3 and 'BTC 24h +0.3% (needs >= +1.0%)' in b3
        p1 = status_line(data, t, 'P1')
        assert 'CANNOT trigger this hour - coins in a liquidation cascade in the last 3h 0 (needs >= 4)' in p1

    def test_closest_coins_with_what_they_lack(self):
        data, t = status_data({'SOLUSDT': {'funding_bp': -0.9, 'buy_ratio_pct_30d': 0.14},
                               'SEIUSDT': {'funding_bp': 0.6, 'buy_ratio_pct_30d': 0.10}})
        assert status_line(data, t, 'P4') == (
            '- P4 «Сжатие шортистов» (long): no coin meets it now. Closest - '
            'SOL: funding -0.9bp (needs <= -2.0bp), retail rank 0.14 (needs <= 0.03); '
            'SEI: funding +0.6bp (needs <= -2.0bp), retail rank 0.10 (needs <= 0.03).')

    def test_coins_meeting_all_conditions_are_named(self):
        dump = {'ret_4h': -5.0, 'oi_chg_4h': -6.0}
        data, t = status_data({'AVAXUSDT': dump, 'ADAUSDT': dump, 'XRPUSDT': {'funding_bp': -2.5, 'buy_ratio_pct_30d': 0.02}},
                              market={'cascade_count_3h': 5})
        assert status_line(data, t, 'P1').endswith('all conditions hold now on ADA, AVAX.')
        assert status_line(data, t, 'P4').endswith('all conditions hold now on XRP.')

    def test_core_only_pattern_ignores_extra_coins_and_off_pattern_is_absent(self):
        data, t = status_data({'SEIUSDT': {'rel_24h': -5.0, 'oi_chg_24h': 6.0, 'vol_z': 2.0},
                               'LTCUSDT': {'rel_24h': -3.0, 'oi_chg_24h': 5.0, 'vol_z': 1.2}},
                              market={'btc_ret_30d': -3.0, 'btc_ret_24h': 1.5})
        b3 = status_line(data, t, 'B3')
        assert 'SEI' not in b3 and 'no coin meets it now. Closest - LTC: volume 1.2x (needs >= 1.5x)' in b3
        s = llm_notebook.pattern_status(data, t)
        assert 'P2' not in s and 'Накопление' not in s                    # выключена владельцем


class TestHealth:
    """Неработающая стратегия выглядела бы как «сигналов нет»: два сбоя подряд — владельцу сообщение."""

    @pytest.fixture()
    def sent(self, notebook_mode, monkeypatch):
        import telegram_notify
        box = []
        monkeypatch.setattr(telegram_notify, 'llm_notebook_health', lambda text: box.append(text) or True)
        return box

    def test_review_that_cannot_be_built_counts_as_a_failure(self, sent, monkeypatch):
        # Вопрос обзора не собрался (нет BTC в данных) — раньше только строка в журнале бота.
        monkeypatch.setattr(llm_notebook, '_review', {'slot': None, 'busy': False})
        data = llm_notebook.market(market_with_cascade())
        data.pop('BTCUSDT')
        run = lambda *a: pytest.fail('обзор без вопроса не запускается')             # noqa: E731
        t = T0 + pd.Timedelta(hours=451)                                             # закрытие в 20:00 UTC
        assert not REAL_MAYBE_REVIEW(data, t, Gate(), run=run)
        assert not sent                                                               # один сбой — ещё не повод
        assert not REAL_MAYBE_REVIEW(data, t + pd.Timedelta(hours=4), Gate(), run=run)
        assert len(sent) == 1 and 'вопрос не собран' in sent[0]

    def test_two_model_failures_in_a_row_tell_the_owner_once(self, sent):
        frames = market_with_cascade()
        broken = dict(frames_of=lambda p: frames[p], price_of=lambda p: 100.0,
                      ask=lambda q: (_ for _ in ()).throw(RuntimeError('llama-server не отвечает')))
        llm_notebook.scan(list(llm_notebook.POOL), Gate(), now_ms=hour_ms(451) + MIN, **broken)
        assert not sent                                                   # один сбой — ещё не повод
        frames2 = market_with_cascade(t_idx=452)
        broken['frames_of'] = lambda p: frames2[p]
        llm_notebook.scan(list(llm_notebook.POOL), Gate(), now_ms=hour_ms(453) + MIN, **broken)
        assert len(sent) == 1 and 'модель не дала решения' in sent[0] and 'llama-server' in sent[0]
        frames3 = market_with_cascade(t_idx=453)
        broken['frames_of'] = lambda p: frames3[p]
        llm_notebook.scan(list(llm_notebook.POOL), Gate(), now_ms=hour_ms(454) + MIN, **broken)
        assert len(sent) == 1                                             # не чаще раза в 6 ч

    def test_success_resets_the_count(self, sent):
        assert not llm_notebook._health_event('decision', False, 'x')
        assert not llm_notebook._health_event('decision', True)
        assert not llm_notebook._health_event('decision', False, 'y')     # снова первый — молчим
        assert not sent

    def test_the_same_hour_retried_by_the_next_cycle_counts_once(self, sent):
        frames = {p: None for p in llm_notebook.UNIVERSE}                 # биржи молчат
        for minutes in (1, 6, 11):                                        # три цикла одного часа
            llm_notebook.scan(list(llm_notebook.POOL), Gate(), now_ms=hour_ms(451) + minutes * MIN,
                              frames_of=frames.get, ask=lambda q: '', price_of=lambda p: 100.0)
        assert not sent
        llm_notebook.scan(list(llm_notebook.POOL), Gate(), now_ms=hour_ms(452) + MIN,
                          frames_of=frames.get, ask=lambda q: '', price_of=lambda p: 100.0)
        assert len(sent) == 1 and 'нет часовых данных' in sent[0]

    def test_review_failures(self, sent):
        llm_notebook._health_event('review', False, 'timeout')
        llm_notebook._health_event('review', False, 'timeout')
        assert len(sent) == 1 and 'обзор рынка не получен' in sent[0]


class TestRawDataForTheNotebookUniverse:
    """Стакан, лента, ликвидации пишутся и по парам тетради сверх пула бота — задним числом их не скачать."""

    def test_recorded_pairs_add_the_notebook_universe(self, monkeypatch):
        import bot
        monkeypatch.setattr(llm_notebook, 'enabled', lambda: True)
        got = bot._recorded_pairs(['BTCUSDT', 'BICOUSDT'])
        assert got[:2] == ['BTCUSDT', 'BICOUSDT']                   # пары бота — как были и первыми
        assert set(llm_notebook.UNIVERSE) <= set(got) and len(got) == len(set(got))

    def test_without_the_notebook_only_the_bot_pairs(self, monkeypatch):
        import bot
        monkeypatch.setattr(llm_notebook, 'enabled', lambda: False)
        assert bot._recorded_pairs(['BTCUSDT', 'ETHUSDT']) == ['BTCUSDT', 'ETHUSDT']


class TestHumanNames:
    """Человеку — имена закономерностей, не коды (28.09.2026: «что за P1?»)."""

    def test_codes_in_model_text_become_names(self):
        text = 'Это не P1, нет ликвидаций; P2 у SEI. Р1 кириллицей и В3 тоже; P3 и B1 — не наши.'
        out = llm_notebook.humanize(text)
        assert '«Отскок после ликвидаций»' in out and '«Накопление при слабом BTC»' in out
        assert out.count('«Отскок после ликвидаций»') == 2 and '«Продажа на отскоке»' in out
        assert 'P3' in out and 'B1' in out and 'SEI' in out

    def test_table_labels_in_russian_text_become_russian(self):
        # Обзор 20:00 28.09: «Агрессивные покупатели (buy share ~0.47)», «рост OI».
        out = llm_notebook.humanize('Агрессивные покупатели (buy share ~0.47), рост OI, высокий retail rank у ZEC; '
                                    'OIL и COIN не трогаем.')
        assert out == ('Агрессивные покупатели (доля агрессивных покупок ~0.47), рост ОИ, высокий ранг розничных '
                       'лонгов у ZEC; OIL и COIN не трогаем.')
        # Пробный обзор 30.09: «Открытый interest растёт», «funding не глубоко отрицательный».
        out = llm_notebook.humanize('Открытый interest растет; funding не глубоко отрицательный; Open Interest и '
                                    'FundingRate.')
        assert out == 'Открытый интерес растет; фандинг не глубоко отрицательный; открытый интерес и FundingRate.'

    def test_code_before_the_name_is_dropped_not_doubled(self):
        # Обзор 16:00 28.09: модель пишет и код, и имя — имя не должно идти дважды.
        text = ('P1 «Отскок после ликвидаций» не сработал. P2 «Накопление при слабом BTC» подходит для LINK. '
                'B3 — «Продажа на отскоке» не актуален; паттерн P1 ждём.')
        out = llm_notebook.humanize(text)
        assert out == ('«Отскок после ликвидаций» не сработал. «Накопление при слабом BTC» подходит для LINK. '
                       '«Продажа на отскоке» не актуален; паттерн «Отскок после ликвидаций» ждём.')

    def test_decision_message_groups_coins_by_pattern_name(self, monkeypatch):
        import telegram_notify
        got = []
        monkeypatch.setattr(telegram_notify, 'llm_notebook_decision', lambda a, p, r: got.append((a, p, r)))
        llm_notebook._notify_decision([('AVAXUSDT', 'P1'), ('ADAUSDT', 'P1'), ('ZECUSDT', 'B3')],
                                      ['AVAXUSDT'], 'P1 cascade is broad')
        alerts, picks, reason = got[0]
        assert alerts == ['Отскок после ликвидаций (лонг, 24 ч): AVAX, ADA', 'Продажа на отскоке (шорт, 48 ч): ZEC']
        assert picks == ['AVAX'] and reason == '«Отскок после ликвидаций» cascade is broad'

    def test_signal_why_and_zone_are_named(self, notebook_mode):
        f = pd.Series({'atr_d': 5.0, 'ret_4h': 0, 'ret_24h': 0, 'ret_7d': 0, 'rel_24h': 0, 'oi_chg_4h': 0,
                       'oi_chg_24h': 0, 'vol_z': 1, 'taker_24h': 0.5, 'funding_bp': 1, 'buy_ratio_pct_30d': 0.5})
        sig = llm_notebook.to_signal('AVAXUSDT', 'P1', f, 100.0, 'fits P1')
        assert sig['llm']['why'] == 'Отскок после ликвидаций (лонг, 24 ч): fits «Отскок после ликвидаций»'
        assert sig['trigger']['zone'] == 'Отскок после ликвидаций' and sig['llm']['pattern'] == 'P1'


class TestPromptExport:
    def test_docs_export_matches_the_prompt(self):
        """Правило проекта: промт меняется только с экспортом (docs/Промт_ИИ_тетрадь.txt)."""
        if not os.path.exists(llm_notebook.EXPORT):
            pytest.skip('нет docs/')
        assert open(llm_notebook.EXPORT, encoding='utf-8').read() == llm_notebook.export_text(), \
            'промт тетради изменён без экспорта: python Live_Bot/llm_notebook.py export'


class TestAnswerParsing:
    ITEMS = [('AVAXUSDT', 'P1'), ('ADAUSDT', 'P1'), ('ZECUSDT', 'B3')]

    def test_full_json_with_either_key(self):
        assert llm_notebook.picks_from('{"buy": ["AVAX"], "reason": "x"}', self.ITEMS) == (['AVAXUSDT'], 'x')
        assert llm_notebook.picks_from('{"trade": ["ZEC", "ADA"], "reason": "y"}', self.ITEMS) == \
            (['ZECUSDT', 'ADAUSDT'], 'y')
        assert llm_notebook.picks_from('{"trade": [], "reason": "skip"}', self.ITEMS) == ([], 'skip')

    def test_answer_cut_inside_reason_keeps_the_list(self):
        # Так модель упирается в n_predict: рассуждает в причине, закрывающей скобки нет (test v2, 8 из 261).
        text = '{\n"buy": [\n"AVAX",\n"ZEC"\n],\n"reason": "AVAX fits P1 with broad cascade. Let\'s re-read carefully'
        picks, reason = llm_notebook.picks_from(text, self.ITEMS)
        assert picks == ['AVAXUSDT', 'ZECUSDT'] and reason.startswith('AVAX fits P1')

    def test_list_cut_in_the_middle_is_not_trusted(self):
        assert llm_notebook.picks_from('{"trade": ["AVAX", "AD', self.ITEMS) == (None, '')
        assert llm_notebook.picks_from('no json at all', self.ITEMS) == (None, '')

    def test_unknown_coins_are_dropped(self):
        assert llm_notebook.picks_from('{"trade": ["SOL", "avaxusdt"], "reason": ""}', self.ITEMS)[0] == ['AVAXUSDT']


class TestShortPattern:
    def test_short_signal_is_mirrored(self, notebook_mode, monkeypatch):
        patterns = dict(llm_notebook.PATTERNS)
        patterns['S1'] = {'label': 'TEST SHORT', 'side': 'short', 'hold': 48, 'stop': 1.3, 'rank': ('rel_24h', 1),
                          'conditions': [['ret_4h', '<', 1e9]]}
        monkeypatch.setattr(llm_notebook, 'PATTERNS', patterns)
        f = pd.Series({'atr_d': 5.0, 'ret_4h': 0, 'ret_24h': 0, 'ret_7d': 0, 'rel_24h': 0, 'oi_chg_4h': 0,
                       'oi_chg_24h': 0, 'vol_z': 1, 'taker_24h': 0.5, 'funding_bp': 1, 'buy_ratio_pct_30d': 0.5})
        sig = llm_notebook.to_signal('SOLUSDT', 'S1', f, 100.0, 'x')
        p = sig['params']
        assert sig['setup']['type'] == 'SHORT' and sig['htf_trend'] == 'BEARISH'
        assert p['entry'] < 100.0 and p['stop_loss'] == pytest.approx(106.5) and p['tp_targets'][0] < 100.0
        assert p['max_hold_hours'] == 48 and p['sl_distance'] > 0
