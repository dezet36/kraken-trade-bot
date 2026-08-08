"""
Сбор данных о позиционировании участников: открытый интерес, соотношение
лонгов и шортов, ставки фандинга, премия перпа над индексом.

ЗАЧЕМ ЭТО ЕСТЬ. Это единственный доступный нам источник, который НЕ является
формой прошлой цены. Семь семейств ценовых паттернов за день дали один
пограничный выживший, и общее у них ровно одно: все предсказывают направление
по геометрии графика. Позиционирование отвечает на другой вопрос — кто и как
расставлен, — и потому может нести информацию там, где форма её не несёт.

ПОЧЕМУ СБОРЩИК, А НЕ ПРОСТО ЗАПРОС ПРИ ЗАМЕРЕ. Биржа отдаёт эти данные с
жёстким пределом ПО ЧИСЛУ ЗАПИСЕЙ, а не по времени:

    открытый интерес       200 записей: 8 дней при часовом шаге, 199 при суточном
    лонг/шорт              500 записей: 20 дней при часовом, 499 при суточном
    фандинг                пагинация работает, 400+ дней
    премия                 пагинация работает, около 50 дней проверено

Наши проверочные периоды — рост 2025-26 и падение 2022-23. Медвежий недостижим
НИ ОДНИМ источником, и даже суточный открытый интерес доходит только до января
2026. Значит двусторонняя приёмка, на которой держится весь проект, для этого
семейства сегодня невозможна.

Выход один: копить самим. Каждый час записи уходят в хранилище, и через два-три
месяца появится собственный набор, которого у биржи не купить. Чем позже
начать, тем позже семейство станет проверяемым, — поэтому сбор запускается
сразу, задолго до того, как появится сама стратегия.

ЧТО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ. Не торгует, не влияет на решения и не может уронить
бота: любая ошибка биржи или диска гасится и пишется в журнал. Он только
складывает данные.
"""

import json
import os
import time

import config
from logger import log

SOURCES = ('open_interest', 'long_short', 'funding', 'premium')

# Раз в час: шаг самих данных часовой, а история отдаётся с запасом в 8 дней —
# даже суточный перерыв не создаст дыры. Частить незачем, это только запросы.
INTERVAL_SEC = int(os.getenv('POSITIONING_INTERVAL_SEC', 3600))
# Сколько последних записей просить. С запасом: пропущенные часы добираются
# сами, а повторы отсеиваются по ключу.
LIMIT = int(os.getenv('POSITIONING_LIMIT', 200))

_last_run = 0.0
_seen = None            # {source: set((pair, timestamp))}


def store_dir():
    return os.path.join(config.DATA_DIR, 'positioning')


def path_for(source):
    return os.path.join(store_dir(), f'{source}.jsonl')


def _load_seen():
    """
    Ключи уже записанного, чтобы не плодить повторы.

    Читается один раз при первом сборе. Записи хранятся построчно, и разбор
    всего файла — плата за то, что дозапись не требует ни базы, ни блокировок.
    """
    seen = {source: set() for source in SOURCES}
    for source in SOURCES:
        path = path_for(source)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue          # оборванная строка — пропускаем
                    key = (row.get('pair'), row.get('ts'))
                    if key[0] and key[1]:
                        seen[source].add(key)
        except OSError as exc:
            log(f'⚠️ позиционирование: не читается {source} — {exc}')
    return seen


def _append(source, rows):
    """Дописывает новые строки. Возвращает сколько записано."""
    if not rows:
        return 0
    os.makedirs(store_dir(), exist_ok=True)
    try:
        with open(path_for(source), 'a', encoding='utf-8') as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError as exc:
        log(f'⚠️ позиционирование: не пишется {source} — {exc}')
        return 0
    return len(rows)


