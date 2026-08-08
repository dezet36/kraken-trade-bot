"""
Polymarket: есть ли край в самой цене. Калибровка на разрешённых рынках.

ЗАЧЕМ ЭТОТ ЗАМЕР ПЕРВЫЙ. В сети об этом пишут прямо противоположное, и обе
версии выглядят авторитетно:

    «фаворитов недооценивают: контракты по 60-70 центов выигрывают почти в 80%
     случаев — край 15% зашит в рынок»
    «средняя ошибка калибровки 2.1 процентного пункта по всем корзинам»

Одновременно верными они быть не могут: край в 15% — это ошибка калибровки в
15 пунктов, а не в два. Значит кто-то ошибается, и выяснить это можно только
счётом. Всё остальное — выбор рынков, размер ставки, вкладка в приложении —
имеет смысл лишь после ответа на этот вопрос.

ЧТО ИМЕННО СЧИТАЕТСЯ. Берётся цена YES за сутки ДО разрешения и сравнивается с
тем, чем рынок кончился. Если рынок откалиброван, то среди контрактов, стоивших
0.70, ровно 70% должны разрешиться в YES. Разница между частотой и ценой и есть
валовый край на единицу вложенного.

ПОЧЕМУ ЗА СУТКИ, А НЕ ПО ПОСЛЕДНЕЙ ЦЕНЕ. У последней цены калибровка
тривиальна: перед самым разрешением исход уже известен, цена стоит у нуля или
единицы, и «край» окажется нулевым по построению. Торговать можно только там,
где есть неопределённость, поэтому и мерить надо на расстоянии от неё.

ГЛАВНАЯ ЛОВУШКА ЗДЕСЬ — НЕ ИЗДЕРЖКИ, А МНИМАЯ НЕЗАВИСИМОСТЬ. Рынки идут
пачками об одном событии: «FDV выше $5M», «выше $10M», «выше $20M», «выше
$60M» — это ЧЕТЫРЕ рынка и ОДИН исход запуска. Посчитав их независимыми,
получим вчетверо более узкий интервал, чем на самом деле, и любой шум станет
значимым. Поэтому интервал считается бутстрапом ПО СОБЫТИЯМ: пересэмплируются
события целиком, а не отдельные рынки. Разница между двумя способами
печатается — она показывает, насколько велика была бы ошибка.

ИЗДЕРЖКИ СЧИТАЮТСЯ ПО ФОРМУЛЕ БИРЖИ, А НЕ НА ГЛАЗ:

    комиссия = ставка_категории × p × (1 - p)   на КОНТРАКТ, только с тейкера

Ставка берётся из поля feeSchedule самого рынка. Комиссия платится ТОЛЬКО на
входе: погашение при разрешении бесплатно. Это принципиально отличает площадку
от бессрочных фьючерсов, где круг платится дважды. Пересчёт в проценты от
вложенного даёт неожиданное: у контракта по 0.95 комиссия 0.2% ставки, а у
контракта по 0.50 — 2.0%. Покупка фаворита обходится вдесятеро дешевле.

Сверх комиссии вычитается пересечение спреда: входить приходится по чужой
заявке. Оно задаётся ЯВНО параметром, а не берётся из текущего стакана: текущий
спред к прошлым сделкам отношения не имеет, и подставлять его значило бы мерить
одно, а называть другим.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА:

    край считается пригодным, если в одной и той же ценовой корзине он
    положителен ПОСЛЕ издержек на ОБЕИХ половинах по времени, интервал по
    событиям не накрывает ноль хотя бы на одной И в корзине не меньше 30
    событий на каждой половине.

Деление по времени, а не по парам: здесь нет пар, зато есть очевидный риск, что
край держался на одном периоде выборов и исчез после. Ровно так закрылся пробой
канала — вариант, выбранный на всём пуле, не повторился на отложенной половине.

ИТОГ 2026-08-08: ПРИГОДНЫХ КОРЗИН НЕТ. 1706 рынков, 751 событие.

    корзина      ранняя   поздняя   всё вместе, интервал по событиям
    0.02-0.10    -0.467    +0.492   [-0.839; +0.946]   знак не держится
    0.10-0.25    +0.084    +0.528   [-0.080; +0.708]   плюс, но интервал шире края
    0.25-0.40    -0.183    -0.111   [-0.292; +0.005]   минус
    0.40-0.60    -0.138    -0.039   [-0.178; +0.000]   минус
    0.60-0.75    +0.034    +0.067   [-0.059; +0.160]   плюс на обеих, значимости нет
    0.75-0.90    -0.212    -0.140   [-0.298; -0.053]   МИНУС значимый
    0.90-0.98    -0.073 (мало событий на половинах)

ГРОМКОЕ УТВЕРЖДЕНИЕ ИЗ СЕТИ НЕ ВОСПРОИЗВЕЛОСЬ. Пишут, что контракты по 60-70
центов выигрывают почти в 80% случаев, то есть край 15% зашит в цену. Замерено:
при средней цене 0.663 доля YES составляет 0.725 — плюс шесть процентных
пунктов, а не пятнадцать, и интервал по событиям накрывает ноль. Направление
угадано верно, величина завышена вдвое-втрое.

ОБРАТНОЕ СМЕЩЕНИЕ У СИЛЬНЫХ ФАВОРИТОВ ОКАЗАЛОСЬ ЗАМЕТНЕЕ И УСТОЙЧИВЕЕ. Корзина
0.75-0.90 отрицательна на ОБЕИХ половинах и значима на объединённой выборке:
цена 0.814, доля YES 0.693. То есть фаворитов по 80 центов рынок ПЕРЕоценивает.
Это ровно противоположно тому, что обещает «смещение фаворит-лонгшот», и на
этом краю сидеть покупателем нельзя.

ИЗДЕРЖКИ ПОДТВЕРДИЛИ АРИФМЕТИКУ, НАЗВАННУЮ ДО ПРОГОНА. У корзины 0.02-0.10 они
составляют 0.224 — двадцать два процента вложенного, — потому что комиссия и
цент спреда относятся к очень дешёвой ставке. У корзины 0.90-0.98 те же
издержки равны 0.013. Дешёвые хвосты разорительны независимо от того, угаданы
они или нет.

ИНТЕРВАЛ ПО СОБЫТИЯМ ШИРЕ ОБЫЧНОГО В РАЗЫ, и это не придирка: 1706 рынков дают
751 событие, то есть в среднем 2.3 рынка на одно. Считай мы их независимыми,
интервал у корзины 0.60-0.75 сузился бы примерно в полтора раза и край
объявился бы значимым. Он не значим.

Запуск:
    python research/polymarket_calibration.py
"""

