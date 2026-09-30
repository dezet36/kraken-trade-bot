"""
Как часто тетрадь v3.4 (P1 + B3 + P4) молчит — по истории (30.09.2026).

Не проверка правила, а ожидание для владельца: сколько недель без единой
сделки, какие паузы между входами, сколько сделок в неделю — механика, 6 мест,
«брать все», как в портфельных расчётах п. 75–77. Живую паузу сравнивать с
этим, а не с «в среднем 3–6 в неделю».

    python research/ai_notebook_quiet_weeks.py std     # train и valid
    python research/ai_notebook_quiet_weeks.py 2021    # 2021 (15 пар с данными)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:2] == ['2021']:
    os.environ['FLOW_CACHE'] = 'flow_cache_2021'      # до импорта стенда: он читает папку при импорте
sys.path.insert(0, HERE)

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

import ai_model_trader_bt as B                        # noqa: E402
import ai_pattern_lab as L                            # noqa: E402
import ai_question_v34 as V4                          # noqa: E402


def run(mode):
    if mode == '2021':
        import ai_notebook_2021 as Y                  # ставит L.SPLITS['y2021'] и готовит данные 2021
        data, splits = Y.prepare(), ['y2021']
    else:
        data, splits = B.prepare(), ['train', 'valid']
    B.PATTERNS = {**{k: v for k, v in B.PATTERNS.items() if k in ('P1', 'B3')}, 'P4': V4.P4}
    lines = []
    for split in splits:
        a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
        evs = B.alerts(data, split)
        entries = []

        def take_all(t, cand, held, free):
            entries.extend((t, k) for _, k in cand[:free])
            return [p for p, _ in cand]
        B.play(evs, B.outcomes(data, evs), take_all)
        times = pd.DatetimeIndex(sorted({t for t, _ in entries}))
        weeks = pd.date_range(a.normalize() - pd.Timedelta(days=a.weekday()), b, freq='7D')
        per_week = np.array([((times >= w) & (times < w + pd.Timedelta(days=7))).sum() for w in weeks[:-1]])
        n_per_week = np.array([sum(1 for t, _ in entries if w <= t < w + pd.Timedelta(days=7)) for w in weeks[:-1]])
        edges = times.insert(0, a).append(pd.DatetimeIndex([b]))
        gaps = pd.Series(edges).diff().dropna().dt.total_seconds().to_numpy() / 86_400
        lines += [f'{split}: {len(entries)} сделок за {len(per_week)} недель ({len(entries) / len(per_week):.1f} в неделю), '
                  f'часов со входом {len(times)}',
                  f'   недель без сделок: {(per_week == 0).mean():.0%}; с 1–2: {((n_per_week >= 1) & (n_per_week <= 2)).mean():.0%}; '
                  f'с 3–6: {((n_per_week >= 3) & (n_per_week <= 6)).mean():.0%}; с 7+: {(n_per_week >= 7).mean():.0%}',
                  f'   пауза между входами, дней: медиана {np.median(gaps):.1f}, 75% {np.quantile(gaps, 0.75):.1f}, '
                  f'90% {np.quantile(gaps, 0.9):.1f}, самая длинная {gaps.max():.0f} '
                  f'({edges[int(np.argmax(gaps))]:%d.%m.%Y} … {edges[int(np.argmax(gaps)) + 1]:%d.%m.%Y}); '
                  f'пауз ≥ 14 дней: {int((gaps >= 14).sum())}',
                  f'   по закономерностям: ' + ', '.join(f"{k} {sum(1 for _, x in entries if x == k)}" for k in B.PATTERNS)]
    text = '\n'.join(lines)
    print(text, flush=True)
    with open(os.path.join(HERE, 'results', f'ai_notebook_quiet_weeks_{mode}.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    run(sys.argv[1])
