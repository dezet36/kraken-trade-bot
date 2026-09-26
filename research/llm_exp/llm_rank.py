"""
Опыт: может ли модель отобрать удачные сетапы по анонимным карточкам.

Каждая пачка — 10 сетапов одной стратегии (вход на откате по тренду), без
названий монет и дат. Модель ставит каждому вероятность дойти до цели раньше
стопа. Исходы на сервер не передаются — сверка у заказчика опыта.

Запуск на сервере (модель свободна, разборы ИИ на паузе):
    python3 llm_rank.py cards.json results_A.jsonl A
Возобновляемый: готовые пачки пропускаются.
"""
import json
import os
import sys
import time
import urllib.request

URL = 'http://127.0.0.1:8788'
BATCH = 10

INTRO = {
    'A': (
        'Ты — трейдер-аналитик. Ниже 10 сетапов одной стратегии: вход лимитом на откате по тренду, '
        'стоп и цель заданы заранее, сделка живёт часы или дни. Названия монет и даты скрыты. '
        'Все проценты хода даны В СТОРОНУ СДЕЛКИ: плюс — цена уже шла туда, куда сделка. '
        'Оцени для каждого сетапа вероятность в процентах, что цена дойдёт до цели РАНЬШЕ стопа. '
        'Средняя доля таких сетапов около 35%, и она сильно зависит от условий — различай их.'
    ),
    'B': (
        'Ты — трейдер-аналитик. Ниже 10 сетапов одной стратегии: вход лимитом на откате по тренду, '
        'стоп и цель заданы заранее, сделка живёт часы или дни. Названия монет и даты скрыты. '
        'Все проценты хода даны В СТОРОНУ СДЕЛКИ: плюс — цена уже шла туда, куда сделка. '
        'Оцени для каждого сетапа вероятность в процентах, что цена дойдёт до цели РАНЬШЕ стопа.\n'
        'ЧТО ИЗМЕРЕНО НА ИСТОРИИ ЭТОЙ СТРАТЕГИИ (11 тысяч сделок, 2022–2025; до цели в среднем 31%; '
        'от сильного к слабому):\n'
        '1. Фандинг в сторону сделки ВРЕДИТ сильнее всего: чем больше толпа платит в сторону сделки '
        '(фандинг и среднее трёх выплат с плюсом), тем хуже — до цели ~29%; когда толпа против сделки '
        '(фандинг с минусом) — ~35–36%.\n'
        '2. Шире стоп и дальше цель по R — ЛУЧШЕ: чаще доходит и выигрыш крупнее.\n'
        '3. Ход BTC за 7 дней в сторону сделки — ХУЖЕ (33% → 29%); за 24 часа — слабее, то же.\n'
        '4. Перегрев по ходу сделки — ХУЖЕ: далеко от дневной EMA50, большой ход за 30 дней, '
        'у края 30-дневного диапазона (32–34% → 29–30%).\n'
        '5. Направленный рынок (эффективность хода выше) — ЛУЧШЕ (30% → 32%).\n'
        '6. Высокий перцентиль ATR — немного хуже.\n'
        '7. Шорты в среднем лучше лонгов.\n'
        'Ход последних 4 часов и последних 12 свечей на исход почти не влияет. Опирайся на эти '
        'факты: разброс твоих оценок должен отражать их, а не общее впечатление.'
    ),
}

FIELDS = (
    'Поля: вход — насколько лимит от цены, %; стоп — дистанция, %; цель — в R; ход 4ч/24ч/7д/30д, %; '
    'место в 30-дневном диапазоне, % (100 — у края по ходу сделки); ATR часа, % и его перцентиль за 30 дней; '
    'дневной размах, %; объём суток к среднему; отрыв от дневной EMA50, %; эффективность хода за 30 дней '
    '(0 — пила, 1 — чистый тренд); BTC 24ч/7д, %; фандинг, базисные пункты (плюс — толпа платит в сторону '
    'сделки), и среднее трёх последних; открытый интерес за 4ч/24ч, %; час UTC; последние 12 часовых '
    'свечей (старые → новые), % в сторону сделки.'
)


def card_line(c):
    def f(x, nd=2):
        return '—' if x is None else f'{x:+.{nd}f}'
    return (f"{c['id']}: {c['side']} | вход {c['entry_away']:.2f}% от цены, стоп {c['stop']:.2f}%, цель {c['rr']:.2f}R | "
            f"ход 4ч {f(c['r4'])}, 24ч {f(c['r24'])}, 7д {f(c['r7d'], 1)}, 30д {f(c['r30d'], 1)} | "
            f"в диапазоне {c['pos30']}% | ATR {c['atr']:.2f}% (п{c['atr_rank']}), размах дня {c['adr']:.1f}%, "
            f"объём ×{c['vol']:.2f} | EMA50д {f(c['ema'], 1)} | эффективность {c['er']:.2f} | "
            f"BTC {f(c['btc24'])}/{f(c['btc7'], 1)} | фандинг {f(c['fund'])} (ср.3 {f(c['fund3'])}) | "
            f"ОИ {f(c['oi4'])}/{f(c['oi24'])} | час {c['hour']} | 12ч: {' '.join(f'{x:+.2f}' for x in c['last12'])}")


def grammar(ids):
    lines = ' '.join(f'"{i}: " num "\\n"' for i in ids)
    return f'root ::= {lines}\nnum ::= [0-9] | [1-9] [0-9]\n'


def post(path, body, timeout=1800):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def ask(prompt, ids):
    text = (f'<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n')
    started = time.time()
    out = post('/completion', {'prompt': text, 'n_predict': 12 * len(ids) + 8, 'temperature': 0.0,
                               'cache_prompt': False, 'grammar': grammar(ids)})
    return out.get('content', ''), time.time() - started, out.get('timings', {})


def main(cards_path, out_path, variant):
    cards = json.load(open(cards_path, encoding='utf-8'))
    done = set()
    if os.path.exists(out_path):
        for line in open(out_path, encoding='utf-8'):
            done.add(json.loads(line)['batch'])
    for b in range(0, len(cards), BATCH):
        if b // BATCH in done:
            continue
        group = cards[b:b + BATCH]
        ids = [c['id'] for c in group]
        prompt = (INTRO[variant] + '\n' + FIELDS + '\n\n' + '\n'.join(card_line(c) for c in group) +
                  '\n\nОтветь строками «ID: вероятность» в том же порядке, только числа от 0 до 99.')
        content, seconds, timings = ask(prompt, ids)
        probs = {}
        for line in content.strip().splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                try:
                    probs[key.strip()] = int(value.strip())
                except ValueError:
                    pass
        with open(out_path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'batch': b // BATCH, 'variant': variant, 'probs': probs, 'raw': content,
                                 'seconds': round(seconds, 1), 'prompt_n': timings.get('prompt_n'),
                                 'predicted_n': timings.get('predicted_n')}, ensure_ascii=False) + '\n')
        print(f'пачка {b // BATCH}: {seconds:.0f} с, {probs}', flush=True)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else 'A')