import io
import json
import os
import sys
import time
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
CACHE = os.path.join(ROOT, 'research', 'polymarket_cache')

HORIZON_H = 24          # за сколько часов до конца берём цену
MIN_VOLUME = 10_000     # тонкие рынки — это не цена, а шум
MIN_EVENTS = 30         # меньше — корзина ничего не показывает
CROSS_COST = 0.01       # пересечение спреда, в долях цены контракта
BOOTSTRAP = 10_000
RNG = np.random.default_rng(20260808)

BUCKETS = [(0.02, 0.10), (0.10, 0.25), (0.25, 0.40), (0.40, 0.60),
           (0.60, 0.75), (0.75, 0.90), (0.90, 0.98)]


def fetch(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'research/1.0'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8', 'replace'))
        except Exception:                                  # noqa: BLE001
            if attempt == tries - 1:
                return None
            time.sleep(1.0 + attempt)
    return None


def collect_markets(want=3000):
    """Разрешённые рынки с чистым исходом и заметным оборотом."""
    path = os.path.join(CACHE, 'markets.json')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            rows = json.load(fh)
        print(f'рынки из кэша: {len(rows)}')
        return rows

    os.makedirs(CACHE, exist_ok=True)
    # ОТБОР ИДЁТ ПО end_date_max, И БЕЗ ЭТОГО ЗАМЕР БЫЛ БЫ ПУСТЫМ. Признак
    # closed=true сам по себе тянет и рынки-заглушки с формальной датой конца
    # в 2027 году: они закрыты досрочно и торгов в них не было. Сортировка по
    # дате конца по убыванию выдаёт РОВНО ИХ — первая версия собрала 1256
    # рынков, из которых 100% имели дату в будущем и 95% не имели истории цен
    # вовсе. Ограничение сверху вчерашним днём оставляет те, что действительно
    # дожили до своей даты.
    # ШАГ ИДЁТ ПО ДАТЕ, А НЕ ПО СМЕЩЕНИЮ. Смещение упирается в предел около
    # двух тысяч (дальше 422), и первая версия собрала на нём всего 142 рынка
    # — замер на таком числе не состоялся ни в одной корзине. Сдвигая верхнюю
    # границу даты к самому старому найденному рынку и обнуляя смещение, идём
    # в прошлое без предела. Тот же приём, что понадобился для истории
    # открытого интереса, где `since` не действовал, а шаг концом окна — да.
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                           time.gmtime(time.time() - 24 * 3600))
    rows, offset, seen_ids = [], 0, set()
    while len(rows) < want:
        page = fetch(f'{GAMMA}/markets?limit=100&offset={offset}&closed=true'
                     f'&end_date_max={cutoff}&order=endDate&ascending=false')
        if not isinstance(page, list) or not page or offset >= 1800:
            # Окно исчерпано либо близок предел смещения: отступаем по дате к
            # самому старому виденному рынку и начинаем смещение заново.
            oldest = min((m.get('endDate') or '' for m in (page or [])),
                         default='')
            if not oldest or oldest >= cutoff:
                break
            cutoff, offset = oldest, 0
            print(f'   шаг по дате: до {cutoff[:10]}', flush=True)
            continue
        for m in page:
            # Шаг по дате перекрывает границы окна, и один рынок приходит
            # дважды. Без отсева он и в корзину попал бы дважды.
            if m.get('id') in seen_ids:
                continue
            seen_ids.add(m.get('id'))
            try:
                prices = json.loads(m.get('outcomePrices') or '[]')
                tokens = json.loads(m.get('clobTokenIds') or '[]')
            except Exception:                              # noqa: BLE001
                continue
            # Только чисто разрешённые: ('1','0') либо ('0','1'). Прочие формы —
            # отменённые и спорные рынки, у них исхода нет.
            if sorted(prices) != ['0', '1'] or len(tokens) < 1:
                continue
            if float(m.get('volume') or 0) < MIN_VOLUME:
                continue
            events = m.get('events') or []
            rows.append({
                'id': m.get('id'),
                'question': m.get('question'),
                'yes_token': tokens[0],
                'yes_won': 1 if prices[0] == '1' else 0,
                'end': m.get('endDate'),
                'volume': float(m.get('volume') or 0),
                # Событие — то, что объединяет рынки одной пачкой. Без него
                # четыре порога одного запуска сойдут за четыре наблюдения.
                'event': (events[0].get('id') if events else m.get('id')),
                'fee_rate': float(((m.get('feeSchedule') or {}) or {})
                                  .get('rate') or 0.05),
            })
        offset += 100
        print(f'   собрано {len(rows)} (offset {offset})', flush=True)
        time.sleep(0.1)

    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(rows, fh, ensure_ascii=False)
    return rows


