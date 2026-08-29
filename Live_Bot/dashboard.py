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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import scan_report
import settings_store
from logger import log


def _app_version():
    """Версия приложения, если её удаётся узнать. Пусто — тоже ответ."""
    try:
        import updater_app
        return updater_app.current_version() or ''
    except Exception:                              # noqa: BLE001
        return ''


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


def _suspect(strategy, geometry, zone, why):
    """
    Сделка, открытая не тем сетапом, что заявлен в её имени.

    ОТКУДА ЭТО ВЗЯЛОСЬ. Диспетчер в bot.py разбирал случаи как «если SMC —
    так, ИНАЧЕ — фибо». Стратегия уровней не подходила ни под одно условие,
    её готовый сигнал выбрасывался, и на тех же свечах заново искался импульс
    Фибоначчи. Результат уходил в журнал под именем LEVELS. Ошибка починена,
    но сделки в журнале остались, и по ним нельзя сравнивать стратегии.

    ПО ЧЕМУ ОПОЗНАЁТСЯ. Разметка. У настоящей сделки от уровня в ней есть
    линия уровня; у подменённой её нет вовсе, потому что фибо-сигнал не несёт
    поля levels, и ветка разметки не находит, что рисовать. Признак прямой:
    он смотрит на то, ЧЕМ сделка была размечена в момент входа, а не на
    сегодняшние настройки.

    Запасной признак — обоснование входа: у уровней оно говорит про уровень и
    прокол, у фибо про зону A и тренд старшего ТФ.

    Возвращает причину строкой либо None. Ничего не скрывает и не удаляет:
    решение, что делать с такими сделками, за человеком.
    """
    if strategy != 'LEVELS':
        return None
    lines = (geometry or {}).get('lines') or []
    if any('уровень' in str(line.get('label') or '') for line in lines):
        return None
    text = f'{zone or ""} {why or ""}'.lower()
    if 'уровн' in text or 'прокол' in text:
        return None
    return ('открыта не своим сетапом: в разметке нет уровня. '
            'Ошибка диспетчера стратегий, исправлена — но эта сделка '
            'считалась по чужой геометрии, и в сравнении стратегий на неё '
            'опираться нельзя')


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
            # Минуты жизни сделки, прожитые без свечей. Пустое значение — у
            # сделок старше самой колонки: про них неизвестно, а «неизвестно»
            # не то же самое, что «дыры не было».
            'gap_min': (_to_float(row.get('data_gap_min'))
                        if str(row.get('data_gap_min', '')).strip() != '' else None),
        })
        trades[-1]['suspect'] = _suspect(
            trades[-1]['strategy'], trades[-1]['geometry'],
            trades[-1]['zone'], trades[-1]['why'])
    trades.sort(key=lambda t: t['closed'])
    return trades


# ── Метрики ──────────────────────────────────────────────────────────────────

def is_measured(trade):
    """
    Годится ли сделка для статистики.

    Не годится ТОЛЬКО та, у которой дыра ИЗМЕРЕНА и больше нуля. Всё
    остальное идёт в счёт, включая сделки старше самой колонки.

    ЭТО РАЗЛИЧИЕ Я СНАЧАЛА СТЁР, И ОНО ЧУТЬ НЕ ОБОШЛОСЬ ДОРОГО. Первая версия
    считала годными только те, у кого gap_min == 0, — то есть выбрасывала и
    «дыра была», и «неизвестно». В нашем журнале под второе попали ровно две
    сделки, #25 и #26, обе убыточные и обе прожитые целиком: они просто
    старше колонки. Статистика от такого фильтра улучшалась с 45% до 56%
    побед — не потому, что стала честнее, а потому, что из неё пропали два
    минуса.

    Отсутствие отметки — не свидетельство чистоты, но и не свидетельство
    порчи. Выбрасывать по нему значит подгонять выборку, а подгонка, которая
    льстит, опаснее шума.

    ПОЧЕМУ ПО УМОЛЧАНИЮ, А НЕ ПО ГАЛОЧКЕ. Статистика существует, чтобы по ней
    решать. Значение по умолчанию должно быть тем, которому можно верить, иначе
    честная цифра становится необязательным уточнением, которое никто не
    включает.
    """
    gap = trade.get('gap_min')
    return gap is None or gap == 0


