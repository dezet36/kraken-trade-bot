"""
Файлы состояния не имеют права запрещать обновление.

ОТКУДА ЭТО. На сервере пропало автообновление. Разбор на копии репозитория
показал механизм целиком:

    running_app.json отслеживался и переписывается при КАЖДОМ запуске
      → git видит изменение отслеживаемого файла
      → dirty_tracked() возвращает его
      → can_update = False
      → панель вместо кнопки пишет «есть незакоммиченные правки кода»

Воспроизведено дословно: даже прямая перемотка падает с «Your local changes to
the following files would be overwritten by merge. Aborting».

Замкнутый круг: из репозитория файлы уже выведены, но сервер к этой правке не
придёт сам — чтобы её забрать, надо обновиться, а мешают ровно они. Поэтому
обновлятор разбирается с ними без посторонней помощи.

ВТОРОЙ ДЕФЕКТ, НАЙДЕННЫЙ ПО ДОРОГЕ. Путь брался срезом line[3:]. Формат
porcelain — два знака состояния, пробел, путь, и срез верен. Но `_git` обрезает
вывод целиком, и ведущий пробел теряет ПЕРВАЯ строка — только она. У неё срез
откусывал лишнюю букву: «ive_Bot/running_app.json». Пока список просто
показывали, это выглядело опечаткой; стоило начать сверять по нему — и сверка
перестала совпадать.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import updater                                              # noqa: E402


class TestThePathIsReadWhateverTheIndent:

    def test_the_first_line_keeps_its_first_letter(self):
        """
        РОВНО ТОТ ДЕФЕКТ: у первой строки нет ведущего пробела, потому что
        вывод обрезан целиком.
        """
        assert (updater._porcelain_path('M Live_Bot/running_app.json')
                == 'Live_Bot/running_app.json')

    def test_a_normal_line_is_read_the_same(self):
        assert (updater._porcelain_path(' M Live_Bot/updater.py')
                == 'Live_Bot/updater.py')

    def test_both_status_letters(self):
        assert updater._porcelain_path('MM a/b.py') == 'a/b.py'

    def test_a_rename_gives_the_new_name(self):
        """Старое имя уже не существует — следить надо за новым."""
        assert updater._porcelain_path('R  old.py -> new.py') == 'new.py'

    def test_an_empty_line_is_not_a_file(self):
        assert updater._porcelain_path('') == ''

    def test_rubbish_is_not_a_file(self):
        assert updater._porcelain_path('просто текст') == ''


class TestRuntimeFilesAreNotCodeChanges:

    def test_the_two_offenders_are_listed(self):
        assert 'Live_Bot/running_app.json' in updater.RUNTIME_FILES
        assert 'Live_Bot/window.json' in updater.RUNTIME_FILES

    def test_they_do_not_count_as_dirty(self, monkeypatch):
        """
        Ровно то, из-за чего кнопка обновления пропала: изменённый файл
        состояния считался правкой кода.
        """
        monkeypatch.setattr(updater, '_git', lambda *a, **k: (
            0, 'M Live_Bot/running_app.json\n M Live_Bot/window.json', ''))
        assert updater.dirty_tracked() == []

    def test_real_code_changes_still_count(self, monkeypatch):
        """Предохранитель обязан остаться: правку кода обновление не затрёт."""
        monkeypatch.setattr(updater, '_git', lambda *a, **k: (
            0, 'M Live_Bot/running_app.json\n M Live_Bot/bot.py', ''))
        assert updater.dirty_tracked() == ['Live_Bot/bot.py']

    def test_a_broken_git_is_not_read_as_clean(self, monkeypatch):
        monkeypatch.setattr(updater, '_git', lambda *a, **k: (128, '', 'ошибка'))
        assert updater.dirty_tracked() == []


class TestTheyAreDroppedBeforeTheMerge:
    """
    Не считаться правкой кода — половина дела. Изменённый отслеживаемый файл
    останавливает саму перемотку, что бы о нём ни думал наш код.
    """

    def test_only_tracked_ones_are_touched(self, monkeypatch):
        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            if args[0] == 'ls-files':
                # Отслеживается только первый.
                return (0, args[-1], '') if 'running_app' in args[-1] else (0, '', '')
            return (0, '', '')

        monkeypatch.setattr(updater, '_git', fake)
        assert updater._drop_runtime_changes() == ['Live_Bot/running_app.json']
        assert ('checkout', '--', 'Live_Bot/window.json') not in calls

    def test_a_failed_checkout_is_not_reported_as_dropped(self, monkeypatch):
        def fake(*args, **kwargs):
            if args[0] == 'ls-files':
                return 0, args[-1], ''
            return 1, '', 'не вышло'
        monkeypatch.setattr(updater, '_git', fake)
        assert updater._drop_runtime_changes() == []

    def test_the_drop_happens_before_the_merge(self):
        src = open(os.path.join(ROOT, 'updater.py'), encoding='utf-8').read()
        spot = src.index('def apply(')
        body = src[spot:src.index('\ndef ', spot + 10)]
        assert '_drop_runtime_changes()' in body, (
            'перемотка снова спотыкается о файлы состояния')
        assert body.index('_drop_runtime_changes()') < body.index("'merge'")


class TestTradingDataIsStillProtected:
    """
    Отбрасывать состояние окна безопасно. Отбрасывать журнал сделок — нет, и
    послабление не должно расползтись на данные торговли.
    """

    def test_no_trading_file_is_in_the_drop_list(self):
        for name in updater.DATA_FILES:
            assert name not in updater.RUNTIME_FILES, name

    def test_the_data_guard_is_untouched(self):
        assert 'Live_Bot/paper_trades.csv' in updater.DATA_FILES
        assert 'Live_Bot/.env' in updater.DATA_FILES
