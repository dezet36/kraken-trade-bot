"""
Журнал сетапов стратегии: всё, что она нашла, — и чем это кончилось.

ЗАЧЕМ. Сведения о сетапе лежали в шести местах: сделки — в paper_trades.jsonl,
что было с ценой после выхода — в follow_up.csv, отказы предохранителей
брокера — в shadow_trades.csv, планы ИИ и их исходы — в llm_calls.csv и
llm_outcomes.csv, судьба взведённых планов — в refused.csv. А выставленная,
но не налившаяся заявка не записывалась нигде: строка лога и сообщение в
Telegram (за четверо суток до 23.09.2026 так ушли 47 заявок).

Здесь — один журнал на стратегию: строка на сетап. «Этап» говорит, чем сетап
кончился для нас (сделка, снятая заявка, не пустил предел, план ИИ не дошёл
до заявки), «исход» — что сделала цена, «что не пустило» — почему не сделка.
Собирается по запросу из первоисточников и их не дублирует; единственный
новый источник — снятые заявки (orders_dropped.csv, пишет брокер).

ФОРМАТ — ДЛЯ EXCEL С РУССКОЙ РАЗМЕТКОЙ: разделитель «;», десятичная запятая,
UTF-8 с меткой BOM, заголовки по-русски, время — UTC. Машиной читается так:
pandas.read_csv(path, sep=';', decimal=',', encoding='utf-8-sig').

Только фантомный счёт: боевой журнал (trades_journal.csv) устроен иначе.
"""

import csv
import io
import json
import math
import os
import re
import time
from datetime import datetime, timezone

import config
import csv_journal
from logger import log

DROPPED_CSV = os.path.join(config.DATA_DIR, 'orders_dropped.csv')

LLM = 'LLM'

# ── Этапы: чем сетап кончился для нас ────────────────────────────────────────
TRADE = 'сделка'
OPEN = 'позиция открыта'
PENDING = 'заявка ждёт'
DROPPED = 'заявка снята'
SHADOW = 'не пустил предел'
PLAN_REFUSED = 'план отклонён'
PLAN_DIED = 'план не дошёл до заявки'
PLAN_ARMED = 'план ждёт условия'
PLAN_LOST = 'план принят, заявки нет'

# Отказы после одобрения плана (strategy_llm): план жил, но заявкой не стал.
# В refused.csv они с приставкой «ИИ: ».
LLM_LIFECYCLE = ('условие не наступило', 'цель достигнута без входа',
                 'рынок обогнал план', 'вердикт устарел', 'вход уже за ценой')

# Контекст сетапа — те же поля, что брокер кладёт в заявку (_context).
CONTEXT_KEYS = [
    'why', 'confirmed', 'missing', 'factors', 'confluence', 'zone', 'poi_type',
    'sweep', 'impulse_pct', 'score', 'htf_trend', 'htf_strength',
    'regime', 'regime_er',
    'llm_analysis', 'llm_trigger', 'llm_stop_why', 'llm_tp_why', 'llm_p',
    'llm_votes', 'llm_critic',
]

DROPPED_COLUMNS = [
    'mode', 'strategy', 'pair', 'direction', 'placed_at', 'dropped_at', 'reason',
    'planned_entry', 'limit_price', 'stop_loss', 'targets', 'rr',
    'cost_share_pct', 'risk_usd',
    # Как близко цена подходила ко входу (% от входа, >0 — не дошла) и сколько
    # R она прошла к цели, пока заявка ждала (paper_broker._process_pending).
    'min_gap_pct', 'best_run_r',
] + CONTEXT_KEYS

