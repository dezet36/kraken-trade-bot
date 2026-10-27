"""
Изоляция от торгового бота (главное требование владельца, 26.10.2026).

Копировщик:
  - не импортирует ни одного модуля торгового бота (Live_Bot);
  - не знает путей торгового бота (/opt/kraken, bot_data, Live_Bot) в коде;
  - файлы движка открывает только на чтение (feed.py, engine.py).
"""

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, 'copier')

BOT_PACKAGES = {'infra', 'data', 'analysis', 'strategies', 'execution', 'accounts', 'control',
                'app', 'bot', 'config', 'research'}
BOT_PATHS = ('/opt/kraken', 'bot_data', 'Live_Bot', 'kraken-bot')
ENGINE_READERS = {'feed.py', 'engine.py'}


def _modules():
    for folder, _, files in os.walk(PACKAGE):
        for name in files:
            if name.endswith('.py'):
                path = os.path.join(folder, name)
                with open(path, 'r', encoding='utf-8') as fh:
                    yield path, fh.read()


def test_no_imports_from_trading_bot():
    bad = []
    for path, src in _modules():
        for node in ast.walk(ast.parse(src)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split('.')[0] in BOT_PACKAGES:
                    bad.append(f'{os.path.relpath(path, ROOT)}: import {name}')
    assert not bad, bad


def test_no_trading_bot_paths():
    bad = [os.path.relpath(p, ROOT) for p, src in _modules()
           if any(marker in src for marker in BOT_PATHS)
           and not os.path.basename(p) == '__init__.py']
    assert not bad, bad


def test_engine_files_opened_read_only():
    """В feed.py и engine.py все open() — на чтение, кроме своего feed_state (через _atomic_write)."""
    bad = []
    for path, src in _modules():
        if os.path.basename(path) not in ENGINE_READERS:
            continue
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'open':
                mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else 'r'
                inside_atomic = 'tmp' in ast.unparse(node.args[0])
                if any(m in mode for m in 'wa+') and not inside_atomic:
                    bad.append(f'{os.path.basename(path)}: open(..., {mode!r})')
    assert not bad, bad
