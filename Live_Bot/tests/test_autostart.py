r"""
Автозапуск: бот должен работать, а не ждать, пока о нём вспомнят.

ОТКУДА ЭТО. Замер по журналу за 12 суток: бот работал 55 часов из 290 — 19%
времени, 23 перерыва, два по 40 и 94 часа. Следом случился ещё один, на девять
суток. Приложение оконное, живёт ровно столько, сколько открыто окно, а
автозапуска не было ни в каком виде.

Цена измерима. Частота сетапов, снятая прогоном настоящего кода стратегий по
9 400 проверкам: LEVELS 0.9 в сутки, RSIBB 2.6 в сутки на 21 паре. При 19%
времени это один раз в 6 суток и один раз в 2 суток — отсюда ноль сделок у
обеих за всю историю наблюдений. Время работы оказалось сильнее любой правки
в логике стратегий.

ПОЧЕМУ ОТДЕЛЬНО ОТ install_service.ps1. Тот ставит venv и уводит данные в
bot_data\, то есть заводит ТРЕТЬЮ папку данных рядом с Live_Bot\ и dist\.
Журнал сделок уже был расколот надвое, и разбор по половине однажды дал
уверенный неверный ответ.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

def _script():
    """
    Путь к установщику. ФУНКЦИЯ, А НЕ ЗАГЛАВНАЯ КОНСТАНТА, и причина есть.

    conftest изолирует файлы данных: он подменяет любое заглавное строковое
    поле модуля, указывающее в папку бота, на путь во временный каталог. Это
    верно для журналов и состояния — и неверно для исходников. Константа
    SCRIPT под это правило попадала, и проверка искала .ps1 в tmp-папке.
    """
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'install_autostart.ps1')


def _source():
    return open(_script(), encoding='utf-8-sig').read()


def _code_only(text):
    """Скрипт без комментариев: в них мы объясняем, чего НЕ делаем."""
    return '\n'.join(ln for ln in text.splitlines()
                     if not ln.strip().startswith('#'))


class TestTheInstallerExists:

    def test_the_script_is_there(self):
        assert os.path.exists(_script())

    def test_it_starts_with_a_bom(self):
        """
        Windows PowerShell 5.1 читает .ps1 как ANSI, если нет BOM. Кириллица в
        комментариях при этом ломает РАЗБОР, а не только вывод: скрипт падает с
        «The string is missing the terminator» на строке, где всё в порядке.
        Проверено — именно так он и не запустился с первого раза.
        """
        assert open(_script(), 'rb').read(3) == b'\xef\xbb\xbf'

    def test_every_powershell_script_keeps_its_bom(self):
        """Правило общее: соседи с BOM, и новый не должен быть исключением."""
        for name in ('build_exe.ps1', 'install_service.ps1', 'install_autostart.ps1'):
            path = os.path.join(ROOT, name)
            if os.path.exists(path):
                assert open(path, 'rb').read(3) == b'\xef\xbb\xbf', name


class TestItLaunchesTheBuiltApp:

    SRC = _source()

    def test_it_runs_the_exe_not_the_source(self):
        """
        install_service.ps1 запускает bot.py через venv. Для настольного
        приложения это другой запуск с другой папкой данных.
        """
        assert 'Kraken.exe' in self.SRC
        assert 'bot.py' not in self.SRC

    def test_it_does_not_move_the_data_directory(self):
        """
        Данные обязаны остаться там, где их пишет exe. Третья папка расколола
        бы журнал ещё раз.
        """
        code = _code_only(self.SRC)
        assert 'BOT_DATA_DIR' not in code
        assert 'bot_data' not in code

    def test_the_working_directory_is_next_to_the_exe(self):
        assert '-WorkingDirectory' in self.SRC

    def test_it_refuses_without_a_build(self):
        """Задача, запускающая несуществующий файл, молча ничего не делает."""
        assert 'Test-Path $exe' in self.SRC and 'throw' in self.SRC


class TestTheTaskSurvivesRealLife:

    SRC = _source()

    def test_it_waits_before_starting(self):
        """
        Сразу после входа в систему сеть чаще всего ещё поднимается, и первый
        цикл уходит в отказы подключения к бирже.
        """
        assert "Delay = 'PT1M'" in self.SRC

    def test_a_laptop_on_battery_keeps_trading(self):
        """Позиции в рынке ведутся, пока работает бот. Батарея — не повод."""
        assert '-AllowStartIfOnBatteries' in self.SRC
        assert '-DontStopIfGoingOnBatteries' in self.SRC

    def test_there_is_no_time_limit(self):
        """
        Значение по умолчанию — трое суток. Задача по смыслу бессрочная, и
        предел убивал бы её посреди недели.
        """
        assert 'ExecutionTimeLimit ([TimeSpan]::Zero)' in self.SRC

    def test_a_crash_is_retried(self):
        assert '-RestartCount' in self.SRC

    def test_it_does_not_ask_for_administrator(self):
        """
        Повышение прав требует подтверждения UAC при каждом входе — то есть
        автозапуск, который не запускается сам.
        """
        assert '-RunLevel Limited' in self.SRC


class TestItCanBeUndone:

    SRC = _source()

    def test_removal_is_supported(self):
        assert 'param([switch]$Remove)' in self.SRC
        assert 'Unregister-ScheduledTask' in self.SRC

    def test_reinstall_does_not_pile_up_tasks(self):
        """Повторная установка обязана заменить задачу, а не добавить вторую."""
        spot = self.SRC.rindex('Register-ScheduledTask -TaskName')
        assert 'Unregister-ScheduledTask' in self.SRC[:spot]

    def test_the_way_out_is_printed(self):
        assert '-Remove' in self.SRC.split('Write-Host')[-1]


class TestTwoCopiesCannotRun:
    """
    Автозапуск и ручной запуск встретятся однажды обязательно. Два бота на
    одном paper_state.json затрут работу друг друга, а журнал сделок — то, на
    чём стоят все замеры.
    """

    def test_the_lock_exists_and_is_keyed_by_data_dir(self):
        import single_instance
        assert hasattr(single_instance, 'acquire')
        a = single_instance._mutex_name(r'C:\one')
        b = single_instance._mutex_name(r'C:\two')
        assert a != b, 'замок должен различать разные папки данных'

    def test_the_same_directory_gives_the_same_lock(self):
        import single_instance
        assert (single_instance._mutex_name(r'C:\same')
                == single_instance._mutex_name(r'c:\SAME'))
