"""
Detailed trade journal — saves every trade to trades_journal.csv.
One row per trade, written at close. Tracks full lifecycle in-memory.
Open in Excel / Google Sheets for analysis.

Расширение 2026-07-05: ПОЛНЫЙ дамп каждой сделки дополнительно пишется в
trades_detail.jsonl (одна JSON-строка на сделку; мульти-тенант — в
state/<telegram_id>/trades_detail.jsonl). Содержит всё для разбора: контекст
скана (score/RR/близость), якоря импульса A/B со временами, границы зоны A,
уровень инвалидации, жизненный цикл входа (лимит/маркет, ожидание, слиппедж),
MFE/MAE с R-мультипликаторами, время безубытка. CSV-колонки легаси не меняются.
Анализ: pd.read_json('trades_detail.jsonl', lines=True).
"""

import csv
import json
import os
from datetime import datetime, timezone

JOURNAL_FILE  = os.path.join(os.path.dirname(__file__), 'trades_journal.csv')
DETAIL_JSONL  = os.path.join(os.path.dirname(__file__), 'trades_detail.jsonl')

COLUMNS = [
    # Идентификация
    'trade_id', 'mode',
    # Открытие
    'open_time', 'pair', 'direction', 'zone', 'htf_trend',
    'impulse_pct', 'setup_high', 'setup_low',
    # Уровни
    'entry_price', 'stop_loss', 'tp1', 'tp2', 'tp3', 'tp4',
    'rr', 'leverage', 'risk_usd', 'position_size', 'balance_before',
    # Тейк-профиты (заполняются по мере отработки)
    'tp1_time', 'tp1_price',
    'tp2_time', 'tp2_price',
    'tp3_time', 'tp3_price',
    # Закрытие
    'close_time', 'exit_price', 'exit_reason',
    'tps_hit', 'pnl_usd', 'pnl_pct', 'duration_min',
    'balance_after',
    # Итог
    'result',       # WIN / LOSS / BREAKEVEN
    'setup_notes',  # почему открылась (краткое описание)
]


