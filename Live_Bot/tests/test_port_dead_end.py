"""
Занятый порт не отправляет человека в диспетчер задач.

ОТКУДА ЭТО. Окно, которое увидел пользователь при перезапуске:

    Порт 8787 занят: python.exe (PID 19520).
    Прежнюю копию закрыть не удалось — снимите её вручную
    (диспетчер задач → Подробности) и запустите снова.

То есть бот оказался в состоянии, из которого его вытаскивает человек.

ПОЧЕМУ ЭТО БЫЛО НЕОБОСНОВАНО. Решение брать соседний порт стояло за условием
`0 if ours`: раз порт держит наша копия, уходить нельзя — «две копии на одних
файлах затрут работу друг друга».

Но к этому месту замок нашего каталога данных УЖЕ У НАС: `acquire` отработал
выше, и без него до проверки порта дело не доходит вовсе. Значит держатель
порта с нашими файлами не работает. Опасение было про общие ДАННЫЕ, а условие
проверяло общее ПРИЛОЖЕНИЕ — это разные вещи, и совпадают они не всегда: копия
из dist/ работает с другим каталогом, а `is_our_bot` всё равно скажет «наша».

Хуже того, `is_our_bot` считает своим любой python.exe без командной строки —
на свежих Windows её нечем добыть. Посторонний питон на 8787 запирал запуск
намертво.

ЧТО ТЕПЕРЬ. Остаёмся на месте, только если держатель — та самая копия, что
записала себя в наш running_app.json, и она жива. Во всех остальных случаях
берём соседний порт и работаем.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import desktop                                             # noqa: E402


class TestOnlySharedDataKeepsUsPut:

    def test_a_stranger_does_not_share_our_data(self, monkeypatch):
        monkeypatch.setattr(desktop, 'our_recorded_pid', lambda: 4242)
        assert not desktop.shares_our_data({'pid': 19520, 'name': 'python.exe'})

    def test_our_own_recorded_copy_does(self, monkeypatch):
        """Ту, что записала себя в НАШ running_app.json, обходить нельзя."""
        monkeypatch.setattr(desktop, 'our_recorded_pid', lambda: os.getpid())
        assert desktop.shares_our_data({'pid': os.getpid()})

    def test_a_dead_recorded_copy_does_not(self, monkeypatch):
        """
        Запись остаётся после аварийного завершения. Мёртвая копия ничего не
        затрёт, а порт освободится сам.
        """
        monkeypatch.setattr(desktop, 'our_recorded_pid', lambda: 999_999)
        assert not desktop.shares_our_data({'pid': 999_999})

    def test_no_holder_is_not_shared_data(self):
        for holder in ({}, None, {'name': 'python.exe'}):
            assert not desktop.shares_our_data(holder)

    def test_no_record_means_not_ours(self, monkeypatch):
        """Записи нет — судить не по чему, и запирать запуск не за что."""
        monkeypatch.setattr(desktop, 'our_recorded_pid', lambda: 0)
        assert not desktop.shares_our_data({'pid': 19520})


class TestTheDecisionAsksTheRightQuestion:
    """
    Разница между `is_our_bot` и `shares_our_data` — вся суть правки, поэтому
    она проверяется прямо на исходнике: подмена одного другим вернула бы
    тупик, и никакая проверка поведения этого бы не поймала.
    """

    SRC = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()

    def _decision(self):
        spot = self.SRC.index('moved = 0 if ')
        return self.SRC[spot:spot + 200]

    def test_the_move_is_not_blocked_by_mere_ownership(self):
        """РОВНО ТОТ ДЕФЕКТ: `moved = 0 if ours else ...`."""
        block = self._decision()
        assert 'if ours' not in block, (
            'соседний порт снова закрыт для любой нашей копии — человек '
            'опять пойдёт в диспетчер задач')

    def test_the_move_is_blocked_only_by_shared_data(self):
        assert 'shares_our_data(holder)' in self._decision()

    def test_the_manual_message_is_for_shared_data_only(self):
        spot = self.SRC.index('диспетчер задач → Подробности')
        assert 'shares_our_data(holder)' in self.SRC[spot:spot + 400], (
            'совет «снимите вручную» снова показывается не только тем, у кого '
            'общие данные')

    def test_the_two_questions_stay_separate(self):
        """
        `is_our_bot` решает, можно ли ЗАКРЫВАТЬ держателя, и остаётся нужной.
        Если её удалить, закрывать начнём кого попало.
        """
        assert 'def is_our_bot(' in self.SRC
        assert 'def shares_our_data(' in self.SRC
        assert 'ours = is_our_bot(' in self.SRC


class TestTheMessageTellsTheTruth:

    SRC = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()

    def test_a_sibling_copy_is_not_called_a_stranger(self):
        """
        Уходя на соседний порт, раньше всегда писали «это не наш бот». Теперь
        порт может держать и наша копия с другим каталогом — врать про неё
        нельзя.
        """
        spot = self.SRC.index('whose = (')
        block = self.SRC[spot:spot + 400]
        assert 'if not ours' in block
        assert 'другая копия бота' in block

    def test_the_stranger_wording_survives(self):
        assert 'Это не наш бот, и закрывать его я не стану.' in self.SRC


class TestTheOldReasoningIsGone:

    def test_the_stale_comment_does_not_contradict_the_code(self):
        """
        Комментарий объяснял отказ уходить на соседний порт тем, что «данные
        при этом общие с прежней копией». После правки это неверно, а
        комментарий, описывающий несуществующее поведение, хуже отсутствующего.
        """
        src = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
        assert 'Данные при этом общие с прежней копией' not in src
