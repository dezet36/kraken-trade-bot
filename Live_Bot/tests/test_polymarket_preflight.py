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


class TestFailureReasonSurvives:
    """
    Причина отказа доходит до человека, а не глохнет.

    ЖАЛОБА: «установил, ввёл ключ, ничего не произошло — пишет, что кошелёк не
    подключён. Почему? Как это проверить?» Ответить было нечем: клиент
    возвращал «не поднялся» и молчал, а под этим молчанием пряталось четыре
    разных случая с четырьмя разными решениями — нет библиотеки, закрыта сеть,
    страна под ограничением, негоден ключ.
    """

    def test_reason_is_remembered(self):
        wallet._remember_failure(RuntimeError('что-то пошло не так'))
        assert wallet.last_failure().get('kind') == 'RuntimeError'

    def test_the_key_is_cut_out_of_the_reason(self, monkeypatch):
        """
        Сообщение библиотеки способно содержать ключ целиком — у некоторых
        версий он попадает туда как есть.
        """
        secret = '0x' + 'e' * 64
        monkeypatch.setenv('PM_PRIVATE_KEY', secret)
        wallet._remember_failure(RuntimeError(f'bad key {secret} rejected'))
        text = str(wallet.last_failure())
        assert secret not in text
        assert 'e' * 64 not in text

    def test_missing_library_is_named(self):
        wallet._remember_failure(ModuleNotFoundError('No module named x'))
        assert 'библиотек' in wallet.explain_failure()

    def test_closed_network_is_named(self):
        wallet._remember_failure(OSError('getaddrinfo failed'))
        assert 'сет' in wallet.explain_failure()

    def test_country_block_is_named(self):
        wallet._remember_failure(RuntimeError('403 Forbidden'))
        assert 'стран' in wallet.explain_failure()

    def test_no_failure_means_empty(self):
        wallet._last_failure.clear()
        assert wallet.explain_failure() == ''


class TestReachIsCheckedSeparately:
    """
    Связь проверяется ОТДЕЛЬНО от ключа и раньше него.

    Закрытая сеть и ограничение по стране выглядели ровно как негодный ключ.
    Человек проверял ключ там, где дело было в сети, — и проверял правильно.
    """

    def test_reach_comes_first(self):
        text = open(os.path.join(ROOT, 'polymarket', 'preflight.py'),
                    encoding='utf-8').read()
        spot = text.index("groups = [")
        assert text.index("'Связь'", spot) < text.index("'Кошелёк'", spot)

    def test_library_and_network_are_different_lines(self):
        rows = preflight.check_reach()
        assert rows, 'проверка связи ничего не сказала'
        assert any('библиотек' in r['name'] for r in rows)

    def test_panel_can_run_the_check(self):
        """На сервере консоли нет — проверка обязана быть в панели."""
        py = open(os.path.join(ROOT, 'dashboard.py'), encoding='utf-8').read()
        assert "'/api/polymarket/check'" in py
        html = open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()
        assert 'pm-wallet-check' in html


