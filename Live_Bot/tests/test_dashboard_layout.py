"""
Устройство панели: направления не смешиваются, принадлежность раздела видна.

ЧТО БЫЛО НЕ ТАК. В приложении два рынка с РАЗНЫМИ деньгами, разными
стратегиями и разной механикой, но меню было плоским. Четыре раздела из семи
говорили только про биржу, нигде этого не называя: человек, открывший «Обзор»,
не мог знать, что Polymarket туда не входит. А весь Polymarket с пятью
стратегиями ютился в одной вкладке сбоку.

Подключения при этом лежали в двух разных местах: ключи биржи в «Управление →
Приложение», кошелёк Polymarket — на вкладке маркет-мейкера. Действие у них
одно: сказать боту, откуда берутся деньги.
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
        for page in ('polymarket', 'pmstrat'):
            spot = HTML.index(f"id: '{page}'")
            assert "dir: 'polymarket'" in HTML[spot:spot + 130], page

    def test_filters_hide_where_they_do_not_apply(self):
        """
        Период и стратегия относятся к сделкам биржи. На страницах Polymarket
        они не фильтруют ничего, но своим видом обещают управление, которого
        нет: пустое место честнее.
        """
        assert "filters.hidden = PAGE_DIR[page] !== 'exchange'" in HTML

    def test_totals_are_not_summed_across_directions(self):
        """
        Сорок тысяч бумажных на бирже и сто настоящих долларов на Polymarket —
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
        for what in ('id="exchange"', 'id="keys"', 'id="pm-wallet"'):
            assert what in block, what

    def test_they_are_no_longer_in_settings(self):
        """Два места для одного действия — это поиск вместо настройки."""
        start = HTML.index('data-sub="app"')
        block = HTML[start:start + 700]
        assert 'id="exchange"' not in block
        assert 'id="keys"' not in block

    def test_wallet_left_the_market_maker_tab(self):
        start = HTML.index('data-page="polymarket"')
        block = HTML[start:HTML.index('<section class="page"', start + 10)]
        assert 'id="pm-wallet"' not in block


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
        for name in ('renderSummary', 'renderPmStrategies', 'buildNav',
                     'showPage', 'renderPolymarket'):
            self._balanced(name)

    def test_new_renderers_are_actually_called(self):
        assert 'renderSummary()' in HTML
        assert 'renderPmStrategies()' in HTML

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

    def test_polymarket_failure_does_not_break_the_summary(self):
        """
        Направление, которое не читается, показывается как нечитаемое — а не
        обрушивает всю сводку вместе с биржей.
        """
        source = open(os.path.join(ROOT, 'dashboard.py'), encoding='utf-8').read()
        spot = source.index('def _directions()')
        block = source[spot:source.index('\ndef ', spot + 10)]
        assert 'except Exception' in block
        assert "'не читается'" in block


class TestActionsArePostAndGuarded:
    """
    Действия принимаются методом POST и только с этой машины.

    ДВЕ ОШИБКИ РАЗОМ, И ОБЕ МОЛЧАЛИ. Адреса действий Polymarket были объявлены
    в обработчике ЧТЕНИЯ, а панель шлёт их методом POST: запрос уходил в
    никуда с ответом 404, и снаружи это выглядело как «кошелёк не подключён»
    без всякого объяснения.

    Вторая хуже первой. В обработчике чтения нет проверки «только с этой
    машины» — той самой, что защищает панель без пароля. Дотянувшийся до порта
    мог запустить маркет-мейкера, включить аварийную остановку или подменить
    кошелёк. Спасал только 404, то есть первая ошибка прикрывала вторую.
    """

    PY = open(os.path.join(ROOT, 'dashboard.py'), encoding='utf-8').read()
    ACTIONS = ('/api/polymarket/start', '/api/polymarket/halt',
               '/api/polymarket/stop', '/api/polymarket/resume',
               '/api/polymarket/wallet', '/api/polymarket/wallet/forget',
               '/api/polymarket/live', '/api/polymarket/budget')

    def _handler_of(self, endpoint):
        get = self.PY.index('def do_GET')
        post = self.PY.index('def do_POST')
        spot = self.PY.index(f"path == '{endpoint}'")
        return 'do_GET' if get < spot < post else 'do_POST'

    def test_every_action_lives_in_the_post_handler(self):
        for endpoint in self.ACTIONS:
            assert self._handler_of(endpoint) == 'do_POST', endpoint

    def test_every_action_is_allowed_through(self):
        """
        Адрес в обработчике, но не в списке разрешённых, отвечает 404 — то
        есть молча не работает.
        """
        start = self.PY.index('if path not in (', self.PY.index('def do_POST'))
        allow = self.PY[start:self.PY.index('):', start)]
        for endpoint in self.ACTIONS:
            assert f"'{endpoint}'" in allow, endpoint

    def test_reading_the_snapshot_stays_a_get(self):
        """Чтение состояния ничего не меняет и остаётся доступным на чтение."""
        assert self._handler_of('/api/polymarket') == 'do_GET'

    def test_the_panel_sends_them_as_post(self):
        """
        Обработчик и панель должны сойтись в методе. Их расхождение и было
        причиной молчаливого отказа.
        """
        for endpoint in self.ACTIONS:
            if endpoint in HTML:
                spot = HTML.index(endpoint)
                near = HTML[max(0, spot - 400):spot + 400]
                assert 'pmPost' in near, endpoint

    def test_local_only_guard_protects_the_post_handler(self):
        post = self.PY.index('def do_POST')
        head = self.PY[post:post + 1600]
        assert '_controls_allowed()' in head
