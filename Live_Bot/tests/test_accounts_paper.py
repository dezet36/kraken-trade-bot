"""
Тестовые счета стратегий (accounts/paper.py) — реорганизация, этап 2.

Стратегия отдаёт сетап без денег; счёт решает, берёт ли он его и на каких
деньгах (docs/Архитектура_модули_2026-10-08.md, договоры «Сетап» и «Решение
счёта»). Проверяется:
  - у каждой стратегии свой счёт: риск — стандарт теста (1%), стороны — обе,
    предел в одну сторону — тот, с которым стратегия измерена (поведение до
    этапа 2: стратегия клала его в сигнал сама);
  - решение счёта: отказ по стороне, деньги в сигнале, множитель риска сетапа;
  - правка одного счёта не задевает другие;
  - перенос денег из прежних настроек стратегий — с копией и историей;
  - панель (settings_store) видит и пишет счета, а runtime_settings.json
    денег больше не держит;
  - стратегии денег не получают и не кладут.
"""

import ast
import inspect
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from accounts import paper as account  # noqa: E402
from strategies import contract, registry  # noqa: E402


def a_signal(direction='LONG', **params):
    base = {'entry': 100.0, 'stop_loss': 98.0 if direction == 'LONG' else 102.0,
            'take_profit_1': 106.0 if direction == 'LONG' else 94.0, 'rr': 3.0}
    base.update(params)
    return {'trading_pair': 'BTCUSDT', 'setup': {'type': direction}, 'params': base}


def data_dir():
    config = __import__('importlib').import_module('infra.config')
    return config.DATA_DIR


# ── Счёт по умолчанию ───────────────────────────────────────────────────────

class TestDefaults:
    def test_every_strategy_has_an_account_at_the_test_standard(self):
        config = __import__('importlib').import_module('infra.config')
        for code in registry.codes():
            assert account.enabled(code)
            assert account.risk_pct(code) == float(config.RISK_PER_TRADE)
            assert account.sides(code) == 'both'

    def test_direction_cap_is_what_each_strategy_was_measured_with(self):
        """Те же числа, что стратегии клали в сигнал до этапа 2."""
        config = __import__('importlib').import_module('infra.config')
        from strategies.fib12 import params as fib12
        from strategies.levels import params as levels
        from strategies.rsibb import params as rsibb
        from strategies.smc import params as smc
        from strategies.smcs import params as smcs
        measured = {'FIBO': config.MAX_SAME_DIRECTION, 'SMC': smc.MAX_SAME_DIRECTION,
                    'LEVELS': levels.MAX_SAME_DIRECTION, 'RSIBB': rsibb.MAX_SAME_DIRECTION,
                    'SMCS': smcs.MAX_SAME_DIRECTION, 'FIB12': fib12.MAX_SAME_DIRECTION}
        for code, cap in measured.items():
            assert account.max_same_direction(code) == cap, code

    @pytest.mark.parametrize('mode', ['notebook', 'rules', 'plans'])
    def test_ai_cap_follows_its_mode(self, mode, monkeypatch):
        """ИИ: тетрадь — по числу мест, правила — своя копия правил SMC, планы
        модели — общее значение (до этапа 2 предела в их сигнале не было)."""
        config = __import__('importlib').import_module('infra.config')
        llm_notebook = __import__('importlib').import_module('strategies.llm.llm_notebook')
        llm_rules = __import__('importlib').import_module('strategies.llm.llm_rules')
        for module in (config, llm_rules.config, llm_notebook.config):
            monkeypatch.setattr(module, 'LLM_MODE', mode, raising=False)
        want = {'notebook': llm_notebook.SLOTS,
                'rules': llm_rules.DECISION.MAX_SAME_DIRECTION,
                'plans': config.MAX_SAME_DIRECTION}[mode]
        assert account.max_same_direction('LLM') == want


# ── Решение счёта ───────────────────────────────────────────────────────────

