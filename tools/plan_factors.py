# -*- coding: utf-8 -*-
"""
Какие свойства плана ИИ связаны с исходом: сторона, условие входа, факторы,
R:R, ширина стопа, дальность цели в дневных размахах, удалённость входа, вид
уровней, тренд 4ч, сессия. Исход — «идея» (цель раньше стопа, без входа) и R
при исполнении ботом, лимитом без условия 12 ч и по рынку.

Разбор и симуляция — из tools/rejected_outcomes.py и tools/idea_vs_rules.py.
Запуск на сервере: /opt/kraken/venv/bin/python tools/plan_factors.py
Выборки малые: смотреть только на крупные и согласные между собой различия.
"""
import collections
import io
import contextlib

_src = open('/opt/kraken/code/tools/idea_vs_rules.py', encoding='utf-8').read()
with contextlib.redirect_stdout(io.StringIO()):
    exec(_src[:_src.index('\nitems = []\n')])

from smc import structure as S

_h4 = {}


def trend4(pair, ts):
    if pair not in _h4:
        try:
            d4 = exchange.fetch_ohlcv('4h', limit=400, symbol=pair)
            _h4[pair] = (d4, S.build_structure(d4, tier='swing'))
        except Exception:
            _h4[pair] = (None, None)
    d4, st = _h4[pair]
    if d4 is None or st is None:
        return ''
    t4 = np.asarray(d4['timestamp'].values).astype('datetime64[ms]').astype('int64')
    j = int(np.searchsorted(t4, ts, side='right')) - 1
    return S.state_at(st, j).get('trend') if j >= 10 else ''


def extra(text, levels_json):
    """Факторы и подписи уровней входа, стопа и цели."""
    if '</think>' in (text or ''):
        text = text.split('</think>', 1)[1]
    try:
        d = json.loads((text or '').strip())
        table = {a: k for a, _b, k in json.loads(levels_json or '[]')}
    except Exception:
        return None

    def kind(v):
        m = re.match(r'\s*(L\d+)', str(v or ''))
        return table.get(m.group(1), '') if m else ''
    tps = d.get('tp') or []
    return {'cf': d.get('cf') or {}, 'k_entry': kind(d.get('entry')),
            'k_stop': kind(d.get('stop')), 'k_tp': kind(tps[0] if tps else None)}


KINDS = (('слом структуры', 'слом'), ('ордер-блок', 'ордер-блок'), ('зоны', 'зона'),
         ('имбаланс', 'имбаланс'), ('брейкер', 'брейкер'), ('EQH', 'EQH/EQL'),
         ('EQL', 'EQH/EQL'), ('скоплен', 'скопление'), ('ликвидаций', 'ликвидации'),
         ('свинг', 'свинг'), ('пивот', 'пивот'), ('POC', 'профиль'), ('VAH', 'профиль'),
         ('VAL', 'профиль'), ('максимум', 'экстремум'), ('минимум', 'экстремум'))


def kind_class(k):
    for word, name in KINDS:
        if word in (k or ''):
            return name
    return 'прочее'


def bucket(v, edges, fmt='{:g}'):
    for e in edges:
        if v < e:
            return '< ' + fmt.format(e)
    return '≥ ' + fmt.format(edges[-1])


