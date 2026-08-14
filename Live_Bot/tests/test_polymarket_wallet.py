"""
Кошелёк и предохранители живого режима. Самая важная часть проекта.

ПОЧЕМУ ЭТИ ТЕСТЫ ВАЖНЕЕ ОСТАЛЬНЫХ. Ошибка в стратегии стоит части прибыли.
Ошибка здесь стоит всего счёта, причём необратимо: приватный ключ блокчейна —
это не пароль, который можно сменить, а прямое распоряжение средствами.

Проверяется три вещи, и каждая закрывает целый класс несчастий:

    ключ не утекает     ни в панель, ни в журналы, ни на диск;
    без разрешения нет  наличия ключа НЕДОСТАТОЧНО, нужен отдельный флаг;
    отказ объясняется   молчаливый отказ неотличим от поломки.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import executor, wallet  # noqa: E402

# Ключ из документации ethereum, публичный и заведомо ничей. Настоящий ключ в
# тестах не появляется никогда — даже «для проверки».
TEST_KEY = '0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318'


class TestKeyNeverLeaks:

    def test_status_never_contains_the_key(self, monkeypatch):
        """
        Состояние кошелька уходит в панель и журналы. Ключа там быть не может.

        Адрес — можно и нужно: по нему всё видно и ничего нельзя подписать.
        """
        monkeypatch.setenv('PM_PRIVATE_KEY', TEST_KEY)
        monkeypatch.setenv('PM_FUNDER', '0x' + '1' * 40)
        state = wallet.status()
        assert TEST_KEY not in str(state)
        assert state['address'] is not None
        assert state['address'] != TEST_KEY

    def test_address_is_derived_not_stored(self, monkeypatch):
        monkeypatch.setenv('PM_PRIVATE_KEY', TEST_KEY)
        # Адрес этого ключа известен и постоянен: он вычисляется, а не хранится.
        assert wallet.address() == '0x2c7536E3605D9C16a7a3D7b1898e529396a65c23'

    def test_key_is_read_only_from_environment(self):
        """
        Ключ берётся ТОЛЬКО из окружения.

        Панель слушает без пароля, и всё, что через неё прошло, оседает в файле
        настроек, который она же отдаёт по /api/settings. Ключ, побывавший там,
        придётся считать скомпрометированным. Это правило уже действует для
        ключей биржи в этом проекте.
        """
        import ast

        source = os.path.join(ROOT, 'polymarket', 'wallet.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        assert "os.getenv('PM_PRIVATE_KEY')" in text

        # Проверяем КОД, а не текст: слово «settings» встречается в пояснении,
        # почему настройки здесь не используются, и грубый поиск по строке
        # ловил бы собственный комментарий.
        tree = ast.parse(text)
        imported = {n.module for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) and n.module}
        imported |= {a.name for n in ast.walk(tree)
                     if isinstance(n, ast.Import) for a in n.names}
        assert not any('settings' in name for name in imported)
        calls = {n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert 'open' not in calls, 'модуль ключей не пишет и не читает файлы'

    def test_broken_key_gives_none_not_an_exception_with_the_key_inside(
            self, monkeypatch):
        """
        Разбор сломанного ключа не выбрасывает наружу текст с ключом.

        Сообщения криптографических библиотек охотно включают в себя исходные
        данные, а такое сообщение уедет в журнал ошибок.
        """
        monkeypatch.setenv('PM_PRIVATE_KEY', 'это не ключ')
        assert wallet.address() is None


class TestLiveNeedsTwoIndependentThings:

    def test_key_alone_is_not_enough(self, monkeypatch):
        """
        Ключ появляется в окружении задолго до готовности торговать.

        Например при настройке или проверке подключения. Если бы его хватало,
        первый же запуск начал бы выставлять заявки на реальные деньги.
        """
        monkeypatch.setenv('PM_PRIVATE_KEY', TEST_KEY)
        monkeypatch.setenv('PM_FUNDER', '0x' + '1' * 40)
        monkeypatch.delenv('PM_LIVE', raising=False)
        assert wallet.configured() is True
        assert wallet.live_enabled() is False
        assert wallet.status()['can_trade_live'] is False

    def test_flag_alone_is_not_enough(self, monkeypatch):
        monkeypatch.delenv('PM_PRIVATE_KEY', raising=False)
        monkeypatch.setenv('PM_LIVE', '1')
        allowed, why = executor.can_trade()
        assert allowed is False
        assert 'ключа' in why

    def test_both_together_pass_the_first_gates(self, monkeypatch):
        monkeypatch.setenv('PM_PRIVATE_KEY', TEST_KEY)
        monkeypatch.setenv('PM_FUNDER', '0x' + '1' * 40)
        monkeypatch.setenv('PM_LIVE', '1')
        allowed, why = executor.can_trade()
        # Дальше упрётся в поднятие клиента (сети в тесте нет) — и это
        # правильный отказ: без клиента подписать нечем.
        assert 'PM_LIVE' not in why and 'ключа' not in why


class TestKillSwitch:

    def test_file_stops_everything(self, monkeypatch, tmp_path):
        """
        Аварийная остановка — ФАЙЛ, а не переменная.

        Переменную надо менять там, где запущен процесс, и она подействует с
        перезапуска. Файл можно создать чем угодно и откуда угодно, и он
        подействует со следующей заявки. Когда что-то пошло не так, счёт идёт
        на секунды.
        """
        monkeypatch.setattr(executor, 'KILL_FILE', str(tmp_path / 'STOP'))
        monkeypatch.setenv('PM_PRIVATE_KEY', TEST_KEY)
        monkeypatch.setenv('PM_FUNDER', '0x' + '1' * 40)
        monkeypatch.setenv('PM_LIVE', '1')
        assert executor.kill_switch_on() is False
        executor.engage_kill_switch('проверка')
        assert executor.kill_switch_on() is True
        allowed, why = executor.can_trade()
        assert allowed is False and 'остановка' in why
        executor.release_kill_switch()
        assert executor.kill_switch_on() is False


class TestOrderGates:

    @pytest.fixture(autouse=True)
    def isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(executor, 'ORDERS_LOG', str(tmp_path / 'orders.jsonl'))
        monkeypatch.setattr(executor, 'KILL_FILE', str(tmp_path / 'STOP'))
        executor._recent.clear()

    def test_refusal_is_logged_with_the_reason(self, monkeypatch):
        """Отказ пишется в журнал: иначе тишину не отличить от поломки."""
        monkeypatch.delenv('PM_LIVE', raising=False)
        out = executor.place('T', 'bid', 0.05, 100)
        assert out['ok'] is False
        with open(executor.ORDERS_LOG, encoding='utf-8') as fh:
            body = fh.read()
        assert 'REFUSE' in body and 'PM_LIVE' in body

    def test_oversized_order_is_refused(self, monkeypatch):
        """
        Потолок размера проверяется В ИСПОЛНИТЕЛЕ, а не только в стратегии.

        Стратегия может ошибиться, её могут подменить, параметры могут
        разъехаться. Последняя проверка стоит там, где деньги уходят.
        """
        monkeypatch.setenv('PM_PRIVATE_KEY', TEST_KEY)
        monkeypatch.setenv('PM_FUNDER', '0x' + '1' * 40)
        monkeypatch.setenv('PM_LIVE', '1')
        # Потолок больше не отдельное число: он считается от бюджета, поэтому
        # задаётся явной настройкой, как это и делается на деле.
        monkeypatch.setenv('PM_MAX_ORDER_USD', '25')
        out = executor.place('T', 'bid', 0.90, 1000)      # $900
        assert out['ok'] is False and 'потолка' in out['why']

    def test_the_cap_shrinks_with_a_small_budget(self, monkeypatch):
        """
        Жёсткие $25 были БОЛЬШЕ всего счёта при бюджете в двадцать долларов:
        заявка, съедающая счёт целиком, проходила проверку, написанную ровно
        против этого. Теперь потолок — доля бюджета.
        """
        monkeypatch.setenv('PM_PRIVATE_KEY', TEST_KEY)
        monkeypatch.setenv('PM_FUNDER', '0x' + '1' * 40)
        monkeypatch.setenv('PM_LIVE', '1')
        monkeypatch.delenv('PM_MAX_ORDER_USD', raising=False)
        monkeypatch.setenv('PM_BUDGET_MM', '20')
        out = executor.place('T', 'bid', 0.90, 20)        # $18 при бюджете $20
        assert out['ok'] is False and 'потолка' in out['why']

    def test_price_outside_the_range_is_refused(self, monkeypatch):
        monkeypatch.setenv('PM_PRIVATE_KEY', TEST_KEY)
        monkeypatch.setenv('PM_FUNDER', '0x' + '1' * 40)
        monkeypatch.setenv('PM_LIVE', '1')
        assert executor.place('T', 'bid', 1.5, 10)['ok'] is False
        assert executor.place('T', 'bid', 0.0, 10)['ok'] is False

    def test_rate_limit_holds(self, monkeypatch):
        """
        Предел частоты защищает и от нашей ошибки, и от лимитов биржи.

        Проверяется ПОСЛЕДНИМ из условий, поэтому предыдущие приходится
        удовлетворить: иначе отказ придёт раньше и по другой причине, а тест
        будет зелёным, ничего не проверив.
        """
        import time

        monkeypatch.setenv('PM_PRIVATE_KEY', TEST_KEY)
        monkeypatch.setenv('PM_FUNDER', '0x' + '1' * 40)
        monkeypatch.setenv('PM_LIVE', '1')
        monkeypatch.setattr(executor, 'MAX_ORDERS_PER_MINUTE', 3)
        executor._recent.extend([time.time()] * 3)
        assert executor._rate_ok() is False
        executor._recent.clear()
        assert executor._rate_ok() is True

    def test_log_is_written_before_sending(self):
        """
        Запись идёт ДО отправки.

        Заявка, ушедшая на биржу и не попавшая в журнал из-за обрыва, — это
        позиция, о которой мы не знаем. Обратный порядок даёт запись без
        заявки, что безобидно.
        """
        source = os.path.join(ROOT, 'polymarket', 'executor.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        send_log = text.index("'action': 'SEND'")
        post = text.index('post_order')
        assert send_log < post


class TestShutdownCancelsOrders:

    def test_cycle_cancels_on_any_exit(self):
        """
        Заявки снимаются при любом выходе, включая аварийный.

        Оставленные без присмотра — худшее состояние: мы не котируем, но нас
        продолжают исполнять, причём тогда, когда это выгодно встречной
        стороне.
        """
        source = os.path.join(ROOT, 'polymarket', 'mm.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        assert 'finally:' in text
        assert 'executor.cancel_all()' in text
        assert text.index('finally:') < text.index('executor.cancel_all()')

    def test_only_limit_orders_are_used(self):
        """
        Только лимитные заявки и только GTC.

        Рыночная сделала бы нас тейкером — то есть заплатила бы комиссию, ради
        отсутствия которой всё затевалось: на цене 0.05 тейкер отдаёт 4.75%
        ставки, мейкер ноль.
        """
        source = os.path.join(ROOT, 'polymarket', 'executor.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        assert 'OrderType.GTC' in text
        assert 'create_market_order' not in text
        assert 'OrderType.FOK' not in text


class TestLiveFillsComeFromTheExchange:
    """
    В живом режиме источник исполнений — биржа, а не лента.

    В бумаге исполнение приходится ОЦЕНИВАТЬ по общей ленте и модели очереди:
    другого способа нет. Как только заявки уходят на биржу, такая оценка
    становится вредной — она отвечает «исполнилось бы», а биржа знает
    «исполнилось». Разойдутся они обязательно, потому что очередь оценивается
    приблизительно, и следом поедет позиция.
    """

    def _maker(self, tmp_path):
        from polymarket import engine
        return engine.PaperMaker(bankroll=1000,
                                 state_path=str(tmp_path / 's.json'))

    def test_exchange_trade_updates_position_and_cash(self, tmp_path):
        maker = self._maker(tmp_path)
        trades = [{'id': 'A1', 'token': 'T', 'side': 'bid', 'price': 0.20,
                   'size': 100, 'ts': 1, 'order_id': 'O1'}]
        done = maker.apply_exchange_trades(trades)
        assert len(done) == 1 and done[0]['source'] == 'exchange'
        assert maker._slot('T')['position'] == 100.0
        assert maker.state['cash'] == pytest.approx(1000 - 20.0)

    def test_the_same_trade_is_never_applied_twice(self, tmp_path):
        """
        Тот же ответ придёт и в следующем цикле.

        Провести его дважды значит удвоить позицию, которой на бирже нет, — и
        дальше котировать против выдуманного запаса.
        """
        maker = self._maker(tmp_path)
        trades = [{'id': 'A1', 'token': 'T', 'side': 'bid', 'price': 0.20,
                   'size': 100, 'ts': 1, 'order_id': 'O1'}]
        maker.apply_exchange_trades(trades)
        assert maker.apply_exchange_trades(trades) == []
        assert maker._slot('T')['position'] == 100.0

    def test_live_accounting_comes_only_from_the_exchange(self):
        """Позиции и деньги в живом режиме ведёт биржа, а не модель."""
        source = os.path.join(ROOT, 'polymarket', 'mm.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        assert 'maker.apply_exchange_trades(got)' in text
        assert 'maker.predict_fills(' in text, 'модель идёт рядом'

    def test_model_runs_beside_reality_without_touching_it(self):
        """
        В живом режиме модель отвечает на свой вопрос, ничего не меняя.

        ЗАГОЛОВОК МОДУЛЯ ОБЕЩАЛ ЭТО С САМОГО НАЧАЛА, а код обещание не
        выполнял: ветка с лентой просто пропускалась, и сравнивать оказывалось
        нечего. Первый живой прогон не сказал бы о точности модели ровно
        ничего — при том что ради этого сравнения бумажный расчёт и
        задумывался.

        Расхождение модели с биржей — самое ценное, что даст живой режим: оно
        скажет, насколько можно верить бумаге, когда придёт время увеличивать
        размер.
        """
        from polymarket import engine
        maker = engine.PaperMaker(bankroll=100, state_path=os.devnull + '.json')
        slot = maker._slot('T')
        slot['orders'] = {'bid': {'price': 0.20, 'size': 5, 'ts': 0,
                                  'queue': 0.0}}
        before = dict(slot)
        tape = [{'ts': 10, 'price': 0.19, 'size': 50, 'side': 'SELL',
                 'asset': 'T'}]
        guess = maker.predict_fills('T', tape)
        assert guess['bid']['model_filled'] is True, 'модель видит исполнение'
        assert maker._slot('T')['position'] == 0.0, 'но позицию не трогает'
        assert maker._slot('T')['orders'] == before['orders'], 'и заявку тоже'

    def test_prediction_is_empty_without_orders(self):
        from polymarket import engine
        maker = engine.PaperMaker(bankroll=100, state_path=os.devnull + '.json')
        assert maker.predict_fills('T', [{'ts': 1, 'price': 0.2, 'size': 1,
                                          'side': 'SELL', 'asset': 'T'}]) == {}


class TestReconciliation:
    """
    Расхождение с биржей находится сверкой, а не по балансу.

    Призрак — заявка есть у нас, но не на бирже. Опаснее сироты: мы считаем,
    что котируем сторону, а её нет, и перестаём сокращать запас, полагая, что
    сокращаем. Сирота — неснятая старая: нас исполнят по забытой цене.
    """

    def test_ghosts_and_orphans_are_separated(self, monkeypatch):
        monkeypatch.setattr(executor, 'open_orders',
                            lambda: [{'id': 'LIVE-1'}, {'id': 'ORPHAN'}])
        out = executor.reconcile({'LIVE-1': ('T', 'bid', 0.2),
                                  'GHOST': ('T', 'ask', 0.3)})
        assert out['ghost'] == ['GHOST']
        assert out['orphan'] == ['ORPHAN']

    def test_unreachable_exchange_gives_none_not_empty(self, monkeypatch):
        """
        «Расхождений нет» и «не смог спросить» — разные вещи.

        На первом можно строить решение, на втором нельзя: пустой ответ
        означал бы, что все наши заявки живы, тогда как мы просто не дозвонились.
        """
        monkeypatch.setattr(executor, 'open_orders', lambda: None)
        assert executor.reconcile({'X': ('T', 'bid', 0.2)}) is None

    def test_forgetting_ghosts_frees_the_side_for_requoting(self, tmp_path):
        from polymarket import engine
        maker = engine.PaperMaker(bankroll=1000,
                                  state_path=str(tmp_path / 's.json'))
        slot = maker._slot('T')
        slot['orders'] = {'bid': {'price': 0.2, 'size': 100, 'ts': 1,
                                  'queue': 0, 'live_id': 'GHOST'}, 'ask': None}
        assert maker.forget_orders(['GHOST']) == 1
        assert maker._slot('T')['orders']['bid'] is None

    def test_reconciliation_runs_before_quoting(self):
        """Сначала узнаём у биржи, что произошло, потом решаем, что делать."""
        source = os.path.join(ROOT, 'polymarket', 'mm.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        assert text.index('executor.reconcile(') < text.index('maker.place(')


class TestAutostart:
    """
    Автозапуск выключен по умолчанию и не может уронить торговлю на бирже.

    Polymarket — другая площадка и другие деньги. Его неудача не имеет права
    остановить основного бота, а его успех не должен наступать сам собой:
    бумажный прогон пока не дал исполнений, и включать по умолчанию то, о чём
    нечего сказать числами, значит навязывать непроверенное.
    """

    def test_disabled_unless_explicitly_enabled(self, monkeypatch):
        from polymarket import service
        monkeypatch.delenv('PM_AUTOSTART', raising=False)
        assert service.autostart_enabled() is False
        assert service.start() is False
        monkeypatch.setenv('PM_AUTOSTART', '1')
        assert service.autostart_enabled() is True

    def test_bot_catches_every_failure(self):
        """
        Бот ловит ВСЁ при запуске маркет-мейкера.

        Пакет может отсутствовать в сборке, зависимость не встать, ключ
        оказаться негодным. В любом случае торговля на бирже обязана
        продолжиться, а причина — попасть в журнал.
        """
        source = os.path.join(ROOT, 'bot.py')
        with open(source, encoding='utf-8') as fh:
            text = fh.read()
        block = text[text.index('def _start_polymarket'):]
        block = block[:block.index('\ndef ')]
        assert 'except Exception' in block
        assert 'продолжается' in block

    def test_second_start_does_not_double_quote(self, tmp_path, monkeypatch):
        """
        Повторный запуск не поднимает второй поток.

        Два потока котировали бы одни рынки, перебивая заявки друг друга и
        удваивая размер — то есть и риск.
        """
        from polymarket import mm, service
        # Замок уводится во временную папку: иначе тест зависел бы от того,
        # работает ли маркет-мейкер на этой машине прямо сейчас.
        monkeypatch.setattr(mm.store, 'DIR', str(tmp_path))
        monkeypatch.setenv('PM_AUTOSTART', '1')
        monkeypatch.setattr(service, '_thread', None)

        started = []
        class FakeThread:
            def __init__(self, *a, **k):
                started.append(1)
            def start(self):
                pass
            def is_alive(self):
                return True

        monkeypatch.setattr(service.threading, 'Thread', FakeThread)
        assert service.start() is True
        assert service.start() is True
        assert len(started) == 1


class TestDashboardNeverExposesTheKey:

    def test_snapshot_has_address_but_no_key(self, monkeypatch):
        """
        Снимок для панели уходит по сети. Ключа в нём быть не может.

        Панель слушает без пароля; всё, что она отдаёт, надо считать
        общедоступным.
        """
        import polymarket
        monkeypatch.setenv('PM_PRIVATE_KEY', TEST_KEY)
        monkeypatch.setenv('PM_FUNDER', '0x' + '1' * 40)
        snap = polymarket.snapshot()
        assert TEST_KEY not in str(snap)
        assert snap['wallet']['address'] is not None
        assert 'private' not in str(snap).lower()


class TestServiceTakesTheLockToo:
    """
    Служба берёт тот же замок, что и запуск из консоли.

    Проверка на живой поток ловила только повтор внутри приложения. Но
    маркет-мейкер запускается ещё и командой из консоли — а это ДРУГОЙ процесс
    с тем же файлом состояния. Два таких пишут вперемешку: заявки разного
    размера, число рынков скачет от цикла к циклу, позиции одного затираются
    другим. Это уже случалось, замок был поставлен в командный запуск — и дыра
    осталась открытой ровно наполовину.
    """

    def test_start_is_refused_when_another_process_holds_the_lock(
            self, tmp_path, monkeypatch):
        from polymarket import mm, service
        monkeypatch.setattr(mm.store, 'DIR', str(tmp_path))
        monkeypatch.setattr(mm, '_alive', lambda pid: True)
        (tmp_path / 'mm.lock').write_text('999999999', encoding='utf-8')
        monkeypatch.setattr(service, '_thread', None)
        assert service.start(force=True) is False
        assert 'другом процессе' in (service.status().get('last_error') or '')

    def test_start_proceeds_when_the_lock_is_free(self, tmp_path, monkeypatch):
        from polymarket import mm, service
        monkeypatch.setattr(mm.store, 'DIR', str(tmp_path))
        monkeypatch.setattr(service, '_thread', None)
        # Поток не поднимаем: проверяем только, что замок не мешает.
        started = {}
        monkeypatch.setattr(service.threading, 'Thread',
                            lambda **kw: type('T', (), {
                                'start': lambda self: started.setdefault('да', True),
                                'is_alive': lambda self: True})())
        assert service.start(force=True) is True
        assert started.get('да')


class TestSecondGenerationClient:
    """
    Живой путь работает на клиенте ВТОРОГО поколения и типе подписи 3.

    ВЫЯСНЕНО ОТПРАВКОЙ НАСТОЯЩЕЙ ЗАЯВКИ, а не чтением документации, и это
    главный урок эпизода. Всё, что проверялось раньше — 792 теста, проверка
    готовности, сквозные прогоны, — останавливалось перед последним шагом. Там
    и оказалась поломка:

        старая библиотека     заархивирована разработчиками площадки; биржа
                              отвечает ей «invalid order version»
        старый залог          площадка перешла с USDC.e на собственный pUSD, и
                              старый клиент показывал ноль при полном счёте
        типы подписи 0,1,2    проходят вход, но на отправке отвечают «maker
                              address not allowed» — все три, при любом счёте
        тип 3 (POLY_1271)     принят: заявка ушла, встала в стакан, снялась

    Старый клиент типа 3 не знал вовсе, поэтому подобрать его перебором было
    нельзя — только сменив библиотеку.
    """

    def test_live_path_uses_the_new_client(self):
        for name in ('wallet.py', 'executor.py'):
            text = open(os.path.join(ROOT, 'polymarket', name),
                        encoding='utf-8').read()
            live = [ln for ln in text.splitlines()
                    if 'py_clob_client' in ln and not ln.strip().startswith('#')]
            assert live, name
            for line in live:
                assert 'py_clob_client_v2' in line, f'{name}: {line.strip()}'

    def test_signature_type_defaults_to_the_one_that_works(self, monkeypatch):
        monkeypatch.delenv('PM_SIGNATURE_TYPE', raising=False)
        assert wallet.signature_type() == 3

    def test_broken_setting_falls_back_to_the_working_type(self, monkeypatch):
        monkeypatch.setenv('PM_SIGNATURE_TYPE', 'не число')
        assert wallet.signature_type() == 3

    def test_detection_tries_the_working_type_first(self):
        """
        Перебор начинается с типа 3: остальные три дают отказ на отправке,
        и ставить их первыми значит тратить обращения впустую.
        """
        from polymarket import connect
        assert connect.SIGNATURE_TYPES[0] == 3
        assert set(connect.SIGNATURE_TYPES) == {0, 1, 2, 3}

    def test_cancel_passes_the_right_type(self):
        """
        В новом клиенте снятие ждёт OrderPayload, а не голую строку: строка
        проходит молча и заявка остаётся висеть — худшее из состояний.
        """
        text = open(os.path.join(ROOT, 'polymarket', 'executor.py'),
                    encoding='utf-8').read()
        assert 'OrderPayload(orderID=' in text
        assert 'api.cancel(' not in text

    def test_both_clients_are_declared_for_the_build(self):
        root = os.path.dirname(ROOT)
        reqs = open(os.path.join(root, 'requirements.txt'), encoding='utf-8').read()
        assert 'py-clob-client-v2' in reqs
        flow = os.path.join(root, '.github', 'workflows', 'build-exe.yml')
        if os.path.exists(flow):
            assert 'py_clob_client_v2' in open(flow, encoding='utf-8').read()
