"""
Запуск окном приложения не тащит за собой чёрное окно терминала.

ОТКУДА ЭТО. Человек запустил приложение и увидел рядом с окном консоль
Python. Она не закрывается: закроешь — уйдёт и бот.

Разбор показал, что правильные способы уже были, а один — нет:

    dist\\Kraken.exe          --windowed, консоли нет            ✓
    ярлык рабочего стола     install_desktop.ps1 → pythonw.exe  ✓
    run.ps1 -Desktop         python.exe → КОНСОЛЬ               ✗

Ключ -Desktop означает «окном приложения», и консоль рядом с ним —
противоречие самому ключу. pythonw.exe — тот же интерпретатор без консоли.

ПЛАТА ЗА ЭТО ОДНА, И ОНА СЕРЬЁЗНАЯ: молчаливый отказ. Не поднимись
приложение — показать ошибку станет некому, ни консоли, ни окна. Поэтому
stderr уводится в файл, а скрипт ждёт три секунды и, если процесс уже умер,
показывает этот файл сам.

ВТОРОЕ, НАЙДЕННОЕ ПРИ ПРОВЕРКЕ. run.ps1 выходил с «Окружение не создано»,
если рядом нет venv. На машине, где бот работает системным Python, скрипт был
попросту нерабочим: venv никто не создавал, потому что он не нужен.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN = os.path.join(ROOT, 'run.ps1')


def _text():
    with open(RUN, encoding='utf-8-sig') as fh:
        return fh.read()


class TestTheWindowedLaunchHasNoConsole:

    def test_the_launcher_exists(self):
        assert os.path.exists(RUN)

    def test_desktop_mode_uses_pythonw(self):
        """РОВНО ТОТ ДЕФЕКТ: -Desktop запускался консольным python.exe."""
        src = _text()
        spot = src.index('if ($Desktop)')
        block = src[spot:src.index('# ── В консоли', spot)]
        assert 'pythonw' in block, (
            'окно приложения снова запускается консольным интерпретатором')

    def test_it_starts_a_separate_process(self):
        """
        Иначе окно самого run.ps1 остаётся висеть и держит процесс — то есть
        консоль никуда не делась, просто стала называться иначе.
        """
        src = _text()
        spot = src.index('if ($Desktop)')
        block = src[spot:src.index('# ── В консоли', spot)]
        assert 'Start-Process' in block
        assert 'exit 0' in block

    def test_console_mode_keeps_its_console(self):
        """
        Без ключа -Desktop консоль и есть смысл запуска: там на экране лог.
        Заменить python.exe на pythonw там значило бы выключить единственное,
        ради чего этот режим существует.
        """
        src = _text()
        tail = src[src.index('# ── В консоли'):]
        assert 'bot.py' in tail
        assert 'pythonw' not in tail


class TestASilentFailureIsStillVisible:
    """
    Главная плата за pythonw: упавшее приложение не может пожаловаться.
    """

    def test_stderr_goes_to_a_file(self):
        assert 'RedirectStandardError' in _text()

    def test_the_script_waits_and_checks(self):
        src = _text()
        assert 'HasExited' in src
        assert 'Start-Sleep' in src

    def test_the_error_is_shown_to_the_human(self):
        src = _text()
        spot = src.index('HasExited')
        block = src[spot:spot + 900]
        assert 'Get-Content' in block, 'файл ошибки пишется, но никем не читается'
        assert 'ExitCode' in block

    def test_a_stale_log_is_cleared_first(self):
        """
        Иначе при следующем падении покажется позапрошлая ошибка — и человек
        будет чинить не то.
        """
        src = _text()
        assert 'Remove-Item $log' in src


class TestAMissingVenvIsNotAWall:

    def test_it_no_longer_refuses_to_start(self):
        # Только код: в комментарии прежний текст приведён дословно — там ему
        # и место, он объясняет, что именно было не так.
        code = '\n'.join(l for l in _text().splitlines()
                         if not l.lstrip().startswith('#'))
        assert 'Окружение не создано. Сначала' not in code

    def test_it_falls_back_to_system_python(self):
        src = _text()
        assert 'Get-Command python' in src

    def test_it_says_which_python_it_took(self):
        """Молча подменять интерпретатор нельзя: пакеты могут быть не те."""
        assert 'системным Python' in _text()

    def test_no_python_at_all_is_still_an_honest_error(self):
        src = _text()
        assert 'Python не найден' in src


class TestTheOtherLaunchPathsStayClean:

    def test_the_built_exe_is_windowed(self):
        wf = os.path.join(ROOT, '.github', 'workflows', 'build-exe.yml')
        assert '--windowed' in open(wf, encoding='utf-8').read()

    def test_the_desktop_shortcut_uses_pythonw(self):
        p = os.path.join(ROOT, 'Live_Bot', 'install_desktop.ps1')
        src = open(p, encoding='utf-8-sig').read()
        spot = src.index('$lnk.TargetPath')
        assert 'pythonw' in src[:spot], 'ярлык снова ведёт на консольный python'

    def test_the_script_is_readable_by_windows_powershell(self):
        """
        Windows PowerShell 5.1 без BOM читает файл как ANSI, и кириллица в
        комментариях ломает разбор. Проверено на install_autostart.ps1 —
        скрипт не запускался вовсе.
        """
        with open(RUN, 'rb') as fh:
            head = fh.read(3)
        assert head == b'\xef\xbb\xbf', 'нет BOM — PowerShell 5.1 не прочитает кириллицу'