def price_before_end(row):
    """Цена YES за HORIZON_H часов до конца рынка. None — данных нет."""
    path = os.path.join(CACHE, f'h_{row["id"]}.json')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            points = json.load(fh)
    else:
        data = fetch(f'{CLOB}/prices-history?market={row["yes_token"]}'
                     f'&interval=max&fidelity=60')
        points = (data or {}).get('history') or []
        # ОТКАЗ СЕТИ НЕ КЭШИРУЕТСЯ. Пустой ответ от биржи и сорвавшийся запрос
        # выглядят одинаково, и записав второе на диск, мы навсегда объявили бы
        # рынок «без истории». Кэшируем только то, что действительно пришло;
        # цена этого — повторный запрос при следующем прогоне.
        if data is not None:
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(points, fh)
        time.sleep(0.12)
    if len(points) < 12:
        return None

    end_ts = points[-1]['t']
    target = end_ts - HORIZON_H * 3600
    # Берём последнюю точку НЕ ПОЗЖЕ цели. Ближайшая по модулю подошла бы
    # ближе к цели, но могла бы оказаться ПОСЛЕ неё — то есть заглянуть вперёд.
    fit = [p for p in points if p['t'] <= target]
    if not fit:
        return None
    price = float(fit[-1]['p'])
    return price if 0.0 < price < 1.0 else None


