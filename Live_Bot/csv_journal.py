"""
Дописать колонку в CSV, не сдвинув то, что уже записано.

В ЧЁМ ОПАСНОСТЬ. Шапка CSV пишется ОДИН РАЗ, при создании файла, а строки —
всегда по текущему списку колонок. Стоит добавить колонку, и в новых строках
значений становится больше, чем имён в заголовке: всё, что стоит после новой
колонки, съезжает влево. Файл при этом открывается, читается и выглядит
правдоподобно — просто в колонке «результат» оказывается баланс, а в
«комиссии» длительность. Глазом такое не ловится, а бьёт по единственному
источнику правды о торговле.

ПОЧЕМУ ОБЩИМ МОДУЛЕМ. Эта защита стояла только в бумажном журнале. Боевой её
не получил — и когда 30 августа 2026 в него добавили двенадцать колонок
издержек, все прежние строки сдвинулись бы молча. Чинили там, куда смотрели, а
второй путь тихо отстал: ровно так боевой журнал и отстал от бумажного на
двенадцать колонок.

Переносить функцию в третий и четвёртый файл (журнал отказов, наблюдение после
выхода) значило бы повторить это ещё дважды. Правило, написанное дважды,
расходится — в этом проекте так уже вышло с дневным стоп-краном. Поэтому оно
написано здесь один раз, а все четыре журнала им пользуются.
"""

import csv
import os

from logger import log


def migrate_header(path, columns, what='журнал'):
    """
    Приводит существующий CSV к текущему набору колонок.

    Ничего не делает, если файла нет, он пуст или шапка уже совпадает — то
    есть в обычной работе это одно чтение первой строки.

    При несовпадении файл переписывается целиком: старые строки получают
    пустое значение в новых колонках, а порядок значений выравнивается ПО
    ИМЕНАМ. Поэтому колонку можно добавлять и в середину списка, а не только
    в конец, — раньше это было нельзя.

    Возвращает число перенесённых строк (0, если переносить не понадобилось).
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    try:
        with open(path, encoding='utf-8-sig', newline='') as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames == list(columns):
                return 0
            rows = list(reader)
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ {what} не прочитан для переноса шапки: {exc}')
        return 0

    # Пишем рядом и подменяем: обрыв на середине не имеет права оставить нас
    # без файла. os.replace атомарен в пределах одной файловой системы.
    tmp = path + '.new'
    try:
        with open(tmp, 'w', encoding='utf-8', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=list(columns),
                                    extrasaction='ignore')
            writer.writeheader()
            for old in rows:
                writer.writerow({name: old.get(name, '') for name in columns})
        os.replace(tmp, path)
        log(f'   {what}: перенесён на новый набор колонок '
            f'({len(rows)} строк сохранено)')
        return len(rows)
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ Не удалось перенести шапку ({what}): {exc}')
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0


def append(path, columns, rows, what='журнал'):
    """
    Дописывает строки, сперва выровняв шапку. Возвращает True при успехе.

    Одна дверь на запись: если бы вызов migrate_header оставался на совести
    каждого места записи, его рано или поздно забыли бы ровно в том журнале,
    куда только что добавили колонку.
    """
    if not rows:
        return True
    try:
        migrate_header(path, columns, what)
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, 'a', encoding='utf-8', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=list(columns),
                                    extrasaction='ignore')
            if fresh:
                writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return True
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ Запись не удалась ({what}): {exc}')
        return False
