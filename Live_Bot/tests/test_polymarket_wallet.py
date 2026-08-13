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
        monkeypatch.setattr(executor, 'MAX_ORDER_USD', 25.0)
        out = executor.place('T', 'bid', 0.90, 1000)      # $900
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