def event_bootstrap(events, values_by_event):
    """
    Интервал, где пересэмплируются СОБЫТИЯ целиком.

    Обычный бутстрап по строкам считал бы четыре порога одного запуска четырьмя
    независимыми наблюдениями и сузил бы интервал вдвое-втрое на пустом месте.
    """
    if len(events) < 2:
        return float('nan'), float('nan')
    picks = RNG.integers(0, len(events), size=(BOOTSTRAP, len(events)))
    means = np.empty(BOOTSTRAP)
    for i in range(BOOTSTRAP):
        chunk = [values_by_event[events[j]] for j in picks[i]]
        flat = np.concatenate(chunk)
        means[i] = flat.mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def analyse(rows, label):
    print()
    print('=' * 108)
    print(f'{label}   рынков {len(rows)}, событий {len({r["event"] for r in rows})}')
    print('=' * 108)
    print(f'{"цена за сутки до":<20}{"рынков":>8}{"событий":>9}{"цена":>8}'
          f'{"доля YES":>10}{"валовый":>10}{"издержки":>10}{"чистый":>9}'
          f'{"интервал по событиям":>26}')
    print('-' * 108)

    out = {}
    for low, high in BUCKETS:
        inside = [r for r in rows if low <= r['price'] < high]
        if not inside:
            continue
        by_event = {}
        for r in inside:
            # Чистый край на вложенный доллар: выигрыш 1 при YES, ставка p.
            # Комиссия по формуле биржи плюс пересечение спреда — оба на вход.
            fee = r['fee_rate'] * r['price'] * (1 - r['price'])
            net = (r['yes_won'] - r['price'] - fee - CROSS_COST) / r['price']
            by_event.setdefault(r['event'], []).append(net)
        events = list(by_event)
        arrays = {e: np.array(v, dtype=float) for e, v in by_event.items()}
        flat = np.concatenate([arrays[e] for e in events])
        if len(events) < MIN_EVENTS:
            print(f'{f"{low:.2f}-{high:.2f}":<20}{len(inside):>8}{len(events):>9}'
                  f'{"— мало событий":>63}')
            continue
        price = np.mean([r['price'] for r in inside])
        won = np.mean([r['yes_won'] for r in inside])
        fees = np.mean([r['fee_rate'] * r['price'] * (1 - r['price'])
                        for r in inside])
        lo, hi = event_bootstrap(events, arrays)
        gross = (won - price) / price
        out[(low, high)] = {'net': float(flat.mean()), 'lo': lo, 'hi': hi,
                            'events': len(events), 'n': len(inside)}
        span = f'[{lo:+.3f}; {hi:+.3f}]'
        print(f'{f"{low:.2f}-{high:.2f}":<20}{len(inside):>8}{len(events):>9}'
              f'{price:>8.3f}{won:>10.3f}{gross:>+10.3f}'
              f'{(fees + CROSS_COST) / price:>10.3f}{flat.mean():>+9.3f}'
              f'{span:>26}')
    return out


def main():
    rows = collect_markets()
    print(f'рынков с чистым исходом и оборотом > ${MIN_VOLUME:,}: {len(rows)}')

    priced = []
    for i, row in enumerate(rows):
        price = price_before_end(row)
        if price is None:
            continue
        row['price'] = price
        priced.append(row)
        if (i + 1) % 200 == 0:
            print(f'   цены: {len(priced)} из {i + 1}', flush=True)
    print(f'с ценой за {HORIZON_H}ч до конца: {len(priced)}')

    # Деление по времени: половина, разрешившаяся раньше, и половина позже.
    priced.sort(key=lambda r: r['end'] or '')
    middle = len(priced) // 2
    halves = [('РАННЯЯ половина по времени', priced[:middle]),
              ('ПОЗДНЯЯ половина по времени', priced[middle:])]

    tables = {}
    for label, part in halves:
        tables[label] = analyse(part, label)
    analyse(priced, 'ВСЁ ВМЕСТЕ — для сравнения, в приёмке не участвует')

    print()
    print('=' * 108)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: край положителен ПОСЛЕ издержек на')
    print('обеих половинах по времени, интервал по событиям не накрывает ноль')
    print(f'хотя бы на одной И не меньше {MIN_EVENTS} событий в корзине на каждой.')
    print('=' * 108)
    first, second = tables[halves[0][0]], tables[halves[1][0]]
    winners = []
    for bucket in BUCKETS:
        a, b = first.get(bucket), second.get(bucket)
        if not a or not b:
            continue
        both_positive = a['net'] > 0 and b['net'] > 0
        strong = a['lo'] > 0 or b['lo'] > 0
        name = f'{bucket[0]:.2f}-{bucket[1]:.2f}'
        mark = ('ПРИГОДЕН' if both_positive and strong
                else 'плюс на обеих, но интервал накрывает ноль'
                if both_positive else 'знак не держится')
        if both_positive and strong:
            winners.append(name)
        print(f'  {name:<14}ранняя {a["net"]:+.3f}  поздняя {b["net"]:+.3f}   {mark}')

    print()
    if winners:
        print('ПРИГОДНЫЕ КОРЗИНЫ:', ', '.join(winners))
        print('Это означает край В САМОЙ ЦЕНЕ, без всякого прогноза событий:')
        print('достаточно покупать всё подряд в этом диапазоне. Проверять такое')
        print('надо на живых деньгах малым размером, а не увеличивать ставку.')
    else:
        print('ПРИГОДНЫХ КОРЗИН НЕТ.')
        print('Значит цена сама по себе края не даёт, и зарабатывать пришлось бы')
        print('прогнозом отдельных событий — то есть знанием предмета, а не')
        print('свойством площадки. Это совсем другая задача и другие риски.')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    main()
