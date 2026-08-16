"""
Новая колонка в журнале не имеет права сдвинуть старые значения.

ОТКУДА ЭТО. Шапка CSV пишется ОДИН РАЗ, при создании файла, а строки — всегда
по текущему COLUMNS. Стоит добавить колонку в середину списка (так появилась
`data_gap_min` после `duration_min`), и в новых строках значений становится на
одно больше, чем в заголовке: всё, что стоит после новой колонки, съезжает.

Файл при этом открывается, читается и выглядит правдоподобно — просто в
колонке «комиссия» оказывается длительность, а в «результате» баланс. Глазом
такое не ловится, а бьёт по единственному источнику правды о торговле.
"""

import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def broker(tmp_path, monkeypatch):
    """Прежние модули возвращаются на место — см. фикстур в test_candle_gap."""
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    saved = {m: sys.modules.pop(m, None) for m in ('config', 'paper_broker')}
    import paper_broker
    yield paper_broker
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def write_old_file(path, columns, rows):
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class TestAnOlderFileIsBroughtForward:

    def test_the_header_is_replaced(self, broker):
        old = [c for c in broker.COLUMNS if c != 'data_gap_min']
        write_old_file(broker.JOURNAL_CSV, old,
                       [{c: c.upper() for c in old}])

        broker._migrate_journal_header()

        with open(broker.JOURNAL_CSV, encoding='utf-8-sig', newline='') as fh:
            assert csv.DictReader(fh).fieldnames == broker.COLUMNS

    def test_old_values_stay_under_their_own_names(self, broker):
        """
        САМОЕ ВАЖНОЕ. Перенос обязан сохранить соответствие имя-значение, а не
        просто число колонок: съехавший журнал — это и есть та порча, ради
        которой всё написано.
        """
        old = [c for c in broker.COLUMNS if c != 'data_gap_min']
        sample = {c: f'знач-{c}' for c in old}
        write_old_file(broker.JOURNAL_CSV, old, [sample])

        broker._migrate_journal_header()

        with open(broker.JOURNAL_CSV, encoding='utf-8-sig', newline='') as fh:
            got = next(iter(csv.DictReader(fh)))
        for name in old:
            assert got[name] == sample[name], (
                f'колонка {name} съехала: было {sample[name]!r}, '
                f'стало {got[name]!r}')

    def test_the_new_column_is_empty_for_old_rows(self, broker):
        """Старые сделки о своей дыре не знают — и врать за них нельзя."""
        old = [c for c in broker.COLUMNS if c != 'data_gap_min']
        write_old_file(broker.JOURNAL_CSV, old, [{c: '1' for c in old}])
        broker._migrate_journal_header()
        with open(broker.JOURNAL_CSV, encoding='utf-8-sig', newline='') as fh:
            assert next(iter(csv.DictReader(fh)))['data_gap_min'] == ''

    def test_every_row_survives(self, broker):
        old = [c for c in broker.COLUMNS if c != 'data_gap_min']
        write_old_file(broker.JOURNAL_CSV, old,
                       [{**{c: '0' for c in old}, 'trade_id': str(i)}
                        for i in range(1, 24)])
        broker._migrate_journal_header()
        with open(broker.JOURNAL_CSV, encoding='utf-8-sig', newline='') as fh:
            rows = list(csv.DictReader(fh))
        assert [r['trade_id'] for r in rows] == [str(i) for i in range(1, 24)]


class TestItDoesNotTouchWhatIsAlreadyRight:

    def test_a_current_file_is_left_alone(self, broker):
        write_old_file(broker.JOURNAL_CSV, broker.COLUMNS,
                       [{c: '7' for c in broker.COLUMNS}])
        before = open(broker.JOURNAL_CSV, encoding='utf-8').read()
        broker._migrate_journal_header()
        assert open(broker.JOURNAL_CSV, encoding='utf-8').read() == before

    def test_no_file_is_not_an_error(self, broker):
        if os.path.exists(broker.JOURNAL_CSV):
            os.remove(broker.JOURNAL_CSV)
        broker._migrate_journal_header()          # не должно бросать

    def test_no_leftover_temp_file(self, broker):
        old = [c for c in broker.COLUMNS if c != 'data_gap_min']
        write_old_file(broker.JOURNAL_CSV, old, [{c: '1' for c in old}])
        broker._migrate_journal_header()
        assert not os.path.exists(broker.JOURNAL_CSV + '.new')


class TestWritingAfterMigrationLinesUp:

    def test_a_new_trade_lands_in_the_right_columns(self, broker):
        """
        Сквозная проверка: старый файл, потом настоящая запись сделки. Именно
        здесь и проявился бы сдвиг.
        """
        old = [c for c in broker.COLUMNS if c != 'data_gap_min']
        write_old_file(broker.JOURNAL_CSV, old,
                       [{**{c: '' for c in old}, 'trade_id': '1',
                         'pair': 'СТАРАЯ', 'result': 'WIN'}])

        broker._write_journal({**{c: '' for c in broker.COLUMNS},
                               'trade_id': '2', 'pair': 'НОВАЯ',
                               'duration_min': 42, 'data_gap_min': 193 * 60,
                               'result': 'LOSS'})

        with open(broker.JOURNAL_CSV, encoding='utf-8-sig', newline='') as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 2
        assert rows[0]['pair'] == 'СТАРАЯ' and rows[0]['result'] == 'WIN'
        assert rows[1]['pair'] == 'НОВАЯ' and rows[1]['result'] == 'LOSS', (
            'значения новой строки съехали относительно шапки')
        assert rows[1]['duration_min'] == '42'
        assert rows[1]['data_gap_min'] == str(193 * 60)