def _next_trade_id() -> int:
    """Returns next sequential trade ID based on existing rows."""
    if not os.path.exists(JOURNAL_FILE):
        return 1
    with open(JOURNAL_FILE, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return 1
    try:
        return max(int(r.get('trade_id', 0)) for r in rows) + 1
    except Exception:
        return len(rows) + 1


def open_trade(position: dict, signal: dict, balance_before: float) -> int:
    """
    Called when trade opens. Populates position dict with journal fields.
    Returns the assigned trade_id.
    """
    setup   = signal['setup']
    trigger = signal['trigger']
    params  = signal['params']

    trade_id = _next_trade_id()

    # Store journal metadata inside position for later use
    position['_journal'] = {
        'trade_id':     trade_id,
        'open_time':    datetime.now().isoformat(timespec='seconds'),
        'pair':         signal['trading_pair'],
        'direction':    setup['type'],
        'zone':         trigger['zone'],
        'htf_trend':    signal.get('htf_trend', 'N/A'),
        'impulse_pct':  round(setup['size'] / setup['end_price'] * 100, 2),
        'setup_high':   round(setup['end_price'] if setup['type'] == 'LONG' else setup['start_price'], 6),
        'setup_low':    round(setup['start_price'] if setup['type'] == 'LONG' else setup['end_price'], 6),
        'entry_price':  params['entry'],
        'stop_loss':    params['stop_loss'],
        'tp1':          params['take_profit_1'],
        'tp2':          params['take_profit_2'],
        'tp3':          params.get('take_profit_3', ''),
        'tp4':          params.get('take_profit_4', ''),
        'rr':           params['rr'],
        'leverage':     params.get('leverage', 'N/A'),
        'risk_usd':     params['risk_amount'],
        'position_size': params['position_size'],
        'balance_before': balance_before,
        # TP hit tracking
        'tp1_time': '', 'tp1_price': '',
        'tp2_time': '', 'tp2_price': '',
        'tp3_time': '', 'tp3_price': '',
        # ── Расширенный контекст (2026-07-05): «почему открылась» ────────────
        'setup_start_time': str(setup.get('start_time', '')),   # время якоря A
        'setup_end_time':   str(setup.get('end_time', '')),     # время якоря B
        'zone_a_top':       (signal.get('zone_a') or {}).get('top', ''),
        'zone_a_bottom':    (signal.get('zone_a') or {}).get('bottom', ''),
        # инвалидация сетапа = дальняя граница зоны B (88.6%-уровень)
        'invalidation':     ((signal.get('zone_b') or {}).get('bottom', '')
                             if setup['type'] == 'LONG'
                             else (signal.get('zone_b') or {}).get('top', '')),
        'be_level':         params.get('be_level', ''),
        'session_hour_utc': datetime.now(timezone.utc).hour,
        # контекст скана: score и компоненты (bot.py/platform_manager кладут в signal)
        **{f'scan_{k}': v for k, v in (signal.get('scan') or {}).items()},
    }
    return trade_id


def record_tp_hit(position: dict, tp_num: int, price: float):
    """Called when each TP level is hit."""
    j = position.get('_journal')
    if not j or tp_num > 3:
        return
    ts = datetime.now().isoformat(timespec='seconds')
    j[f'tp{tp_num}_time']  = ts
    j[f'tp{tp_num}_price'] = round(price, 6)


def close_trade(position: dict, exit_price: float, exit_reason: str,
                pnl_usd: float, balance_after: float, import_config, telegram_id=None):
    """
    Вызывается при закрытии сделки. Пишет полную строку:
    - telegram_id задан (мульти-тенант) -> в БД (db.record_trade)
    - иначе -> в общий CSV (legacy одно-юзер)
    Возвращает построенную строку (dict) либо None.
    """
    j = position.get('_journal')
    if not j:
        return None

    open_dt  = datetime.fromisoformat(j['open_time'])
    close_dt = datetime.now()
    duration = int((close_dt - open_dt).total_seconds() / 60)

    balance_before = j['balance_before']
    pnl_pct = round(pnl_usd / balance_before * 100, 2) if balance_before else 0

    if pnl_usd > 0:
        result = 'WIN'
    elif pnl_usd < 0:
        result = 'LOSS'
    else:
        result = 'BREAKEVEN'

    tps_hit = position.get('tp_hit', 0)

    # Build human-readable setup note
    direction = j['direction']
    zone      = j['zone']
    htf       = j['htf_trend']
    imp       = j['impulse_pct']
    rr        = j['rr']
    notes = (f"{direction} в {zone}, HTF={htf}, "
             f"импульс={imp}%, RR=1:{rr}, "
             f"выход={exit_reason}")

    row = {
        'trade_id':     j['trade_id'],
        'mode':         import_config.TRADING_MODE,
        'open_time':    j['open_time'],
        'pair':         j['pair'],
        'direction':    direction,
        'zone':         zone,
        'htf_trend':    htf,
        'impulse_pct':  imp,
        'setup_high':   j['setup_high'],
        'setup_low':    j['setup_low'],
        'entry_price':  j['entry_price'],
        'stop_loss':    j['stop_loss'],
        'tp1':          j['tp1'],
        'tp2':          j['tp2'],
        'tp3':          j['tp3'],
        'tp4':          j['tp4'],
        'rr':           rr,
        'leverage':     j['leverage'],
        'risk_usd':     j['risk_usd'],
        'position_size': j['position_size'],
        'balance_before': balance_before,
        'tp1_time':     j['tp1_time'],
        'tp1_price':    j['tp1_price'],
        'tp2_time':     j['tp2_time'],
        'tp2_price':    j['tp2_price'],
        'tp3_time':     j['tp3_time'],
        'tp3_price':    j['tp3_price'],
        'close_time':   close_dt.isoformat(timespec='seconds'),
        'exit_price':   round(exit_price, 6),
        'exit_reason':  exit_reason,
        'tps_hit':      tps_hit,
        'pnl_usd':      round(pnl_usd, 2),
        'pnl_pct':      pnl_pct,
        'duration_min': duration,
        'balance_after': round(balance_after, 2),
        'result':       result,
        'setup_notes':  notes,
    }

    # ── Расширенный дамп (2026-07-05): «как отработала» ──────────────────────
    # Все дополнительные j-поля (setup_*, zone_a_*, invalidation, scan_*, ...)
    for k, v in j.items():
        if k not in row:
            row[k] = v

    entry = float(j['entry_price'] or 0)
    sl    = float(j['stop_loss'] or 0)
    sl_dist = abs(entry - sl)
    mfe = position.get('mfe_price')
    mae = position.get('mae_price')
    if mfe is not None and mae is not None and sl_dist > 0:
        sign = 1 if direction == 'LONG' else -1
        row['mfe_price'] = round(mfe, 6)
        row['mae_price'] = round(mae, 6)
        row['mfe_r'] = round(sign * (mfe - entry) / sl_dist, 3)   # макс. ход ЗА нас, в R
        row['mae_r'] = round(sign * (mae - entry) / sl_dist, 3)   # макс. ход ПРОТИВ нас, в R
    row['be_time'] = position.get('be_time', '')
    row['breakeven_set'] = bool(position.get('breakeven_set'))
    for k, v in (position.get('_lifecycle') or {}).items():
        row[f'entry_{k}'] = v
    if row.get('entry_placed_at') and row.get('entry_filled_at'):
        try:
            row['entry_wait_min'] = int((datetime.fromisoformat(row['entry_filled_at'])
                                         - datetime.fromisoformat(row['entry_placed_at']))
                                        .total_seconds() / 60)
        except Exception:
            pass

    if telegram_id is not None:
        import db
        db.record_trade(telegram_id, row)   # полный row уходит в data-JSON
        detail_path = os.path.join(os.path.dirname(__file__), 'state',
                                   str(telegram_id), 'trades_detail.jsonl')
    else:
        write_header = not os.path.exists(JOURNAL_FILE) or os.path.getsize(JOURNAL_FILE) == 0
        with open(JOURNAL_FILE, 'a', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction='ignore')
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        detail_path = DETAIL_JSONL

    # JSONL с ПОЛНЫМ дампом — в обоих режимах (сбой записи не роняет закрытие сделки)
    try:
        os.makedirs(os.path.dirname(detail_path), exist_ok=True)
        with open(detail_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass
    return row
