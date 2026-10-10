"""
Вердикт ИИ по сетапам других стратегий — в тени (strategies/llm/llm_shadow).

Что держится:
- вопрос собирается на момент сетапа: рынок — из снимка последнего закрытого
  часа, старый снимок не подставляется (иначе вердикт видел бы не тот рынок);
- вердикт — только журнал: свои сделки ИИ не оцениваются, сбой не бросается
  в цикл бота, очередь ограничена;
- модель спрашивается только во второй половине часа (тетрадь важнее);
- промт меняется только с экспортом docs/Промт_ИИ_тень.txt;
- модуль не импортирует пакеты других стратегий.
"""

import json
import os
import sys
import time

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from infra import config  # noqa: E402
from strategies.llm import llm_shadow  # noqa: E402

T = pd.Timestamp('2026-10-10 10:00', tz='UTC')
NOW = T.timestamp() + 3600 + 40 * 60            # 11:40 UTC: снимок часа 10:00–11:00 свежий


def signal(side='SHORT', entry=100.0, stop=103.0, target=94.0, price=99.0, why=None):
    sig = {'setup': {'type': side}, 'market_price': price,
           'params': {'entry': entry, 'stop_loss': stop, 'take_profit_1': target, 'tp_targets': [target]},
           'trigger': {'trigger_price': entry}}
    if why:
        sig['why'] = why
    return sig


def snapshot():
    btc = pd.DataFrame({'btc_ret_4h': [0.5], 'btc_ret_24h': [-1.2], 'btc_ret_7d': [3.0], 'btc_ret_30d': [6.1],
                        'cascade_count_3h': [0]}, index=[T])
    eth = pd.DataFrame({'ret_4h': [1.1], 'ret_24h': [-2.0], 'ret_7d': [4.0], 'ret_30d': [9.0], 'rel_24h': [-0.8],
                        'oi_chg_4h': [0.3], 'oi_chg_24h': [2.5], 'taker_24h': [0.47], 'funding_bp': [0.9],
                        'buy_ratio_pct_30d': [0.93], 'vol_z': [1.4]}, index=[T])
    return {'BTCUSDT': (None, btc), 'ETHUSDT': (None, eth)}


