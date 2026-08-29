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

    def test_the_default_page_shows_the_real_numbers(self):
        """
        ЗАМЫСЕЛ ТОТ ЖЕ, ЦЕЛЬ ДРУГАЯ. Здесь проверялось, что первой открывается
        «Сводка»: прежний «Обзор» показывал одну биржу и молчал об этом, а
        направлений было два.

        Направление осталось одно — Polymarket вырезан, — и «Сводка» стала
        показывать его же таблицей беднее той, что рядом: без винрейта,
        ожидания, профит-фактора и просадки. Первым экраном стоял пересказ
        соседней страницы, и открывался он вместо неё.

        Требование к первому экрану не изменилось: он обязан показывать
        настоящие числа торговли, а не отсылать за ними дальше.
        """
        assert "q.get('page') : 'overview'" in HTML
        assert "q.get('page') : 'summary'" not in HTML


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

        ПРАВИЛО ПЕРЕЖИВАЕТ СТРАНИЦУ. Проверялось оно на тексте «Сводки», а та
        удалена: направление осталось одно, и складывать пока нечего. Но
        «пока» — не гарантия: вернись второе направление, сложение первым
        делом появилось бы в браузере. Поэтому проверяется не текст
        объяснения, а само отсутствие сложения.
        """
        server = open(os.path.join(ROOT, 'dashboard.py'), encoding='utf-8').read()
        for name, src in (('панель', HTML), ('сервер', server)):
            for bad in ('directions.reduce', 'sum(d[', 'sum(direction'):
                assert bad not in src, (
                    f'{name}: суммы по направлениям снова складываются — '
                    f'бумажные и настоящие деньги дадут убедительное ничто')


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
        for name in ('buildNav', 'showPage'):
            self._balanced(name)

    def test_the_removed_renderer_left_nothing_behind(self):
        """
        Удалять страницу надо целиком. Забытый вызов исчезнувшей функции — это
        ошибка в консоли, после которой не рисуется ВСЁ, что шло следом:
        ровно так панель осталась пустой, когда вырезали Polymarket, а
        `pmMoney` остался в вызовах.
        """
        assert 'renderSummary' not in HTML
        assert 'summary-body' not in HTML
        assert "title: 'Сводка'" not in HTML

    def test_the_page_list_has_no_hole(self):
        """Меню и страницы обязаны совпадать и после удаления."""
        pages = set(re.findall(r'<section class="page" data-page="(\w+)"', HTML))
        menu = set(re.findall(r"\{ id: '(\w+)',\s+ic:", HTML))
        assert pages == menu, (pages ^ menu)
        assert 'summary' not in pages


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
