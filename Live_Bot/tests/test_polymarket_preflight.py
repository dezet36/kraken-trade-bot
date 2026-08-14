"""
Проверка перед живыми деньгами.

ЗАЧЕМ ОНА ОТДЕЛЬНО ОТ УСЛОВИЙ В executor. Там условия решают, отправлять ли
ОДНУ заявку, и отвечают в тот момент, когда деньги уже в игре. Здесь тот же
список читается заранее и целиком. Разница как между «двигатель не завёлся» и
«в баке нет бензина» — второе хочется знать до поворота ключа.

ГЛАВНОЕ СВОЙСТВО: проверка НИЧЕГО НЕ МЕНЯЕТ И НИЧЕГО НЕ ОТПРАВЛЯЕТ. Проверка,
способная сама создать заявку, опаснее её отсутствия.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polymarket import executor, params, preflight, wallet  # noqa: E402


class TestPreflightNeverActs:

    def test_it_does_not_place_or_cancel_anything(self):
        """
        Проверка не умеет отправлять — это видно по тексту, а не по поведению.

        Поведение можно случайно не покрыть; отсутствие вызова в исходнике
        покрыть нельзя никак иначе.
        """
        text = open(os.path.join(ROOT, 'polymarket', 'preflight.py'),
                    encoding='utf-8').read()
        for forbidden in ('executor.place(', 'executor.cancel(',
                          'executor.cancel_all(', 'engage_kill_switch('):
            assert forbidden not in text, forbidden

    def test_it_does_not_touch_the_kill_switch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(executor, 'KILL_FILE', str(tmp_path / 'STOP'))
        preflight.check_permission()
        assert not os.path.exists(str(tmp_path / 'STOP'))


class TestWalletIsReportedHonestly:

    def test_missing_key_is_the_first_and_only_word(self, monkeypatch):
        monkeypatch.setattr(wallet, 'status',
                            lambda: {'configured': False, 'address': None,
                                     'live_enabled': False,
                                     'can_trade_live': False})
        rows = preflight.check_wallet()
        assert rows[0]['mark'] == preflight.BAD
        assert len(rows) == 1, 'дальше проверять нечего'

    def test_the_key_itself_is_never_printed(self, monkeypatch):
        """
        В отчёт попадает адрес, но никогда ключ.

        Адрес — можно и нужно: по нему всё видно и ничего нельзя подписать.
        """
        secret = '0x' + 'a' * 64
        monkeypatch.setenv('PM_PRIVATE_KEY', secret)
        monkeypatch.setattr(wallet, 'status',
                            lambda: {'configured': True, 'address': '0xABC',
                                     'live_enabled': False,
                                     'can_trade_live': False})
        monkeypatch.setattr(wallet, 'client', lambda: None)
        text = str(preflight.check_wallet())
        assert secret not in text
        assert 'a' * 64 not in text

    def test_unknown_balance_is_a_warning_not_a_zero(self, monkeypatch):
        """
        Неизвестный остаток — не ноль. На нуле торговать нельзя осознанно,
        при неизвестном нельзя вообще: неизвестно, чем рискуем.
        """
        monkeypatch.setattr(wallet, 'status',
                            lambda: {'configured': True, 'address': '0xABC',
                                     'live_enabled': False,
                                     'can_trade_live': False})
        monkeypatch.setattr(wallet, 'client', lambda: object())
        monkeypatch.setattr(wallet, 'balance', lambda: None)
        marks = [r['mark'] for r in preflight.check_wallet()]
        assert preflight.WARN in marks
        assert preflight.BAD not in marks

    def test_empty_balance_blocks(self, monkeypatch):
        monkeypatch.setattr(wallet, 'status',
                            lambda: {'configured': True, 'address': '0xABC',
                                     'live_enabled': False,
                                     'can_trade_live': False})
        monkeypatch.setattr(wallet, 'client', lambda: object())
        monkeypatch.setattr(wallet, 'balance', lambda: 0.0)
        assert preflight.BAD in [r['mark'] for r in preflight.check_wallet()]


class TestLimitsFollowTheBudget:

    def test_order_cap_is_a_share_of_the_budget(self, monkeypatch):
        """
        Жёсткие $25 были БОЛЬШЕ всего счёта при бюджете в двадцать долларов:
        заявка, съедающая счёт целиком, проходила бы проверку, которая для
        того и написана.
        """
        monkeypatch.delenv('PM_MAX_ORDER_USD', raising=False)
        monkeypatch.setitem(params.BUDGET, 'MM', 20.0)
        monkeypatch.delenv('PM_BUDGET_MM', raising=False)
        assert executor.max_order_usd() < 20.0

    def test_explicit_setting_still_wins(self, monkeypatch):
        monkeypatch.setenv('PM_MAX_ORDER_USD', '3')
        assert executor.max_order_usd() == 3.0

    def test_zero_budget_is_named_as_a_blocker(self, monkeypatch):
        monkeypatch.setitem(params.BUDGET, 'MM', 0.0)
        monkeypatch.delenv('PM_BUDGET_MM', raising=False)
        assert preflight.BAD in [r['mark'] for r in preflight.check_budget()]


class TestVerdict:

    def test_a_single_blocker_stops_the_verdict(self, monkeypatch):
        monkeypatch.setattr(preflight, 'check_wallet',
                            lambda: [preflight._line(preflight.BAD, 'нет ключа')])
        monkeypatch.setattr(preflight, 'check_markets', lambda budget=None: [])
        _, bad = preflight.run()
        assert bad >= 1

    def test_warnings_alone_do_not_block(self, monkeypatch):
        monkeypatch.setattr(preflight, 'check_wallet',
                            lambda: [preflight._line(preflight.WARN, 'остаток неизвестен')])
        monkeypatch.setattr(preflight, 'check_markets', lambda budget=None: [])
        monkeypatch.setattr(preflight, 'check_budget',
                            lambda: [preflight._line(preflight.OK, 'бюджет $20')])
        _, bad = preflight.run()
        assert bad == 0


class TestBuildAndSelftestAgree:
    """
    Список для упаковщика и список самопроверки обязаны совпадать.

    ПОЙМАНО СБОРКОЙ, А НЕ ТЕСТОМ, И ЭТО СТОИЛО ВЫПУСКА. Модули добавлены в
    самопроверку, но не в скрытые импорты — PyInstaller их не положил, и .exe
    не собрался. Самопроверка отработала верно: она для того и написана.

    Но узнавать об этом от сборки на семь минут позже, чем от теста, незачем.
    Пакет polymarket импортируется только внутри функций и под try/except —
    так задумано, чтобы его отсутствие не роняло торговлю на бирже. Обратная
    сторона: упаковщик такие импорты находит ненадёжно, и каждый новый модуль
    надо называть дважды. Значит списки будут расходиться и впредь.
    """

    def _lists(self):
        import re
        root = os.path.dirname(ROOT)
        flow = os.path.join(root, '.github', 'workflows', 'build-exe.yml')
        if not os.path.exists(flow):
            return None, None
        packed = set(re.findall(r'--hidden-import (polymarket[.\w]*)',
                                open(flow, encoding='utf-8').read()))
        text = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
        start = text.find("'polymarket', 'polymarket.mm'")
        checked = set(re.findall(r"'(polymarket[.\w]*)'",
                                 text[start:text.find(')', start)]))
        return packed, checked

    def test_every_checked_module_is_also_packed(self):
        packed, checked = self._lists()
        if packed is None:
            return
        missing = sorted(checked - packed)
        assert not missing, f'самопроверка ждёт, упаковщик не кладёт: {missing}'

    def test_the_new_modules_are_in_both(self):
        packed, checked = self._lists()
        if packed is None:
            return
        for name in ('polymarket.preflight', 'polymarket.selector',
                     'polymarket.oneside', 'polymarket.oneside_run'):
            assert name in packed, f'{name} не попадёт в .exe'