@pytest.fixture
def shadow(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(config, 'LLM_SERVER_URL', 'http://127.0.0.1:1', raising=False)
    monkeypatch.setattr(config, 'LLM_SHADOW', True, raising=False)
    monkeypatch.setattr(llm_shadow, '_snapshot', {'t': None, 'data': None, 'at': 0.0})
    monkeypatch.setattr(llm_shadow, '_jobs', llm_shadow.queue.Queue())
    # Поток вердиктов в тестах не поднимается: очередь проверяется сама.
    monkeypatch.setattr(llm_shadow, '_worker', {'thread': type('Alive', (), {'is_alive': lambda self: True})()})
    return tmp_path


def journal(tmp_path):
    path = tmp_path / llm_shadow.FILE
    return [json.loads(x) for x in open(path, encoding='utf-8')] if path.exists() else []


class TestQuestion:
    def test_setup_and_market_at_the_moment(self, shadow):
        llm_shadow.remember_market(T, snapshot())
        llm_shadow._snapshot['at'] = NOW - 600
        text, rec = llm_shadow.question('SMCS', 'ETHUSDT', signal(why='BOS 4ч вниз'), now=NOW, title='SMC-структура 4ч')
        assert 'Strategy: SMC-структура 4ч. Setup: SHORT ETH.' in text
        assert 'BOS 4ч вниз' in text
        assert 'stop +3.00% from entry' in text and 'target -6.00%' in text and '= 2.00 R' in text
        assert 'BTC: +0.5% in 4h, -1.2% in 24h' in text
        assert 'retail longs rank 0.93' in text and 'funding +0.90 bp' in text
        assert rec['market_hour'] == '2026-10-10 10:00' and rec['rr'] == pytest.approx(2.0)
        assert rec['direction'] == 'SHORT' and rec['entry'] == 100.0

    def test_stale_snapshot_is_not_used(self, shadow):
        """Снимок старше двух часов — не тот рынок: вопрос без строк рынка."""
        llm_shadow.remember_market(T, snapshot())
        llm_shadow._snapshot['at'] = NOW - 3 * 3600
        text, rec = llm_shadow.question('FIBO', 'ETHUSDT', signal(), now=NOW, title='Фибоначчи')
        assert llm_shadow.NO_MARKET in text and rec['market_hour'] is None

    def test_pair_outside_the_snapshot(self, shadow):
        llm_shadow.remember_market(T, snapshot())
        llm_shadow._snapshot['at'] = NOW
        text, _ = llm_shadow.question('FIBO', 'BICOUSDT', signal(), now=NOW, title='Фибоначчи')
        assert llm_shadow.NO_MARKET in text

    def test_price_is_never_faked(self, shadow):
        """Цены нет ни в сигнале, ни от цикла — так и сказано, а не «0% от цены»."""
        sig = signal()
        del sig['market_price']
        text, rec = llm_shadow.question('FIBO', 'ETHUSDT', sig, now=NOW, title='Фибоначчи')
        assert 'current price not given' in text and rec['price'] is None
        text, rec = llm_shadow.question('FIBO', 'ETHUSDT', sig, now=NOW, title='Фибоначчи', price=102.0)
        assert '-1.96% from the current price 102' in text


class TestParse:
    def test_verdicts(self):
        assert llm_shadow.parse('{"take": "yes", "conf": 4, "reason": "по тренду"}') == \
            {'take': True, 'conf': 4, 'reason': 'по тренду'}
        assert llm_shadow.parse('думаю...\n{"take": "no", "conf": 2, "reason": "толпа в шортах"}')['take'] is False

    def test_bad_answers(self):
        assert llm_shadow.parse('') is None
        assert llm_shadow.parse('{"take": "maybe", "conf": 3, "reason": "x"}') is None
        assert llm_shadow.parse('{"take": "yes", "conf": 9, "reason": "x"}')['conf'] is None


class TestQueue:
    def test_own_trades_and_switch_off_are_skipped(self, shadow, monkeypatch):
        assert llm_shadow.observe('LLM', 'ETHUSDT', signal()) is False
        monkeypatch.setattr(config, 'LLM_SHADOW', False)
        assert llm_shadow.observe('FIBO', 'ETHUSDT', signal()) is False
        assert llm_shadow._jobs.qsize() == 0

    def test_broken_signal_does_not_raise(self, shadow):
        assert llm_shadow.observe('FIBO', 'ETHUSDT', {'setup': {}}) is False

    def test_queue_is_bounded(self, shadow, monkeypatch):
        monkeypatch.setattr(llm_shadow, 'QUEUE_MAX', 3)
        for _ in range(5):
            assert llm_shadow.observe('FIBO', 'ETHUSDT', signal())
        assert llm_shadow._jobs.qsize() == 3
        assert [r['error'] for r in journal(shadow)] == ['очередь переполнена'] * 2

    def test_answer_is_journaled(self, shadow):
        llm_shadow.observe('SMCS', 'ETHUSDT', signal())
        job = llm_shadow._jobs.get_nowait()
        rec = llm_shadow.answer(job, ask=lambda q: ('{"take": "no", "conf": 3, "reason": "против BTC"}', 80.0))
        assert rec['take'] is False and rec['conf'] == 3 and rec['seconds'] == 80.0
        assert journal(shadow)[-1]['reason'] == 'против BTC'

    def test_model_failure_is_journaled(self, shadow):
        llm_shadow.observe('SMCS', 'ETHUSDT', signal())
        job = llm_shadow._jobs.get_nowait()

        def fail(q):
            raise RuntimeError('нет связи')
        rec = llm_shadow.answer(job, ask=fail)
        assert 'нет связи' in rec['error'] and 'take' not in rec

    def test_window_is_the_second_half_of_the_hour(self):
        """Тетрадь спрашивает модель сразу после закрытия часа, обзор — до 25 мин: вердикты позже."""
        assert not llm_shadow.in_window(T.timestamp() + 5 * 60)
        assert not llm_shadow.in_window(T.timestamp() + 25 * 60)
        assert llm_shadow.in_window(T.timestamp() + 40 * 60)
        assert not llm_shadow.in_window(T.timestamp() + 55 * 60)


class TestBotHook:
    def test_bot_hook_never_raises(self, monkeypatch):
        import bot
        monkeypatch.setattr(llm_shadow, 'observe', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('сбой')))
        bot._shadow_verdict('FIBO', 'ETHUSDT', signal(why='x'))       # не бросает

    def test_bot_calls_the_hook_after_an_order(self):
        """Вызов стоит сразу за broker.open в цикле бумаги — и только там."""
        src = open(os.path.join(HERE, 'bot.py'), encoding='utf-8').read()
        at = src.index('if signal and broker.open(strategy, signal):')
        assert "_shadow_verdict(strategy, candidate['pair'], signal, candidate)" in src[at:at + 300]


class TestRules:
    def test_docs_export_matches_the_prompt(self):
        """Правило проекта: промт меняется только с экспортом (docs/Промт_ИИ_тень.txt)."""
        if not os.path.isdir(os.path.dirname(llm_shadow.EXPORT)):
            pytest.skip('нет docs/')
        assert open(llm_shadow.EXPORT, encoding='utf-8').read() == llm_shadow.export_text(), \
            'промт вердикта изменён без экспорта: python -m strategies.llm.llm_shadow export'

    def test_no_other_strategy_is_imported(self):
        from test_strategy_isolation import modules_of, PACKAGES
        path = os.path.join(HERE, 'strategies', 'llm', 'llm_shadow.py')
        assert not modules_of(path) & set(PACKAGES)
        text = open(path, encoding='utf-8').read()
        for name in ('strategies.fibo', 'strategies.smcs', 'strategies.smc', 'strategies.levels'):
            assert f'from {name}' not in text and f'import {name}' not in text
