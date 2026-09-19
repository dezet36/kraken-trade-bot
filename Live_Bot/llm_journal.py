"""
Журнал разборов модели: что она увидела и чем кончила — на каждый вызов.

ЗАЧЕМ ОТДЕЛЬНЫЙ ЖУРНАЛ, КОГДА ЕСТЬ ДВА ДРУГИХ. Журнал сделок записывает только
то, что модель ОДОБРИЛА, — а одобряет она одно решение из двадцати. Журнал
отказов записывает имя правила и двести знаков объяснения, чего хватает
предохранителю с числами и совершенно не хватает разбору на полстраницы.

Между ними пропадало главное. Модель тратит на один сетап две-четыре минуты
процессора, и весь продукт этих минут — разбор: какой режим рынка она увидела,
какие уровни прочла, что посчитала противоречием, чего ждала перед входом.
Осмысленность этого разбора и есть единственный способ узнать, стоит ли пятая
стратегия своего счёта, ПОКА сделок ещё нет. 19 сентября 2026 модель сделала
123 вызова и ни одной сделки: по журналу сделок она не существовала вовсе.

ПОЧЕМУ ЗДЕСЬ ЖЕ ТОКЕНЫ И СЕКУНДЫ. Тот же день показал, чем оборачивается их
отсутствие в записи: 119 ответов подряд обрывались на границе окна контекста,
и понять это можно было только сопоставив «1703 вход» из лога бота с окном
2048 из настроек. Сопоставлять было негде. Теперь вопрос, ответ, предел, окно
и причина остановки лежат в одной строке с самим разбором.

ФАЙЛ ОБЩИЙ ДЛЯ БУМАГИ И БОЯ, поэтому первой колонкой идёт режим — иначе две
выборки смешаются без возможности разделить, как уже смешались журналы двух
одновременно работавших копий бота.
"""

import os
from datetime import datetime, timezone

import config
import csv_journal
from logger import log

CSV_PATH = os.path.join(config.DATA_DIR, 'llm_calls.csv')

COLUMNS = [
    'mode', 'at', 'pair', 'donor',
    # Чем кончилось: 'enter' — модель предложила сделку и та прошла проверки,
    # 'skip' — отказ. Имя отказа в gate, его числа в detail.
    'decision', 'gate', 'detail',
    # Что модель разглядела. Эти четыре колонки и есть смысл файла.
    'regime', 'analysis', 'bias', 'trigger', 'alt',
    'why', 'risk',
    # Решение в числах. Пусто, когда модель отказалась: числа появляются
    # только у сетапа, дошедшего до проверок.
    'side', 'entry', 'stop', 'tp1', 'inval',
    'rr', 'ev', 'p', 'cost_r', 'votes', 'confluence',
    # Второе мнение — только у планов «войти»: confirm / reject / broken,
    # возражения и самое сильное из них. Пусто у отказов аналитика.
    'critic', 'critic_issues', 'critic_worst',
    # Препятствия между входом и первой целью, посчитанные кодом (плиты,
    # встречные зоны, пулы, оценочные ликвидации). Только у планов «войти».
    'obstacles',
    # Сырой ответ модели, до 6000 знаков. Появился после того, как первый
    # обрезанный ответ «войти» нельзя было разобрать: журнал хранил только
    # хвост в 80 знаков, и куда ушли 1200 токенов, оставалось гадать.
    'raw',
    # Цена разбора и его границы — см. шапку модуля. При вызове критика
    # токены и finish — его вызова (последнего), seconds — обоих вместе.
    'model', 'ctx', 'prompt_tokens', 'answer_tokens', 'limit', 'finish',
    'seconds',
]


def _first(values):
    """Первая цель. Их бывает три, а в колонке помещается одна."""
    try:
        return values[0]
    except (IndexError, TypeError):
        return ''


def record(pair, donor, verdict, stats=None):
    """
    Пишет один разбор. Молча: наблюдение не имеет права мешать торговле.

    Пишется КАЖДЫЙ вызов, включая поломки разбора. Именно они и оказались тем,
    что нужно было увидеть: сто девятнадцать одинаковых отказов подряд — это
    не свойство рынка, а неисправность, и в журнале она должна быть видна
    строкой, а не выводиться из отсутствия строк.
    """
    try:
        verdict = verdict or {}
        stats = stats or {}
        confluence = verdict.get('confluence') or {}
        row = {
            'mode': config.TRADING_MODE,
            'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'pair': pair,
            'donor': donor or '',
            'decision': 'enter' if verdict.get('ok') else 'skip',
            'gate': verdict.get('gate', ''),
            'detail': str(verdict.get('detail', ''))[:300],
            'regime': verdict.get('regime', ''),
            'analysis': verdict.get('analysis', ''),
            'bias': verdict.get('bias', ''),
            'trigger': verdict.get('trigger', ''),
            'alt': verdict.get('alt', ''),
            'why': verdict.get('why', ''),
            'risk': verdict.get('risk', ''),
            'side': verdict.get('side', ''),
            'entry': verdict.get('entry', ''),
            'stop': verdict.get('stop', ''),
            'tp1': _first(verdict.get('targets')),
            'inval': verdict.get('inval', ''),
            'rr': verdict.get('rr', ''),
            'ev': verdict.get('ev', ''),
            'p': verdict.get('p', ''),
            'cost_r': verdict.get('cost_r', ''),
            'votes': verdict.get('votes', ''),
            # Списком имён, а не пятью колонками: набор факторов меняется
            # вместе с промтом, и колонки пришлось бы переименовывать следом.
            'confluence': ','.join(name for name, yes in confluence.items() if yes),
            'critic': (verdict.get('critic') or {}).get('verdict', ''),
            'critic_issues': (verdict.get('critic') or {}).get('issues', ''),
            'critic_worst': (verdict.get('critic') or {}).get('worst', ''),
            'obstacles': '; '.join(verdict.get('obstacles') or []),
            'raw': str(verdict.get('raw') or '')[:6000],
            'model': stats.get('model', ''),
            'ctx': stats.get('ctx', ''),
            'prompt_tokens': stats.get('prompt_tokens', ''),
            'answer_tokens': stats.get('answer_tokens', ''),
            'limit': stats.get('limit', ''),
            'finish': stats.get('finish', ''),
            'seconds': stats.get('seconds', ''),
        }
        csv_journal.append(CSV_PATH, COLUMNS, [row], 'журнал разборов ИИ')
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ Разбор ИИ не записан: {exc}')


def last(limit=40, mode=None):
    """
    Последние разборы, свежие впереди. Пустой список — файла ещё нет.

    Читается целиком и режется в памяти: файл растёт на строку за разбор, то
    есть десятками в сутки, и хитрость с чтением с конца обошлась бы дороже
    самой работы.
    """
    import csv

    try:
        with open(CSV_PATH, encoding='utf-8', newline='') as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, ValueError):
        return []
    if mode:
        rows = [row for row in rows if (row.get('mode') or '') == mode]
    return list(reversed(rows[-limit:]))