# Колонки журнала: ключ — подпись. Порядок — «как читает человек»: что и чем
# кончилось, когда, план, почему, рынок, разбор ИИ, итог, путь цены в сделке,
# после выхода, несостоявшийся вход, отказ.
COLUMNS = [
    ('stage', 'этап'), ('outcome', 'исход'), ('strategy', 'стратегия'),
    ('pair', 'пара'), ('direction', 'сторона'), ('trade_id', 'сделка №'),

    ('placed_at', 'сетап, UTC'), ('opened_at', 'вход, UTC'),
    ('closed_at', 'выход или снятие, UTC'),
    ('entry_wait_min', 'ожидание входа, мин'), ('duration_min', 'в сделке, мин'),
    ('hour_utc', 'час входа UTC'),

    ('planned_entry', 'вход по плану'), ('entry', 'вход'), ('stop', 'стоп'),
    ('targets', 'цели'), ('rr', 'R:R'), ('cost_share_pct', 'издержки, % риска'),
    ('risk_usd', 'риск, $'),

    ('why', 'почему'), ('confirmed', 'подтверждения'), ('missing', 'не хватило'),
    ('factors', 'факторы'), ('confluence', 'конфлюенс'), ('zone', 'зона'),
    ('poi_type', 'тип зоны'), ('sweep', 'вынос ликвидности'),
    ('impulse_pct', 'импульс, %'), ('score', 'оценка'),
    ('htf_trend', 'тренд 4ч'), ('htf_strength', 'сила тренда 4ч'),

    ('regime', 'режим рынка (BTC, 1д)'), ('regime_er', 'направленность BTC (ER)'),
    ('atr_pct', 'ATR, % цены'),

    ('llm_analysis', 'ИИ: разбор'), ('llm_trigger', 'ИИ: условие входа'),
    ('llm_stop_why', 'ИИ: почему стоп здесь'), ('llm_tp_why', 'ИИ: почему цель здесь'),
    ('llm_p', 'ИИ: вероятность'), ('llm_votes', 'ИИ: факторов из 5'),
    ('llm_critic', 'ИИ: критик'),
    # «сделана» / «выключена» (с 26.09.2026 переделки выключены: отказ,
    # который раньше ушёл бы на вторую попытку) — llm_calls.revision.
    ('llm_revision', 'ИИ: переделка'),

    ('exit_price', 'цена выхода'), ('exit_reason', 'причина выхода'),
    ('tps_hit', 'целей взято'), ('tp_min', 'цели взяты на минуте'),
    ('pnl_r', 'итог, R'), ('pnl_usd', 'итог, $'), ('fees_usd', 'комиссии, $'),
    ('funding_usd', 'фандинг, $'), ('breakeven_set', 'стоп в безубытке'),
    ('data_gap_min', 'дыра в свечах, мин'),

    ('mfe_r', 'лучший ход, R'), ('mae_r', 'худший ход, R'),
    ('mfe_min', 'лучший ход на минуте'), ('mae_min', 'худший ход на минуте'),
    ('first_1r_min', '+1R на минуте'), ('r_if_be_1r', 'итог с безубытком от +1R, R'),

    ('after_best_r', 'после выхода: лучший, R'), ('after_worst_r', 'после выхода: худший, R'),
    ('after_r_1h', 'после выхода: через 1ч, R'), ('after_r_4h', 'после выхода: через 4ч, R'),
    ('after_r_12h', 'после выхода: через 12ч, R'),
    ('after_hit_tp1', 'после выхода дошла до цели 1'),
    ('after_hit_sl', 'после выхода дошла до стопа'),

    ('min_gap_pct', 'ближе всего ко входу, %'), ('best_run_r', 'ушла к цели без нас, R'),
    ('entry_touched', 'вход задет'), ('entry_hours', 'вход задет через, ч'),
    ('hit_tp1', 'цель 1 достигнута'), ('tp_hours', 'цель 1 через, ч'),
    ('hit_sl', 'стоп задет'), ('sl_hours', 'стоп через, ч'),
    ('cond_hours', 'условие ИИ наступило бы через, ч'),
    ('fill_hours', 'налилась бы через, ч'), ('closed_hours', 'закрылась бы через, ч'),
    ('observed_hours', 'наблюдали, ч'),

    ('gate', 'что не пустило'), ('detail', 'подробности'), ('refusals', 'отказов подряд'),
]
KEYS = [key for key, _label in COLUMNS]
TIME_KEYS = ('placed_at', 'opened_at', 'closed_at')

_NUMERIC = re.compile(r'-?\d+(\.\d+)?([eE][-+]?\d+)?')


# ── Мелочи ───────────────────────────────────────────────────────────────────

def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec='seconds')


