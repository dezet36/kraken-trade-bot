"""
Новая панель (/v2, control/panel/) — сторож раздачи и связности модулей.

1. Сервер отдаёт только файлы панели: «..», скрытые файлы, обратная косая,
   чужие типы (.py) и всё вне control/panel — 404. Панель без пароля, и
   раздача файлов не должна стать окном в код и данные бота.
2. Модули JS импортируют только то, что модуль-источник экспортирует, и
   только существующие файлы. Браузер на таком промахе не загружает модуль
   вовсе — страница пустая, а сервер отвечает 200 (как 19.09.2026 с белым
   окном). Node.js на машинах нет, поэтому связность проверяется разбором.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from control import dashboard  # noqa: E402

PANEL = os.path.join(ROOT, 'control', 'panel')


class TestServing:
    def test_panel_files_are_served(self):
        for rel in ('', 'index.html', 'app.css', 'js/app.js', 'js/pages/home.js'):
            path = dashboard._panel_file(rel)
            assert path and os.path.isfile(path), rel

    def test_nothing_outside_the_panel(self):
        for rel in ('../dashboard.py', 'js/../../dashboard.py', '../../infra/config.py',
                    '..', '.hidden.js', 'js/.x.js', 'js\\app.js', 'C:/Windows/win.ini',
                    'js/app.py', 'js//app.js', './index.html', 'nope.js'):
            assert dashboard._panel_file(rel) is None, rel

    def test_every_type_has_a_content_type(self):
        for dp, _dn, fn in os.walk(PANEL):
            for f in fn:
                assert os.path.splitext(f)[1].lower() in dashboard.PANEL_TYPES, f

    def test_stamp_changes_with_files(self):
        assert dashboard._panel_stamp()

    def test_route_and_registry(self):
        src = open(os.path.join(ROOT, 'control', 'dashboard.py'), encoding='utf-8').read()
        assert "path.startswith('/v2/')" in src and "self._send_panel(path[4:])" in src
        html = open(os.path.join(PANEL, 'index.html'), encoding='utf-8').read()
        assert '</head>' in html, 'реестр стратегий вставляется перед </head>'
        assert '<script type="module" src="/v2/js/app.js">' in html


IMPORT = re.compile(r"import\s*\{([^}]*)\}\s*from\s*'([^']+)'", re.S)
IMPORT_STAR = re.compile(r"import\s+\*\s+as\s+\w+\s+from\s*'([^']+)'")
EXPORT_DECL = re.compile(r'^export\s+(?:async\s+)?(?:function\*?|const|let|class)\s+(\w+)', re.M)
EXPORT_LIST = re.compile(r'^export\s*\{([^}]*)\}', re.M)


def _js_files():
    for dp, _dn, fn in os.walk(PANEL):
        for f in fn:
            if f.endswith('.js'):
                yield os.path.join(dp, f)


def _exports(path):
    src = open(path, encoding='utf-8').read()
    names = set(EXPORT_DECL.findall(src))
    for block in EXPORT_LIST.findall(src):
        for item in block.split(','):
            item = item.strip()
            if item:
                names.add(item.split(' as ')[-1].strip())
    return names


class TestModules:
    def test_imports_resolve_to_exports(self):
        problems = []
        for path in _js_files():
            src = open(path, encoding='utf-8').read()
            for names, target in IMPORT.findall(src):
                file = os.path.normpath(os.path.join(os.path.dirname(path), target))
                if not os.path.isfile(file):
                    problems.append(f'{path}: нет файла {target}')
                    continue
                have = _exports(file)
                for item in names.split(','):
                    name = item.strip().split(' as ')[0].strip()
                    if name and name not in have:
                        problems.append(f'{os.path.relpath(path, PANEL)}: {name} не экспортирует {target}')
            for target in IMPORT_STAR.findall(src):
                file = os.path.normpath(os.path.join(os.path.dirname(path), target))
                if not os.path.isfile(file):
                    problems.append(f'{path}: нет файла {target}')
        assert not problems, '\n'.join(problems)

    def test_pages_have_title_and_render(self):
        for path in _js_files():
            if os.sep + 'pages' + os.sep not in path or path.endswith('soon.js'):
                continue
            names = _exports(path)
            assert {'title', 'render'} <= names, path

    def test_data_goes_in_as_text(self):
        """Данные в DOM — только текстом: innerHTML — лишь для своих значков в ui.js."""
        for path in _js_files():
            src = open(path, encoding='utf-8').read()
            if path.endswith('ui.js'):
                assert src.count('innerHTML') == 1, 'innerHTML — только в h() для значков'
                continue
            assert 'innerHTML' not in src and "html:" not in src, path

    def test_old_panel_links_to_the_new(self):
        old = open(os.path.join(ROOT, 'control', 'dashboard.html'), encoding='utf-8').read()
        assert 'href="/v2/"' in old
