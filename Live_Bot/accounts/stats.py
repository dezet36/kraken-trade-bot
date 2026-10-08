"""
Статистика стратегий и их тестовых счетов (реорганизация, этап 6, 08.10.2026).

Что владелец хочет видеть по стратегии: сколько сетапов она дала, сколько дошло
до входа, сколько отработало в плюс и в минус — и всё, что нужно для разбора:
качество сделок, разрезы по сторонам, парам, причинам выхода и месяцам, что не
стало сделкой и почему (с тем, чем это кончилось бы). По счёту — депозит,
капитал, итог, просадка, открытый риск, настройки.

Источник один — журнал сетапов (setup_journal: строка на сетап — сделка,
открытая позиция, ждущая заявка, снятая заявка, отказ предела счёта с тенью,
план ИИ) и состояние бумажного брокера. Здесь только счёт, без записи: числа
берутся из того же, что видит выгрузка в Excel.
"""

import math
import time
from collections import Counter

import setup_journal as sj

# Этапы журнала, ставшие ЗАЯВКОЙ (до входа дошло или могло дойти).
ORDER_STAGES = (sj.TRADE, sj.OPEN, sj.PENDING, sj.DROPPED)
PLAN_STAGES = (sj.PLAN_REFUSED, sj.PLAN_DIED, sj.PLAN_ARMED, sj.PLAN_LOST)
WIN, LOSS, FLAT = 'плюс', 'минус', 'ноль'
# Исходы тени (shadow): чем кончился бы сетап, который не пустил предел счёта.
SHADOW_FINISHED = ('цель', 'частично', 'стоп')


def _num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _at(row):
    """Момент сетапа (секунды UTC) — по нему период."""
    return row.get('_placed') or row.get('_t') or 0


def window(rows, days=None, now=None):
    """Сетапы за последние days суток (None — все)."""
    if not days:
        return list(rows)
    edge = (now if now is not None else time.time()) - float(days) * 86400
    return [r for r in rows if _at(r) >= edge]


def _closed_order(trades):
    """Сделки по времени выхода — порядок кривой результата."""
    return sorted(trades, key=lambda t: (str(t.get('closed_at') or ''), _at(t)))