items = []
for r in rows:
    if r['decision'] == 'enter':
        text, gate = r.get('raw'), 'ПРИНЯТ'
    else:
        text, gate = r.get('raw'), r['gate']
        if plan_of(text, r.get('levels')) is None and r.get('rejected_raw'):
            text, gate = r.get('rejected_raw'), (r.get('revised_from') or r['gate']).split(':', 1)[0]
    p = plan_of(text, r.get('levels'))
    if p is None:
        continue
    d = prep(r['pair'], r['at'], p)
    x = extra(text, r.get('levels'))
    if d is None or x is None:
        continue
    i0, long = d['i0'], d['long']
    price = d['cl'][i0 - 1]
    ts = int(datetime.datetime.fromisoformat(r['at'].replace('Z', '+00:00')).timestamp() * 1000)
    lo_i = max(0, i0 - 24 * 14)
    days = [(d['hi'][j:j + 24].max() - d['lo'][j:j + 24].min()) / d['cl'][j] * 100
            for j in range(lo_i, i0 - 24, 24)]
    day_range = float(np.median(days)) if days else None
    stop_pct = abs(p['entry'] - d['stop']) / p['entry'] * 100
    tp_pct = abs(p['tp'] - p['entry']) / p['entry'] * 100
    pull = (p['entry'] < price) if long else (p['entry'] > price)
    tr = trend4(r['pair'], ts)
    align = ('по тренду 4ч' if (tr == 'BULLISH' and long) or (tr == 'BEARISH' and not long)
             else 'против 4ч' if tr in ('BULLISH', 'BEARISH') else 'тренда 4ч нет')
    hour = int(r['at'][11:13])
    items.append({
        'at': r['at'], 'group': 'принят' if gate == 'ПРИНЯТ' else 'отклонён',
        'сторона': p['side'], 'условие': p['when'],
        'факторов': str(sum(1 for v in x['cf'].values() if v)),
        **{f'фактор {k}': ('есть' if x['cf'].get(k) else 'нет') for k in ('poi', 'vp', 'der', 'smc', 'flow')},
        'R:R плана': bucket(tp_pct / stop_pct, (2.5, 3.5, 5)),
        'стоп, %': bucket(stop_pct, (0.8, 1.5, 3)),
        'цель / дневной размах': bucket(tp_pct / day_range, (0.5, 1.0, 1.5)) if day_range else '—',
        'вход': ('на откате' if pull else 'на пробое') + ', ' + bucket(abs(p['entry'] - price) / price * 100, (0.5, 1.5, 3), '{:g}%'),
        'вход — уровень': kind_class(x['k_entry']),
        'стоп — уровень': kind_class(x['k_stop']),
        'цель — уровень': kind_class(x['k_tp']),
        'тренд 4ч': align,
        'сессия UTC': 'Азия 0-7' if hour < 7 else 'Европа 7-13' if hour < 13 else 'США 13-21' if hour < 21 else 'ночь 21-24',
        'idea': idea(d), 'бот': simulate(r['pair'], r['at'], p),
        'лимит 12ч': limit(d, p['entry'], 12), 'по рынку': market(d),
    })


def cell(sel, pol):
    v = [x[pol] for x in sel if x[pol] is not None]
    k = collections.Counter(o for o, _ in v)
    return '%2d/%-2d %+6.1f' % (k['цель'], k['стоп'], sum(r for _, r in v))


def report(feature):
    print(f'\n{feature}')
    by = collections.defaultdict(list)
    for x in items:
        by[x[feature]].append(x)
    for key in sorted(by, key=lambda k: -len(by[k])):
        sel = by[key]
        c = collections.Counter(x['idea'] for x in sel)
        dec = c['цель раньше'] + c['стоп раньше']
        share = '%3.0f%%' % (c['цель раньше'] / dec * 100) if dec else '  — '
        print(f'  {key:26} {len(sel):3} | идея {share} ({c["цель раньше"]:2}/{dec:2}) | '
              f'бот {cell(sel, "бот")} | лимит {cell(sel, "лимит 12ч")} | рынок {cell(sel, "по рынку")}')


print(f'Планов «войти»: {len(items)}. В ячейках: целей/стопов и сумма R за вычетом комиссий.')
for f in ('group', 'сторона', 'тренд 4ч', 'условие', 'факторов', 'фактор poi', 'фактор vp',
          'фактор der', 'фактор smc', 'фактор flow', 'R:R плана', 'стоп, %', 'цель / дневной размах',
          'вход', 'вход — уровень', 'стоп — уровень', 'цель — уровень', 'сессия UTC'):
    report(f)
