"""
Вторая копия приложения видна, а не только вредна.

ОТКУДА ЭТО. Разбор 364 сделок с сервера за 5–29 августа 2026. В журнале у
FIBO 139 номеров сделок из 189 повторялись, цепочек баланса оказалось две, а
в разметке встретились два разных определения зоны B — 61.8–88.6% и
78.6–88.6%. Складывается это в одно: работали ДВЕ копии, из исходников и
собранная, одновременно, 23 дня подряд.

120 сигналов были взяты дважды:

    NEARUSDT  копия A  вход 08-07 07:00 по 1.63744  → -1.071R
    NEARUSDT  копия B  вход 08-07 07:25 по 1.64320  → -1.091R
              та же пара, тот же стоп, та же цель

Риск на идею оказался вдвое выше заявленного, а замеры — смесью двух опытов
с разными настройками.

ПОЧЕМУ ЗАМОК НЕ ПОМОГ. Он стережёт КАТАЛОГ ДАННЫХ, и это верно: копии с
разными папками не портят файлы друг другу и запускаться должны обе. Но
рынок у них один, и про это устройство ничего не знало.

ЧЕГО ЗДЕСЬ НЕТ. Запрета. Две копии бывают нужны — проверить новую сборку
рядом с рабочей. Дело кода показать, решение за человеком.
"""

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import single_instance                                    # noqa: E402


def _registry(monkeypatch, tmp_path):
    """Свой реестр на проверку: общий на машину трогать нельзя."""
    monkeypatch.setattr(single_instance, '_registry_dir',
                        lambda: str(tmp_path / 'instances'))


def _mark(tmp_path, name, pid, data_dir):
    d = tmp_path / 'instances'
    d.mkdir(parents=True, exist_ok=True)
    path = d / f'{name}.json'
    path.write_text(json.dumps({'pid': pid, 'data_dir': str(data_dir),
                                'started': 0}), encoding='utf-8')
    return path


class TestACopyMarksItself:

    def test_registering_creates_a_mark(self, tmp_path, monkeypatch):
        _registry(monkeypatch, tmp_path)
        mine = tmp_path / 'data'
        mine.mkdir()
        single_instance.register(str(mine))
        marks = list((tmp_path / 'instances').glob('*.json'))
        assert len(marks) == 1
        info = json.loads(marks[0].read_text(encoding='utf-8'))
        assert info['pid'] == os.getpid()

    def test_the_same_folder_keeps_one_mark(self, tmp_path, monkeypatch):
        """Перезапуск не должен плодить отметки: имя файла — от папки."""
        _registry(monkeypatch, tmp_path)
        mine = tmp_path / 'data'
        mine.mkdir()
        single_instance.register(str(mine))
        single_instance.register(str(mine))
        assert len(list((tmp_path / 'instances').glob('*.json'))) == 1

    def test_an_unwritable_registry_does_not_break_startup(self, monkeypatch):
        """Отметка — удобство, а не условие запуска."""
        monkeypatch.setattr(single_instance, '_registry_dir',
                            lambda: '\x00нельзя такой путь')
        single_instance.register('.')          # не бросает — этого и хотим