def _summarise(trades):
    """
    Сводка по списку сделок одной стратегии — по ТЕМ, что годятся для счёта.

    Сколько отброшено, сводка сообщает отдельным полем: молча уменьшить число
    сделок значило бы заменить одну неверную картину другой.
    """
    everything = trades
    trades = [t for t in trades if is_measured(t)]
    skipped = len(everything) - len(trades)
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
        # Отброшенные — не «минус две сделки», а «две сделки, про которые мы
        # не знаем правды». Панель обязана назвать их число.
        'skipped': skipped,
        'trades_all': len(everything),
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
        'exchange': config.EXCHANGE_NAME,
        # Можно ли менять что-либо с этой страницы. Панель ключей без
        # этого флага рисовалась бы и на открытом наружу дашборде.
        'writable': _controls_allowed(),
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
        'exchange': config.EXCHANGE_NAME,
        # Можно ли менять что-либо с этой страницы. Панель ключей без
        # этого флага рисовалась бы и на открытом наружу дашборде.
        'writable': _controls_allowed(),
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


def _directions():
    """
    Сводка по направлению: сколько вложено, сколько стало, сколько работает.

    НАПРАВЛЕНИЕ ОСТАЛОСЬ ОДНО — биржа. Рядом стояло второе, Polymarket, и
    сводка писалась ради их сравнения; теперь оно вырезано и живёт в ветке
    polymarket-archive. Форма ответа сохранена: она отвечает на тот же вопрос
    и не требует от панели складывать чужие суммы самой — сложение в браузере
    верный способ однажды сложить несуммируемое.
    """
    out = []

    # ── Биржа ───────────────────────────────────────────────────────────────
    #
    # «ВЛОЖЕНО» БЕРЁТСЯ ИЗ СОСТОЯНИЯ, А НЕ ИЗ НАСТРОЕК, и это не тонкость.
    #
    # Настройка говорит, с какой суммой НАЧАТЬ. Но брокер, найдя начатый
    # эксперимент, оставляет прежний депозит — иначе доходность считалась бы от
    # подменённого знаменателя. Он даже предупреждает об этом в журнале:
    # «в настройках депозит $20 000, но эксперимент начат с $10 000».
    #
    # Панель же брала «вложено» из НАСТРОЕК, а «стало» из СОСТОЯНИЯ — и
    # показывала разницу как результат. На живых данных это выглядело так:
    #
    #     FIBO   вложено $20 000 → стало $10 076   итог -$9 924
    #     на деле по журналу сделок:                итог   +$112
    #
    #     SMC    вложено  $6 800 → стало  $9 764   итог +$2 964
    #     на деле по журналу сделок:                итог   -$236
    #
    # У обеих знак перевёрнут, и суммы не имеют отношения к торговле. Человек
    # видел убыток в шесть тысяч там, где бот отработал в небольшой плюс.
    strategies = []
    invested = current = 0.0
    book = _broker or _trade_manager
    for name, start in (config.PAPER_START_BALANCES or {}).items():
        info = (_STRATEGY_CACHE or {}).get(name) or {}
        if hasattr(book, 'start_balance'):
            try:
                started_with = float(book.start_balance(name)) or float(start)
            except Exception:                               # noqa: BLE001
                started_with = float(start)
            start = started_with
        equity = float(info.get('equity') or start)
        invested += float(start)
        current += equity
        strategies.append({
            'name': name, 'invested': round(float(start), 2),
            'equity': round(equity, 2),
            'pnl': round(equity - float(start), 2),
            'open': int(info.get('open') or 0),
            'enabled': bool(info.get('enabled', True)),
        })
    out.append({
        'id': 'exchange', 'title': 'Биржа',
        'subtitle': f'{config.EXCHANGE_NAME.upper()} · бессрочные фьючерсы',
        'invested': round(invested, 2), 'equity': round(current, 2),
        'pnl': round(current - invested, 2),
        'strategies': strategies,
        'connected': bool(config.EXCHANGE_NAME),
    })

    return out


_STRATEGY_CACHE = {}


def build_payload():
    """Полный набор данных для дашборда."""
    payload = _paper_payload() if (config.PAPER_MODE or _broker is not None) else _live_payload()
    payload['status'] = dict(_status)
    # Сводка по направлениям считается ПОСЛЕ основного набора: ей нужны
    # результаты стратегий биржи, которые тот и собирает.
    global _STRATEGY_CACHE
    _STRATEGY_CACHE = payload.get('strategies') or {}
    payload['directions'] = _directions()
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


