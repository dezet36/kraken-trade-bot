"""
Устройство панели: раздел в меню и страница в разметке отвечают друг другу.

НАПРАВЛЕНИЕ ОСТАЛОСЬ ОДНО — биржа. Рядом стояло второе, Polymarket, и правила
ниже писались ради того, чтобы они не смешивались: разные деньги, разные
стратегии, разная механика. Направление вырезано и живёт в ветке
polymarket-archive; правила остались, потому что они про устройство панели, а
не про ту конкретную площадку.

Подключения при этом лежали в двух разных местах — ключи биржи в «Управление →
Приложение», кошелёк на вкладке площадки. Действие у них было одно: сказать
боту, откуда берутся деньги. Теперь место для этого одно.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)



class TestServerSideSummary:

    def test_directions_are_built_on_the_server(self):
        """
        Складывать чужие суммы в браузере — верный способ однажды сложить
        несуммируемое. Считается там же, где данные.
        """
        source = open(os.path.join(ROOT, 'control', 'dashboard.py'), encoding='utf-8').read()
        assert 'def _directions()' in source
        assert "payload['directions']" in source

    def test_the_summary_names_its_direction(self):
        """
        Сводка обязана называть, о чём она.

        Здесь проверялось, что нечитаемое направление показывается
        нечитаемым, а не роняет всю сводку вместе с биржей. Правило
        относилось ко второму направлению, которого больше нет: осталась одна
        касса, и падать ей не с чем.
        """
        source = open(os.path.join(ROOT, 'control', 'dashboard.py'), encoding='utf-8').read()
        spot = source.index('def _directions()')
        block = source[spot:source.index('\ndef ', spot + 10)]
        assert "'id': 'exchange'" in block


class TestActionsArePostAndGuarded:
    """
    Действия принимаются методом POST и только с этой машины.

    ЗДЕСЬ ПРОВЕРЯЛИСЬ ДЕЙСТВИЯ POLYMARKET, и проверка появилась после двух
    ошибок разом. Их адреса были объявлены в обработчике ЧТЕНИЯ, а панель шлёт
    их методом POST: запрос уходил в никуда с ответом 404. Вторая хуже первой —
    в обработчике чтения нет проверки «только с этой машины», и дотянувшийся до
    порта мог остановить торговлю или подменить кошелёк. Спасал только 404, то
    есть первая ошибка прикрывала вторую.

    Направление вырезано, но правило осталось и распространяется на всё, что
    меняет состояние.
    """

    PY = open(os.path.join(ROOT, 'control', 'dashboard.py'), encoding='utf-8').read()
    ACTIONS = ('/api/settings', '/api/deposit', '/api/action',
               '/api/update', '/api/errors/clear', '/api/accounts/save',
               '/api/accounts/delete', '/api/accounts/keys')

    def _handler_of(self, endpoint):
        get = self.PY.index('def do_GET')
        post = self.PY.index('def do_POST')
        spot = self.PY.index(f"'{endpoint}'")
        return 'do_GET' if get < spot < post else 'do_POST'

    def test_every_action_is_allowed_through(self):
        """
        Адрес в обработчике, но не в списке разрешённых, отвечает 404 — то
        есть молча не работает.
        """
        start = self.PY.index('if path not in (', self.PY.index('def do_POST'))
        allow = self.PY[start:self.PY.index('):', start)]
        for endpoint in self.ACTIONS:
            assert f"'{endpoint}'" in allow, endpoint

    def test_changing_state_requires_the_local_check(self):
        """Панель без пароля: менять что-либо можно только с этой машины."""
        post = self.PY[self.PY.index('def do_POST'):]
        assert '_controls_allowed()' in post[:2000]

    def test_reading_stays_a_get(self):
        """Чтение состояния ничего не меняет и остаётся доступным на чтение."""
        get = self.PY[self.PY.index('def do_GET'):self.PY.index('def do_POST')]
        assert "'/api/data'" in get


class TestHiddenMeansHidden:
    """
    Атрибут hidden слабее правила по классу, и на этом уже один раз погорели:
    `.filters { display: flex }` перебивало браузерное `[hidden]{display:none}`,
    и панель фильтров оставалась видимой на всех страницах. В новой панели
    правило одно на всё — [hidden] сильнее любого класса.
    """

    def test_hidden_beats_every_class(self):
        css = open(os.path.join(ROOT, 'control', 'panel', 'app.css'), encoding='utf-8').read()
        assert re.search(r'\[hidden\]\s*\{\s*display:\s*none\s*!important', css)
