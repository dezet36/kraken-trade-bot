"""
Почти прошедшие правила — повтор на данных, на которые они не смотрели (30.09.2026, п. 83).
Условия записаны до расчёта (docs/ИИ_замечания_на_проверку.md, п. 83).

    python research/ai_nearmiss_replication.py std     # (б) 22 монеты сверх основных, train + valid вместе
    python research/ai_nearmiss_replication.py 2021    # (а) 2021, 15 пар с данными
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:2] == ['2021']:
    os.environ['FLOW_CACHE'] = 'flow_cache_2021'      # до импорта стенда: он читает папку при импорте
sys.path.insert(0, HERE)

import ai_new_sources as S                            # noqa: E402

L = S.install()
import ai_model_trader_bt as B                        # noqa: E402
import ai_universe_extend as U                        # noqa: E402

MIN_TRADES = 30


def candidates():
    r6 = {x['rule']['name']: x['rule'] for x in
          json.load(open(os.path.join(HERE, 'results', 'model_research', 'round6_scored.json'), encoding='utf-8'))}
    h7 = {r['name']: r for r in json.load(open(os.path.join(HERE, 'results', 'ai_round7_hyp_rules.json'), encoding='utf-8'))}
    return [r6['crowded_long_funding_short'], h7['H3_fear_spike_long']]


def main(mode):
    if mode == '2021':
        import ai_notebook_2021 as Y                  # ставит L.SPLITS['y2021'] и готовит данные 2021
        data, split, label = S.enrich(Y.prepare()), 'y2021', '(а) 2021, пары с данными'
    else:
        base = B.prepare()                            # 30 пар тетради, широта по основным
        btc, breadth = base['BTCUSDT'][0], base['BTCUSDT'][1]['cascade_count_3h']
        data = {p: v for p, v in base.items() if p in B.EXTRA}
        data.update(U.load('flow_cache_new', btc, breadth, U.NEW))
        S.enrich(data)
        L.SPLITS['tv'] = (L.SPLITS['train'][0], L.SPLITS['valid'][1])
        split, label = 'tv', f'(б) {len(data)} монет сверх основных, train + valid 2022-01…2025-04'
    lines = [f'Повтор почти прошедших (п. 83), {label}: проход — R > 0 и ≥ {MIN_TRADES} сделок; t ≥ 2 хотя бы в одном из двух']
    for rule in candidates():
        s, _ = L.evaluate(rule, data=data, splits=(split,))
        x = s[split]
        lines.append(f"  {rule['name']:28s} {x['n']:5d} сд ({x['per_week']:.1f}/нед) R {x['r'] if x['n'] else float('nan'):+.3f} "
                     f"t {x['t'] if x['n'] > 1 else float('nan'):+.2f} плюс {x['win'] * 100 if x['n'] else 0:.0f}%  "
                     f"{'R > 0 и ≥ 30' if x['n'] >= MIN_TRADES and x['r'] > 0 else 'НЕТ'}")
    text = '\n'.join(lines)
    print(text, flush=True)
    with open(os.path.join(HERE, 'results', f'ai_nearmiss_{mode}.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1])
