"""
Статистика маркет-мейкера: что именно надо мерить, чтобы его улучшать.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Панель отвечает на вопрос «что сейчас», а улучшать
стратегию можно только по вопросу «что вышло». Это разные наборы чисел и разные
источники: первый живёт в памяти работающего потока, второй — в журналах на
диске, которые переживают перезапуск.

ПЯТЬ ВОПРОСОВ, И КАЖДЫЙ МЕНЯЕТ КОНКРЕТНУЮ НАСТРОЙКУ.

    1. КАКИЕ РЫНКИ ПЛАТЯТ. Отбор ранжирует по расчётному доходу в час, и это
       предположение. Если рынок дал десять исполнений и ноль кругов, он
       занимает деньги и место в списке — а по расчёту выглядит хорошим.
       Меняет: отбор (selector), пороги MM_MIN_USD_PER_HOUR и MM_MAX_WAIT_HOURS.

    2. СКОЛЬКО СПРЕДА МЫ РЕАЛЬНО БЕРЁМ. Планировали взять столько-то, взяли
       столько-то. Разница — цена того, что нас двигают и обгоняют.
       Меняет: глубину шага внутрь спреда (step_ticks).

    3. КУДА ШЛА ЦЕНА ПОСЛЕ НАШИХ ИСПОЛНЕНИЙ. Неблагоприятный отбор — число,
       решающее судьбу затеи: спред виден заранее, а сколько его отберут
       обратно, видно только так.
       Меняет: сам вывод о том, стоит ли этим заниматься.

    4. ОБЕЩАНИЕ МОДЕЛИ ПРОТИВ ДЕЛА. Расчёт ожидания считает стороны
       независимыми; на деле они связаны против нас.
       Меняет: поправку, на которую делится расчётный доход.

    5. ДОЛЯ ИСПОЛНЕНИЯ. Сколько выставленных заявок вообще дождались своего.
       Меняет: решение вставать внутрь спреда или на лучшую цену.

ЧИТАЕТСЯ ТОЛЬКО С ДИСКА и ничего не пишет. Статистику должно быть безопасно
спросить в любой момент, включая момент, когда бот торгует.
"""

import json
import os
from collections import defaultdict

from . import engine, executor, mm, store


