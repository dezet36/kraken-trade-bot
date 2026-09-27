"""
Модель как фильтр «да/нет» (опыт C): что стало бы с потоком сетапов, если
брать только одобренные моделью.

evaluate.py смотрит «верхние 5 из 10 в пачке» — это ранжирование. Здесь
вопрос владельца: модель говорит «да» или «нет» по каждому сетапу. Порог —
медиана оценок модели (половина потока) и нижние 30% (модель отсекает
худшую треть). Для сравнения — та же доля по статистике (гребневая, обучена
на других периодах). Интервалы — бутстреп по сетапам.

Отдельно — только сетапы, которые пропустил бы брокер (стоп не теснее
--min-stop, по умолчанию 0.75%: предел издержек 10% у ИИ, docs п. 64).

Запуск (в папке с данными опыта):
    python filter_view.py results_C.jsonl smc_cards.json smc_outcomes.json [--min-stop 0.75]
"""
import json
import sys

import numpy as np


def auc(scores, labels):
    scores, labels = np.asarray(scores, float), np.asarray(labels, bool)
    pos, neg = scores[labels], scores[~labels]
    if not len(pos) or not len(neg):
        return float('nan')
    order = np.argsort(np.concatenate([pos, neg]), kind='mergesort')
    ranks = np.empty(len(order))
    allv = np.concatenate([pos, neg])[order]
    i = 0
    while i < len(allv):                              # средние ранги для ничьих
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return (ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def boot_diff(a, b, n=4000, seed=7):
    rng = np.random.default_rng(seed)
    a, b = np.asarray(a), np.asarray(b)
    d = [rng.choice(a, len(a)).mean() - rng.choice(b, len(b)).mean() for _ in range(n)]
    return np.percentile(d, 2.5), np.percentile(d, 97.5)


def view(label, p, m, r, period):
    print(f'\n== {label}: сетапов {len(r)}, средний R {r.mean():+.3f}, в плюс {np.mean(r > 0.05) * 100:.0f}%')
    print(f'   AUC (исход > 0): модель {auc(p, r > 0.05):.3f}, статистика {auc(m, r > 0.05):.3f}')
    print(f'   оценки модели: среднее {p.mean():.1f}%, разброс {p.std():.1f}, разных значений {len(set(p))}')
    half = None
    for name, share in (('половина потока', 0.5), ('модель отсекает худшую треть', 0.7)):
        cut_p = np.quantile(p, 1 - share)
        cut_m = np.quantile(m, 1 - share)
        yes_p, yes_m = p > cut_p, m > cut_m
        if yes_p.sum() < 10 or (~yes_p).sum() < 10:
            # у модели много ничьих: «да» — не ниже порога
            yes_p = p >= cut_p
        half = yes_p if half is None else half
        lo, hi = boot_diff(r[yes_p], r)
        lo2, hi2 = boot_diff(r[yes_m], r)
        print(f'   {name}: «да» модели {yes_p.sum():3d} сетапов {r[yes_p].mean():+.3f}R '
              f'(к потоку {r[yes_p].mean() - r.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]), «нет» {r[~yes_p].mean():+.3f}R; '
              f'статистика «да» {r[yes_m].mean():+.3f}R (к потоку {r[yes_m].mean() - r.mean():+.3f} '
              f'[{lo2:+.3f}; {hi2:+.3f}])')
    line = []
    for per in sorted(set(period)):
        k = period == per
        yes = k & half
        if yes.sum() and k.sum():
            line.append(f'{per}: поток {r[k].mean():+.3f} ({k.sum()}), «да» {r[yes].mean():+.3f} ({yes.sum()})')
    print('   по периодам («да» — как в строке «половина потока»): ' + ' | '.join(line))


def main(argv):
    results, cards_path, outcomes_path = argv[:3]
    min_stop = float(argv[argv.index('--min-stop') + 1]) if '--min-stop' in argv else 0.75
    cards = {c['id']: c for c in json.load(open(cards_path, encoding='utf-8'))}
    outs = {o['id']: o for o in json.load(open(outcomes_path, encoding='utf-8'))}
    probs = {}
    for line in open(results, encoding='utf-8'):
        row = json.loads(line)
        probs.update(row['probs'])
    ids = [i for i in probs if i in outs and i in cards]
    p = np.array([probs[i] for i in ids], float)
    m = np.array([outs[i]['ml'] for i in ids], float)
    r = np.array([outs[i]['r'] for i in ids], float)
    period = np.array([outs[i].get('period', '—') for i in ids])     # у карточек Фибо (A, B) периода нет
    stop = np.array([cards[i]['stop'] for i in ids], float)
    view('все сетапы', p, m, r, period)
    k = stop >= min_stop
    view(f'только стоп от {min_stop}% (брокер пропустил бы)', p[k], m[k], r[k], period[k])


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1:])
