"""
Торговые счета (accounts/live.py) — реорганизация, этап 7, шаг 1.

Счёт на бирже (Bybit, BingX: демо или реал) или проп «по инструкциям»
(HashHedge: API нет). Работают рядом с тестовыми счетами стратегий: у каждого
свой набор стратегий и свои правила денег. Проверяется:
  - поля счёта проверяются и зажимаются в пределы, чужие значения не проходят;
  - новый проп получает черновик правил, биржа — умолчания;
  - ИИ на торговый счёт не попадает (он торгуется только на тесте);
  - счёт биржи не включается без ключей;
  - ключи не возвращаются в панель и не попадают в историю настроек;
  - удаление счёта удаляет и его ключи;
  - адреса панели: чтение, запись, ключи — через проверку на бирже.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from accounts import live  # noqa: E402

SECRET = 'sEcReT-never-shown-0123456789'


def history_text():
    config = __import__('importlib').import_module('infra.config')
    from accounts import paper
    try:
        with open(os.path.join(config.DATA_DIR, paper.HISTORY_NAME), encoding='utf-8') as fh:
            return fh.read()
    except OSError:
        return ''


@pytest.fixture
def checked(monkeypatch):
    """Проверка ключей на бирже — подменена: сеть в тестах не трогаем."""
    exchange_keys = __import__('importlib').import_module('accounts.exchange_keys')
    calls = []

    def fake(exchange, mode, key, secret):
        calls.append((exchange, mode, key, secret))
        return (key != 'bad'), ('' if key != 'bad' else 'invalid api key')
    monkeypatch.setattr(exchange_keys, 'check_keys', fake)
    return calls


# ── Поля счёта ──────────────────────────────────────────────────────────────

class TestClean:
    def test_numbers_are_clamped_to_limits(self):
        acc = live.clean({'risk_pct': 50, 'max_slots': 3.7, 'daily_loss_pct': -4,
                          'max_drawdown_pct': 'много', 'deposit': float('nan')})
        assert acc['risk_pct'] == live.LIMITS['risk_pct'][1]
        assert acc['max_slots'] == 3 and isinstance(acc['max_slots'], int)
        assert acc['daily_loss_pct'] == 0.0
        assert acc['max_drawdown_pct'] == live.DEFAULTS['max_drawdown_pct']
        assert acc['deposit'] == live.DEFAULTS['deposit']

    def test_unknown_choices_do_not_pass(self):
        acc = live.clean({'kind': 'margin', 'exchange': 'binance', 'mode': 'paper', 'sides': 'up'})
        for field in ('kind', 'exchange', 'mode', 'sides'):
            assert acc[field] == live.DEFAULTS[field], field

    def test_strategies_only_from_the_live_cycle(self):
        """ИИ торгуется только на бумаге — на торговый счёт его не поставить."""
        acc = live.clean({'strategies': ['smc', 'SMC', 'LLM', 'НЕТ', 'FIBO']})
        assert acc['strategies'] == ['SMC', 'FIBO']
        assert 'LLM' not in live.allowed_strategies()

    def test_name_is_trimmed(self):
        assert live.clean({'name': '  ' + 'я' * 100})['name'] == 'я' * 60

    def test_code_from_name(self):
        assert live.slug('ХэшХедж 10k') == 'heshedj-10k'
        assert live.slug('Bybit демо') == 'bybit-demo'
        assert live.slug('!!!') == 'account'


# ── Реестр ──────────────────────────────────────────────────────────────────

class TestSave:
    def test_new_prop_gets_a_draft_of_the_rules(self):
        code, acc = live.save({'name': 'HashHedge 10k', 'kind': 'manual'})
        assert code == 'hashhedge-10k'
        assert acc['draft'] is True
        assert (acc['profit_target_pct'], acc['max_drawdown_pct'], acc['daily_loss_pct']) == (10.0, 10.0, 5.0)
        assert acc['risk_pct'] == 1.0 and acc['created_at']
        assert live.get(code) == acc

    def test_saved_rules_clear_the_draft(self):
        code, _ = live.save({'name': 'HashHedge', 'kind': 'manual'})
        _, acc = live.save({'id': code, 'profit_target_pct': 8, 'draft': False})
        assert acc['draft'] is False and acc['profit_target_pct'] == 8.0

    def test_new_exchange_account_has_no_prop_rules(self):
        _, acc = live.save({'name': 'Bybit демо', 'kind': 'exchange', 'exchange': 'bybit'})
        assert acc['draft'] is False
        assert acc['profit_target_pct'] == acc['max_drawdown_pct'] == acc['daily_loss_pct'] == 0.0
        assert acc['enabled'] is False and acc['mode'] == 'demo'

    def test_panel_form_starts_from_the_same_rules(self):
        """Форма нового счёта берёт начальные правила у сервера — те же, что save."""
        for kind, rules in live.kind_defaults().items():
            _, acc = live.save({'name': f'Счёт {kind}', 'kind': kind})
            same = {k: v for k, v in rules.items() if k != 'name'}
            assert {k: acc[k] for k in same} == same, kind

    def test_unknown_kind_is_the_default_kind_with_its_rules(self):
        _, acc = live.save({'name': 'Странный', 'kind': 'margin'})
        assert acc['kind'] == live.DEFAULTS['kind'] and acc['draft'] is True

    def test_names_give_unique_codes(self):
        first, _ = live.save({'name': 'Проп'})
        second, _ = live.save({'name': 'Проп'})
        assert first != second and second == f'{first}-2'

    def test_bad_requests_change_nothing(self):
        with pytest.raises(ValueError):
            live.save({'name': ''})
        with pytest.raises(ValueError):
            live.save({'id': 'нет-такого', 'risk_pct': 2})
        assert live.load(force=True) == {}

    def test_exchange_account_does_not_start_without_keys(self):
        code, _ = live.save({'name': 'BingX', 'kind': 'exchange', 'exchange': 'bingx'})
        with pytest.raises(ValueError):
            live.save({'id': code, 'enabled': True})
        assert live.get(code)['enabled'] is False

    def test_prop_starts_without_keys(self):
        code, _ = live.save({'name': 'HashHedge', 'kind': 'manual'})
        _, acc = live.save({'id': code, 'enabled': True, 'strategies': ['SMCS']})
        assert acc['enabled'] is True and acc['strategies'] == ['SMCS']

    def test_rules_go_to_settings_history(self):
        code, _ = live.save({'name': 'HashHedge', 'kind': 'manual'})
        live.save({'id': code, 'risk_pct': 0.5})
        text = history_text()
        assert f'Счёт {code}' in text

    def test_account_change_leaves_strategy_test_accounts_alone(self):
        """Торговые счета — рядом с тестом стратегий, а не вместо него."""
        from accounts import paper
        before = json.dumps(paper.load(force=True), sort_keys=True, default=str)
        live.save({'name': 'HashHedge', 'kind': 'manual', 'strategies': ['SMC'], 'risk_pct': 2})
        assert json.dumps(paper.load(force=True), sort_keys=True, default=str) == before


# ── Ключи ───────────────────────────────────────────────────────────────────

class TestKeys:
    def test_keys_are_checked_on_the_account_exchange_and_mode(self, checked):
        code, _ = live.save({'name': 'BingX real', 'kind': 'exchange', 'exchange': 'bingx', 'mode': 'live'})
        assert live.check_keys(code, 'k', SECRET) == (True, '')
        assert checked == [('bingx', 'LIVE', 'k', SECRET)]

    def test_prop_needs_no_keys(self, checked):
        code, _ = live.save({'name': 'HashHedge', 'kind': 'manual'})
        ok, error = live.check_keys(code, 'k', SECRET)
        assert not ok and error and checked == []

    def test_keys_never_leave_the_server(self):
        code, _ = live.save({'name': 'Bybit', 'kind': 'exchange'})
        live.set_keys(code, 'key-1', SECRET)
        assert live.has_keys(code) and live.keys(code) == ('key-1', SECRET)
        shown = json.dumps(live.listing(), ensure_ascii=False)
        assert SECRET not in shown and 'key-1' not in shown
        assert live.listing()[0]['has_keys'] is True
        live.save({'id': code, 'enabled': True})
        assert SECRET not in history_text() and 'key-1' not in history_text()
        with open(live.path(), encoding='utf-8') as fh:
            assert SECRET not in fh.read()

    @pytest.mark.skipif(os.name != 'posix', reason='права файла — на сервере (Linux)')
    def test_keys_file_is_owner_only(self):
        code, _ = live.save({'name': 'Bybit', 'kind': 'exchange'})
        live.set_keys(code, 'key-1', SECRET)
        assert os.stat(live.keys_path()).st_mode & 0o077 == 0

    def test_empty_keys_are_refused(self):
        code, _ = live.save({'name': 'Bybit', 'kind': 'exchange'})
        with pytest.raises(ValueError):
            live.set_keys(code, ' ', SECRET)
        assert not live.has_keys(code)

    def test_remove_drops_the_keys(self):
        code, _ = live.save({'name': 'Bybit', 'kind': 'exchange'})
        other, _ = live.save({'name': 'BingX', 'kind': 'exchange', 'exchange': 'bingx'})
        live.set_keys(code, 'key-1', SECRET)
        live.set_keys(other, 'key-2', SECRET)
        live.remove(code)
        assert live.get(code) is None and live.keys(code) is None
        assert live.keys(other) == ('key-2', SECRET)
        with pytest.raises(ValueError):
            live.remove(code)

    def test_removal_leaves_a_trace_in_history(self):
        code, _ = live.save({'name': 'HashHedge', 'kind': 'manual'})
        live.remove(code)
        last = json.loads(history_text().strip().splitlines()[-1])
        gone = {c['field']: c['to'] for c in last['changes']}
        assert gone[f'Счёт {code}.kind'] is None and gone[f'Счёт {code}.risk_pct'] is None


# ── Панель ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def server():
    dashboard = __import__('importlib').import_module('control.dashboard')
    port = 8937
    threading.Thread(
        target=lambda: dashboard.start_dashboard(port=port, host='127.0.0.1'),
        daemon=True).start()
    time.sleep(2.0)
    return f'http://127.0.0.1:{port}'


class TestPanel:
    @pytest.fixture(autouse=True)
    def _control(self, monkeypatch):
        monkeypatch.setenv('DASHBOARD_ALLOW_CONTROL', 'true')

    @staticmethod
    def _get(url):
        with urllib.request.urlopen(url, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))

    @staticmethod
    def _post(url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode('utf-8') or '{}')

    def test_list_shows_rules_and_choices(self, server):
        live.save({'name': 'HashHedge', 'kind': 'manual', 'strategies': ['SMC']})
        status, data = self._get(f'{server}/api/accounts')
        assert status == 200
        assert [a['id'] for a in data['accounts']] == ['hashhedge']
        assert data['exchanges'] == ['bybit', 'bingx'] and data['modes'] == ['demo', 'live']
        assert 'LLM' not in [s['code'] for s in data['strategies']]
        # проп по инструкциям (accounts/manual.py) и биржи (accounts/onexchange.py)
        assert data['engine'] == {'manual': True, 'exchange': True}
        assert data['live_enabled'] is False                # реальные деньги — после демо
        assert list(data['books']) == ['hashhedge']
        assert data['defaults'] == live.kind_defaults()

    def test_save_edit_delete(self, server):
        status, data = self._post(f'{server}/api/accounts/save',
                                  {'name': 'Bybit демо', 'kind': 'exchange', 'risk_pct': 1})
        assert status == 200 and data['id'] == 'bybit-demo'
        status, data = self._post(f'{server}/api/accounts/save', {'id': 'bybit-demo', 'enabled': True})
        assert status == 400 and 'ключ' in data['error']
        status, _ = self._post(f'{server}/api/accounts/delete', {'id': 'bybit-demo'})
        assert status == 200 and live.load(force=True) == {}

    def test_keys_are_checked_then_stored(self, server, checked):
        live.save({'name': 'Bybit', 'kind': 'exchange'})
        status, data = self._post(f'{server}/api/accounts/keys', {'id': 'bybit', 'key': 'bad', 'secret': SECRET})
        assert status == 409 and not live.has_keys('bybit')
        status, data = self._post(f'{server}/api/accounts/keys', {'id': 'bybit', 'key': 'good', 'secret': SECRET})
        assert status == 200 and live.keys('bybit') == ('good', SECRET)
        assert checked[-1] == ('bybit', 'DEMO', 'good', SECRET)
        _, listing = self._get(f'{server}/api/accounts')
        assert SECRET not in json.dumps(listing) and listing['accounts'][0]['has_keys'] is True

    def test_empty_keys_are_a_bad_request(self, server, checked):
        live.save({'name': 'Bybit', 'kind': 'exchange'})
        status, _ = self._post(f'{server}/api/accounts/keys', {'id': 'bybit', 'key': '', 'secret': ''})
        assert status == 400 and checked == []


def test_page_has_the_accounts_screen():
    html = open(os.path.join(ROOT, 'control', 'dashboard.html'), encoding='utf-8').read()
    assert "id: 'accounts'" in html and 'data-page="accounts"' in html
    assert "'/api/accounts/keys'" in html or '/api/accounts/keys' in html
    # прежняя запись ключей в общий .env убрана
    assert '/api/keys' not in html
