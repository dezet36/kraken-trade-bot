"""
Лента мастера: книга ТОРГОВОГО СЧЁТА бота → события для участников.

Решение владельца 27.10.2026: участники повторяют сделки счёта бота (сейчас
`upscale`: FIBO + SMC, риск 1%, ведёт ИИ бота), а не стратегии. Бот отдаёт
книгу только для чтения на localhost:

  GET {COPIER_SOURCE_URL}?id=<счёт>&since_ts=<мс>     (format 1, коммит 8443ac6)
  status · pending[] (id = PAIR@placed_ts) · positions[] (remaining, stop_loss, tp_hit,
  ai_half, last_price) · closed[] (trade_id, exit_reason) · instructions[] (kind ai …)

Это отменяет прежнее «бот участников не реагирует на правки бота» для этого
источника: счёт торгует на живом коде бота (решение владельца). Импортов из
бота и чтения его файлов по-прежнему нет — только этот адрес.

Ключ сделки — PAIR@placed_ts: одинаков у заявки и у позиции из неё.

События (словари с kind):
  new_order   — у мастера новая заявка (лимит или стоп-заявка stop_entry);
  order_gone  — заявка исчезла, позиции из неё нет (срок, уход за уровень, слом);
  filled      — заявка мастера налилась;
  opened      — позиция появилась без заявки: ИИ одобрил вход, когда лимит уже за рынком;
  stop_moved  — сменился стоп позиции (безубыток после тейка, ИИ «безубыток»/«подтянуть»);
  reduced     — остаток позиции уменьшился (тейк или «половина» ИИ);
  closed      — позиция закрыта, подтверждено строкой closed (SL, BE, TPn, TIME, AI, RULES…);
  close_missing — позиция исчезла, а строки о закрытии нет дольше предела (тревога);
  ai_note     — новое событие ИИ в книге (для участников, у кого сделка по этой паре);
  master_status — счёт мастера встал (target, failed) или ожил;
  freeze / stale / fresh — состояние самой ленты.

Предохранители:
  - позиции участников закрываются ТОЛЬКО по строке closed;
  - сменился этап счёта мастера или разом исчезло много записей — заморозка:
    событий нет, пока админ не снимет её (unfreeze);
  - сменился format ответа или записи не той схемы — заморозка;
  - адрес не отвечает дольше SOURCE_STALE_S (бот перезапускается) — stale:
    новых входов нет, позиции держат стопы на стороне Upscale.
"""

import json
import os
import time

from copier import config
from copier.log import log, scrub

FORMAT = 1
REQUIRED_TOP = ('format', 'account', 'status', 'phase', 'pending', 'positions', 'closed')
REQUIRED_PENDING = ('id', 'pair', 'direction', 'limit_price', 'stop_loss', 'targets', 'placed_ts')
REQUIRED_POSITION = ('trade_id', 'pair', 'direction', 'entry_price', 'stop_loss', 'initial_stop',
                     'targets', 'remaining', 'placed_ts')
REQUIRED_CLOSED = ('trade_id', 'pair', 'exit_reason')

KEEP_PENDING = REQUIRED_PENDING + ('strategy', 'fractions', 'stop_entry', 'entry_type', 'expires_ts',
                                   'invalidation', 'cancel_beyond', 'max_hold_hours')
KEEP_POSITION = REQUIRED_POSITION + ('strategy', 'fractions', 'tp_hit', 'ai_half', 'breakeven_set',
                                     'opened_ts', 'last_price', 'last_ts', 'max_hold_hours')
RECENT_ROWS = 500


def master_key(pair, placed_ts):
    return f'{pair}@{int(placed_ts)}'


