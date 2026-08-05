"""
Проверка обновления кода из git.

Главное, что здесь защищается, — торговые данные. Обновление имеет право
переносить только код; журнал сделок, состояние позиций, настройки и ключи
обязаны пережить его без единого изменения. Проверяется это на настоящем
git-репозитории во временном каталоге, а не на моках: подделка git скрыла бы
ровно тот класс ошибок, ради которого тесты и пишутся.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def git(cwd, *args):
    return subprocess.run(('git',) + args, cwd=cwd, capture_output=True,
                          text=True, encoding='utf-8', errors='replace')


def has_git():
    try:
        subprocess.run(['git', '--version'], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not has_git(), reason='git недоступен')


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Пара репозиториев: удалённый с новым коммитом и локальный клон."""
    origin = tmp_path / 'origin'
    origin.mkdir()
    git(origin, 'init', '--quiet', '--initial-branch=main')
    git(origin, 'config', 'user.email', 'test@example.com')
    git(origin, 'config', 'user.name', 'test')

    (origin / 'Live_Bot').mkdir()
    (origin / 'Live_Bot' / 'strategy.py').write_text('VERSION = 1\n', encoding='utf-8')
    (origin / '.gitignore').write_text(
        'Live_Bot/trades_journal.csv\nLive_Bot/runtime_settings.json\n'
        'Live_Bot/positions_state.json\nLive_Bot/.env\n', encoding='utf-8')
    git(origin, 'add', '-A')
    git(origin, 'commit', '--quiet', '-m', 'первая версия')

    work = tmp_path / 'work'
    git(tmp_path, 'clone', '--quiet', str(origin), str(work))
    git(work, 'config', 'user.email', 'test@example.com')
    git(work, 'config', 'user.name', 'test')

    # Данные бота: их не должно коснуться ничто
    data = work / 'Live_Bot'
    (data / 'trades_journal.csv').write_text('trade_id,pnl\n1,100\n', encoding='utf-8')
    (data / 'runtime_settings.json').write_text('{"SMC": {"risk_pct": 0.5}}',
                                                encoding='utf-8')
    (data / '.env').write_text('BYBIT_API_KEY=secret\n', encoding='utf-8')

    # Новая версия в origin
    (origin / 'Live_Bot' / 'strategy.py').write_text('VERSION = 2\n', encoding='utf-8')
    git(origin, 'add', '-A')
    git(origin, 'commit', '--quiet', '-m', 'вторая версия')

    import config
    monkeypatch.setattr(config, 'DATA_DIR', str(data))
    import updater
    monkeypatch.setattr(updater, 'ROOT', str(work))
    monkeypatch.setattr(updater, 'STATE_FILE', str(data / 'update_state.json'))
    monkeypatch.setattr(updater, 'run_tests', lambda: (True, '1 passed'))
    return updater, work, data


def test_vidit_dostupnoe_obnovlenie(repo):
    updater, work, _ = repo
    info = updater.status()
    assert info['available'] is True
    assert info['behind'] == 1
    assert info['can_update'] is True
    assert info['pending'][0]['subject'] == 'вторая версия'


def test_obnovlenie_menyaet_kod(repo):
    updater, work, _ = repo
    ok, message, info = updater.apply()
    assert ok, message
    assert (work / 'Live_Bot' / 'strategy.py').read_text(encoding='utf-8') == 'VERSION = 2\n'
    assert info['restart_required'] is True


def test_dannye_perezhivayut_obnovlenie(repo):
    """Ради этого всё и написано."""
    updater, work, data = repo
    before = {
        'journal': (data / 'trades_journal.csv').read_text(encoding='utf-8'),
        'settings': (data / 'runtime_settings.json').read_text(encoding='utf-8'),
        'env': (data / '.env').read_text(encoding='utf-8'),
    }
    ok, message, _ = updater.apply()
    assert ok, message
    assert (data / 'trades_journal.csv').read_text(encoding='utf-8') == before['journal']
    assert (data / 'runtime_settings.json').read_text(encoding='utf-8') == before['settings']
    assert (data / '.env').read_text(encoding='utf-8') == before['env']


def test_otkaz_pri_pravkah_koda_na_servere(repo):
    """Незакоммиченные правки отслеживаемого файла запрещают обновление."""
    updater, work, _ = repo
    (work / 'Live_Bot' / 'strategy.py').write_text('VERSION = 99\n', encoding='utf-8')
    info = updater.status()
    assert info['can_update'] is False
    assert 'незакоммиченные' in info['reason']
    ok, message, _ = updater.apply()
    assert ok is False


def test_otkaz_pri_razoshedsheysya_istorii(repo):
    """Локальный коммит делает перемотку невозможной — сливать нельзя."""
    updater, work, _ = repo
    (work / 'Live_Bot' / 'local.py').write_text('x = 1\n', encoding='utf-8')
    git(work, 'add', '-A')
    git(work, 'commit', '--quiet', '-m', 'локальная правка')
    info = updater.status()
    assert info['ahead'] == 1
    assert info['can_update'] is False
    assert 'перемотка невозможна' in info['reason']


def test_otkaz_esli_dannye_popali_pod_kontrol_versiy(repo):
    """
    Если журнал сделок однажды закоммитят, обновление начнёт затирать историю.
    Проверка выполняется перед каждым обновлением, а не один раз при настройке.
    """
    updater, work, _ = repo
    git(work, 'add', '-f', 'Live_Bot/trades_journal.csv')
    git(work, 'commit', '--quiet', '-m', 'случайно добавили журнал')
    ok, problem = updater.verify_data_untracked()
    assert ok is False
    assert 'trades_journal.csv' in problem
    assert updater.status()['can_update'] is False


def test_provalennye_testy_otkatyvayut_kod(repo, monkeypatch):
    updater, work, _ = repo
    monkeypatch.setattr(updater, 'run_tests', lambda: (False, '3 failed'))
    ok, message, _ = updater.apply()
    assert ok is False
    assert '3 failed' in message
    assert 'возвращён' in message
    # Код вернулся на прежнюю версию
    assert (work / 'Live_Bot' / 'strategy.py').read_text(encoding='utf-8') == 'VERSION = 1\n'


def test_otkat_vruchnuyu(repo):
    updater, work, _ = repo
    ok, _, _ = updater.apply()
    assert ok
    ok, message = updater.rollback()
    assert ok, message
    assert (work / 'Live_Bot' / 'strategy.py').read_text(encoding='utf-8') == 'VERSION = 1\n'


def test_bez_zapisi_o_versii_otkat_ne_delaetsya(repo):
    updater, _, _ = repo
    ok, message = updater.rollback()
    assert ok is False
    assert 'нет записи' in message


def test_povtornoe_obnovlenie_soobshchaet_chto_vsyo_svezhee(repo):
    updater, _, _ = repo
    assert updater.apply()[0] is True
    info = updater.status()
    assert info['behind'] == 0
    assert info['can_update'] is False
    assert 'последняя версия' in info['reason']


def test_sostoyanie_pishetsya_v_katalog_dannyh(repo):
    updater, _, data = repo
    updater.apply()
    saved = json.loads((data / 'update_state.json').read_text(encoding='utf-8'))
    assert saved['previous']
