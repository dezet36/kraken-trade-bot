"""
Подключение кошелька из панели.

ПОЧЕМУ ЭТО ВООБЩЕ РАЗРЕШЕНО. Долгое время ключ Polymarket принимался только из
окружения: панель слушает без пароля, и секрет, прошедший через неё, надо
считать раскрытым. Довод верный, но применялся непоследовательно — ключи БИРЖИ
в этом же приложении давно задаются из панели, проверяются и пишутся в .env.
Особый случай для Polymarket ничем не обосновывался, кроме привычки, и оставлял
человека с сервером без способа подключить кошелёк вообще: консоли там нет.

ЧТО ОСТАЁТСЯ ВЕРНЫМ. Ключ биржи можно урезать в правах — запретить вывод.
Приватный ключ Polygon урезать нельзя: он распоряжается ВСЕМ на адресе. Отсюда
правила ниже, и каждое проверяется отдельно.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polymarket import connect, wallet  # noqa: E402

GOOD = '0x' + '1' * 64


class TestKeyNeverLeaves:

    def test_state_has_no_key_field_at_all(self, monkeypatch):
        """
        Наружу уходит адрес и признак настройки — и ничего, чем можно
        подписать сделку. Проверяется по СОДЕРЖИМОМУ, а не по именам полей:
        ключ мог бы просочиться в любое из них.
        """
        monkeypatch.setenv('PM_PRIVATE_KEY', GOOD)
        monkeypatch.setenv('PM_FUNDER', '0x' + '2' * 40)
        text = json.dumps(connect.state(), ensure_ascii=False)
        assert '1' * 64 not in text
        assert GOOD not in text

    def test_mask_shows_enough_to_recognise_and_no_more(self):
        masked = connect.mask(GOOD)
        assert GOOD not in masked
        assert '1' * 20 not in masked
        assert masked.startswith('0x')

    def test_library_error_text_is_not_forwarded(self, monkeypatch):
        """
        Текст ошибки библиотеки наружу не идёт: у некоторых версий ключ
        попадает в него целиком.
        """
        def boom():
            raise RuntimeError(f'bad key {GOOD}')
        monkeypatch.setattr(wallet, 'address', boom)
        ok, _, why = connect.check(GOOD)
        assert ok is False
        assert GOOD not in why


class TestCheckHappensBeforeWriting:

    def test_bad_key_is_refused_and_nothing_is_written(self, monkeypatch):
        written = []
        import first_run
        monkeypatch.setattr(first_run, '_write_env', lambda v: written.append(v))
        ok, _, why = connect.save('не ключ')
        assert ok is False and written == []
        assert 'знак' in why

    def test_bad_funder_is_refused(self):
        ok, _, why = connect.save(GOOD, funder='не адрес')
        assert ok is False and 'адрес' in why

    def test_environment_is_restored_after_a_failed_check(self, monkeypatch):
        """
        Проверка не должна незаметно включить кошелёк, который человек ещё не
        сохранил: окружение возвращается в прежний вид при любом исходе.
        """
        monkeypatch.delenv('PM_PRIVATE_KEY', raising=False)
        monkeypatch.setattr(wallet, 'client', lambda force=False: None)
        connect.check(GOOD)
        assert os.environ.get('PM_PRIVATE_KEY') is None

    def test_existing_key_is_not_clobbered_by_a_check(self, monkeypatch):
        monkeypatch.setenv('PM_PRIVATE_KEY', '0x' + '9' * 64)
        monkeypatch.setattr(wallet, 'client', lambda force=False: None)
        connect.check(GOOD)
        assert os.environ['PM_PRIVATE_KEY'] == '0x' + '9' * 64


class TestConnectingDoesNotStartTrading:
    """
    Подключить кошелёк и разрешить тратить с него деньги — разные решения.
    Одно действие не должно делать оба.
    """

    def test_save_does_not_touch_pm_live(self, monkeypatch):
        written = {}
        import first_run
        monkeypatch.setattr(first_run, '_write_env', written.update)
        monkeypatch.setattr(connect, 'check',
                            lambda k, f=None: (True, '0xABC', ''))
        monkeypatch.setattr(wallet, 'client', lambda force=False: None)
        connect.save(GOOD)
        assert 'PM_LIVE' not in written

    def test_live_cannot_be_enabled_without_a_wallet(self, monkeypatch):
        monkeypatch.setattr(wallet, 'configured', lambda: False)
        ok, why = connect.set_live(True)
        assert ok is False
        assert 'кошелёк' in why

    def test_live_can_always_be_turned_off(self, monkeypatch):
        """
        Выключение доступно всегда, даже без кошелька: это единственное
        безопасное направление, и мешать ему нельзя.
        """
        written = {}
        import first_run
        monkeypatch.setattr(first_run, '_write_env', written.update)
        monkeypatch.setattr(wallet, 'configured', lambda: False)
        ok, _ = connect.set_live(False)
        assert ok is True and written.get('PM_LIVE') == '0'


class TestForgetIsComplete:

    def test_forget_clears_key_funder_and_live(self, monkeypatch):
        written = {}
        import first_run
        monkeypatch.setattr(first_run, '_write_env', written.update)
        monkeypatch.setenv('PM_PRIVATE_KEY', GOOD)
        monkeypatch.setattr(wallet, 'client', lambda force=False: None)
        connect.forget()
        assert written['PM_PRIVATE_KEY'] == ''
        assert written['PM_FUNDER'] == ''
        assert written['PM_LIVE'] == '0', 'отключая кошелёк, гасим и торговлю'
        assert os.environ.get('PM_PRIVATE_KEY') is None


class TestPanelMarkup:

    def _html(self):
        return open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()

    def test_the_warning_precedes_the_field(self):
        """
        Про то, что ключ распоряжается всеми средствами, нужно прочитать ДО
        того, как он вставлен, а не после.
        """
        html = self._html()
        assert html.index('pm-wallet-warn') < html.index('id="pm-key"')

    def test_key_field_is_a_password_and_not_autofilled(self):
        html = self._html()
        block = html[html.index('id="pm-key"') - 200:html.index('id="pm-key"') + 200]
        assert 'type="password"' in block
        assert 'autocomplete="off"' in block

    def test_the_field_is_cleared_after_saving(self):
        """
        Ключ, оставшийся в разметке, доступен всякому, кто откроет ту же
        страницу или посмотрит историю ввода.
        """
        html = self._html()
        assert "el('pm-key').value = ''" in html

    def test_turning_live_on_asks_for_confirmation(self):
        """
        Подтверждение спрашивается на ВКЛЮЧЕНИЕ и не спрашивается на
        выключение: гасить всегда безопасно, и мешать этому в тот момент,
        когда спешат, нельзя.
        """
        html = self._html()
        # Ищем обработчик, а не разметку: первое вхождение имени — это кнопка.
        spot = html.index("closest('#pm-live-toggle')")
        block = html[spot:spot + 1200]
        assert 'confirm(' in block
        assert 'turningOn &&' in block, 'подтверждение только на включение'


class TestDashboardShowsWhatMatters:
    """
    Панель отвечает на два вопроса: чем бот занят и сколько получилось.

    ПРЕЖДЕ НИ НА ОДИН ОТВЕТИТЬ БЫЛО НЕЛЬЗЯ. Показывались только рынки с
    позицией — то есть где нас УЖЕ исполнили, — а мейкер большую часть времени
    именно СТОИТ, и это его работа. И «капитал $100» не отвечает на вопрос
    «сколько получили»: начальной суммы на экране не было вовсе, вычитать
    приходилось в уме.
    """

    def _html(self):
        return open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()

    def test_the_bottom_line_comes_first(self):
        html = self._html()
        spot = html.index('function renderPolymarket()')
        block = html[spot:spot + 3000]
        assert "['Итог'" in block
        assert 'Было / стало' in block, 'начальная сумма показана рядом'

    def test_standing_quotes_are_shown(self):
        html = self._html()
        assert 'Где стоим сейчас' in html
        assert 'PM.standing' in html

    def test_expected_and_actual_waiting_stand_together(self):
        """
        Расхождение «стоит» и «ждали» — первый признак, что модель ожидания
        врёт. Поэтому оба числа в одной строке, а не в разных таблицах.
        """
        html = self._html()
        spot = html.index('Где стоим сейчас')
        block = html[spot:spot + 2200]
        assert 'standing_min' in block and 'expected_min' in block

    def test_round_trips_are_shown_separately_from_fills(self):
        """
        Круг — главная мерка: отдельное исполнение не говорит ничего, купить
        может каждый. Заработок появляется только при закрытии.
        """
        html = self._html()
        assert 'Завершённые круги' in html
        assert 'PM.rounds' in html

    def test_adverse_selection_carries_its_caveat(self):
        """
        Число сноса без оговорки вводит в заблуждение: по завершённому кругу
        снос сокращается сам собой, и судить надо по незакрытым позициям.
        """
        html = self._html()
        spot = html.index('Куда шла цена после наших сделок')
        block = html[spot:spot + 1800]
        assert 'сокращается сам собой' in block

    def test_render_function_is_balanced(self):
        """Грубая, но полезная проверка: функция закрывается, кавычки парны."""
        html = self._html()
        start = html.index('function renderPolymarket()')
        depth, end = 0, None
        for k in range(start, len(html)):
            if html[k] == '{':
                depth += 1
            elif html[k] == '}':
                depth -= 1
                if depth == 0:
                    end = k
                    break
        assert end is not None, 'функция не закрывается'
        body = html[start:end + 1]
        assert body.count('`') % 2 == 0
        assert body.count("'") % 2 == 0