def _epoch(text):
    """Секунды UTC из ISO-строки; None, если строки нет или она не время."""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _float(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _same_price(a, b):
    a, b = _float(a), _float(b)
    return a is not None and b is not None and math.isclose(a, b, rel_tol=1e-6)


def _split(text, sep=';'):
    """«1.2;1.5» -> [1.2, 1.5]; нечисла пропускаются."""
    out = []
    for part in str(text or '').split(sep):
        value = _float(part.strip())
        if value is not None:
            out.append(int(value) if value.is_integer() and '.' not in part else value)
    return out


def _csv_rows(path):
    """Строки CSV текущего режима. Нет файла — пусто."""
    try:
        with open(path, encoding='utf-8', newline='') as fh:
            return [r for r in csv.DictReader(fh)
                    if (r.get('mode') or config.TRADING_MODE) == config.TRADING_MODE]
    except OSError:
        return []


def _jsonl_rows(path):
    out = []
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _context_columns(ctx):
    """Контекст заявки брокера (_context) — в колонки журнала."""
    ctx = ctx or {}
    llm = ctx.get('llm') or {}
    critic = llm.get('critic')
    factors = ctx.get('factors')
    return {
        'why': ctx.get('why', ''),
        'confirmed': '; '.join(ctx.get('confirmed') or []),
        'missing': '; '.join(ctx.get('missing') or []),
        'factors': ', '.join(factors) if isinstance(factors, (list, tuple)) else (factors or ''),
        'confluence': ctx.get('confluence', ''),
        'zone': ctx.get('zone', ''),
        'poi_type': ctx.get('poi_type', ''),
        'sweep': ctx.get('sweep', ''),
        'impulse_pct': ctx.get('impulse_pct', ''),
        'score': ctx.get('score', ''),
        'htf_trend': ctx.get('htf_trend', ''),
        'htf_strength': ctx.get('htf_strength', ''),
        'regime': ctx.get('regime', ''),
        'regime_er': ctx.get('regime_er', ''),
        'llm_analysis': llm.get('analysis', ''),
        'llm_trigger': llm.get('trigger', ''),
        'llm_stop_why': llm.get('stop_why', ''),
        'llm_tp_why': llm.get('tp_why', ''),
        'llm_p': llm.get('p', ''),
        'llm_votes': llm.get('votes', ''),
        'llm_critic': critic.get('verdict', '') if isinstance(critic, dict) else '',
    }


# ── Запись: снятые заявки ────────────────────────────────────────────────────

def record_dropped(strategy, pair, order, reason, now_ms):
    """
    Пишет снятую заявку. Молча: журнал не имеет права мешать торговле.

    order — заявка брокера целиком, с её контекстом; reason — его формулировка
    («лимит не заполнен за 48ч», «цена дошла до цели без нас», …).
    """
    if not order:
        return
    try:
        row = {
            'mode': config.TRADING_MODE, 'strategy': strategy, 'pair': pair,
            'direction': order.get('direction', ''),
            'placed_at': order.get('placed_at', ''),
            'dropped_at': _iso(now_ms), 'reason': reason,
            'planned_entry': order.get('planned_entry', ''),
            'limit_price': order.get('limit_price', ''),
            'stop_loss': order.get('stop_loss', ''),
            'targets': ';'.join(f'{float(t):.10g}' for t in (order.get('targets') or [])),
            'rr': round(float(order['rr']), 3) if order.get('rr') else '',
            'cost_share_pct': order.get('cost_share_pct', ''),
            'risk_usd': round(float(order['risk_amount']), 2) if order.get('risk_amount') else '',
            'min_gap_pct': order.get('min_gap_pct', ''),
            'best_run_r': order.get('best_run_r', ''),
            **_context_columns(order.get('context')),
        }
        csv_journal.append(DROPPED_CSV, DROPPED_COLUMNS, [row], 'снятые заявки')
    except Exception as exc:                           # noqa: BLE001
        log(f'⚠️ Снятая заявка {pair} не записана в журнал сетапов: {exc}')


# ── Сборка: источники по этапам ──────────────────────────────────────────────

def _trades(strategy):
    """Закрытые сделки — и что было с ценой после выхода (follow_up)."""
    if not config.PAPER_MODE:
        return []
    import follow_up
    import paper_broker
    after = {str(f.get('trade_id')): f for f in _csv_rows(follow_up.CSV_PATH)
             if f.get('strategy') == strategy}
    out = []
    for t in _jsonl_rows(paper_broker.JOURNAL_JSON):
        if t.get('strategy') != strategy:
            continue
        f = after.get(str(t.get('trade_id'))) or {}
        opened = _epoch(t.get('open_time'))
        wait = _float(t.get('entry_wait_min'))
        placed = opened - wait * 60 if opened is not None and wait is not None else opened
        targets = (_split(t.get('targets_all')) or
                   [x for x in (_float(t.get('tp1')), _float(t.get('tp2'))) if x is not None])
        # tps_hit брокера считает только ЧАСТИЧНЫЕ фиксации: выход по TP1 целиком
        # записан как «0 целей». Человеку нужно, сколько целей цена взяла.
        reason = str(t.get('exit_reason') or '')
        tps = int(reason[2:]) if re.fullmatch(r'TP\d+', reason) else t.get('tps_hit')
        out.append({
            'stage': TRADE,
            'outcome': {'WIN': 'плюс', 'LOSS': 'минус'}.get(t.get('result'), 'ноль'),
            'strategy': strategy, 'pair': t.get('pair'), 'direction': t.get('direction'),
            'trade_id': t.get('trade_id'),
            'placed_at': _iso(placed * 1000) if placed is not None else '',
            'opened_at': t.get('open_time'), 'closed_at': t.get('close_time'),
            'entry_wait_min': t.get('entry_wait_min'), 'duration_min': t.get('duration_min'),
            'hour_utc': t.get('hour_utc'),
            'planned_entry': t.get('planned_entry'), 'entry': t.get('entry_price'),
            'stop': t.get('stop_loss'), 'targets': targets, 'rr': t.get('rr'),
            'cost_share_pct': t.get('cost_share_pct'), 'risk_usd': t.get('risk_usd'),
            'why': t.get('why'), 'confirmed': t.get('confirmed_ru'),
            'missing': t.get('missing_ru'), 'factors': t.get('factors'),
            'confluence': t.get('confluence'), 'zone': t.get('zone'),
            'poi_type': t.get('poi_type'), 'sweep': t.get('sweep'),
            'impulse_pct': t.get('impulse_pct'), 'score': t.get('score'),
            'htf_trend': t.get('htf_trend'), 'htf_strength': t.get('htf_strength'),
            'regime': t.get('regime'), 'regime_er': t.get('regime_er'),
            'atr_pct': t.get('atr_pct'),
            'llm_analysis': t.get('llm_analysis'), 'llm_trigger': t.get('llm_trigger'),
            'llm_stop_why': t.get('llm_stop_why'), 'llm_tp_why': t.get('llm_tp_why'),
            'llm_p': t.get('llm_p'), 'llm_votes': t.get('llm_votes'),
            'llm_critic': t.get('llm_critic'),
            'exit_price': t.get('exit_price'),
            'exit_reason': t.get('exit_reason_ru') or t.get('exit_reason'),
            'tps_hit': tps, 'tp_min': _split(t.get('tp_min')),
            'pnl_r': t.get('pnl_r'), 'pnl_usd': t.get('pnl_usd'),
            'fees_usd': t.get('fees_usd'), 'funding_usd': t.get('funding_usd'),
            'breakeven_set': t.get('breakeven_set'), 'data_gap_min': t.get('data_gap_min'),
            'mfe_r': t.get('mfe_r'), 'mae_r': t.get('mae_r'),
            'mfe_min': t.get('mfe_min'), 'mae_min': t.get('mae_min'),
            'first_1r_min': t.get('first_1r_min'), 'r_if_be_1r': t.get('r_if_be_1r'),
            'after_best_r': f.get('best_after_r'), 'after_worst_r': f.get('worst_after_r'),
            'after_r_1h': f.get('r_1h'), 'after_r_4h': f.get('r_4h'),
            'after_r_12h': f.get('r_12h'),
            'after_hit_tp1': f.get('hit_tp1_after'), 'after_hit_sl': f.get('hit_sl_after'),
            '_t': placed or 0, '_placed': placed, '_entry': t.get('planned_entry'),
        })
    return out


_DROP_OUTCOME = (('цели без нас', 'цель без входа'), ('разрушен', 'сетап разрушен'),
                 ('не заполнен', 'не налилась за срок'), ('оператор', 'снята оператором'))


def _dropped(strategy):
    out = []
    for r in _csv_rows(DROPPED_CSV):
        if r.get('strategy') != strategy:
            continue
        reason = r.get('reason') or ''
        placed = _epoch(r.get('placed_at'))
        out.append({
            'stage': DROPPED,
            'outcome': next((o for key, o in _DROP_OUTCOME if key in reason), 'не налилась'),
            'strategy': strategy, 'pair': r.get('pair'), 'direction': r.get('direction'),
            'placed_at': r.get('placed_at'), 'closed_at': r.get('dropped_at'),
            'planned_entry': r.get('planned_entry'), 'entry': r.get('limit_price'),
            'stop': r.get('stop_loss'), 'targets': _split(r.get('targets')),
            'rr': r.get('rr'), 'cost_share_pct': r.get('cost_share_pct'),
            'risk_usd': r.get('risk_usd'),
            **{k: r.get(k, '') for k in CONTEXT_KEYS},
            'min_gap_pct': r.get('min_gap_pct'), 'best_run_r': r.get('best_run_r'),
            'gate': reason,
            '_t': placed or 0, '_placed': placed, '_entry': r.get('planned_entry'),
        })
    return out


def _shadow_rows(strategy):
    """Сетапы, отклонённые пределом брокера. «Открыт позже» — строкой сделки."""
    import shadow
    rows = _csv_rows(shadow.CSV_PATH)
    try:
        rows += shadow.running_rows()
    except Exception:                                  # noqa: BLE001
        pass
    out = []
    for r in rows:
        if r.get('strategy') != strategy or r.get('outcome') == 'открыт позже':
            continue
        first = _epoch(r.get('first_at'))
        try:
            targets = [float(x) for x in json.loads(r.get('targets') or '[]')]
        except (TypeError, ValueError):
            targets = []
        out.append({
            'stage': SHADOW, 'outcome': r.get('outcome'),
            'strategy': strategy, 'pair': r.get('pair'), 'direction': r.get('direction'),
            'placed_at': r.get('first_at'),
            'entry': r.get('entry'), 'stop': r.get('stop'), 'targets': targets,
            'cost_share_pct': r.get('cost_share_pct'),
            'tps_hit': r.get('targets_hit'), 'pnl_r': r.get('result_r'),
            'mfe_r': r.get('best_r'), 'mae_r': r.get('worst_r'),
            'fill_hours': r.get('fill_hours'), 'closed_hours': r.get('closed_hours'),
            'min_gap_pct': r.get('min_gap_pct'), 'best_run_r': r.get('best_run_r'),
            'gate': r.get('gate'), 'detail': r.get('detail'), 'refusals': r.get('refusals'),
            '_t': first or 0, '_placed': first, '_entry': r.get('entry'),
        })
    return out


def _book(strategy, broker):
    """Живое: заявки, ждущие цену, и открытые позиции."""
    if broker is None:
        return []
    out = []
    for pair, order in list(broker.pending(strategy).items()):
        placed = _epoch(order.get('placed_at'))
        out.append({
            'stage': PENDING, 'outcome': 'ждёт входа',
            'strategy': strategy, 'pair': pair, 'direction': order.get('direction'),
            'placed_at': order.get('placed_at'),
            'planned_entry': order.get('planned_entry'), 'entry': order.get('limit_price'),
            'stop': order.get('stop_loss'), 'targets': list(order.get('targets') or []),
            'rr': round(float(order['rr']), 3) if order.get('rr') else '',
            'cost_share_pct': order.get('cost_share_pct'),
            'risk_usd': round(float(order['risk_amount']), 2) if order.get('risk_amount') else '',
            **_context_columns(order.get('context')),
            'min_gap_pct': order.get('min_gap_pct'), 'best_run_r': order.get('best_run_r'),
            '_t': placed or 0, '_placed': placed, '_entry': order.get('planned_entry'),
        })
    for pair, pos in list(broker.positions(strategy).items()):
        placed = (pos.get('placed_ts') or 0) / 1000 or _epoch(pos.get('opened_at'))
        risk = abs(pos['entry_price'] - pos['initial_stop'])
        sign = 1 if pos['direction'] == 'LONG' else -1
        out.append({
            'stage': OPEN, 'outcome': 'идёт',
            'strategy': strategy, 'pair': pair, 'direction': pos.get('direction'),
            'trade_id': pos.get('trade_id'),
            'placed_at': _iso(placed * 1000) if placed else '',
            'opened_at': pos.get('opened_at'),
            'entry_wait_min': (int((pos['opened_ts'] - pos['placed_ts']) / 60000)
                               if pos.get('placed_ts') else ''),
            'duration_min': int(((pos.get('last_ts') or pos['opened_ts']) - pos['opened_ts']) / 60000),
            'hour_utc': pos.get('hour_utc'),
            'planned_entry': pos.get('planned_entry'), 'entry': pos.get('entry_price'),
            'stop': pos.get('initial_stop'), 'targets': list(pos.get('targets') or []),
            'rr': round(float(pos['rr']), 3) if pos.get('rr') else '',
            'cost_share_pct': pos.get('cost_share_pct'),
            'risk_usd': round(float(pos['risk_amount']), 2) if pos.get('risk_amount') else '',
            **_context_columns(pos.get('context')),
            'atr_pct': pos.get('atr_pct'),
            'tps_hit': pos.get('tp_hit'), 'tp_min': list(pos.get('tp_min') or []),
            'breakeven_set': pos.get('breakeven_set'),
            'mfe_r': round(sign * (pos['mfe_price'] - pos['entry_price']) / risk, 3) if risk else '',
            'mae_r': round(sign * (pos['mae_price'] - pos['entry_price']) / risk, 3) if risk else '',
            'mfe_min': int((pos.get('mfe_ts', pos['opened_ts']) - pos['opened_ts']) / 60000),
            'mae_min': int((pos.get('mae_ts', pos['opened_ts']) - pos['opened_ts']) / 60000),
            'first_1r_min': (int((pos['r1_ts'] - pos['opened_ts']) / 60000)
                             if pos.get('r1_ts') else ''),
            '_t': placed or 0, '_placed': placed, '_entry': pos.get('planned_entry'),
        })
    return out


# ── Планы ИИ ─────────────────────────────────────────────────────────────────

def _plan_outcome(o):
    """
    Что сделала цена с планом, по наблюдению llm_outcomes. Приближение: время
    — первое касание каждого уровня; цель, взятая ДО входа, повторно не
    отслеживается — такой план помечается «цель до входа».
    """
    touched = str(o.get('entry_touched')) == '1'
    tp = str(o.get('hit_tp1')) == '1'
    sl = str(o.get('hit_sl')) == '1'
    if not touched:
        return 'цель без входа' if tp else 'вход не задет'
    at = _float(o.get('entry_hours')) or 0.0
    tp_h, sl_h = _float(o.get('tp_hours')), _float(o.get('sl_hours'))
    if tp and tp_h is not None and tp_h < at:
        return 'цель до входа'
    # Стоп и цель в одной свече — стоп: трактовка в свою пользу завышает итог.
    if sl and (not tp or (sl_h is not None and tp_h is not None and sl_h <= tp_h)):
        return 'стоп'
    if tp:
        return 'цель'
    return 'ни цели, ни стопа'


def _llm_calls():
    """Разборы с текстом (llm_calls.csv): пара -> [(время, строка)]."""
    import llm_journal
    by_pair = {}
    for r in _csv_rows(llm_journal.CSV_PATH):
        at = _epoch(r.get('at'))
        if at is not None:
            by_pair.setdefault(r.get('pair'), []).append([at, r, False])
    return by_pair


def _take_call(calls, pair, gate, at):
    """
    Разбор, которому принадлежит наблюдение: та же пара и тот же отказ,
    записан не позже наблюдения и не раньше чем за полчаса (разбор пишется
    в потоке модели, наблюдение заводит цикл, забирая вердикт).
    """
    best = None
    for item in calls.get(pair) or ():
        call_at, row, used = item
        if used or (row.get('gate') or '') != (gate or ''):
            continue
        if not (at - 1800 <= call_at <= at + 60):
            continue
        if best is None or abs(at - call_at) < abs(at - best[0]):
            best = item
    if best is None:
        return {}
    best[2] = True
    return best[1]


def _llm_plans(orders, text=True):
    """
    Планы модели, НЕ ставшие заявкой: отказ кода или критика, условие не
    наступило, цель без входа, рынок обогнал план, вердикт устарел, — и
    взведённые, ждущие условия. План, ставший заявкой, здесь не повторяется:
    он строкой сделки, снятой заявки, живой заявки или тени.

    orders — уже собранные строки ИИ с '_placed' и '_entry' (сделки, заявки,
    тени): по ним план узнаётся как «ставший заявкой».
    """
    import llm_outcomes
    import refused

    observed = [r for r in _csv_rows(llm_outcomes.CSV_PATH) if r.get('side')]
    try:
        observed += [r for r in llm_outcomes.running_rows() if r.get('side')]
    except Exception:                                  # noqa: BLE001
        pass
    observed.sort(key=lambda r: _epoch(r.get('at')) or 0)

    fates = {}
    broker_gates = {}
    for r in _csv_rows(refused.CSV_PATH):
        if r.get('strategy') != LLM:
            continue
        at = _epoch(r.get('at'))
        gate = r.get('gate') or ''
        if at is None:
            continue
        if gate.startswith('ИИ: ') and gate[4:] in LLM_LIFECYCLE:
            fates.setdefault(r.get('pair'), []).append((at, gate[4:], r.get('detail') or ''))
        elif not gate.startswith('ИИ: '):
            broker_gates.setdefault(r.get('pair'), []).append([at, r])

    armed = {}
    try:
        import strategy_llm
        for a in strategy_llm.current_setups().get('armed') or []:
            armed[a['pair']] = a
    except Exception:                                  # noqa: BLE001
        pass

    # ОДОБРЕННЫЕ ПЛАНЫ ПО ПАРЕ. Модель пересматривает и взведённую пару, и
    # каждый новый одобренный план сменяет прежний (strategy_llm._arm держит
    # один план на пару): 20.09.2026 UNIUSDT взводился четыре раза за 2.5 ч с
    # одним и тем же входом. Поэтому заявка и судьба достаются ПОСЛЕДНЕМУ
    # одобренному плану перед ними, а не первому подходящему.
    SPAN = 13 * 3600                 # срок условия 12 ч и запас на цикл
    accepted = [(i, _epoch(o.get('at'))) for i, o in enumerate(observed)
                if not o.get('gate') and _epoch(o.get('at')) is not None]

    def latest(pair, before, fits=lambda o: True, taken=()):
        """Последний одобренный план пары не позже before (и не раньше SPAN)."""
        best = None
        for i, at in accepted:
            o = observed[i]
            if (i in taken or o.get('pair') != pair or not (before - SPAN <= at <= before)
                    or not fits(o)):
                continue
            if best is None or at > best[1]:
                best = (i, at)
        return best[0] if best else None

    became_order = set()
    for r in sorted((r for r in orders if r.get('_placed') is not None),
                    key=lambda r: r['_placed']):
        i = latest(r.get('pair'), r['_placed'] + 600,
                   lambda o, r=r: (o.get('side') == r.get('direction')
                                   and _same_price(o.get('entry'), r.get('_entry'))),
                   became_order)
        if i is not None:
            became_order.add(i)
    fate_of = {}
    for pair, items in fates.items():
        for at, gate, detail in sorted(items):
            i = latest(pair, at + 60, taken=became_order | set(fate_of))
            if i is not None:
                fate_of[i] = (gate, detail)

    calls = _llm_calls() if text else {}
    out = []
    for i, o in enumerate(observed):
        at = _epoch(o.get('at'))
        if at is None or i in became_order:
            continue
        pair, side, gate = o.get('pair'), o.get('side'), o.get('gate') or ''
        stage, fate_gate, fate_detail = None, gate, ''
        newer = next((t for j, t in accepted if j != i and observed[j].get('pair') == pair
                      and at < t <= at + SPAN), None)
        if gate:
            stage = PLAN_REFUSED
        elif i in fate_of:
            stage, (fate_gate, fate_detail) = PLAN_DIED, fate_of[i]
        elif (pair in armed and newer is None
              and _same_price(armed[pair].get('entry'), o.get('entry'))):
            stage = PLAN_ARMED
        else:
            hit = next((r for t, r in broker_gates.get(pair) or ()
                        if at - 60 <= t <= at + SPAN and r.get('direction') == side
                        and _same_price(r.get('entry'), o.get('entry'))), None)
            if hit:
                stage, fate_gate, fate_detail = PLAN_LOST, hit.get('gate', ''), hit.get('detail', '')
            elif newer is not None and (o.get('trigger_when') or '') != 'now':
                # Взведённый (или неизвестно какой — столбца условия до 22.09
                # не было) план сменила новая версия, пока он ждал.
                stage, fate_gate = PLAN_DIED, 'сменён новым планом'
            else:
                stage, fate_gate = PLAN_LOST, 'судьба не записана'
        call = _take_call(calls, pair, gate, at) if text else {}
        entry, stop, tp1 = _float(o.get('entry')), _float(o.get('stop')), _float(o.get('tp1'))
        out.append({
            'stage': stage, 'outcome': _plan_outcome(o),
            'strategy': LLM, 'pair': pair, 'direction': side,
            'placed_at': o.get('at'),
            'entry': o.get('entry'), 'stop': o.get('stop'),
            'targets': [tp1] if tp1 is not None else [],
            'rr': (round(abs(tp1 - entry) / abs(entry - stop), 2)
                   if None not in (entry, stop, tp1) and entry != stop else ''),
            'why': call.get('why', ''), 'confluence': call.get('confluence', ''),
            'htf_trend': o.get('htf_trend', ''),
            'llm_analysis': call.get('analysis', ''), 'llm_trigger': call.get('trigger', ''),
            'llm_stop_why': call.get('stop_why', ''), 'llm_tp_why': call.get('tp_why', ''),
            'llm_p': call.get('p', ''), 'llm_votes': call.get('votes', ''),
            'llm_critic': call.get('critic', ''),
            'llm_revision': call.get('revision') or ('сделана' if call.get('revised_from') else ''),
            'mfe_r': o.get('best_r'), 'mae_r': o.get('worst_r'),
            'min_gap_pct': o.get('min_dist_entry_pct'), 'best_run_r': o.get('missed_move_r'),
            'entry_touched': o.get('entry_touched'), 'entry_hours': o.get('entry_hours'),
            'hit_tp1': o.get('hit_tp1'), 'tp_hours': o.get('tp_hours'),
            'hit_sl': o.get('hit_sl'), 'sl_hours': o.get('sl_hours'),
            'cond_hours': o.get('cond_hours'), 'observed_hours': o.get('observed_hours'),
            'gate': fate_gate,
            'detail': fate_detail or (call.get('detail', '') if gate else ''),
            '_t': at,
        })
    return out


# ── Сборка целиком ───────────────────────────────────────────────────────────

def _safe(part, *args):
    """Источник, который не читается, не валит журнал: его строк просто нет."""
    try:
        return part(*args)
    except Exception as exc:                           # noqa: BLE001
        log(f'⚠️ журнал сетапов: {part.__name__} не прочитан — {exc}')
        return []


def build(strategy, broker=None, text=True):
    """
    Все сетапы стратегии по времени сетапа. broker — фантомный брокер: с ним
    в журнал попадают и живые заявки с позициями. text=False — без текстов
    разборов ИИ (для сводки: llm_calls.csv — самый тяжёлый файл).
    """
    rows = _safe(_trades, strategy) + _safe(_dropped, strategy)
    rows += _safe(_book, strategy, broker)
    rows += _safe(_shadow_rows, strategy)
    if strategy == LLM:
        rows += _safe(_llm_plans, list(rows), text)
    rows.sort(key=lambda r: r.get('_t') or 0)
    return rows


_summary_cache = {'at': 0.0, 'value': None}
SUMMARY_TTL_S = 120


def summary(strategies, broker=None, now=None):
    """
    Сколько сетапов на каждом этапе — для панели. Пересчёт не чаще раза в
    SUMMARY_TTL_S: панель спрашивает каждые несколько секунд, а журнал
    собирается из шести файлов.
    """
    now = now if now is not None else time.monotonic()
    cached = _summary_cache['value']
    if cached is not None and now - _summary_cache['at'] < SUMMARY_TTL_S:
        return cached
    out = {}
    for strategy in strategies:
        stages = {}
        total_r = 0.0
        for r in build(strategy, broker, text=False):
            stages[r['stage']] = stages.get(r['stage'], 0) + 1
            if r['stage'] == TRADE:
                total_r += _float(r.get('pnl_r')) or 0.0
        out[strategy] = {'stages': stages, 'total': sum(stages.values()),
                         'trades_r': round(total_r, 2)}
    _summary_cache.update(at=now, value=out)
    return out


# ── Выгрузка ─────────────────────────────────────────────────────────────────

def _cell(key, value):
    """Значение для Excel с русской разметкой: запятая в дробях, время без «T»."""
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        return ' / '.join(_cell('', v) for v in value)
    if isinstance(value, bool) or value in ('True', 'False'):
        return 'да' if value in (True, 'True') else 'нет'
    if key in TIME_KEYS:
        at = _epoch(value)
        return (datetime.fromtimestamp(at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
                if at is not None else str(value))
    if isinstance(value, float):
        return f'{value:.10g}'.replace('.', ',') if math.isfinite(value) else ''
    text = str(value)
    return text.replace('.', ',') if _NUMERIC.fullmatch(text) else text


def to_csv(rows):
    """Байты CSV: UTF-8 с BOM, «;», русские заголовки, десятичная запятая."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=';', lineterminator='\r\n')
    writer.writerow([label for _key, label in COLUMNS])
    for r in rows:
        writer.writerow([_cell(key, r.get(key)) for key in KEYS])
    return ('﻿' + buf.getvalue()).encode('utf-8')


def filename(strategy, now=None):
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y%m%d-%H%M')
    return f'journal-{strategy}-{stamp}.csv'
