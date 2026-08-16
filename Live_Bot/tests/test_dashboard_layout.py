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

HTML = open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()


class TestEveryPageExists:

    def test_menu_and_markup_agree(self):
        """
        Раздел в меню без страницы открывается пустым, страница без меню
        недостижима. И то и другое молча.
        """
        pages = set(re.findall(r'<section class="page" data-page="(\w+)"', HTML))
        menu = set(re.findall(r"\{ id: '(\w+)',\s+ic:", HTML))
        assert menu - pages == set(), f'в меню есть, страницы нет: {menu - pages}'
        assert pages - menu == set(), f'страница есть, в меню нет: {pages - menu}'

    def test_summary_is_the_default_page(self):
        """
        Открывается сводка, а не «Обзор». Прежний «Обзор» показывал только
        биржу и молчал об этом — плохой первый экран для приложения с двумя
        направлениями.
        """
        assert "q.get('page') : 'summary'" in HTML


class TestDirectionsDoNotMix:

    def test_each_trading_page_declares_its_direction(self):
        """
        Принадлежность видна ДО нажатия. Иначе её приходится помнить, а
        помнить нечего: имена разделов у направлений одинаковые.
        """
        for page in ('overview', 'positions', 'history', 'analytics'):
            spot = HTML.index(f"id: '{page}'")
            assert "dir: 'exchange'" in HTML[spot:spot + 120], page
        # Второе направление вырезано; проверка осталась на том, что есть.

    def test_filters_hide_where_they_do_not_apply(self):
        """
        Период и стратегия относятся к сделкам биржи. На чужих страницах они
        не фильтруют ничего, но своим видом обещают управление, которого нет:
        пустое место честнее.
        """
        assert "filters.hidden = PAGE_DIR[page] !== 'exchange'" in HTML

    def test_totals_are_not_summed_across_directions(self):
        """
        Сорок тысяч бумажных на бирже и сто настоящих долларов рядом —
        величины разной природы. Сложив их, мы получили бы число, которое
        ничего не значит, зато выглядит убедительно.
        """
        spot = HTML.index('function renderSummary()')
        block = HTML[spot:spot + 4000]
        assert 'не складываются' in block


class TestConnectionsAreInOnePlace:

    def _connect_block(self):
        start = HTML.index('data-page="connect"')
        return HTML[start:HTML.index('<section class="page"', start + 10)]

    def test_exchange_and_wallet_live_together(self):
        block = self._connect_block()
        for what in ('id="exchange"', 'id="keys"'):
            assert what in block, what

    def test_they_are_no_longer_in_settings(self):
        """Два места для одного действия — это поиск вместо настройки."""
        start = HTML.index('data-sub="app"')
        block = HTML[start:start + 700]
        assert 'id="exchange"' not in block
        assert 'id="keys"' not in block

    def test_no_wallet_lives_outside_connections(self):
        """
        Кошелёк площадки жил на вкладке маркет-мейкера — втором месте для того
        же действия. Вкладки больше нет, и вернуться ей некуда.
        """
        assert 'pm-wallet' not in HTML


class TestRenderersAreIntact:

    def _balanced(self, name):
        start = HTML.index(f'function {name}(')
        depth, end = 0, None
        for k in range(HTML.index('{', start), len(HTML)):
            if HTML[k] == '{':
                depth += 1
            elif HTML[k] == '}':
                depth -= 1
                if depth == 0:
                    end = k
                    break
        assert end is not None, f'{name} не закрывается'
        body = HTML[start:end + 1]
        assert body.count('`') % 2 == 0, f'{name}: непарные шаблонные кавычки'
        return body

    def test_new_renderers_are_whole(self):
        for name in ('renderSummary', 'buildNav', 'showPage'):
            self._balanced(name)

    def test_new_renderers_are_actually_called(self):
        assert 'renderSummary()' in HTML

    def test_summary_refreshes_on_every_poll(self):
        """
        Рисовать сводку только при открытии вкладки значило бы показывать
        вчерашние числа тому, кто держит панель открытой.
        """
        spot = HTML.index('function render() {')
        assert 'renderSummary()' in HTML[spot:spot + 600]


class TestServerSideSummary:

    def test_directions_are_built_on_the_server(self):
        """
        Складывать чужие суммы в браузере — верный способ однажды сложить
        несуммируемое. Считается там же, где данные.
        """
        source = open(os.path.join(ROOT, 'dashboard.py'), encoding='utf-8').read()
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
        source = open(os.path.join(ROOT, 'dashboard.py'), encoding='utf-8').read()
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

    PY = open(os.path.join(ROOT, 'dashboard.py'), encoding='utf-8').read()
    ACTIONS = ('/api/settings', '/api/deposit', '/api/action', '/api/keys',
               '/api/update', '/api/errors/clear')

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
