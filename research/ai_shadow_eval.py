"""
Оценка вердиктов ИИ в тени (Live_Bot/strategies/llm/llm_shadow.py, с 10.10.2026).

Вердикт «брал бы / не брал бы» стоит на каждой заявке тестовых счетов других
стратегий. Здесь вердикт сводится с исходом сделки (paper_trades.jsonl): та же
стратегия, пара и сторона, налив после вердикта и не позже 30 суток, плановый
вход тот же (±0.2%). Не налившиеся заявки исхода не имеют и в сравнение не идут.

Вопрос один: лучше ли сделки, которые модель взяла бы, тех, что она пропустила
бы. Разница R на сделку с 95% интервалом бутстрапа; по стратегиям и по
уверенности — для понимания, не для решения. Правило решения записано ДО
данных (docs/ИИ_замечания_на_проверку.md, п. 87): польза есть, если при ≥ 100
вердиктах с исходом «брал бы» лучше «не брал бы» и нижняя граница разницы > 0.

    python research/ai_shadow_eval.py <paper_trades.jsonl> <llm_shadow.jsonl>
"""
import json
import sys
from datetime import datetime, timedelta

import numpy as np

MATCH_DAYS = 30
ENTRY_TOL = 0.002


def rows(path):
    with open(path, encoding='utf-8') as fh:
        return [json.loads(x) for x in fh if x.strip()]


def ts(text):
    return datetime.fromisoformat(str(text).replace('Z', '+00:00'))


def match(verdicts, trades):
    """Вердикт -> R его сделки (или None). Одна сделка — одному вердикту."""
    used = set()
    out = []
    for v in sorted(verdicts, key=lambda r: r['at']):
        at = ts(v['at'])
        best = None
        for i, t in enumerate(trades):
            if i in used or t.get('strategy') != v['strategy'] or t.get('pair') != v['pair']:
                continue
            if str(t.get('direction', '')).upper() != v['direction']:
                continue
            opened = ts(t['open_time'])
            if not (at - timedelta(minutes=10) <= opened <= at + timedelta(days=MATCH_DAYS)):
                continue
            planned = float(t.get('planned_entry') or t.get('entry_price') or 0)
            if not planned or abs(planned / float(v['entry']) - 1) > ENTRY_TOL:
                continue
            if best is None or opened < ts(trades[best]['open_time']):
                best = i
        if best is not None:
            used.add(best)
            out.append((v, float(trades[best]['pnl_r'])))
        else:
            out.append((v, None))
    return out


def boot_diff(a, b, n=5000, seed=1):
    rng = np.random.default_rng(seed)
    a, b = np.asarray(a), np.asarray(b)
    d = [rng.choice(a, len(a)).mean() - rng.choice(b, len(b)).mean() for _ in range(n)]
    return np.percentile(d, [2.5, 97.5])


def line(name, rs):
    if not rs:
        return f'  {name:28s} нет'
    return f'  {name:28s} {len(rs):4d} сд  {np.mean(rs):+.3f} R/сд  сумма {np.sum(rs):+7.2f}R'


def main(trades_path, shadow_path):
    trades = [t for t in rows(trades_path) if t.get('pnl_r') not in (None, '')]
    verdicts = [v for v in rows(shadow_path) if 'take' in v]
    errors = [v for v in rows(shadow_path) if 'take' not in v]
    pairs = match(verdicts, trades)
    done = [(v, r) for v, r in pairs if r is not None]
    print(f'вердиктов {len(verdicts)} (без вердикта {len(errors)}), с исходом {len(done)}, '
          f'ещё без исхода {len(pairs) - len(done)}')
    yes = [r for v, r in done if v['take']]
    no = [r for v, r in done if not v['take']]
    print(line('брал бы', yes))
    print(line('не брал бы', no))
    print(line('все (как торгуют сейчас)', yes + no))
    if len(yes) >= 5 and len(no) >= 5:
        lo, hi = boot_diff(yes, no)
        print(f'  разница «брал бы − не брал бы»: {np.mean(yes) - np.mean(no):+.3f} R/сд [{lo:+.3f}; {hi:+.3f}]')
        verdict = ('ПОЛЬЗА ЕСТЬ' if len(done) >= 100 and lo > 0 else
                   'данных мало (нужно ≥ 100 с исходом)' if len(done) < 100 else 'пользы не видно')
        print(f'  по правилу п. 87: {verdict}')
    print('\nпо стратегиям:')
    for s in sorted({v['strategy'] for v, _ in done}):
        y = [r for v, r in done if v['strategy'] == s and v['take']]
        n = [r for v, r in done if v['strategy'] == s and not v['take']]
        print(f'  {s:6s} брал бы {len(y):3d} {np.mean(y) if y else 0:+.3f} | не брал бы {len(n):3d} '
              f'{np.mean(n) if n else 0:+.3f}')
    print('\nпо уверенности (брал бы +, не брал бы −):')
    for c in range(1, 6):
        y = [r for v, r in done if v['take'] and v.get('conf') == c]
        n = [r for v, r in done if not v['take'] and v.get('conf') == c]
        print(f'  {c}/5  брал бы {len(y):3d} {np.mean(y) if y else 0:+.3f} | не брал бы {len(n):3d} '
              f'{np.mean(n) if n else 0:+.3f}')


if __name__ == '__main__':
    main(*sys.argv[1:3])