def quality(trades):
    """Качество закрытых сделок: доля выигрышей, R, профит-фактор, просадка в R,
    худшая серия, ход за нас и против, время в сделке."""
    trades = _closed_order(trades)
    n = len(trades)
    rs = [r for r in (_num(t.get('pnl_r')) for t in trades) if r is not None]
    usd = [u for u in (_num(t.get('pnl_usd')) for t in trades) if u is not None]
    wins = sum(1 for t in trades if t.get('outcome') == WIN)
    losses = sum(1 for t in trades if t.get('outcome') == LOSS)
    gross_win = sum(r for r in rs if r > 0)
    gross_loss = -sum(r for r in rs if r < 0)

    peak = curve = max_dd = 0.0
    for r in rs:
        curve += r
        peak = max(peak, curve)
        max_dd = max(max_dd, peak - curve)
    streak = worst_streak = 0
    for t in trades:
        streak = streak + 1 if t.get('outcome') == LOSS else 0
        worst_streak = max(worst_streak, streak)

    minutes = sorted(m for m in (_num(t.get('duration_min')) for t in trades) if m is not None)
    mfe = [m for m in (_num(t.get('mfe_r')) for t in trades) if m is not None]
    mae = [m for m in (_num(t.get('mae_r')) for t in trades) if m is not None]
    return {
        'trades': n, 'wins': wins, 'losses': losses, 'flat': n - wins - losses,
        'win_rate': round(wins / n, 4) if n else None,
        'total_r': round(sum(rs), 3), 'avg_r': round(sum(rs) / len(rs), 3) if rs else None,
        'total_usd': round(sum(usd), 2),
        'profit_factor': round(gross_win / gross_loss, 3) if gross_loss else None,
        'best_r': round(max(rs), 3) if rs else None, 'worst_r': round(min(rs), 3) if rs else None,
        'max_dd_r': round(max_dd, 3), 'max_loss_streak': worst_streak,
        'median_minutes': minutes[len(minutes) // 2] if minutes else None,
        'avg_mfe_r': round(sum(mfe) / len(mfe), 3) if mfe else None,
        'avg_mae_r': round(sum(mae) / len(mae), 3) if mae else None,
    }


def breakdown(trades, key):
    """Сделки по ключу: сколько, сколько в плюс, итог и средний R."""
    groups = {}
    for t in trades:
        name = key(t)
        if name in (None, ''):
            continue
        g = groups.setdefault(str(name), {'name': str(name), 'trades': 0, 'wins': 0, 'total_r': 0.0})
        g['trades'] += 1
        g['wins'] += t.get('outcome') == WIN
        g['total_r'] += _num(t.get('pnl_r')) or 0.0
    out = []
    for g in groups.values():
        g['total_r'] = round(g['total_r'], 3)
        g['avg_r'] = round(g['total_r'] / g['trades'], 3)
        g['win_rate'] = round(g['wins'] / g['trades'], 4)
        out.append(g)
    return sorted(out, key=lambda g: -g['total_r'])


def funnel(rows):
    """Путь сетапов: сколько найдено, сколько стало заявкой, налилось, закрылось
    и чем, сколько снято и не пущено, сколько планов ИИ не дошло до заявки."""
    stages = Counter(r.get('stage') for r in rows)
    trades = [r for r in rows if r.get('stage') == sj.TRADE]
    outcomes = Counter(t.get('outcome') for t in trades)
    return {
        'setups': len(rows),
        'orders': sum(stages[s] for s in ORDER_STAGES),
        'filled': stages[sj.TRADE] + stages[sj.OPEN],
        'closed': stages[sj.TRADE], 'open': stages[sj.OPEN], 'pending': stages[sj.PENDING],
        'wins': outcomes[WIN], 'losses': outcomes[LOSS], 'flat': outcomes[FLAT],
        'dropped': stages[sj.DROPPED], 'refused': stages[sj.SHADOW],
        'plans': sum(stages[s] for s in PLAN_STAGES),
    }


def not_traded(rows):
    """Что не стало сделкой: снятые заявки по причинам, отказы счёта по тому,
    что не пустило, — и чем кончились бы (тень)."""
    dropped = Counter(r.get('outcome') or 'не налилась' for r in rows if r.get('stage') == sj.DROPPED)
    refused = {}
    for r in rows:
        if r.get('stage') != sj.SHADOW:
            continue
        g = refused.setdefault(r.get('gate') or 'предел', {'gate': r.get('gate') or 'предел',
                                                             'count': 0, 'outcomes': Counter(),
                                                             'would_r': 0.0, 'finished': 0})
        g['count'] += 1
        outcome = r.get('outcome') or 'наблюдается'
        g['outcomes'][outcome] += 1
        if outcome in SHADOW_FINISHED:
            g['finished'] += 1
            g['would_r'] += _num(r.get('pnl_r')) or 0.0
    out = []
    for g in refused.values():
        g['outcomes'] = dict(g['outcomes'])
        g['would_r'] = round(g['would_r'], 3)
        out.append(g)
    return {'dropped': dict(dropped), 'refused': sorted(out, key=lambda g: -g['count'])}


def report(strategy, rows, days=None, now=None):
    """Всё по стратегии за период: воронка, качество, разрезы, несделанное."""
    rows = window(rows, days, now)
    trades = [r for r in rows if r.get('stage') == sj.TRADE]
    pairs = breakdown(trades, lambda t: t.get('pair'))
    return {
        'strategy': strategy, 'days': days,
        'funnel': funnel(rows),
        'quality': quality(trades),
        'sides': breakdown(trades, lambda t: t.get('direction')),
        'exits': breakdown(trades, lambda t: t.get('exit_reason')),
        'months': sorted(breakdown(trades, lambda t: str(t.get('closed_at') or '')[:7]),
                         key=lambda g: g['name']),
        'pairs_best': pairs[:5], 'pairs_worst': list(reversed(pairs[-5:])) if len(pairs) > 5 else [],
        'not_traded': not_traded(rows),
    }


def account(strategy, broker=None, trades=None):
    """
    Тестовый счёт стратегии: настройки (accounts/paper.py) и деньги брокера —
    депозит, капитал, итог, просадка счёта по закрытым сделкам с перезапуска,
    открытый риск.
    """
    from accounts import paper
    out = {'settings': paper.describe(strategy)}
    if broker is None:
        return out
    start = float(broker.start_balance(strategy) or 0)
    equity = float(broker.equity(strategy) or 0)
    positions = broker.positions(strategy)
    pending = broker.pending(strategy)
    open_risk = sum(broker._live_risk(p) for p in positions.values())
    open_risk += sum(float(o.get('risk_amount') or 0) for o in pending.values())
    since = broker.reset_at(strategy) if hasattr(broker, 'reset_at') else None
    curve = peak = start
    max_dd = 0.0
    for t in _closed_order(trades or []):
        if since and str(t.get('closed_at') or '') < str(since):
            continue
        curve += _num(t.get('pnl_usd')) or 0.0
        peak = max(peak, curve)
        if peak > 0:
            max_dd = max(max_dd, (peak - curve) / peak * 100)
    out.update({
        'start': round(start, 2), 'balance': round(float(broker.balance(strategy) or 0), 2),
        'equity': round(equity, 2),
        'return_pct': round((equity / start - 1) * 100, 3) if start else None,
        'max_dd_pct': round(max_dd, 3), 'open_risk': round(open_risk, 2),
        'open_risk_pct': round(open_risk / equity * 100, 3) if equity else None,
        'positions': len(positions), 'pending': len(pending), 'since': since,
    })
    return out