def _missing(rec, required):
    return [k for k in required if k not in rec]


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_feed(data):
    """
    Ответ бота → (снимок, проблемы схемы). Снимок:
    {'account', 'status', 'phase', 'pending': {key: rec}, 'positions': {key: rec}}.
    """
    if not isinstance(data, dict):
        return None, ['ответ бота — не объект']
    miss = _missing(data, REQUIRED_TOP)
    if miss:
        return None, [f'нет полей: {", ".join(miss)}']
    if data.get('format') != FORMAT:
        return None, [f'формат книги {data.get("format")!r}, ожидался {FORMAT}']
    problems = []
    # Риск на сделку — из правил счёта в боте (решение владельца 27.10.2026): у
    # копировщика своего риска и своих пределов нет.
    risk = _num((data.get('rules') or {}).get('risk_pct'))
    snap = {'account': data['account'], 'status': data['status'], 'phase': data['phase'],
            'risk_pct': risk, 'pending': {}, 'positions': {}}
    for book, required, keep in (('pending', REQUIRED_PENDING, KEEP_PENDING),
                                 ('positions', REQUIRED_POSITION, KEEP_POSITION)):
        for rec in data.get(book) or []:
            if not isinstance(rec, dict):
                problems.append(f'{book}: не объект')
                continue
            miss = _missing(rec, required)
            if miss:
                problems.append(f'{book}/{rec.get("pair")}: нет {", ".join(miss)}')
                continue
            if str(rec.get('direction')).upper() not in ('LONG', 'SHORT'):
                problems.append(f'{book}/{rec.get("pair")}: сторона {rec.get("direction")!r}')
                continue
            item = {k: rec.get(k) for k in keep if k in rec}
            item['direction'] = str(rec['direction']).upper()
            item['strategy'] = rec.get('strategy') or ''
            snap[book][master_key(rec['pair'], rec['placed_ts'])] = item
    return snap, problems


def _event(kind, key=None, rec=None, **data):
    ev = {'kind': kind, 'key': key}
    if rec is not None:
        ev['strategy'] = rec.get('strategy')
        ev['pair'] = rec.get('pair')
        ev['direction'] = rec.get('direction')
        ev['rec'] = rec
    ev.update(data)
    return ev


def diff(prev, cur, mass_vanish=None):
    """
    Два снимка → (события, заморозка|None, исчезнувшие позиции {key: rec}).
    prev=None — первый взгляд: текущие заявки становятся new_order (лимит ещё не
    налился — войти по той же цене законно); уже открытые позиции не трогаются:
    войти в идущую сделку по рынку — это другая сделка, не та, что у мастера.
    """
    mass_vanish = config.FEED_MASS_VANISH if mass_vanish is None else mass_vanish
    events, vanished_positions = [], {}
    if prev is None:
        for key, rec in cur['pending'].items():
            events.append(_event('new_order', key, rec, first_seen=True))
        return events, None, vanished_positions

    if prev.get('account') != cur.get('account'):
        return [], f'сменился счёт-источник ({prev.get("account")} → {cur.get("account")})', {}
    if prev.get('phase') != cur.get('phase'):
        return [], f'у счёта мастера начался новый этап ({prev.get("phase")} → {cur.get("phase")})', {}

    gone_orders = []
    for key, rec in prev['pending'].items():
        if key in cur['pending']:
            continue
        if key in cur['positions']:
            events.append(_event('filled', key, cur['positions'][key]))
        else:
            gone_orders.append((key, rec))
    for key, rec in prev['positions'].items():
        if key not in cur['positions']:
            vanished_positions[key] = rec

    total_prev = len(prev['pending']) + len(prev['positions'])
    vanished = len(gone_orders) + len(vanished_positions)
    if vanished >= mass_vanish and total_prev and vanished > total_prev / 2:
        return [], f'разом исчезло {vanished} из {total_prev} записей мастера', {}

    if prev.get('status') != cur.get('status'):
        events.append(_event('master_status', status=cur.get('status'), was=prev.get('status')))

    for key, rec in gone_orders:
        events.append(_event('order_gone', key, rec))

    for key, rec in cur['pending'].items():
        if key not in prev['pending']:
            events.append(_event('new_order', key, rec))

    for key, rec in cur['positions'].items():
        if key in prev['positions']:
            old = prev['positions'][key]
            if _num(old.get('stop_loss')) != _num(rec.get('stop_loss')):
                events.append(_event('stop_moved', key, rec, old_stop=old.get('stop_loss'),
                                     new_stop=rec.get('stop_loss'),
                                     breakeven=bool(rec.get('breakeven_set'))))
            if (_num(rec.get('remaining')) or 0) < (_num(old.get('remaining')) or 0) - 1e-9:
                why = 'target' if int(rec.get('tp_hit') or 0) > int(old.get('tp_hit') or 0) else 'ai_half'
                events.append(_event('reduced', key, rec, remaining=_num(rec.get('remaining')), why=why))
        elif key not in prev['pending']:
            # Позиция без заявки: ИИ одобрил вход, когда лимит уже за рынком
            # (или заявка появилась и налилась между двумя опросами).
            events.append(_event('opened', key, rec))
    return events, None, vanished_positions


