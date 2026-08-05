"""
Веб-дашборд: результаты торговли в разрезе стратегий.

Поднимается прямо в процессе бота отдельным потоком-демоном, поэтому не
требует ни внешнего веб-сервера, ни дополнительных зависимостей — только
стандартная библиотека. Если порт занят или сервер падает, торговля
продолжается: дашборд не должен ронять бота.

Два источника данных, в зависимости от режима:

    PAPER  — фантомный счёт: paper_trades.csv (закрытые сделки) и живое
             состояние брокера (открытые позиции, депозиты). У каждой
             стратегии свой депозит, поэтому доходность считается в
             процентах от своей базы, а не в долларах.

    DEMO/LIVE — боевой счёт: trades_journal.csv, positions_state.json и
             pair_strategy.json (кто из стратегий владеет парой).

Запуск отдельно от бота (для просмотра истории):
    python Live_Bot/dashboard.py
"""

import csv
import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import config
import scan_report
import settings_store
from logger import log


def _controls_allowed():
    """
    Можно ли менять настройки через дашборд.

    Только когда сервер слушает петлевой адрес: страница не имеет ни пароля,
    ни HTTPS, и открытая в сеть она дала бы любому желающему поднять риск на
    сделку или выключить стратегию. Осознанно разрешить: DASHBOARD_ALLOW_CONTROL=true.
    """
    if os.getenv('DASHBOARD_ALLOW_CONTROL', '').lower() == 'true':
        return True
    return config.DASHBOARD_HOST in ('127.0.0.1', 'localhost', '::1')


# Страница — часть КОДА, а не данных: в собранном .exe она лежит во временной
# папке распаковки (sys._MEIPASS), рядом с остальными ресурсами сборки.
_CODE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
HTML_FILE = os.path.join(_CODE_DIR, 'dashboard.html')
JOURNAL_FILE = os.path.join(config.DATA_DIR, 'trades_journal.csv')
POSITIONS_FILE = os.path.join(config.DATA_DIR, 'positions_state.json')
PENDING_FILE = os.path.join(config.DATA_DIR, 'pending_orders.json')
STRATEGY_FILE = os.path.join(config.DATA_DIR, 'pair_strategy.json')
PAPER_JOURNAL = os.path.join(config.DATA_DIR, 'paper_trades.csv')
PAPER_JSONL = os.path.join(config.DATA_DIR, 'paper_trades.jsonl')

LOG_FILE = os.path.join(config.DATA_DIR, 'bot_log.txt')

_trade_manager = None   # боевой режим: ставится ботом, чтобы показать баланс
_broker = None          # фантомный режим: живое состояние счетов
_status = {'state': 'starting', 'detail': '', 'since': None}


def set_status(state, detail=''):
    """
    Состояние бота для окна приложения.

    В настольном режиме консоли нет, и упавший поток бота остался бы незаметен:
    дашборд продолжал бы показывать последние данные как ни в чём не бывало.
    """
    _status['state'] = state
    _status['detail'] = detail
    _status['since'] = datetime.now().isoformat(timespec='seconds')


