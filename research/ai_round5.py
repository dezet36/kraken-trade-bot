"""
Круг 5 поиска закономерностей (30.09.2026, docs/ИИ_замечания_на_проверку.md, п. 76).

Ступень А: «почти прошедшие» правила кругов 1–4 (на train и valid в плюсе,
≥ 50 и ≥ 20 сделок, по одному от семьи механизмов) — финальная проверка на
2021 годе. Портфельная проверка прошедших: P1 + B3 + новое против P1 + B3.
Условия записаны до расчёта (п. 76).

    python research/ai_round5.py a2021                      # ступень А на 2021
    python research/ai_round5.py portfolio std  <имена…>    # портфель на train и valid
    python research/ai_round5.py portfolio 2021 <имена…>    # портфель на 2021

Круг 6 (повтор круга 5 модели с коротким вопросом) — правила берутся из файла своего круга,
портфель — против текущей тетради P1 + B3 + P4 (v3.4):
    python research/ai_round5.py y2021 6 [имена…]            # правила круга 6, прошедшие train и valid, на 2021
    python research/ai_round5.py portfolio3 std  6 <имена…>  # P1 + B3 + P4 против + новое, train и valid
    python research/ai_round5.py portfolio3 2021 6 <имена…>  # то же на 2021
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if len(sys.argv) > 1 and (sys.argv[1] in ('a2021', 'y2021')
                          or (sys.argv[1] in ('portfolio', 'portfolio3') and sys.argv[2:3] == ['2021'])):
    os.environ['FLOW_CACHE'] = 'flow_cache_2021'      # до импорта стенда: он читает папку при импорте
sys.path.insert(0, HERE)

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

import ai_model_trader_bt as B                        # noqa: E402
import ai_pattern_lab as L                            # noqa: E402

STAGE_A = ('whale_trend_accel', 'neg_funding_short_squeeze', 'intraday_surge_cont',
           'funding_exhaustion_grind', 'bear_fomo_bounce_fail')
MIN_TRADES_2021 = 20


def rules_by_name():
    out = {}
    for k in range(1, 6):
        path = os.path.join(HERE, 'results', 'model_research', f'round{k}_scored.json')
        if os.path.exists(path):
            for item in json.load(open(path, encoding='utf-8')):
                out.setdefault(item['rule']['name'], item['rule'])
    return out


def rules_of_round(k):
    """Правила одного круга с их итогами train/valid (имена у кругов повторяются — круг 5 переписал круг 4)."""
    path = os.path.join(HERE, 'results', 'model_research', f'round{k}_scored.json')
    return json.load(open(path, encoding='utf-8'))


def passed_train_valid(item):
    """Ступень Б (п. 76): train — R > 0, t ≥ 2, ≥ 0.5 сделки в неделю; valid — R > 0 при ≥ 20 сделках."""
    tr, va = item['train'], item['valid']
    return (bool(tr['n']) and tr['r'] is not None and tr['r'] > 0 and (tr['t'] or 0) >= 2.0
            and tr['per_week'] >= 0.5 and va['n'] >= 20 and va['r'] is not None and va['r'] > 0)


def round_2021(k, names):
    items = rules_of_round(k)
    chosen = [x for x in items if (x['rule']['name'] in names if names else passed_train_valid(x))]
    data = data_2021()
    lines = [f'Круг {k}: правила, прошедшие train и valid, на 2021 годе ({len(data)} пар) — проход R > 0 и '
             f'≥ {MIN_TRADES_2021} сделок']
    for x in items:
        tr, va = x['train'], x['valid']
        mark = 'train+valid да ' if passed_train_valid(x) else 'train+valid нет'
        lines.append(f"  {mark} {x['rule']['name']:32s} {x['rule']['side']:5s} train {tr['n']:4d} "
                     f"{(tr['r'] or 0):+.3f} t {(tr['t'] or 0):+.1f} {tr['per_week']:.2f}/нед | valid {va['n']:4d} "
                     f"{(va['r'] or 0):+.3f}")
    passed = []
    for x in chosen:
        s, _ = L.evaluate(x['rule'], data=data, splits=('y2021',))
        y = s['y2021']
        ok = y['n'] >= MIN_TRADES_2021 and y['r'] > 0
        passed += [x['rule']['name']] if ok else []
        lines.append(f"  2021: {'да ' if ok else 'НЕТ'} {x['rule']['name']:32s} {y['n']:4d} сд "
                     f"R {y['r'] if y['n'] else float('nan'):+.3f} t {y['t'] if y['n'] > 1 else float('nan'):+.1f}")
    lines.append(f"\nПрошли три ступени: {', '.join(passed) or 'нет'}")
    print('\n'.join(lines), flush=True)
    with open(os.path.join(HERE, 'results', f'ai_round{k}_2021.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return passed


def data_2021():
    import ai_notebook_2021 as Y                      # ставит L.SPLITS['y2021'] и готовит данные 2021
    return Y.prepare()


def stage_a():
    rules = rules_by_name()
    data = data_2021()
    lines = [f'Круг 5, ступень А: правила кругов 1–4 на 2021 годе ({len(data)} пар), проход — R > 0 и ≥ {MIN_TRADES_2021} сделок']
    passed = []
    for name in STAGE_A:
        rule = rules[name]
        s, _ = L.evaluate(rule, data=data, splits=('y2021',))
        y = s['y2021']
        ok = y['n'] >= MIN_TRADES_2021 and y['r'] > 0
        passed += [name] if ok else []
        lines.append(f"  {'да ' if ok else 'НЕТ'} {name:26s} {rule['side']:5s} {y['n']:4d} сд ({y['per_week']:.2f}/нед) "
                     f"R {y['r'] if y['n'] else float('nan'):+.3f} t {y['t'] if y['n'] > 1 else float('nan'):+.1f} "
                     f"плюс {y['win'] * 100 if y['n'] else 0:.0f}%")
    lines.append(f"\nПрошли ступень А: {', '.join(passed) or 'нет'}")
    print('\n'.join(lines), flush=True)
    with open(os.path.join(HERE, 'results', 'ai_round5_a2021.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return passed


def as_pattern(rule):
    """Правило круга -> запись тетради (сигнал — первый час серии, как у P1/B3)."""
    return {'label': rule['name'], 'side': rule['side'], 'hold': int(rule['hold_hours']),
            'stop': float(rule['stop_atr']), 'rank': ('ret_24h', 1 if rule['side'] == 'long' else -1),
            'conditions': rule['conditions']}


def portfolio(mode, names, k=None):
    rules = {x['rule']['name']: x['rule'] for x in rules_of_round(k)} if k else rules_by_name()
    data = data_2021() if mode == '2021' else B.prepare()
    splits = ['y2021'] if mode == '2021' else ['train', 'valid']
    base = {key: v for key, v in B.PATTERNS.items() if key in ('P1', 'B3')}
    if k:                                             # круг 6 и дальше — против текущей тетради v3.4
        import ai_question_v34 as V4
        base['P4'] = V4.P4
    base_name = ' + '.join(base)
    lines = [f"Портфель 6 мест: {base_name} против {base_name} + {', '.join(names)} ({mode})"]
    for split in splits:
        a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
        weeks = (b - a).days / 7
        for label, patterns in (('основа', base),
                                ('+ новое', {**base, **{f'N{i}': as_pattern(rules[n]) for i, n in enumerate(names, 1)}})):
            B.PATTERNS = patterns
            evs = B.alerts(data, split)
            got = B.play(evs, B.outcomes(data, evs), lambda t, c, h, f: [p for p, _ in c])
            r = np.array([x[0] for x in got])
            eq = np.cumsum(r)
            dd = float(np.min(eq - np.maximum.accumulate(eq))) if len(r) else 0.0
            by = ', '.join(f"{k} {sum(1 for x in got if x[1] == k)}" for k in patterns)
            lines.append(f"  {split:6s} {label:8s} {len(r):4d} сд {len(r) / weeks:4.1f}/нед сумма {r.sum():+6.1f}R "
                         f"на сделку {r.mean() if len(r) else float('nan'):+.3f} просадка {dd:6.1f}R  ({by})")
    print('\n'.join(lines), flush=True)
    out = f'ai_round{k}_portfolio_{mode}.txt' if k else f'ai_round5_portfolio_{mode}.txt'
    with open(os.path.join(HERE, 'results', out), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1] == 'a2021':
        stage_a()
    elif sys.argv[1] == 'portfolio':
        portfolio(sys.argv[2], sys.argv[3:])
    elif sys.argv[1] == 'y2021':
        round_2021(int(sys.argv[2]), sys.argv[3:])
    elif sys.argv[1] == 'portfolio3':
        portfolio(sys.argv[2], sys.argv[4:], k=int(sys.argv[3]))