def _fetch(client, source, pair):
    """Сырые записи одного источника по одной паре в общем виде."""
    if source == 'open_interest':
        raw = client.fetch_open_interest_history(pair, '1h', limit=LIMIT)
        return [{'ts': r['timestamp'],
                 'value': r.get('openInterestAmount'),
                 'notional': r.get('openInterestValue')}
                for r in raw if r.get('timestamp')]
    if source == 'long_short':
        raw = client.fetch_long_short_ratio_history(pair, '1h', limit=LIMIT)
        return [{'ts': r['timestamp'], 'value': r.get('longShortRatio')}
                for r in raw if r.get('timestamp')]
    if source == 'funding':
        raw = client.fetch_funding_rate_history(pair, limit=LIMIT)
        return [{'ts': r['timestamp'], 'value': r.get('fundingRate')}
                for r in raw if r.get('timestamp')]
    if source == 'premium':
        raw = client.fetch_premium_index_ohlcv(pair, '1h', limit=LIMIT)
        # Свечи премии: берём закрытие — это и есть отклонение перпа от индекса
        # на конец часа.
        return [{'ts': row[0], 'value': row[4]}
                for row in raw if row and row[0]]
    return []


def collect(client, pairs=None, sources=SOURCES):
    """
    Один проход сбора. Возвращает {источник: сколько новых записей}.

    Ошибки НЕ поднимаются наверх: сбор данных не должен мешать торговле. Пара,
    по которой биржа промолчала, просто пропускается — в следующий час
    доберётся, запас истории это позволяет.
    """
    global _seen
    if _seen is None:
        _seen = _load_seen()

    pairs = pairs or config.TRADING_PAIRS_POOL
    written = {source: 0 for source in sources}

    for source in sources:
        fresh, failed, reason = [], [], ''
        for pair in pairs:
            try:
                rows = _fetch(client, source, pair)
            except Exception as exc:               # noqa: BLE001
                # Отказы КОПЯТСЯ и пишутся одной строкой на источник. Построчно
                # это давало 84 записи в журнал при недоступной бирже — каждый
                # час, — и настоящая причина тонула в повторах.
                failed.append(pair)
                reason = reason or str(exc)[:60]
                continue
            for row in rows:
                if row.get('value') is None:
                    continue
                key = (pair, int(row['ts']))
                if key in _seen[source]:
                    continue
                _seen[source].add(key)
                fresh.append({'pair': pair, 'ts': int(row['ts']),
                              **{k: v for k, v in row.items() if k != 'ts'}})
        if failed:
            log(f'   позиционирование: {source} не отдался по {len(failed)} '
                f'парам из {len(pairs)} — {reason}')
        written[source] = _append(source, fresh)
    return written


def collect_if_due(client, pairs=None):
    """
    Сбор, если пришло время. Вызывается из цикла бота и почти всегда молчит.

    Отдельного процесса не заводим сознательно: лишний процесс — это лишняя
    вещь, которая может незаметно умереть. Здесь сбор живёт ровно столько,
    сколько живёт бот.
    """
    global _last_run
    now = time.time()
    if now - _last_run < INTERVAL_SEC:
        return None
    _last_run = now
    try:
        written = collect(client, pairs)
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ позиционирование: сбор не удался — {exc}')
        return None
    total = sum(written.values())
    if total:
        log('📊 Позиционирование: записей добавлено — ' + ', '.join(
            f'{name} {count}' for name, count in written.items() if count))
    return written


def summary():
    """Сколько собрано и за какой срок — для диагностики в дашборде."""
    out = {}
    for source in SOURCES:
        path = path_for(source)
        if not os.path.exists(path):
            out[source] = {'rows': 0}
            continue
        rows, oldest, newest, pairs = 0, None, None, set()
        try:
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    stamp = row.get('ts')
                    if not stamp:
                        continue
                    rows += 1
                    pairs.add(row.get('pair'))
                    oldest = stamp if oldest is None else min(oldest, stamp)
                    newest = stamp if newest is None else max(newest, stamp)
        except OSError:
            pass
        out[source] = {'rows': rows, 'pairs': len(pairs),
                       'oldest': oldest, 'newest': newest,
                       'days': ((newest - oldest) / 86_400_000
                                if oldest and newest else 0)}
    return out