_TF_MINUTES = (('5m', 5), ('15m', 15), ('1h', 60), ('4h', 240), ('1d', 1440))
# Больше трёх сотен свечей на холст шириной в семьсот точек — это полоски по
# два пикселя: формально данные есть, разобрать нельзя ничего.
_MAX_BARS = 320


def _pick_timeframe(window_minutes):
    """
    Таймфрейм под ВСЁ окно графика — от начала сетапа до выхода с запасом.

    Правило теперь одно, а было два. Раньше выбор зависел ещё и от возраста
    сделки, потому что свечи запрашивались без отметки начала и биржа отдавала
    последние 1000 от текущего момента: до месячной давности пятиминутки не
    добивали. Теперь начало передаётся явно, и возраст ни на что не влияет —
    остаётся единственный вопрос, влезает ли окно в читаемое число свечей.
    """
    for name, minutes in _TF_MINUTES:
        if window_minutes / minutes <= _MAX_BARS:
            return name, minutes
    return _TF_MINUTES[-1]


def _trade_candles(pair, opened, closed, setup_from=None):
    """
    Свечи вокруг сделки, начиная от сетапа, по которому в неё вошли.

    ПОЧЕМУ ОТ СЕТАПА, А НЕ ОТ ВХОДА. Окно от входа до выхода показывает, чем
    сделка кончилась, но не показывает, ПОЧЕМУ её открыли. Импульс, который
    размечали сеткой Фибоначчи, движение до ордер-блока, касания уровня — всё
    это происходит ДО входа и в такое окно не попадает. График получался
    честным и бесполезным: три линии плана и свечи вокруг них.

    setup_from — время начала сетапа (начало импульса у Фибоначчи и SMC).
    Если его нет — у старых записей и у стратегии уровней, где момент
    образования уровня не сохраняется, — берём запас в четверть длительности
    сделки, но не меньше часа: хоть какой-то подход к цене видно.
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

    entry_at = _naive(opened)
    end = _naive(closed) if closed else pd.Timestamp.utcnow().tz_localize(None)

    start = entry_at
    if setup_from:
        try:
            candidate = _naive(setup_from)
            # Строго раньше входа и не абсурдно раньше: битая метка из старой
            # записи не должна растянуть окно на год и превратить график в
            # дневки, на которых сделки не видно вовсе.
            if candidate < entry_at and (entry_at - candidate).days <= 30:
                start = candidate
        except Exception:                          # noqa: BLE001
            pass
    if start == entry_at:
        lead = max((end - entry_at).total_seconds() / 60 * 0.25, 60)
        start = entry_at - timedelta(minutes=lead)

    span = max((end - start).total_seconds() / 60, 30)
    pad = timedelta(minutes=span * 0.08)
    since = start - pad
    until = end + pad
    timeframe, tf_min = _pick_timeframe((until - since).total_seconds() / 60)
    need = int((until - since).total_seconds() / 60 / tf_min) + 5

    key = (pair, timeframe, since.strftime('%Y%m%d%H%M'), need)
    if key in _candle_cache:
        return _candle_cache[key]

    from exchange import fetch_ohlcv
    limit = min(max(need, 60), 1000)
    # Отметка начала — то, чего здесь не было и из-за чего у закрытых сделок
    # график не строился вовсе. Берём с запасом в две свечи: биржи по-разному
    # округляют границу, и без запаса первая свеча окна иногда не приходит.
    # Считаем от эпохи вычитанием, а не через .timestamp(): у наивной метки
    # трактовка зоны различается между pandas и стандартным datetime, и такая
    # ошибка сдвинула бы окно на часовой пояс — молча, без единого исключения.
    epoch = pd.Timestamp('1970-01-01')
    since_ms = int((since - timedelta(minutes=2 * tf_min) - epoch)
                   // pd.Timedelta('1ms'))
    df = fetch_ohlcv(timeframe, limit=limit, symbol=pair, since=since_ms)
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


def _exchange_state(stored=None):
    """
    Состояние подключения к биржам: выбранная, настроенные, их возможности.

    КЛЮЧИ ОТСЮДА НЕ ОТДАЮТСЯ И НЕ ПРИНИМАЮТСЯ — только признак «настроена».
    Дашборд слушает без пароля, и секрет, прошедший через него, считался бы
    скомпрометированным. Ключи живут в .env; панель лишь переключает между
    теми биржами, что уже настроены.

    Возможности показываются потому, что биржи расходятся сильно: у BingX из
    четырёх источников данных о позиционировании есть только фандинг. Человек,
    переключивший биржу, должен видеть, что именно он теряет, — а не
    обнаружить это через неделю по пустому хранилищу.
    """
    import exchange as ex_mod

    stored = settings_store.load() if stored is None else stored
    configured = ex_mod.configured_exchanges()
    active = ex_mod.active_exchange_name()

    caps = {}
    try:
        client = ex_mod.get_exchange()
        caps = {name: ex_mod.supports(client, name)
                for name in ex_mod.CAPABILITIES}
    except Exception as exc:                       # noqa: BLE001
        caps = {'error': str(exc)[:80]}

    return {
        'active': active,
        'configured': configured,
        'supported': list(ex_mod.SUPPORTED_EXCHANGES),
        'capabilities': caps,
        'chosen': (stored.get(settings_store.EXCHANGE) or {}).get('name'),
    }


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

    # Дневной итог показывается всегда, даже при выключенном пределе: цифра
    # «сколько потеряно сегодня» нужна, чтобы решение о пределе принималось
    # по факту, а не наугад.
    day_pnl, day_pct, day_limit = 0.0, 0.0, 0.0
    try:
        day_limit = settings_store.daily_loss_pct()
        if source is not None and hasattr(source, 'daily_result'):
            day_pnl, day_pct, _dep = source.daily_result()
    except Exception:                              # noqa: BLE001
        pass

    # ВЫКЛЮЧЕННЫЙ ПРЕДЕЛ НАЗЫВАЕТСЯ ПРЯМО. В поле стоит ноль, и на глаз это
    # неотличимо от «ещё не задал»: оба портфельных предела стояли
    # выключенными, а панель показывала их значения так, будто они работают.
    # Рядом — сколько депозита МОЖЕТ оказаться под риском одновременно, если
    # каждая стратегия займёт свои слоты: без этого числа «предела нет» звучит
    # безобидно.
    import risk_gate
    off = risk_gate.disabled_limits(max_positions, limit, day_limit)
    try:
        import settings_store as st
        from levels import params as lp
        from rsibb import params as rp
        from smc import params as sp
        worst, unbounded = risk_gate.max_exposure([
            ('FIBO', st.max_slots('FIBO'), config.RISK_PER_TRADE),
            ('SMC', st.max_slots('SMC'), sp.RISK_PER_TRADE_PCT),
            ('LEVELS', st.max_slots('LEVELS'), lp.RISK_PCT),
            ('RSIBB', st.max_slots('RSIBB'), rp.RISK_PCT),
        ])
    except Exception:                              # noqa: BLE001
        worst, unbounded = 0.0, []

    return {
        'risk_usd': round(used, 2), 'risk_pct': round(pct, 2),
        'deposit': round(deposit, 2), 'slots': slots,
        'limit_pct': limit, 'limit_positions': max_positions,
        'over': bool(limit and pct > limit),
        'day_pnl': round(day_pnl, 2), 'day_pct': round(day_pct, 2),
        'day_limit': day_limit,
        'day_stopped': bool(day_limit and day_pnl < 0 and -day_pct >= day_limit),
        'limits_off': off,
        'worst_case_pct': round(worst, 1),
        'unbounded': unbounded,
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

    # НЕ ПОДКЛЮЧЕНО — ЭТО ПЕРВОЕ, ЧТО НАДО ЗНАТЬ: без ключей бот не торгует
    # вовсе, и «почему нет сделок» иначе выясняется перебором разделов.
    #
    # Предупреждение жило на странице «Сводка» и вместе с ней исчезло: та
    # страница показывала одно направление (второе, Polymarket, вырезано) и
    # повторяла «Обзор» более бедной таблицей, занимая при этом место первого
    # экрана. Само предупреждение к сводке отношения не имело — ему место
    # здесь, среди того, что требует реакции.
    for direction in payload.get('directions') or []:
        if not direction.get('connected'):
            items.append({
                'level': 'bad',
                'text': f"{direction.get('title') or 'Биржа'}: не подключено",
                'detail': 'ключи не заданы — новые сделки не открываются. '
                          'Раздел «Подключения».',
            })

    status = payload.get('status') or {}
    if status.get('state') == 'error':
        items.append({'level': 'bad', 'text': 'Бот остановлен ошибкой',
                      'detail': status.get('detail', '')})
    elif status.get('state') == 'paused':
        items.append({'level': 'warn', 'text': 'Бот на паузе — новые входы не открываются',
                      'detail': ''})

    # Дневной предел. Сработавший стоп-кран обязан быть виден на первом
    # экране: иначе «почему бот ничего не открывает» выясняется через журнал,
    # а выглядит это как поломка.
    portfolio = payload.get('portfolio') or {}
    day_limit = portfolio.get('day_limit') or 0
    day_pct = portfolio.get('day_pct') or 0
    if portfolio.get('day_stopped'):
        items.append({
            'level': 'bad',
            'text': f'Дневной предел убытка достигнут: {day_pct:.2f}% при {day_limit:.2f}%',
            'detail': 'новые сделки до завтра не открываются, открытые ведутся как обычно',
        })
    elif day_limit and day_pct < 0 and -day_pct >= day_limit / 2:
        items.append({
            'level': 'warn',
            'text': f'За день потеряно {abs(day_pct):.2f}% при пределе {day_limit:.2f}%',
            'detail': 'ещё немного — и новые сделки перестанут открываться',
        })

    # Сделки, открытые не своим сетапом. Это не текущая беда, а испорченные
    # данные: пока такие лежат в журнале, сравнение стратегий врёт, и решение
    # «какую усиливать» принимается вслепую. Молча их не выкидываем — что с
    # ними делать, решает человек, — но и промолчать нельзя.
    suspect = [t for t in payload.get('closed') or [] if t.get('suspect')]
    if suspect:
        by_strategy = {}
        for trade in suspect:
            by_strategy[trade['strategy']] = by_strategy.get(trade['strategy'], 0) + 1
        where = ', '.join(f'{name}: {count}' for name, count in by_strategy.items())
        items.append({
            'level': 'warn',
            'text': f'{len(suspect)} сделок открыты не своим сетапом ({where})',
            'detail': 'ошибка диспетчера стратегий, исправлена; эти сделки '
                      'считались по чужой геометрии — в сравнении стратегий '
                      'на них опираться нельзя. Помечены в истории',
        })

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
        elif path == '/api/export/save':
            self._save_export()
        elif path == '/api/whoami':
            # КТО ДЕРЖИТ ПОРТ. Отвечает только наше приложение, и по ответу его
            # можно узнать наверняка — в отличие от имени процесса и командной
            # строки, по которым опознание врёт: запуск из исходников, дочерний
            # процесс сборщика, служба — всё это выглядит по-разному, а порт
            # один. Из-за этого обновлённая копия отказывалась закрывать свою же
            # предыдущую («это не наш бот») и вставала намертво.
            #
            # Отдаётся только то, что и так видно снаружи: ничего чувствительного
            # здесь нет и быть не должно — обработчик доступен без пароля.
            import os as _os
            self._send_json({'app': 'kraken-trade-bot',
                             'pid': _os.getpid(),
                             'version': _app_version()})
        elif path.startswith('/api/report.txt'):
            # Отчёт собирается на лету, а не лежит файлом: он должен отражать
            # состояние на момент нажатия, иначе присланное описывает не ту
            # неполадку, из-за которой его и делали.
            try:
                import report
                body = report.build().encode('utf-8')
                name = report.filename()
            except Exception as exc:               # noqa: BLE001
                self._fail(500, f'отчёт не собрался: {exc}')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            # Имя с кириллицей — только в filename*, по RFC 5987. Простой
            # filename с не-ASCII часть браузеров обрезает до мусора.
            from urllib.parse import quote
            self.send_header('Content-Disposition',
                             f"attachment; filename=\"report.txt\"; "
                             f"filename*=UTF-8''{quote(name)}")
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith('/api/candles'):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            pair = (q.get('pair') or [''])[0]
            opened = (q.get('from') or [''])[0]
            closed = (q.get('to') or [''])[0]
            # Начало сетапа: по нему окно разворачивается назад, до импульса,
            # по которому вошли. Необязательный — у старых записей его нет.
            setup_from = (q.get('setup') or [''])[0]
            if not pair or not opened:
                self._fail(400, 'нужны pair и from')
                return
            try:
                data = _trade_candles(pair, opened, closed, setup_from or None)
            except Exception as exc:               # noqa: BLE001
                self._fail(502, f'свечи недоступны: {exc}')
                return
            if data is None:
                self._fail(404, 'свечей за этот период нет')
                return
            self._send_json(data)
        elif path == '/api/settings/history':
            # Отдельная выдача, а не часть общей: история нужна редко, а
            # общая выдача опрашивается каждые несколько секунд.
            try:
                self._send_json({'history': settings_store.history()})
            except Exception as exc:               # noqa: BLE001
                self._fail(500, f'история настроек недоступна: {exc}')
        elif path == '/api/errors':
            try:
                import error_log
                # Ошибки чистятся от ключей ДО показа, а не только в отчёте.
                # Сообщения об отказе биржи часто содержат полный адрес
                # запроса вместе с ключом, а трассировки — куски конфига. На
                # экране это лежит открытым текстом, и достаточно одного
                # скриншота, отправленного за помощью, чтобы ключ уехал.
                import report
                self._send_json({
                    'errors': report.scrub_obj(error_log.snapshot()),
                    'summary': report.scrub_obj(error_log.summary()),
                    'writable': _controls_allowed(),
                })
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
                'notify': stored.get(settings_store.NOTIFY, {}),
                'limits': settings_store.LIMITS,
                'exchange': _exchange_state(stored),
                'writable': _controls_allowed()})
        elif path in ('/', '/index.html'):
            self._send_html()
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split('?')[0]
        if path not in ('/api/settings', '/api/deposit', '/api/action',
                        '/api/update', '/api/update/rollback',
                        '/api/errors/clear', '/api/keys'):
            self.send_error(404)
            return
        if not _controls_allowed():
            # Дашборд не имеет ни пароля, ни HTTPS. Пока он слушает только
            # localhost, менять параметры торговли безопасно; открытый в сеть
            # он позволил бы любому желающему поднять риск на сделку.
            #
            # ОТКАЗ ОБЪЯСНЯЕТ, ЧТО ДЕЛАТЬ. Прежнее «доступно только с этой
            # машины» верно и бесполезно: человек сидит за той самой машиной по
            # удалённому столу и не понимает, о какой другой речь. Дело не в
            # том, откуда он смотрит, а в том, на каком адресе слушает сервер.
            self._fail(403,
                       'Управление выключено: панель открыта в сеть '
                       f'(DASHBOARD_HOST={config.DASHBOARD_HOST}), а пароля у '
                       'неё нет. Чтобы разрешить, добавьте в .env строку '
                       'DASHBOARD_ALLOW_CONTROL=true и перезапустите — но '
                       'только если порт закрыт извне брандмауэром.')
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

        if path == '/api/keys':
            # Ключи биржи меняются отсюда, а не правкой .env в блокноте.
            # Окно первого запуска эту работу уже делает, но оно показывается
            # РОВНО один раз: протух ключ или сменили биржу — и человек снова
            # оставался наедине с текстовым файлом.
            #
            # Ключи не попадают ни в журнал, ни в ответ: в лог пишется только
            # факт замены. Проверка идёт тем же способом и на том же адресе,
            # каким потом пойдёт бот, — иначе демо-ключи при боевом режиме
            # прошли бы молча.
            import first_run
            exchange = str(changes.get('exchange') or config.EXCHANGE_NAME).lower()
            mode = str(changes.get('mode') or config.TRADING_MODE).upper()
            api_key = str(changes.get('key') or '').strip()
            secret = str(changes.get('secret') or '').strip()
            if not api_key or not secret:
                self._fail(400, 'Заполните оба поля')
                return
            ok, error = first_run.check_keys(exchange, mode, api_key, secret)
            if not ok:
                self._fail(409, f'Биржа не приняла ключи: {error}')
                return
            prefix = exchange.upper()
            values = {'EXCHANGE': exchange, 'TRADING_MODE': mode,
                      f'{prefix}_API_KEY': api_key, f'{prefix}_SECRET_KEY': secret}
            if mode == 'LIVE':
                values['LIVE_CONFIRMED'] = 'YES'
            try:
                first_run._write_env(values)
            except Exception as exc:               # noqa: BLE001
                self._fail(500, f'Не удалось записать настройки: {exc}')
                return
            log(f"🔑 ключи биржи заменены оператором ({exchange}, {mode})")
            self._send_json({'ok': True, 'message':
                             'Ключи проверены и сохранены. '
                             'Перезапустите приложение, чтобы они начали действовать.'})
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
            stored = settings_store.load()
            self._send_json({'ok': True, 'message': message,
                             'settings': _strategy_settings(stored),
                             'notify': stored.get(settings_store.NOTIFY, {})})
            return

        result = settings_store.save(changes)
        if _broker is not None:
            _broker.apply_settings(result)
        self._send_json({'settings': _strategy_settings(result),
                         'portfolio': result.get(settings_store.PORTFOLIO, {}),
                         'notify': result.get(settings_store.NOTIFY, {}),
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
        """
        Отдаёт ответ. Ушедший клиент — не ошибка сервера.

        ОБРЫВ СОЕДИНЕНИЯ СЛУЧАЕТСЯ ПОСТОЯННО и не значит ничего: человек ушёл
        со страницы, закрыл окно, обновил её на середине долгого ответа. Раньше
        каждый такой случай печатал полную простыню исключения — по двадцать
        строк на каждое закрытое окно. В журнале это выглядит как поломка
        сервера, а поломки там нет; настоящие ошибки в этом шуме теряются.
        """
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError, OSError):
            self.close_connection = True

    def _save_export(self):
        """
        Сохраняет выгрузку НА ДИСК и отвечает путём к ней.

        ЗАЧЕМ ЭТО ПОМИМО ОБЫЧНОГО СКАЧИВАНИЯ. Приложение открывается в своём
        окне через pywebview с движком WebView2, а там ссылка со свойством
        `download` не делает НИЧЕГО: обработчик загрузок не зарегистрирован, и
        нажатие проходит впустую. Со стороны это выглядит как поломка сервера,
        хотя сервер отвечает исправно — проверено, все три выгрузки отдают 200 с
        правильными заголовками.

        Здесь файл пишется сам, и в ответ уходит полный путь: человеку остаётся
        его открыть. Работает одинаково и в окне приложения, и в браузере, и по
        сети — в отличие от скачивания, которое зависит от того, чем открыли.
        """
        from urllib.parse import parse_qs, urlparse

        kind = (parse_qs(urlparse(self.path).query).get('kind') or ['csv'])[0]
        folder = os.path.join(config.DATA_DIR, 'exports')
        try:
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d-%H%M')
            if kind == 'report':
                import report
                body = report.build().encode('utf-8')
                name = report.filename()
            else:
                source = export_paths()[1 if kind == 'jsonl' else 0]
                if not os.path.exists(source):
                    self._fail(404, 'История пока пуста — сохранять нечего')
                    return
                with open(source, 'rb') as fh:
                    body = fh.read()
                name = f'{stamp}-{os.path.basename(source)}'
            target = os.path.join(folder, name)
            with open(target, 'wb') as fh:
                fh.write(body)
        except Exception as exc:                   # noqa: BLE001
            self._fail(500, f'не удалось сохранить: {exc}')
            return
        self._send_json({'ok': True, 'path': os.path.abspath(target),
                         'folder': os.path.abspath(folder),
                         'bytes': len(body)})

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
        # СЕРВЕР МНОГОПОТОЧНЫЙ, И ЭТО НЕ ОПТИМИЗАЦИЯ, А ИСПРАВЛЕНИЕ.
        #
        # Обычный HTTPServer обрабатывает запросы ПО ОДНОМУ. Снимок Polymarket
        # ходит на биржу трижды — остаток, заявки, сделки, — и на это время
        # замирала ВСЯ панель: страница не открывалась, кнопки не отвечали,
        # снаружи это выглядело как зависшее приложение. Наблюдалось прямо на
        # запуске: процесс жив, порт слушает, а ответа нет две минуты.
        #
        # Потоки демонские: сервер не должен удерживать выход из программы.
        server = ThreadingHTTPServer((host, port), _Handler)
        server.daemon_threads = True
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