def _rows(path, limit=100_000):
    """Записи журнала. Битая строка пропускается, а не роняет отчёт."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out[-limit:]


def _pairs(fills):
    """
    Круги: покупка и продажа на одном рынке, сведённые по порядку журнала.

    КРУГ — ЕДИНСТВЕННАЯ ЧЕСТНАЯ МЕРКА. Отдельное исполнение не говорит ничего:
    купить может каждый, заработок появляется только при закрытии. Незакрытые
    исполнения тоже считаются — отдельно, как незавершённые.
    """
    waiting = defaultdict(list)
    rounds, dangling = [], []
    for fill in fills:
        token = str(fill.get('token'))
        side = fill.get('side')
        if side not in ('bid', 'ask'):
            continue
        other = waiting[(token, 'ask' if side == 'bid' else 'bid')]
        if other:
            first = other.pop(0)
            entry, exit_ = (first, fill) if first['side'] == 'bid' else (fill, first)
            size = min(float(entry.get('size') or 0), float(exit_.get('size') or 0))
            rounds.append({
                'token': token,
                'question': entry.get('question') or exit_.get('question') or '',
                'bought': float(entry['price']), 'sold': float(exit_['price']),
                'size': size,
                'gain_usd': round((float(exit_['price']) - float(entry['price'])) * size, 4),
                'opened_at': entry.get('at'), 'closed_at': exit_.get('at'),
                'planned_gain': entry.get('planned_gain'),
            })
        else:
            waiting[(token, side)].append(fill)
    for queue in waiting.values():
        dangling.extend(queue)
    return rounds, dangling


def overview(limit=100_000):
    """Общая картина одним словарём. Пусто — значит исполнений ещё не было."""
    fills = [f for f in _rows(engine.FILLS, limit)
             if f.get('source') == 'exchange']
    rounds, dangling = _pairs(fills)
    timing = _rows(engine.TIMING, limit)
    drift = _rows(engine.DRIFT, limit)
    orders = _rows(executor.ORDERS_LOG, limit)

    placed = sum(1 for o in orders if o.get('action') == 'PLACED')
    refused = sum(1 for o in orders if o.get('action') in ('REFUSE', 'ERROR'))

    gains = [r['gain_usd'] for r in rounds]
    won = sum(1 for g in gains if g > 0)
    ratios = sorted(float(t['ratio']) for t in timing if t.get('ratio'))
    per_contract = [d.get('gain_per_contract') or 0 for d in drift]

    return {
        'fills': len(fills),
        'rounds': len(rounds),
        'open_fills': len(dangling),
        'gain_usd': round(sum(gains), 4),
        'wins': won,
        'losses': len(gains) - won,
        'avg_gain': round(sum(gains) / len(gains), 5) if gains else None,
        # Доля исполнения: сколько выставленных дождались своего. Решает,
        # вставать внутрь спреда или на лучшую цену.
        'orders_placed': placed,
        'orders_refused': refused,
        'fill_rate': round(len(fills) / placed, 4) if placed else None,
        # Обещание модели против дела.
        'timing_count': len(ratios),
        'timing_factor': round(ratios[len(ratios) // 2], 2) if ratios else None,
        # Неблагоприятный отбор: куда шла цена после нас.
        'drift_count': len(per_contract),
        'drift_per_contract': (round(sum(per_contract) / len(per_contract), 5)
                               if per_contract else None),
        'drift_against_us': sum(1 for x in per_contract if x < 0),
    }


def by_market(limit=100_000):
    """
    Итог по каждому рынку. Лучшие сверху, а убыточные видно сразу.

    ЭТО ГЛАВНАЯ ТАБЛИЦА ДЛЯ УЛУЧШЕНИЯ ОТБОРА. Расчётный доход в час —
    предположение; здесь стоит то, что вышло на самом деле, и рядом с ним то,
    что обещалось. Рынок с исполнениями и без кругов занимает деньги впустую,
    и по расчёту этого не видно.
    """
    fills = [f for f in _rows(engine.FILLS, limit)
             if f.get('source') == 'exchange']
    rounds, dangling = _pairs(fills)

    plan = {}
    if os.path.exists(mm.PLAN_FILE):
        try:
            with open(mm.PLAN_FILE, encoding='utf-8') as fh:
                for row in (json.load(fh).get('markets') or []):
                    plan[str(row.get('token_id'))] = row
        except (OSError, ValueError):
            plan = {}

    tally = defaultdict(lambda: {'fills': 0, 'rounds': 0, 'gain_usd': 0.0,
                                 'open_fills': 0, 'question': '', 'spent': 0.0})
    for fill in fills:
        row = tally[str(fill.get('token'))]
        row['fills'] += 1
        row['question'] = row['question'] or fill.get('question') or ''
        row['spent'] += float(fill.get('price') or 0) * float(fill.get('size') or 0)
    for done in rounds:
        row = tally[done['token']]
        row['rounds'] += 1
        row['gain_usd'] += done['gain_usd']
        row['question'] = row['question'] or done['question']
    for left in dangling:
        tally[str(left.get('token'))]['open_fills'] += 1

    out = []
    for token, row in tally.items():
        promised = plan.get(token) or {}
        out.append({
            'token': token,
            'question': row['question'] or promised.get('question') or '—',
            'fills': row['fills'], 'rounds': row['rounds'],
            'open_fills': row['open_fills'],
            'gain_usd': round(row['gain_usd'], 4),
            'spent_usd': round(row['spent'], 2),
            # Что обещал отбор — рядом с тем, что вышло.
            'promised_per_hour': promised.get('usd_per_hour'),
            'promised_wait_min': (round(promised['wait_hours'] * 60)
                                  if promised.get('wait_hours') not in (None, float('inf'))
                                  else None),
        })
    out.sort(key=lambda r: (-r['gain_usd'], -r['fills']))
    return out


def to_csv(limit=100_000):
    """
    Всё в одну таблицу для разбора вне приложения.

    Разбирать стратегию удобнее там, где есть сводные таблицы и графики, а не в
    панели. Строка на рынок, а не на сделку: сделки лежат в журнале и никуда не
    денутся, а решения принимаются по рынкам.
    """
    import csv
    import io as _io

    buf = _io.StringIO()
    writer = csv.writer(buf, delimiter=';')
    writer.writerow(['рынок', 'исполнений', 'кругов', 'незакрытых',
                     'заработано', 'потрачено', 'обещано в час',
                     'обещано ждать, мин'])
    for row in by_market(limit):
        writer.writerow([row['question'], row['fills'], row['rounds'],
                         row['open_fills'], row['gain_usd'], row['spent_usd'],
                         row['promised_per_hour'], row['promised_wait_min']])
    return buf.getvalue()


def report(limit=100_000):
    """Обе части сразу — для панели и для печати в консоль."""
    return {'overview': overview(limit), 'markets': by_market(limit)[:40]}


if __name__ == '__main__':
    data = report()
    top = data['overview']
    print(f"исполнений {top['fills']}, кругов {top['rounds']}, "
          f"незакрытых {top['open_fills']}, заработано ${top['gain_usd']:,.4f}")
    if top['fill_rate'] is not None:
        print(f"выставлено заявок {top['orders_placed']}, "
              f"доля исполнения {top['fill_rate']:.1%}, "
              f"отказов {top['orders_refused']}")
    if top['timing_factor']:
        print(f"модель оптимистичнее в {top['timing_factor']}× "
              f"({top['timing_count']} замеров)")
    print()
    print(f"{'рынок':<44}{'испол':>6}{'круг':>6}{'заработано':>12}{'обещано/ч':>11}")
    for row in data['markets']:
        print(f"{row['question'][:43]:<43} {row['fills']:>6} {row['rounds']:>5} "
              f"{row['gain_usd']:>11.4f} "
              f"{row['promised_per_hour'] if row['promised_per_hour'] is not None else '—':>10}")


def capture_factor(limit=100_000, least=5):
    """
    Какую ДОЛЮ обещанного спреда мы забираем на самом деле.

    ПОЧЕМУ ЭТО ЧИСЛО НУЖНО ОТДЕЛЬНО ОТ ПОПРАВКИ НА ВРЕМЯ. Поправка на время
    отвечает «как быстро закроется круг». Здесь другой вопрос — «сколько в нём
    останется», и ответы у них разошлись до противоположных.

    Замер по тринадцати закрытым кругам:

        купили 0.554 → продали 0.527   −0.027   обещано +0.014
        купили 0.235 → продали 0.208   −0.027   обещано +0.019
        купили 0.047 → продали 0.024   −0.023   обещано +0.009

        взято всего     −$0.37
        обещано моделью +$0.97
        доля             −38%

    В КАЖДОМ УБЫТОЧНОМ КРУГЕ МЫ ПРОДАЛИ ДЕШЕВЛЕ, ЧЕМ КУПИЛИ. Модель считает,
    что обе стороны исполнятся по нашим ценам при неподвижной середине. На деле
    между двумя исполнениями проходит время, и мейкера подбирают по одной
    стороне ровно тогда, когда вторая уже невыгодна.

    ОТРИЦАТЕЛЬНОЕ ЗНАЧЕНИЕ — ЭТО НЕ ПОЛОМКА, А ОТВЕТ. Оно означает, что захват
    спреда пока отнимает деньги, и планировать доход от него нельзя.

    Возвращает None, пока кругов меньше `least`: по трём наблюдениям делать
    вывод хуже, чем не делать никакого. Пять — не статистика, но направление по
    ним уже видно, а цена бездействия известна: план третьи сутки обещает доход,
    которого ни разу не было.
    """
    rounds, _ = _pairs(_rows(engine.FILLS, limit))
    took = promised = 0.0
    counted = 0
    for row in rounds:
        want = row.get('planned_gain')
        if want in (None, ''):
            continue
        try:
            want = float(want)
        except (TypeError, ValueError):
            continue
        if want <= 0:
            continue
        size = float(row.get('size') or 0)
        took += float(row.get('gain_usd') or 0)
        promised += want * size
        counted += 1
    if counted < int(least) or promised <= 0:
        return None
    return round(took / promised, 3)