class TestDecision:
    def test_forbidden_side_is_refused_with_a_reason(self):
        account.save({'FIBO': {'sides': 'short'}})
        signal, why = account.decide('FIBO', a_signal('LONG'), 10_000)
        assert signal is None and 'short' in why
        signal, why = account.decide('FIBO', a_signal('SHORT'), 10_000)
        assert signal is not None and why is None

    def test_smc_naming_is_understood(self):
        account.save({'SMC': {'sides': 'short'}})
        assert account.decide('SMC', a_signal('BULLISH'), 10_000)[0] is None
        assert account.decide('SMC', a_signal('BEARISH'), 10_000)[0] is not None

    def test_money_is_written_by_the_account(self):
        signal, _ = account.decide('LEVELS', a_signal(), 10_000)
        p = signal['params']
        assert p['risk_pct'] == account.risk_pct('LEVELS')
        assert p['risk_amount'] == pytest.approx(10_000 * p['risk_pct'] / 100)
        assert p['position_size'] == pytest.approx(p['risk_amount'] / 2.0)
        assert p['max_same_direction'] == account.max_same_direction('LEVELS')
        assert set(contract.MONEY_KEYS) <= set(p)

    def test_the_same_signal_object_comes_back(self):
        """Сборка сигнала в bot.py отдаёт дальше тот же объект — журнал и
        график ссылаются на него."""
        signal = a_signal()
        assert account.decide('LEVELS', signal, 10_000)[0] is signal

    def test_no_setup_multiplies_the_risk(self):
        """Риск у всех один (решение владельца 08.10.2026): множитель в сигнале,
        если он там остался, счёт не принимает — SMC в тренде тоже 1%."""
        signal, _ = account.decide('SMC', a_signal(risk_scale=0.5), 10_000)
        assert signal['params']['risk_pct'] == account.risk_pct('SMC')
        assert signal['params']['risk_amount'] == pytest.approx(10_000 * account.risk_pct('SMC') / 100)

    def test_smc_signal_carries_no_multiplier(self):
        from strategies.smc import adapter as strategy_smc
        from test_strategy_smc_adapter import make_setup
        assert 'risk_scale' not in strategy_smc._to_bot_signal(make_setup(), 'BTCUSDT')['params']

    def test_owner_cap_overrides_the_measured_one(self):
        strategy_profile = __import__('importlib').import_module('strategies.strategy_profile')
        account.save({'SMCS': {'max_same_direction': 2}})
        assert account.decide('SMCS', a_signal(), 10_000)[0]['params']['max_same_direction'] == 2
        account.save({'SMCS': {'max_same_direction': None}})
        assert account.max_same_direction('SMCS') == strategy_profile.max_same_direction('SMCS')

    def test_money_left_in_an_old_plan_is_overwritten(self):
        """План, взведённый до этапа 2, может нести деньги: решает счёт."""
        signal, _ = account.decide('LLM', a_signal(risk_pct=7.0, max_same_direction=99), 10_000)
        assert signal['params']['risk_pct'] == account.risk_pct('LLM')
        assert signal['params']['max_same_direction'] == account.max_same_direction('LLM')

    def test_executors_read_the_account_numbers(self):
        from execution.exit_plan import direction_cap
        signal, _ = account.decide('SMC', a_signal(), 10_000)
        assert direction_cap(signal['params']) == account.max_same_direction('SMC')

    def test_absurd_risk_is_clamped(self):
        """50% на сделку — не настройка, а опечатка."""
        account.save({'FIBO': {'risk_pct': 50}})
        assert account.risk_pct('FIBO') == account.LIMITS['risk_pct'][1]


class TestIsolation:
    def test_one_account_change_does_not_touch_the_others(self):
        before = {code: account.describe(code) for code in registry.codes()}
        account.save({'FIB12': {'risk_pct': 0.5, 'sides': 'long', 'max_slots': 3,
                                'max_same_direction': 4, 'deposit': 5000}})
        for code in registry.codes():
            if code != 'FIB12':
                assert account.describe(code) == before[code], code
        assert account.describe('FIB12')['max_same_direction'] == 4


# ── Перенос из прежних настроек ─────────────────────────────────────────────

