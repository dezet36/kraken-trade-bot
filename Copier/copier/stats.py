"""
Статистика сделок участников: по счёту, участнику, стратегии, всем вместе.
Считается из своей базы (сделки копировщика), а не из чужих файлов.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from copier.db import parse_iso


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize(trades):
    """Закрытые сделки → число, доля прибыльных, P&L $, сумма и средний R, худший ряд, просадка в R."""
    closed = [t for t in trades if t.get('state') == 'closed']
    closed.sort(key=lambda t: t.get('closed_at') or '')
    pnls = [_f(t.get('pnl_usd')) for t in closed]
    rs = [_f(t.get('pnl_r')) for t in closed if _f(t.get('pnl_r')) is not None]
    known = [p for p in pnls if p is not None]
    wins = [p for p in known if p > 0]
    losses = [p for p in known if p < 0]
    run = peak = dd = 0.0
    streak = worst_streak = 0
    for r in rs:
        run += r
        peak = max(peak, run)
        dd = min(dd, run - peak)
        streak = streak + 1 if r < 0 else 0
        worst_streak = max(worst_streak, streak)
    return {
        'trades': len(closed),
        'open': sum(1 for t in trades if t.get('state') == 'open'),
        'pending': sum(1 for t in trades if t.get('state') in ('new', 'pending')),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(len(wins) / len(known) * 100, 1) if known else None,
        'pnl_usd': round(sum(known), 2),
        'sum_r': round(sum(rs), 2),
        'avg_r': round(sum(rs) / len(rs), 3) if rs else None,
        'best_usd': round(max(known), 2) if known else None,
        'worst_usd': round(min(known), 2) if known else None,
        'profit_factor': round(sum(wins) / -sum(losses), 2) if losses else None,
        'max_dd_r': round(dd, 2),
        'loss_streak': worst_streak,
    }


def by(trades, field):
    groups = defaultdict(list)
    for t in trades:
        groups[t.get(field)].append(t)
    return {k: summarize(v) for k, v in groups.items()}


def since(trades, days):
    edge = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for t in trades:
        at = parse_iso(t.get('closed_at') or t.get('created_at'))
        if at and at >= edge:
            out.append(t)
    return out


def periods(trades):
    """P&L за день, неделю, месяц и всё время."""
    return {'day': summarize(since(trades, 1)), 'week': summarize(since(trades, 7)),
            'month': summarize(since(trades, 30)), 'all': summarize(trades)}


def risk_per_trade(status, risk_pct):
    """
    Сколько $ участник рискует в одной сделке: баланс ЕГО счёта × риск счёта-мастера
    (как в sizing.plan_entry). У 10к — 100 $, у 100к — 1000 $ при одном и том же 1%.
    """
    balance = _f(status.get('accountBalance')) or _f(status.get('currentEquity'))
    if not balance or not risk_pct:
        return None
    return round(balance * float(risk_pct) / 100, 2)


def challenge(status):
    """Прогресс фазы пропа по снимку счёта: цель, просадки, профитные дни, правило 30%."""
    eq = _f(status.get('currentEquity'))
    initial = _f(status.get('initialAccountBalance'))
    day_start = _f(status.get('dayStartEquity'))
    out = {'equity': eq, 'balance': _f(status.get('accountBalance')), 'initial': initial,
           'phase': status.get('phase'), 'status': status.get('status'),
           'antifraud': status.get('antifraudStatus'), 'lock': status.get('tradingLockReason')}
    if eq is not None and initial:
        out['profit_pct'] = round((eq - initial) / initial * 100, 2)
    target = _f(status.get('profitTarget'))
    if target:
        out['target_pct'] = target
        if out.get('profit_pct') is not None:
            out['target_progress'] = round(max(out['profit_pct'], 0) / target * 100, 1)
    daily = _f(status.get('maxDailyDrawdown'))
    if daily and day_start and eq is not None:
        used = max(day_start - eq, 0) / day_start * 100
        out['daily_dd_used_pct'] = round(used, 2)
        out['daily_dd_limit_pct'] = daily
    total = _f(status.get('maxTotalDrawdown'))
    if total and initial and eq is not None:
        out['total_dd_used_pct'] = round(max(initial - eq, 0) / initial * 100, 2)
        out['total_dd_limit_pct'] = total
    out['profitable_days'] = status.get('profitableDays')
    out['min_days'] = status.get('minTradingDays')
    out['consistency_ratio'] = _f(status.get('consistencyRuleRatio'))
    out['consistency_limit'] = _f(status.get('consistencyRuleLimit'))
    out['consistency_met'] = status.get('consistencyRuleMet')
    return out