class TestJournalsDoNotAlarmWithoutReason:
    """
    Отсутствие журнала до первого запуска — не повод для тревоги.

    ЖАЛОБА: «осталась такая ошибка» — и снимок с шестью строками «внимание,
    журнал ещё не создан». Ошибки там не было ни одной: маркет-мейкер просто
    не запускали, и журналам неоткуда взяться. Но выглядело это списком
    поломок, и человек справедливо принял его за ошибку.

    Тревога уместна там, где что-то ДОЛЖНО было случиться и не случилось.
    """

    def _in(self, tmp, monkeypatch):
        from polymarket import store
        monkeypatch.setattr(store, 'DIR', str(tmp))
        monkeypatch.setattr(executor, 'ORDERS_LOG',
                            os.path.join(str(tmp), 'live_orders.jsonl'))

    def test_fresh_install_says_one_calm_line(self, tmp_path, monkeypatch):
        self._in(tmp_path, monkeypatch)
        rows = preflight.check_data()
        assert len(rows) == 1, 'шесть тревог на пустом месте — не отчёт'
        assert rows[0]['mark'] == preflight.OK
        assert 'не запускался' in rows[0]['name']

    def test_nothing_here_ever_blocks(self, tmp_path, monkeypatch):
        """Журналы не мешают торговать: их отсутствие ничего не решает."""
        self._in(tmp_path, monkeypatch)
        assert all(r['mark'] != preflight.BAD for r in preflight.check_data())
        (tmp_path / 'mm_state.json').write_text('{}', encoding='utf-8')
        assert all(r['mark'] != preflight.BAD for r in preflight.check_data())

    def test_events_that_have_not_happened_are_calm(self, tmp_path, monkeypatch):
        """
        Исполнений может не быть часами — на медленных рынках это норма, а не
        поломка. Тревожиться об этом каждую проверку незачем.
        """
        self._in(tmp_path, monkeypatch)
        (tmp_path / 'mm_state.json').write_text('{}', encoding='utf-8')
        rows = {r['name']: r for r in preflight.check_data()}
        for name in ('журнал «исполнения»', 'журнал «снос цены»',
                     'журнал «мнение модели»', 'журнал «живые заявки»'):
            assert rows[name]['mark'] == preflight.OK, name
            assert 'событий' in rows[name]['detail']

    def test_unwritable_folder_is_a_real_blocker(self, tmp_path, monkeypatch):
        """
        А вот это уже беда: бот отработает вхолостую и не оставит следов, по
        которым потом разбираться.
        """
        self._in(tmp_path, monkeypatch)
        (tmp_path / 'mm_state.json').write_text('{}', encoding='utf-8')
        monkeypatch.setattr(os, 'access', lambda p, m: False)
        assert any(r['mark'] == preflight.BAD for r in preflight.check_data())


class TestStartDoesNotFailSilently:
    """
    «Нажимаю Запустить, и ничего не происходит».

    ТРИ МОЛЧАЛИВЫХ ОТКАЗА СРАЗУ, и каждый выглядел как неработающая кнопка.

    ПЕРВЫЙ, И ОН РЕШАЮЩИЙ: на свежей установке папки данных ещё нет, и запись
    замка падала с «нет такого файла» — ПЕРВОЙ же строкой, до всякой торговли.
    Служба умирала раньше, чем успевала что-либо сказать.

    ВТОРОЙ: у кнопки не было перехвата ошибки. Запрос падал, обработчик
    обрывался, кнопка возвращалась в исходное — и на экране ровно ничего.

    ТРЕТИЙ: отказ «управление доступно только с этой машины» ничего не
    объясняет человеку, который сидит за той самой машиной по удалённому
    столу. Дело не в том, откуда он смотрит, а в том, на каком адресе слушает
    сервер.
    """

    def test_lock_creates_the_folder_it_needs(self, tmp_path, monkeypatch):
        from polymarket import mm
        missing = os.path.join(str(tmp_path), 'ещё-нет-такой')
        monkeypatch.setattr(mm.store, 'DIR', missing)
        assert mm._single_instance() is True
        assert os.path.isdir(missing), 'папка обязана создаться сама'

    def test_start_failure_is_reported_not_swallowed(self, monkeypatch):
        from polymarket import service
        monkeypatch.setattr(service, '_thread', None)
        monkeypatch.setattr(service.threading, 'Thread',
                            lambda **kw: (_ for _ in ()).throw(
                                RuntimeError('поток не поднялся')))
        from polymarket import mm
        monkeypatch.setattr(mm, '_single_instance', lambda: True)
        assert service.start(force=True) is False
        assert 'поток не поднялся' in (service.status().get('last_error') or '')

    def test_the_button_shows_what_went_wrong(self):
        html = open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()
        spot = html.index("closest('#pm-run')")
        block = html[spot:spot + 2000]
        assert 'catch (e)' in block, 'ошибка обязана перехватываться'
        assert 'не вышло' in block.lower()

    def test_refusal_names_the_setting_that_fixes_it(self):
        py = open(os.path.join(ROOT, 'dashboard.py'), encoding='utf-8').read()
        spot = py.index('_controls_allowed():', py.index('def do_POST'))
        block = py[spot:spot + 1200]
        assert 'DASHBOARD_ALLOW_CONTROL' in block
        assert 'DASHBOARD_HOST' in block