LEGACY = {
    'FIBO': {'enabled': True, 'risk_pct': 1.0, 'min_stop_pct': 0.8, 'deposit': 20000.0,
             'max_slots': 0, 'sides': 'short', 'critic': True, 'notify': False},
    'RSIBB': {'enabled': False, 'risk_pct': 1.0, 'min_stop_pct': 0.8, 'deposit': 4000.0,
              'max_slots': 2, 'sides': 'both', 'critic': True, 'notify': False},
    'LLM': {'enabled': True, 'risk_pct': 1.0, 'min_stop_pct': 0.8, 'deposit': 10000.0,
            'max_slots': 0, 'sides': 'both', 'critic': False, 'notify': True},
    'PORTFOLIO': {'portfolio_risk_pct': 6.0, 'portfolio_max_positions': 0, 'daily_loss_pct': 0.0},
    'EXCHANGE': {'name': 'bybit'},
    'NOTIFY': {'error_telegram': False},
}


def write_legacy(folder):
    with open(os.path.join(folder, 'runtime_settings.json'), 'w', encoding='utf-8') as fh:
        json.dump(LEGACY, fh)


class TestMigration:
    def test_money_moves_to_the_accounts_with_a_copy_and_a_history_line(self):
        folder = data_dir()
        write_legacy(folder)
        account.load(force=True)
        assert os.path.exists(os.path.join(folder, 'accounts.json'))
        assert account.sides('FIBO') == 'short'
        assert account.deposit('FIBO') == 20000.0
        assert account.enabled('RSIBB') is False
        assert account.max_slots('RSIBB') == 2
        assert account.portfolio_risk_pct() == 6.0
        with open(os.path.join(folder, 'runtime_settings.before_accounts.json'), encoding='utf-8') as fh:
            assert json.load(fh) == LEGACY
        with open(os.path.join(folder, 'settings_history.jsonl'), encoding='utf-8') as fh:
            rows = [json.loads(line) for line in fh]
        assert [c['field'] for c in rows[-1]['changes']] == ['Счета.перенос']

    def test_after_the_move_the_old_money_fields_are_not_read(self):
        folder = data_dir()
        write_legacy(folder)
        account.load(force=True)
        changed = json.loads(json.dumps(LEGACY))
        changed['FIBO']['sides'] = 'long'
        with open(os.path.join(folder, 'runtime_settings.json'), 'w', encoding='utf-8') as fh:
            json.dump(changed, fh)
        assert account.load(force=True)['FIBO']['sides'] == 'short'

    def test_nothing_to_move_means_no_file(self):
        account.load(force=True)
        assert not os.path.exists(os.path.join(data_dir(), 'accounts.json'))

    def test_broken_accounts_file_does_not_stop_trading(self):
        with open(os.path.join(data_dir(), 'accounts.json'), 'w', encoding='utf-8') as fh:
            fh.write('{ это не json')
        assert account.load(force=True)['SMC']['enabled'] is True
        assert account.risk_pct('SMC') > 0


# ── Окно для панели и Telegram ──────────────────────────────────────────────

