"""
Сверка обещанной награды с выплаченной.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ ПОД ОДНУ ПРОВЕРКУ. Модель награды ошибалась дважды и оба
раза крупно: сперва обещала $2.85 в сутки при выплаченных восьми центах, потом
по одному рынку сулила $0.0011 там, где биржа заплатила $0.0767. Ошибки эти
обнаруживались вручную и не сразу — а решения по ним принимались сразу.

Площадка знает точный ответ и отдаёт его по дням: `get_earnings_for_user_for_day`.
Значит проверять модель можно не рассуждением, а вычитанием. Обещание пишется в
журнал в момент раскладки, выплата спрашивается у биржи, разница видна в панели.

САМА ПРОВЕРКА НИЧЕГО НЕ РЕШАЕТ И НИЧЕМ НЕ УПРАВЛЯЕТ. Она только показывает
расхождение — потому что менять раскладку по одному дню выплат значило бы
гоняться за шумом: пул делится между всеми, кто стоял, и состав их меняется.
"""

import datetime
import json
import os

from . import store

JOURNAL = os.path.join(store.DIR, 'mm_reward_promise.jsonl')


def remember(plan):
    """
    Записывает обещание раскладки: сколько награды ждём за сутки и с чего.

    Пишется при каждом пересмотре, а не раз в день: состав рынков меняется, и
    честное обещание за день — это то, что стояло в среднем, а не последнее.
    """
    try:
        row = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'day': datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
               'promised_daily': round(float(plan.get('rewards_daily') or 0), 5),
               'used': round(float(plan.get('used') or 0), 2),
               'markets': len(plan.get('markets') or [])}
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
        with open(JOURNAL, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    except Exception:                                          # noqa: BLE001
        pass                        # журнал — удобство, ронять из-за него нечего
    return True


def promises(days=7):
    """Обещания по дням: среднее за день, потому что план пересматривается."""
    out = {}
    try:
        with open(JOURNAL, encoding='utf-8') as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                day = row.get('day')
                if not day:
                    continue
                out.setdefault(day, []).append(float(row.get('promised_daily') or 0))
    except OSError:
        return {}
    recent = sorted(out)[-int(days):]
    return {day: round(sum(out[day]) / len(out[day]), 5) for day in recent}


def paid(client, days=7):
    """Что биржа заплатила по дням. Пустой словарь, если не спросили."""
    if client is None:
        return {}
    out = {}
    today = datetime.datetime.now(datetime.timezone.utc).date()
    for back in range(int(days)):
        day = (today - datetime.timedelta(days=back)).isoformat()
        try:
            rows = client.get_earnings_for_user_for_day(day) or []
        except Exception:                                      # noqa: BLE001
            continue                # не ответила — молчим, а не выдумываем ноль
        total = 0.0
        for row in rows:
            try:
                total += float(row.get('earnings') or 0)
            except (TypeError, ValueError, AttributeError):
                continue
        out[day] = round(total, 6)
    return out


def report(client=None, days=7):
    """
    Обещание против выплаты по дням, и во сколько раз модель промахнулась.

    ДЕНЬ СЧИТАЕТСЯ ТОЛЬКО ЗАКОНЧЕННЫЙ. Сегодняшняя выплата всегда меньше
    обещанной просто потому, что сутки ещё идут, и сравнивать её значило бы
    каждый день видеть мнимый провал.
    """
    want = promises(days)
    got = paid(client, days)
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    rows = []
    for day in sorted(set(want) | set(got), reverse=True):
        promised = want.get(day)
        real = got.get(day)
        row = {'day': day, 'promised': promised, 'paid': real,
               'closed': day != today}
        if promised and real is not None and day != today:
            row['ratio'] = round(promised / real, 1) if real > 0 else None
        rows.append(row)
    closed = [r for r in rows if r.get('ratio')]
    return {
        'days': rows,
        # Во сколько раз модель завышает по закрытым дням. Одна — точно.
        'overstates': (round(sum(r['ratio'] for r in closed) / len(closed), 1)
                       if closed else None),
        'checked_days': len(closed),
    }