class TestSiblingsAreFound:

    def test_our_own_copy_is_not_a_sibling(self, tmp_path, monkeypatch):
        _registry(monkeypatch, tmp_path)
        mine = tmp_path / 'data'
        mine.mkdir()
        single_instance.register(str(mine))
        assert single_instance.siblings(str(mine)) == []

    def test_a_live_stranger_is_found(self, tmp_path, monkeypatch):
        """
        Настоящий чужой процесс, а не выдуманный: своим PID тут не обойтись —
        его код отбрасывает как собственную копию, и проверка прошла бы вхолостую.
        """
        _registry(monkeypatch, tmp_path)
        mine = tmp_path / 'data'
        mine.mkdir()
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            _mark(tmp_path, 'other', proc.pid, tmp_path / 'elsewhere')
            got = single_instance.siblings(str(mine))
            assert len(got) == 1 and got[0]['pid'] == proc.pid
            assert 'elsewhere' in got[0]['data_dir']
        finally:
            proc.kill()
            proc.wait()

    def test_a_dead_mark_is_not_a_copy(self, tmp_path, monkeypatch):
        """
        Отметки остаются после аварийного завершения. Считать их работающей
        копией — пугать человека призраком.
        """
        _registry(monkeypatch, tmp_path)
        mine = tmp_path / 'data'
        mine.mkdir()
        _mark(tmp_path, 'dead', 999_999, tmp_path / 'elsewhere')
        assert single_instance.siblings(str(mine)) == []

    def test_a_dead_mark_is_swept_away(self, tmp_path, monkeypatch):
        """Иначе список растёт с каждым падением и перестаёт что-то значить."""
        _registry(monkeypatch, tmp_path)
        mine = tmp_path / 'data'
        mine.mkdir()
        path = _mark(tmp_path, 'dead', 999_999, tmp_path / 'elsewhere')
        single_instance.siblings(str(mine))
        assert not path.exists()

    def test_the_same_folder_is_never_a_sibling(self, tmp_path, monkeypatch):
        """
        Ту же папку стережёт мьютекс. Отметка с чужим PID и нашей папкой —
        это мы сами после перезапуска, а не сосед.
        """
        _registry(monkeypatch, tmp_path)
        mine = tmp_path / 'data'
        mine.mkdir()
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            _mark(tmp_path, 'same', proc.pid, mine)
            assert single_instance.siblings(str(mine)) == []
        finally:
            proc.kill()
            proc.wait()

    def test_no_registry_is_not_an_error(self, tmp_path, monkeypatch):
        _registry(monkeypatch, tmp_path)
        assert single_instance.siblings(str(tmp_path / 'data')) == []

    def test_a_broken_mark_is_skipped(self, tmp_path, monkeypatch):
        _registry(monkeypatch, tmp_path)
        d = tmp_path / 'instances'
        d.mkdir()
        (d / 'junk.json').write_text('не json', encoding='utf-8')
        assert single_instance.siblings(str(tmp_path / 'data')) == []


class TestLivenessIsCheckedHonestly:

    def test_our_process_is_alive(self):
        assert single_instance._alive(os.getpid())

    def test_a_finished_process_is_not(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'])
        proc.wait()
        time.sleep(0.3)
        assert not single_instance._alive(proc.pid)

    def test_nonsense_is_not_alive(self):
        for bad in (0, -1, None):
            assert not single_instance._alive(bad)


class TestTheAppAndDiagnosticsUseIt:

    def test_startup_registers_the_copy(self):
        src = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
        spot = src.index('single_instance.mark_running(')
        assert 'single_instance.register(' in src[max(0, spot - 400):spot], (
            'копия не отмечается при запуске — соседей будет не видно')

    def test_diagnostics_has_the_check(self):
        import doctor
        assert any(fn is doctor.check_copies for _, fn in doctor.CHECKS), (
            'проверка есть, но в списке её нет — значит она не выполняется')

    def test_it_warns_and_never_fails(self, monkeypatch):
        """
        Две копии — повод посмотреть, а не отказ работать: рядом с рабочей
        держат новую сборку, и это законно.
        """
        import doctor
        monkeypatch.setattr(single_instance, 'siblings',
                            lambda _d: [{'pid': 4242, 'data_dir': 'C:\\где-то'}])
        r = doctor.check_copies()
        assert r['level'] == 'warn'
        assert '4242' in r['detail'] and 'где-то' in r['detail']
        assert 'дважды' in r['fix']

    def test_one_copy_reads_as_ok(self, monkeypatch):
        import doctor
        monkeypatch.setattr(single_instance, 'siblings', lambda _d: [])
        assert doctor.check_copies()['level'] == 'ok'

    def test_a_broken_check_does_not_break_diagnostics(self, monkeypatch):
        import doctor

        def boom(_d):
            raise OSError('реестр недоступен')

        monkeypatch.setattr(single_instance, 'siblings', boom)
        assert doctor.check_copies()['level'] == 'warn'