def read_log(limit=200):
    """Последние строки лога бота — заменяют консоль в оконном режиме."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as fh:
            # Читаем хвост, а не весь файл: за месяц работы он вырастает
            # до десятков мегабайт, и дашборд опрашивает его каждые 15 секунд.
            size = os.path.getsize(LOG_FILE)
            window = min(size, 256 * 1024)
            fh.seek(size - window)
            lines = fh.read().splitlines()
    except Exception as exc:
        return [f'лог недоступен: {exc}']
    if window < size and lines:
        lines = lines[1:]        # первая строка обрезана посередине
    return lines[-limit:]


def _read_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as fh:
                return json.load(fh)
    except Exception as exc:
        log(f"⚠️ Дашборд: не читается {os.path.basename(path)}: {exc}")
    return default


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _geometry(raw):
    """
    Разметка сетапа из журнала. В CSV она лежит строкой JSON.

    У сделок, закрытых до появления колонки, её нет — это нормально, график
    просто нарисует один план входа. Пустой словарь честнее выдумывания
    зон задним числом.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_rows(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except Exception as exc:
        log(f"⚠️ Дашборд: не читается {os.path.basename(path)}: {exc}")
        return []


# ── Закрытые сделки ──────────────────────────────────────────────────────────

def _read_closed_trades():
    """Закрытые сделки боевого журнала, по времени закрытия."""
    trades = []
    for row in _read_rows(JOURNAL_FILE):
        if not row.get('close_time'):
            continue   # сделка ещё открыта
        trades.append({
            'id': row.get('trade_id', ''),
            # Сделки, записанные до появления A/B-режима, помечаем FIBO:
            # тогда работала только она.
            'strategy': row.get('strategy') or 'FIBO',
            'pair': row.get('pair', ''),
            'direction': row.get('direction', ''),
            'zone': row.get('zone', ''),
            'entry': _to_float(row.get('entry_price')),
            'stop': _to_float(row.get('stop_loss')),
            'tp1': _to_float(row.get('tp1')),
            'exit': _to_float(row.get('exit_price')),
            'pnl': _to_float(row.get('pnl_usd')),
            'pnl_pct': _to_float(row.get('pnl_pct')),
            'rr': _to_float(row.get('rr')),
            'risk': _to_float(row.get('risk_usd')),
            'reason': row.get('exit_reason', ''),
            'result': row.get('result', ''),
            'opened': row.get('open_time', ''),
            'closed': row.get('close_time', ''),
            'duration_min': _to_float(row.get('duration_min')),
            'why': row.get('setup_notes', ''),
            'fees': 0.0,
            'funding': 0.0,
        })
    trades.sort(key=lambda t: t['closed'])
    return trades


def _read_paper_trades():
    """Закрытые фантомные сделки — с издержками и обоснованием входа."""
    trades = []
    for row in _read_rows(PAPER_JOURNAL):
        risk = _to_float(row.get('risk_usd'))
        pnl = _to_float(row.get('pnl_usd'))
        trades.append({
            'id': row.get('trade_id', ''),
            'strategy': row.get('strategy') or 'FIBO',
            'pair': row.get('pair', ''),
            'direction': row.get('direction', ''),
            'zone': row.get('zone', ''),
            'entry': _to_float(row.get('entry_price')),
            'stop': _to_float(row.get('stop_loss')),
            'tp1': _to_float(row.get('tp1')),
            'exit': _to_float(row.get('exit_price')),
            'pnl': pnl,
            'pnl_pct': _to_float(row.get('pnl_pct')),
            'pnl_r': _to_float(row.get('pnl_r')) or (pnl / risk if risk else 0.0),
            'rr': _to_float(row.get('rr')),
            'risk': risk,
            'reason': row.get('exit_reason', ''),
            'result': row.get('result', ''),
            'opened': row.get('open_time', ''),
            'closed': row.get('close_time', ''),
            'duration_min': _to_float(row.get('duration_min')),
            'fees': _to_float(row.get('fees_usd')),
            'funding': _to_float(row.get('funding_usd')),
            'mfe_r': _to_float(row.get('mfe_r')),
            'mae_r': _to_float(row.get('mae_r')),
            'why': row.get('why', ''),
            'reason_ru': row.get('exit_reason_ru') or row.get('exit_reason', ''),
            'confirmed': [x for x in (row.get('confirmed_ru') or '').split('; ') if x],
            'missing': [x for x in (row.get('missing_ru') or '').split('; ') if x],
            'geometry': _geometry(row.get('geometry')),
            'balance_after': _to_float(row.get('balance_after')),
        })
    trades.sort(key=lambda t: t['closed'])
    return trades


# ── Метрики ──────────────────────────────────────────────────────────────────

def _summarise(trades):
    """Сводка по списку сделок одной стратегии."""
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    gross_win = sum(t['pnl'] for t in wins)
    gross_loss = abs(sum(t['pnl'] for t in losses))
    # Сумма R — метрика, не зависящая от размера депозита: позволяет
    # сравнивать стратегии, даже если риск на сделку у них разный.
    total_r = sum(t.get('pnl_r') if t.get('pnl_r') is not None
                  else (t['pnl'] / t['risk'] if t['risk'] else 0.0)
                  for t in trades)

    return {
        'trades': len(trades),
        'wins': len(wins),
        'winrate': round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        'pnl': round(sum(t['pnl'] for t in trades), 2),
        'profit_factor': round(gross_win / gross_loss, 3) if gross_loss else None,
        'avg_win': round(gross_win / len(wins), 2) if wins else 0.0,
        'avg_loss': round(-gross_loss / len(losses), 2) if losses else 0.0,
        'sum_r': round(total_r, 2),
        'expectancy_r': round(total_r / len(trades), 3) if trades else 0.0,
        'fees': round(sum(t.get('fees', 0.0) for t in trades), 2),
        'funding': round(sum(t.get('funding', 0.0) for t in trades), 2),
    }


def _equity_series(trades, start_balance=0.0):
    """
    Кривая депозита по времени закрытия.

    Каждая точка — [время, депозит, доходность в % от старта]. Проценты нужны,
    чтобы две стратегии с разными депозитами ложились на один график.
    """
    balance, series = float(start_balance), []
    for trade in trades:
        balance += trade['pnl']
        growth = (balance / start_balance - 1) * 100 if start_balance else 0.0
        series.append([trade['closed'], round(balance, 2), round(growth, 2)])
    return series


def _max_drawdown_pct(series, start_balance):
    """Максимальная просадка кривой депозита, % от пика."""
    if not series or not start_balance:
        return 0.0
    peak, worst = float(start_balance), 0.0
    for _time, balance, _growth in series:
        peak = max(peak, balance)
        if peak > 0:
            worst = max(worst, (peak - balance) / peak * 100)
    return round(worst, 2)


# ── Открытые позиции (боевой режим) ──────────────────────────────────────────

def _exchange_prices(pairs):
    """Текущие цены с биржи — иначе плавающий результат посчитать не из чего."""
    if not pairs or _trade_manager is None:
        return {}
    try:
        tickers = _trade_manager.exchange.fetch_tickers(list(pairs))
    except Exception:
        return {}
    out = {}
    for key, value in tickers.items():
        symbol = (key or '').replace('/', '').replace(':USDT', '').upper()
        price = value.get('last') or value.get('close')
        if price:
            out[symbol] = float(price)
    return out


def _open_positions():
    """
    Открытые позиции боевого счёта.

    Источник истины — БИРЖА, а не файл состояния: позиция могла открыться
    заполнением лимита в цикл, когда биржа не отдала статус ордера, и в файле
    её нет. Файл добавляет то, чего биржа не хранит: уровни, обоснование
    входа и стратегию-владельца.
    """
    state = _read_json(POSITIONS_FILE, {})
    owners = _read_json(STRATEGY_FILE, {})

    live = {}
    if _trade_manager is not None:
        try:
            snapshot = _trade_manager.sync_exchange_state()
            if snapshot['pos_ok']:
                live = snapshot['positions']
        except Exception as exc:
            log(f"⚠️ Дашборд: позиции с биржи недоступны: {exc}")

    pairs = set(state) | set(live)
    prices = _exchange_prices(pairs)

    out = []
    for pair in pairs:
        item = state.get(pair, {})
        exch = live.get(pair, {})
        direction = (item.get('direction')
                     or ('LONG' if str(exch.get('side', '')).lower() == 'long' else 'SHORT'))
        entry = _to_float(item.get('entry_price')) or _to_float(exch.get('entryPrice'))
        size = _to_float(exch.get('contracts')) or _to_float(item.get('position_size'))
        price = prices.get(pair, 0.0)
        sign = 1 if direction == 'LONG' else -1
        floating = sign * (price - entry) * size if price and entry and size else 0.0
        risk = _to_float(item.get('risk_amount'))

        # Уровни берём из состояния бота, а при его отсутствии — С БИРЖИ:
        # стоп и тейк прикреплены к самой позиции, и это единственный источник
        # для позиций, о которых бот не знает.
        info = exch.get('info') or {}
        stop = _to_float(item.get('stop_loss')) or _to_float(info.get('stopLoss'))
        targets = [t for t in (_to_float(item.get('take_profit_1')),
                               _to_float(item.get('take_profit_2'))) if t]
        if not targets:
            exch_tp = _to_float(info.get('takeProfit'))
            if exch_tp:
                targets = [exch_tp]
        if not risk and stop and entry and size:
            risk = abs(entry - stop) * size

        out.append({
            'pair': pair,
            'strategy': owners.get(pair, 'FIBO'),
            'direction': direction,
            'zone': item.get('zone', '—'),
            'entry': entry,
            'price': price or entry,
            'stop': stop,
            'targets': targets or [_to_float(item.get('take_profit_1'))],
            'tp1': _to_float(item.get('take_profit_1')),
            'tp2': _to_float(item.get('take_profit_2')),
            'rr': _to_float(item.get('rr')),
            'risk': risk,
            'size': size,
            'opened': item.get('entry_time', ''),
            'realized': 0.0,
            'floating': round(floating, 2),
            'costs': 0.0,
            'unrealised': round(floating, 2),
            'unrealised_r': round(floating / risk, 2) if risk else 0.0,
            'progress': (round(min(max(abs(price - stop) / abs(targets[-1] - stop), 0), 1), 4)
                         if price and stop and targets and targets[-1] != stop else 0.0),
            'why': item.get('htf_trend') and f"{direction}, HTF {item.get('htf_trend')}" or '',
            # Позиция есть на бирже, но бот её не отслеживает: чаще всего
            # лимит заполнился в цикл, когда биржа не отдала статус ордера.
            'untracked': pair not in state,
        })
    out.sort(key=lambda p: p['opened'])
    return out


def _live_pending():
    """
    Ордера боевого счёта, ожидающие цену.

    Раньше дашборд их не показывал вовсе, и бот выглядел бездействующим, хотя
    держал пять выставленных лимитов.
    """
    pending = _read_json(PENDING_FILE, {})
    owners = _read_json(STRATEGY_FILE, {})
    prices = _exchange_prices(set(pending))

    out = []
    now = datetime.now()
    for pair, item in pending.items():
        params = item.get('params') or {}
        limit = _to_float(item.get('limit_price'))
        price = prices.get(pair, 0.0)
        try:
            placed = datetime.fromisoformat(item.get('placed_at'))
            waiting = int((now - placed).total_seconds() / 60)
        except Exception:
            waiting = 0
        try:
            expires = datetime.fromisoformat(item.get('max_valid_until'))
            left = max(0, int((expires - now).total_seconds() / 60))
        except Exception:
            left = 0

        out.append({
            'pair': pair,
            'strategy': owners.get(pair) or (item.get('signal') or {}).get('strategy', 'FIBO'),
            'direction': 'LONG' if item.get('side') == 'buy' else 'SHORT',
            'zone': ((item.get('signal') or {}).get('trigger') or {}).get('zone', '—'),
            'entry': limit,
            'price': price or None,
            'distance_pct': round(abs(limit - price) / price * 100, 2) if price else None,
            'stop': _to_float(params.get('stop_loss')),
            'targets': [t for t in (_to_float(params.get('take_profit_1')),
                                    _to_float(params.get('take_profit_2'))) if t],
            'tp1': _to_float(params.get('take_profit_1')),
            'rr': _to_float(params.get('rr')),
            'risk': _to_float(params.get('risk_amount')),
            'invalidation': _to_float(item.get('invalidation_price')),
            'opened': item.get('placed_at', ''),
            'waiting_min': waiting,
            'expires_in_min': left,
            'pending': True,
            'why': ((item.get('signal') or {}).get('htf_trend')
                    and f"HTF {(item.get('signal') or {}).get('htf_trend')}" or ''),
        })
    out.sort(key=lambda p: p['opened'])
    return out


# ── Сборка данных ────────────────────────────────────────────────────────────

def _paper_payload():
    """Данные фантомного эксперимента."""
    closed = _read_paper_trades()
    snapshot = (_broker.snapshot() if _broker is not None
                else {'strategies': {}, 'open': [], 'pending': []})
    live = snapshot.get('strategies', {})
    open_positions = snapshot.get('open', [])
    pending_orders = snapshot.get('pending', [])

    # Стратегии берём из брокера, но если он не поднят (дашборд запущен
    # отдельно от бота) — восстанавливаем список по журналу.
    names = list(live.keys()) or sorted({t['strategy'] for t in closed}) or ['FIBO', 'SMC']

    strategies, equity = {}, {}
    for name in names:
        info = live.get(name, {})
        subset = [t for t in closed if t['strategy'] == name]
        # Сделки до перезапуска стратегии остаются в журнале и в выгрузке, но
        # в её текущую статистику не входят: депозит с тех пор другой, и
        # смешивать доходность от разных баз нельзя.
        started = info.get('reset_at')
        dropped = 0
        if started:
            before = len(subset)
            subset = [t for t in subset if t['closed'] >= started]
            dropped = before - len(subset)
        start = info.get('start_balance')
        if start is None:
            # Без живого брокера стартовый депозит берём из первой сделки.
            start = (subset[0]['balance_after'] - subset[0]['pnl']) if subset else 0.0
        series = _equity_series(subset, start)

        summary = _summarise(subset)
        summary['open'] = sum(1 for p in open_positions if p['strategy'] == name)
        summary['pending'] = sum(1 for p in pending_orders if p['strategy'] == name)
        # Плавающий результат открытых позиций: без него депозит на карточке
        # расходится с суммой закрытых сделок, и это выглядит как ошибка.
        summary['floating'] = round(
            sum(p['unrealised'] for p in open_positions if p['strategy'] == name), 2)
        summary['start_balance'] = round(start, 2)
        summary['balance'] = info.get('balance', round(start + summary['pnl'], 2))
        summary['equity'] = info.get('equity', summary['balance'])
        summary['return_pct'] = (round((summary['equity'] / start - 1) * 100, 2)
                                 if start else 0.0)
        summary['max_dd_pct'] = _max_drawdown_pct(series, start)
        summary['reset_at'] = started
        summary['dropped_before_reset'] = dropped
        strategies[name] = summary
        equity[name] = series

    return {
        'generated': datetime.now().isoformat(timespec='seconds'),
        'mode': config.TRADING_MODE,
        'paper': True,
        'started_at': snapshot.get('started_at'),
        'strategy_mode': config.STRATEGY,
        'balance': None,
        'costs': {
            'maker': config.PAPER_FEE_MAKER,
            'taker': config.PAPER_FEE_TAKER,
            'slippage': config.PAPER_SLIPPAGE_PCT,
            'funding': config.PAPER_FUNDING,
        },
        'strategies': strategies,
        'equity': equity,
        'open_positions': open_positions,
        'pending_orders': pending_orders,
        'closed': list(reversed(closed))[:500],
        'closed_total': len(closed),
    }


def _live_payload():
    """Данные боевого счёта (DEMO/LIVE)."""
    closed = _read_closed_trades()
    open_positions = _open_positions()
    pending_orders = _live_pending()

    strategies, equity = {}, {}
    for name in ('FIBO', 'SMC'):
        subset = [t for t in closed if t['strategy'] == name]
        summary = _summarise(subset)
        summary['open'] = sum(1 for p in open_positions if p['strategy'] == name)
        summary['pending'] = sum(1 for p in pending_orders if p['strategy'] == name)
        summary['floating'] = round(
            sum(p['unrealised'] for p in open_positions if p['strategy'] == name), 2)
        strategies[name] = summary
        equity[name] = _equity_series(subset, 0.0)

    balance = None
    if _trade_manager is not None:
        try:
            balance = round(_trade_manager.get_real_balance(), 2)
        except Exception:
            balance = None

    return {
        'generated': datetime.now().isoformat(timespec='seconds'),
        'mode': config.TRADING_MODE,
        'paper': False,
        'strategy_mode': config.STRATEGY,
        'balance': balance,
        'strategies': strategies,
        'equity': equity,
        'open_positions': open_positions,
        'pending_orders': pending_orders,
        'closed': list(reversed(closed))[:300],   # последние 300, новые сверху
        'closed_total': len(closed),
    }


def build_payload():
    """Полный набор данных для дашборда."""
    payload = _paper_payload() if (config.PAPER_MODE or _broker is not None) else _live_payload()
    payload['status'] = dict(_status)
    # Воронка отсева: бот считает причины отказа на каждом цикле, но раньше
    # они уходили только в лог. Это ответ на самый частый вопрос при
    # наблюдении — «почему он ничего не делает».
    payload['funnel'] = scan_report.snapshot()
    payload['regime'] = _regime()
    payload['portfolio'] = _portfolio()
    payload['errors'] = _errors_summary()
    payload['attention'] = _attention(payload)
    return payload


# Свечи для графика сделки. Кэш живёт в памяти процесса: одну и ту же
# сделку открывают по нескольку раз, а каждый показ — это запрос к бирже.
_candle_cache = {}
_CANDLE_CACHE_MAX = 60


# Биржа отдаёт максимум 1000 свечей от текущего момента назад. Это задаёт
# предел досягаемости каждого таймфрейма.
_TF_MINUTES = (('5m', 5), ('15m', 15), ('1h', 60), ('4h', 240), ('1d', 1440))
_MAX_CANDLES = 1000


def _pick_timeframe(span_minutes, age_minutes):
    """
    Таймфрейм под длительность сделки И её возраст.

    Длительность задаёт нижнюю границу: сделку длиной в час пятиминутками
    видно (12 свечей), а двухнедельную — уже каша из трёх тысяч.

    Возраст задаёт верхнюю, и про неё легко забыть. Свечи запрашиваются
    БЕЗ отметки начала: биржа отдаёт последние 1000 от текущего момента.
    Пятиминутки добивают на 3.5 дня назад, часовые на 41 день. Короткая
    сделка месячной давности при выборе только по длительности получила бы
    пятиминутки — и пустой график вместо ошибки, которую видно.
    """
    need_back = age_minutes + span_minutes * 1.4
    for name, minutes in _TF_MINUTES:
        fits_detail = span_minutes / minutes <= 220
        reaches = need_back / minutes <= _MAX_CANDLES - 20
        if fits_detail and reaches:
            return name, minutes
    return _TF_MINUTES[-1]


def _trade_candles(pair, opened, closed):
    """
    Свечи вокруг сделки: окно расширено на 30% с каждой стороны.

    Без запаса вход и выход упираются в края графика, и не видно, откуда
    цена пришла и куда ушла — а это половина смысла разбора.
    """
    from datetime import timedelta

    import pandas as pd

    # Время приводим к наивному UTC. В журнале метки бывают и с зоной, и без:
    # сравнение таких напрямую падает, а тихое приведение одной из них дало бы
    # сдвиг на часы и график не от той сделки.
    def _naive(value):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert('UTC').tz_localize(None)
        return stamp

    start = _naive(opened)
    end = _naive(closed) if closed else pd.Timestamp.utcnow().tz_localize(None)
    span = max((end - start).total_seconds() / 60, 30)
    age = max((pd.Timestamp.utcnow().tz_localize(None) - start).total_seconds() / 60, 0)
    timeframe, tf_min = _pick_timeframe(span, age)
    pad = timedelta(minutes=span * 0.35)
    since = start - pad
    until = end + pad
    need = int((until - since).total_seconds() / 60 / tf_min) + 5

    key = (pair, timeframe, since.strftime('%Y%m%d%H%M'), need)
    if key in _candle_cache:
        return _candle_cache[key]

    from exchange import fetch_ohlcv
    limit = min(max(need, 60), 1000)
    df = fetch_ohlcv(timeframe, limit=limit, symbol=pair)
    if df is None or not len(df):
        return None

    stamps = pd.to_datetime(df['timestamp'])
    if getattr(stamps.dt, 'tz', None) is not None:
        stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
    mask = (stamps >= since) & (stamps <= until)
    window = df[mask.to_numpy()]
    if not len(window):
        # Сделка старше того, что отдаёт биржа при этом лимите: показать
        # нечего, и честнее сказать об этом, чем нарисовать чужие свечи.
        return None

    out = {
        'timeframe': timeframe,
        'candles': [
            # Время — строго ISO-8601 с зоной. str() у pandas даёт
            # «2026-08-05 13:15:00»: без «T» и без зоны браузер читает такую
            # строку как МЕСТНОЕ время, и отметки входа и выхода уезжали на
            # столько часов, на сколько часовой пояс отличается от UTC.
            [t.strftime('%Y-%m-%dT%H:%M:%SZ'), float(o), float(h), float(l), float(c)]
            for t, o, h, l, c in zip(
                stamps[mask.to_numpy()], window['open'], window['high'],
                window['low'], window['close'])
        ],
    }
    if len(_candle_cache) >= _CANDLE_CACHE_MAX:
        _candle_cache.clear()
    _candle_cache[key] = out
    return out


def _strategy_settings(stored=None):
    """
    Только настройки стратегий, без раздела портфеля.

    Портфель лежит в том же файле, и панель управления, перебирая ключи,
    рисовала его как ещё одну стратегию — карточку с названием PORTFOLIO и
    пустыми полями риска.
    """
    stored = settings_store.load() if stored is None else stored
    return {k: v for k, v in stored.items() if k in settings_store.STRATEGIES}


def _errors_summary():
    """Короткая сводка по ошибкам — для значка в меню."""
    try:
        import error_log
        return error_log.summary()
    except Exception:                              # noqa: BLE001
        return {'groups': 0, 'total': 0, 'last': None, 'categories': []}


def _portfolio():
    """
    Текущая загрузка портфеля и действующий предел.

    Показывается всегда, даже когда предел выключен: цифра «под риском
    сейчас» нужна, чтобы решение о пределе принималось по факту, а не
    наугад.
    """
    source = _broker if _broker is not None else _trade_manager
    used, pct, deposit, slots = 0.0, 0.0, 0.0, 0
    try:
        if source is not None:
            used, pct, deposit = source.portfolio_risk()
            slots = source.portfolio_slots()
    except Exception:                              # noqa: BLE001
        pass
    try:
        limit = settings_store.portfolio_risk_pct()
        max_positions = settings_store.portfolio_max_positions()
    except Exception:                              # noqa: BLE001
        limit, max_positions = 0.0, 0
    return {
        'risk_usd': round(used, 2), 'risk_pct': round(pct, 2),
        'deposit': round(deposit, 2), 'slots': slots,
        'limit_pct': limit, 'limit_positions': max_positions,
        'over': bool(limit and pct > limit),
    }


def _regime():
    """
    Режим рынка и текущий множитель риска SMC.

    Показывается, потому что иначе уменьшенный размер позиции выглядит как
    сбой: сделка открыта, а риск вдвое меньше настроенного, и объяснения
    этому на дашборде нет.

    Берётся ТОЛЬКО готовое значение из кэша адаптера. Считать здесь нельзя:
    дашборд обновляется раз в несколько секунд, и расчёт тянул бы дневные
    свечи с биржи на каждом обновлении — при недоступной сети страница
    висла бы на таймауте.
    """
    try:
        import strategy_smc
        name, mult, text = strategy_smc.regime_snapshot()
        return {'name': name, 'scale': mult, 'text': text,
                'reduced': mult < 1.0}
    except Exception as exc:
        return {'name': None, 'scale': 1.0, 'text': f'режим неизвестен ({exc})',
                'reduced': False}


def _attention(payload):
    """
    То, что требует реакции прямо сейчас.

    Собирается в одном месте, потому что иначе это приходится вылавливать
    глазами по трём разным таблицам и простыне лога.
    """
    items = []

    status = payload.get('status') or {}
    if status.get('state') == 'error':
        items.append({'level': 'bad', 'text': 'Бот остановлен ошибкой',
                      'detail': status.get('detail', '')})
    elif status.get('state') == 'paused':
        items.append({'level': 'warn', 'text': 'Бот на паузе — новые входы не открываются',
                      'detail': ''})

    untracked = [p for p in payload.get('open_positions') or [] if p.get('untracked')]
    if untracked:
        items.append({
            'level': 'bad',
            'text': f'{len(untracked)} позиций бот не ведёт',
            'detail': ', '.join(p['pair'] for p in untracked) +
                      ' — стоп стоит на бирже, но в журнал сделка не попадёт',
        })

    # Ордер, которому осталось меньше двух часов, скорее всего истечёт
    expiring = [o for o in payload.get('pending_orders') or []
                if 0 < (o.get('expires_in_min') or 0) <= 120]
    if expiring:
        items.append({
            'level': 'warn',
            'text': f'{len(expiring)} ордеров скоро истекут',
            'detail': ', '.join(f"{o['pair']} ({o['expires_in_min']} мин)"
                                for o in expiring[:4]),
        })

    # Число берём из журнала ошибок, а не пересчитываем строки лога: иначе
    # полоса и раздел «Ошибки» показывают разные цифры об одном и том же, и
    # непонятно, какой верить.
    errors = payload.get('errors') or {}
    if errors.get('groups'):
        last = ', '.join(errors.get('categories') or [])
        items.append({
            'level': 'warn',
            'text': (f'Ошибок: {errors["groups"]} видов, '
                     f'{errors["total"]} случаев'),
            'detail': f'категории: {last}. Разбор — в разделе «Ошибки»'})

    return items


def _run_action(request):
    """
    Действия оператора над позициями и ордерами.

    Разрешены ТОЛЬКО снижающие риск: закрыть, снять, перевести стоп в
    безубыток, поставить на паузу. Ручного открытия и отодвигания стопа здесь
    нет и не будет — у страницы нет ни пароля, ни HTTPS, и увеличивать
    экспозицию отсюда нельзя.

    Возвращает (получилось, сообщение).
    """
    action = str(request.get('action') or '')
    pair = str(request.get('pair') or '')
    strategy = str(request.get('strategy') or '')

    if action in ('pause', 'resume'):
        try:
            from telegram_bot import controller
            with controller._lock:
                controller._paused = (action == 'pause')
            state = 'на паузе' if action == 'pause' else 'возобновлён'
            log(f"🖐 Бот {state} из дашборда")
            set_status('paused' if action == 'pause' else 'running')
            return True, f'Бот {state}'
        except Exception as exc:
            return False, f'Не удалось: {exc}'

    if _broker is not None:
        handlers = {
            'close': lambda: _broker.close_one(strategy, pair),
            'cancel': lambda: _broker.cancel_pending(strategy, pair),
            'breakeven': lambda: _broker.move_to_breakeven(strategy, pair),
            'close_all': lambda: _broker.close_all(strategy),
        }
        handler = handlers.get(action)
        if handler is None:
            return False, f'Неизвестное действие: {action}'
        try:
            return handler()
        except Exception as exc:
            log(f"⚠️ Действие {action} по {pair}: {exc}")
            return False, str(exc)

    if _trade_manager is None:
        return False, 'Бот не запущен'

    # Боевой счёт: те же действия, но через биржу
    try:
        if action == 'close':
            ok, price = _trade_manager.close_position_by_pair(pair)
            return (ok, f'{pair}: закрыто по {price}' if ok
                    else f'{pair}: закрыть не удалось')
        if action == 'cancel':
            pending = _read_json(PENDING_FILE, {}).get(pair)
            if not pending:
                return False, f'{pair}: ожидающего ордера нет'
            _trade_manager._cancel_pending_order(pair, pending.get('order_id'))
            return True, f'{pair}: ордер снят'
        if action == 'breakeven':
            for position in _trade_manager.active_positions.get(pair, []):
                if position['status'] == 'OPEN':
                    _trade_manager._move_sl_to_breakeven(pair, position)
                    return True, f'{pair}: стоп в безубытке'
            return False, f'{pair}: позиция не отслеживается ботом'
        if action == 'close_all':
            closed = 0
            for open_pair in list(_trade_manager.get_open_pairs()):
                if _trade_manager.close_position_by_pair(open_pair)[0]:
                    closed += 1
            return True, f'Закрыто позиций: {closed}'
    except Exception as exc:
        log(f"⚠️ Действие {action} по {pair}: {exc}")
        return False, str(exc)

    return False, f'Неизвестное действие: {action}'


def export_paths():
    """(csv, jsonl) — файлы истории сделок текущего режима."""
    if config.PAPER_MODE or _broker is not None:
        return PAPER_JOURNAL, PAPER_JSONL
    return JOURNAL_FILE, os.path.join(config.DATA_DIR, 'trades_detail.jsonl')


# ── HTTP ─────────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if path.startswith('/api/data'):
            self._send_json(build_payload())
        elif path == '/api/log':
            self._send_json({'lines': read_log(), 'status': dict(_status)})
        elif path == '/api/export.csv':
            self._send_file(export_paths()[0], 'text/csv; charset=utf-8')
        elif path == '/api/export.jsonl':
            self._send_file(export_paths()[1], 'application/x-ndjson; charset=utf-8')
        elif path.startswith('/api/candles'):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            pair = (q.get('pair') or [''])[0]
            opened = (q.get('from') or [''])[0]
            closed = (q.get('to') or [''])[0]
            if not pair or not opened:
                self._fail(400, 'нужны pair и from')
                return
            try:
                data = _trade_candles(pair, opened, closed)
            except Exception as exc:               # noqa: BLE001
                self._fail(502, f'свечи недоступны: {exc}')
                return
            if data is None:
                self._fail(404, 'свечей за этот период нет')
                return
            self._send_json(data)
        elif path == '/api/errors':
            try:
                import error_log
                self._send_json({'errors': error_log.snapshot(),
                                 'summary': error_log.summary(),
                                 'writable': _controls_allowed()})
            except Exception as exc:               # noqa: BLE001
                self._fail(500, f'журнал ошибок недоступен: {exc}')
        elif path == '/api/update':
            # fetch по требованию: без него страница ждала бы сеть на каждом
            # обновлении, а состояние репозитория меняется несравнимо реже.
            import updater
            fetch = 'check' in (self.path.split('?', 1) + [''])[1]
            self._send_json({'update': updater.status(fetch=fetch),
                             'writable': _controls_allowed()})
        elif path == '/api/settings':
            # Раздел портфеля отдаём ОТДЕЛЬНО от стратегий. Он лежит в том же
            # файле настроек, и панель управления, перебирая ключи, рисовала
            # его как ещё одну стратегию — с названием PORTFOLIO и пустыми
            # полями риска.
            stored = settings_store.load()
            self._send_json({
                'settings': _strategy_settings(stored),
                'portfolio': stored.get(settings_store.PORTFOLIO, {}),
                'limits': settings_store.LIMITS,
                'writable': _controls_allowed()})
        elif path in ('/', '/index.html'):
            self._send_html()
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split('?')[0]
        if path not in ('/api/settings', '/api/deposit', '/api/action',
                        '/api/update', '/api/update/rollback',
                        '/api/errors/clear'):
            self.send_error(404)
            return
        if not _controls_allowed():
            # Дашборд не имеет ни пароля, ни HTTPS. Пока он слушает только
            # localhost, менять параметры торговли безопасно; открытый в сеть
            # он позволил бы любому желающему поднять риск на сделку.
            self._fail(403, 'Управление доступно только с этой машины')
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(min(length, 64 * 1024)) if length else b'{}'
            changes = json.loads(body.decode('utf-8') or '{}')
            if not isinstance(changes, dict):
                raise ValueError('ожидается объект')
        except Exception as exc:
            self._fail(400, f'Неверные данные: {exc}')
            return

        if path == '/api/errors/clear':
            import error_log
            ok = error_log.clear()
            if not ok:
                self._fail(500, 'не удалось очистить журнал ошибок')
                return
            self._send_json({'ok': True, 'message': 'журнал ошибок очищен'})
            return

        if path in ('/api/update', '/api/update/rollback'):
            import updater
            if path.endswith('rollback'):
                ok, message = updater.rollback()
                info = updater.status(fetch=False)
            else:
                ok, message, info = updater.apply()
            if not ok:
                self._fail(409, message)
                return
            self._send_json({'ok': True, 'message': message, 'update': info})
            return

        if path == '/api/action':
            ok, message = _run_action(changes)
            if not ok:
                self._fail(409, message)
                return
            self._send_json({'ok': True, 'message': message})
            return

        if path == '/api/deposit':
            if _broker is None:
                self._fail(400, 'Депозит задаётся только в фантомном режиме')
                return
            ok, message = _broker.set_deposit(
                changes.get('strategy'), changes.get('deposit'),
                restart=bool(changes.get('restart')))
            if not ok:
                self._fail(409, message)
                return
            # Держим настройки в согласии с состоянием счёта, иначе панель
            # покажет одно число, а брокер будет считать по другому.
            settings_store.save({changes['strategy']: {'deposit': changes['deposit']}})
            self._send_json({'ok': True, 'message': message,
                             'settings': _strategy_settings()})
            return

        result = settings_store.save(changes)
        if _broker is not None:
            _broker.apply_settings(result)
        self._send_json({'settings': _strategy_settings(result),
                         'portfolio': result.get(settings_store.PORTFOLIO, {}),
                         'limits': settings_store.LIMITS,
                         'writable': True})

    def _fail(self, code, text):
        """
        Ответ с ошибкой и русским пояснением.

        Через send_error это делать нельзя: строка состояния HTTP кодируется
        latin-1, кириллица валит обработчик, и клиент получает разрыв
        соединения вместо кода ошибки. Пояснение уходит телом ответа.
        """
        body = json.dumps({'error': text}, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        """Отдаёт файл истории на скачивание (для анализа в Excel / pandas)."""
        if not os.path.exists(path):
            self._fail(404, 'История пока пуста')
            return
        try:
            with open(path, 'rb') as fh:
                body = fh.read()
        except Exception as exc:
            self._fail(500, f'Файл не читается: {exc}')
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Disposition',
                         f'attachment; filename="{os.path.basename(path)}"')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        try:
            with open(HTML_FILE, 'rb') as fh:
                body = fh.read()
        except Exception as exc:
            self._fail(500, f'dashboard.html не найден: {exc}')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass   # не засоряем лог бота обращениями браузера


def start_dashboard(port=None, trade_manager=None, broker=None, host=None):
    """
    Поднимает дашборд в фоновом потоке.

    Ошибка запуска намеренно не пробрасывается: занятый порт или отсутствие
    прав не должны мешать боту торговать.
    """
    global _trade_manager, _broker
    _trade_manager = trade_manager
    _broker = broker
    port = port or config.DASHBOARD_PORT
    host = host or config.DASHBOARD_HOST

    try:
        server = HTTPServer((host, port), _Handler)
    except Exception as exc:
        log(f"⚠️ Дашборд не запущен ({host}:{port}): {exc}")
        return None

    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name='dashboard')
    thread.start()
    log(f"📊 Дашборд: http://localhost:{port}")
    if host not in ('127.0.0.1', 'localhost'):
        log(f"   ⚠️ Слушает {host}: страница доступна всей сети без пароля "
            f"(баланс, позиции, история сделок)")
    return server


if __name__ == '__main__':
    srv = start_dashboard()
    if srv:
        log('Дашборд запущен отдельно от бота. Ctrl+C для остановки.')
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            log('Остановка дашборда')