def _atomic_write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


class Feed:
    """
    Опрос книги счёта бота. Своё состояние (последний снимок, отметка закрытых,
    ожидающие подтверждения закрытия) — в copier/data/source_state.json, чтобы
    после перезапуска копировщика события за время простоя не потерялись.
    fetch — подменяемая выдача (тесты без сети): async fetch(account, since_ts) -> dict.
    """

    def __init__(self, account=None, state_path=None, fetch=None, now=time.time):
        self.account = account or config.SOURCE_ACCOUNT
        self.state_path = state_path or os.path.join(config.DATA_DIR, 'source_state.json')
        self.now = now
        self._fetch_fn = fetch
        self._session = None
        self.st = self._load()
        self.last_book = None

    # ── своё состояние ─────────────────────────────────────────────────────
    def _load(self):
        blank = {'snapshot': None, 'closed_ts': 0, 'pending_close': {}, 'recent_rows': {},
                 'frozen': None, 'stale': False, 'schema': None, 'last_ok': None,
                 'last_error': None, 'instruction_id': None}
        try:
            with open(self.state_path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            blank.update(data if isinstance(data, dict) else {})
        except FileNotFoundError:
            pass
        except Exception as exc:                       # noqa: BLE001
            log(f'лента: source_state.json нечитаем ({exc}) — начинаю с текущей книги')
        return blank

    def _save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        _atomic_write(self.state_path, self.st)

    # ── здоровье ───────────────────────────────────────────────────────────
    def health(self):
        snap = self.st.get('snapshot') or {}
        last_ok = self.st.get('last_ok')
        return {
            'account': self.account,
            'age_s': None if not last_ok else round(self.now() - last_ok),
            'stale': bool(self.st.get('stale')),
            'frozen': self.st.get('frozen'),
            'schema': self.st.get('schema'),
            'last_error': self.st.get('last_error'),
            'master_status': snap.get('status'),
            'phase': snap.get('phase'),
            'pending': len(snap.get('pending') or {}),
            'positions': len(snap.get('positions') or {}),
            'awaiting_close': len(self.st.get('pending_close') or {}),
        }

    def entries_allowed(self):
        snap = self.st.get('snapshot') or {}
        return not (self.st.get('frozen') or self.st.get('stale') or self.st.get('schema')) \
            and snap.get('status') == 'active'

    def master_risk_pct(self):
        """Риск на сделку счёта-мастера, % (из его правил в боте); None — книги ещё нет."""
        return (self.st.get('snapshot') or {}).get('risk_pct')

    def master_pending(self):
        return dict(((self.st.get('snapshot') or {}).get('pending')) or {})

    def master_positions(self):
        return dict(((self.st.get('snapshot') or {}).get('positions')) or {})

    def set_account(self, account):
        """Другой счёт-источник: состояние с нуля, первый взгляд — без входов в идущие позиции."""
        self.account = account
        self.st = {'snapshot': None, 'closed_ts': 0, 'pending_close': {}, 'recent_rows': {},
                   'frozen': None, 'stale': False, 'schema': None, 'last_ok': None,
                   'last_error': None, 'instruction_id': None}
        self._save()

    async def unfreeze(self):
        """Снять заморозку: текущая книга становится точкой отсчёта, событий за прошлое нет."""
        try:
            data = await self._fetch(0)
        except Exception as exc:                       # noqa: BLE001
            return False, f'книга не получена: {scrub(exc)}'
        snap, problems = parse_feed(data)
        if snap is None or problems:
            return False, 'схема книги не та: ' + '; '.join(problems or [])
        self.st.update(snapshot=snap, frozen=None, schema=None, pending_close={},
                       instruction_id=_last_instruction(data))
        self._save()
        log('лента: заморозка снята, отсчёт от текущей книги')
        return True, ''

    # ── чтение ─────────────────────────────────────────────────────────────
    async def _fetch(self, since_ts):
        if self._fetch_fn is not None:
            return await self._fetch_fn(self.account, since_ts)
        import aiohttp
        # Одно соединение на всё время: опрос раз в несколько секунд.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        params = {'id': self.account, 'since_ts': str(int(since_ts))}
        async with self._session.get(config.SOURCE_URL, params=params) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f'HTTP {resp.status}: {text[:200]}')
            return json.loads(text)

    async def poll(self):
        """Один опрос → список событий. Своё состояние сохраняется, если что-то поменялось."""
        events, changed = [], False
        try:
            data = await self._fetch(self.st.get('closed_ts') or 0)
            self.st['last_ok'] = self.now()
            self.st['last_error'] = None
            self.last_book = data
        except Exception as exc:                       # noqa: BLE001
            data = None
            err = scrub(exc)[:200]
            if self.st.get('last_error') != err:
                log(f'лента: книга счёта {self.account} не получена — {err}')
                self.st['last_error'] = err
                changed = True

        last_ok = self.st.get('last_ok')
        stale_now = not last_ok or (self.now() - last_ok) > config.SOURCE_STALE_S
        if stale_now != bool(self.st.get('stale')):
            self.st['stale'] = stale_now
            events.append(_event('stale' if stale_now else 'fresh'))
            changed = True

        if data is not None:
            changed = True
            # Закрытые — до снимка: строка может прийти раньше, чем позиция исчезнет.
            recent = self.st.setdefault('recent_rows', {})
            for row in data.get('closed') or []:
                if not isinstance(row, dict) or _missing(row, REQUIRED_CLOSED):
                    continue
                recent[str(row['trade_id'])] = {k: row.get(k) for k in (
                    'trade_id', 'strategy', 'pair', 'direction', 'exit_reason', 'exit_price',
                    'pnl_r', 'closed_at')}
                t_ms = int(float(row.get('_t') or 0) * 1000)
                self.st['closed_ts'] = max(int(self.st.get('closed_ts') or 0), t_ms)
            if len(recent) > RECENT_ROWS:
                for tid in sorted(recent, key=lambda t: int(t) if str(t).isdigit() else 0)[:len(recent) - RECENT_ROWS]:
                    recent.pop(tid, None)

            if not self.st.get('frozen'):
                events.extend(self._apply_book(data))

        if not self.st.get('frozen'):
            recent = self.st.get('recent_rows') or {}
            for key, item in list(self.st['pending_close'].items()):
                rec = item['rec']
                row = recent.get(str(rec.get('trade_id')))
                if row:
                    events.append(_event('closed', key, rec, trade=row))
                    self.st['pending_close'].pop(key, None)
                    changed = True
                elif (not item.get('alerted')
                      and self.now() - item['since'] > config.FEED_CLOSE_WAIT_MIN * 60):
                    item['alerted'] = True
                    events.append(_event('close_missing', key, rec))
                    changed = True

        if changed:
            self._save()
        return events

    def _apply_book(self, data):
        events = []
        snap, problems = parse_feed(data)
        if snap is None or problems:
            first = (problems or ['книга не разобрана'])[0]
            if self.st.get('schema') != first:
                self.st['schema'] = first
                events.append(_event('freeze', reason=f'книга счёта не той схемы: {first}'))
                log(f'лента: ЗАМОРОЗКА — схема: {"; ".join(problems[:3])}')
            return events
        self.st['schema'] = None
        new_events, freeze, vanished = diff(self.st.get('snapshot'), snap)
        if freeze:
            self.st['frozen'] = freeze
            log(f'лента: ЗАМОРОЗКА — {freeze}')
            return [_event('freeze', reason=freeze)]
        events.extend(new_events)
        for key, rec in vanished.items():
            self.st['pending_close'][key] = {'rec': rec, 'since': self.now(), 'alerted': False}
        self.st['snapshot'] = snap
        # События ИИ из книги — новые с прошлого опроса (на первом — только отметка).
        last_id = self.st.get('instruction_id')
        for item in data.get('instructions') or []:
            if last_id is not None and int(item.get('id') or 0) > int(last_id) and item.get('kind') == 'ai':
                events.append(_event('ai_note', pair=item.get('pair'), title=item.get('title'),
                                     lines=item.get('lines') or []))
        # Пустая книга на первом взгляде — отметка 0, иначе первая пачка решений ИИ потерялась бы.
        self.st['instruction_id'] = _last_instruction(data, last_id if last_id is not None else 0)
        return events


def _last_instruction(data, default=None):
    ids = [int(i.get('id') or 0) for i in (data.get('instructions') or []) if isinstance(i, dict)]
    return max(ids) if ids else default