class TestPanelWindow:
    def test_panel_sees_the_moved_money_and_the_strategy_knobs(self):
        settings_store = __import__('importlib').import_module('accounts.settings_store')
        write_legacy(data_dir())
        stored = settings_store.load(force=True)
        assert stored['FIBO']['sides'] == 'short' and stored['FIBO']['deposit'] == 20000.0
        assert stored['LLM']['critic'] is False
        assert stored['PORTFOLIO']['portfolio_risk_pct'] == 6.0
        assert settings_store.critic_enabled() is False

    def test_save_routes_money_to_the_account_and_knobs_to_the_strategy(self):
        settings_store = __import__('importlib').import_module('accounts.settings_store')
        from strategies import settings as knobs
        result = settings_store.save({'FIBO': {'risk_pct': 0.7, 'min_stop_pct': 1.2, 'notify': False}})
        assert result['FIBO']['risk_pct'] == 0.7 and account.risk_pct('FIBO') == 0.7
        assert knobs.min_stop_pct('FIBO') == pytest.approx(0.012)
        with open(settings_store.SETTINGS_FILE, encoding='utf-8') as fh:
            stored = json.load(fh)
        assert 'risk_pct' not in stored['FIBO'] and stored['FIBO']['min_stop_pct'] == 1.2
        assert stored['FIBO']['notify'] is False
        with open(account.path(), encoding='utf-8') as fh:
            money = json.load(fh)
        assert money['FIBO']['risk_pct'] == 0.7 and 'min_stop_pct' not in money['FIBO']

    def test_old_money_fields_leave_the_settings_file_on_the_next_save(self):
        settings_store = __import__('importlib').import_module('accounts.settings_store')
        write_legacy(data_dir())
        settings_store.save({'NOTIFY': {'daily_telegram': False}})
        with open(settings_store.SETTINGS_FILE, encoding='utf-8') as fh:
            stored = json.load(fh)
        for name in ('FIBO', 'RSIBB', 'LLM'):
            assert not set(stored[name]) & set(account.FIELDS), name
        assert 'PORTFOLIO' not in stored
        assert account.sides('FIBO') == 'short'           # деньги — на счёте

    def test_one_history_record_per_panel_save(self):
        settings_store = __import__('importlib').import_module('accounts.settings_store')
        settings_store.save({'FIBO': {'risk_pct': 0.7, 'min_stop_pct': 1.2}})
        rows = settings_store.history()
        assert len(rows) == 1
        assert {c['field'] for c in rows[0]['changes']} == {'FIBO.risk_pct', 'FIBO.min_stop_pct'}

    def test_telegram_toggle_reaches_the_account(self):
        settings_store = __import__('importlib').import_module('accounts.settings_store')
        settings_store.save({'SMCS': {'enabled': False}})
        assert account.enabled('SMCS') is False and settings_store.enabled('SMCS') is False


# ── Стратегии денег не знают ────────────────────────────────────────────────

@pytest.mark.parametrize('code', registry.codes())
def test_strategy_interface_carries_no_deposit(code):
    adapter = registry.adapter(code)
    for name in ('scan', 'build_signal'):
        assert 'balance' not in inspect.signature(getattr(adapter, name)).parameters, \
            f'{code}.{name}() получает депозит'


def _strategy_sources():
    """Код слоя «стратегии» (кроме договора, где деньги перечислены)."""
    sys.path.insert(0, os.path.join(ROOT, 'tests'))
    from test_module_layers import LAYER
    out = []
    for name, layer in LAYER.items():
        if layer != 'strategies':
            continue
        path = os.path.join(ROOT, name + '.py')
        if os.path.exists(path):
            out.append(path)
        folder = os.path.join(ROOT, name)
        if os.path.isdir(folder):
            out += [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith('.py')]
    folder = os.path.join(ROOT, 'strategies')
    out += [os.path.join(folder, f) for f in os.listdir(folder)
            if f.endswith('.py') and f != 'contract.py']
    return sorted(out)


MONEY_CONSTANTS = {'risk_pct', 'risk_amount', 'position_size'}
MONEY_NAMES = {'risk_pct', 'RISK_PCT', 'RISK_PER_TRADE', 'RISK_PER_TRADE_PCT', 'MAX_TOTAL_RISK_PCT',
               'BALANCE', 'deposit'}


def test_strategy_code_holds_no_money():
    """Ни ключей денег в сигнале, ни чтения риска и депозита — в коде стратегий."""
    found = []
    for path in _strategy_sources():
        tree = ast.parse(open(path, encoding='utf-8').read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in MONEY_CONSTANTS:
                found.append((os.path.relpath(path, ROOT), node.lineno, node.value))
            elif isinstance(node, ast.Attribute) and node.attr in MONEY_NAMES:
                found.append((os.path.relpath(path, ROOT), node.lineno, node.attr))
            elif isinstance(node, ast.Name) and node.id in MONEY_NAMES:
                found.append((os.path.relpath(path, ROOT), node.lineno, node.id))
    assert not found, f'деньги в коде стратегий: {found}'
