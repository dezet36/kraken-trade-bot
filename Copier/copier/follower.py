"""
Исполнитель: события счёта-мастера (книга торгового счёта бота, сейчас `upscale`)
→ заявки и позиции на счетах участников Upscale.

Правило одно: сделка участника живёт, пока жива сделка мастера.
  new_order   → лимит (или стоп-заявка, если у мастера stop_entry) по цене мастера,
                стоп (и тейк, если цель одна) прямо на ордере;
  order_gone  → снять заявку; если у участника она успела налиться — закрыть по рынку;
  filled      → ничего: налив участника видит сверка (цены Upscale ≠ цены бота);
  opened      → вход по рынку (ИИ бота одобрил, когда лимит уже за рынком), если
                цена мастера ещё не за стопом и не за первой целью;
  stop_moved  → перенести стоп участника, только в сторону уменьшения риска
                (безубыток после тейка, ИИ «безубыток» или «подтянуть»);
  reduced     → довести остаток до доли мастера (тейк или «половина» ИИ) по рынку,
                если тейк участника на Upscale ещё не сработал;
  closed      → закрыть остаток по рынку (если стоп/тейк Upscale не закрыли раньше);
  ai_note     → участникам со сделкой по этой паре — что решил ИИ бота.

Сверка (sync_account) раз в RECONCILE_S: наливы, закрытия стопом/тейком Upscale,
ручные действия участника, статус счёта (просадки, правило 30%, антифрод).
Стоп стоит на стороне Upscale — участник защищён и когда копировщик лежит.
"""

import asyncio
import json
import time
from decimal import Decimal

from copier import config, crypto, sizing
from copier import markets as mk
from copier.db import now_iso
from copier.log import log
from copier.upscale import (UpscaleClient, UpscaleError, close_order, entry_order, from_fp9,
                            new_idem_key)

MARKETS_TTL_S = 3600
EXIT_REASONS = {'SL': 'стоп', 'BE': 'безубыток', 'TP': 'цель', 'TIME': 'срок удержания',
                'AI': 'решение ИИ', 'RULES': 'правила пропа счёта', 'MANUAL': 'вручную'}
ORDER_GRACE_S = 90          # свежий ордер может ещё не попасть в список активных
STATUS_FRESH_S = 30         # снимок счёта из сверки (раз в 10 с) годится для входа без нового запроса


def pick(obj, names, default=None):
    for name in names:
        if isinstance(obj, dict) and obj.get(name) not in (None, ''):
            return obj.get(name)
    return default


def _ids(value):
    """positionId в схеме местами массив — приводим к множеству строк."""
    if value is None:
        return set()
    if isinstance(value, (list, tuple)):
        return {str(v) for v in value if v}
    return {str(value)}


def _rule_pct(raw):
    """
    Правила фазы (maxDailyDrawdown, maxTotalDrawdown, profitTarget…) — проценты
    ОБЫЧНЫМ числом: "5" = 5%. Проверено на демо-счёте 27.10.2026; до этого они
    читались как fp9, дневной лимит выходил 0.0000005%, и запас пропа закрывал
    каждый вход. Больше 100 — значит, всё-таки fp9 процента (на будущее).
    """
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None or raw == '':
        return None
    try:
        v = Decimal(str(raw))
    except Exception:                                  # noqa: BLE001
        return None
    return v / Decimal(10) ** 9 if v > 100 else v


def _ratio_pct(raw):
    """consistencyRule* — доля в fp9 ("300000000" = 0.3) → проценты (30)."""
    v = from_fp9(raw)
    return None if v is None else v * 100


def position_entry(pos):
    """
    Цена входа позиции. Поля entryPrice у Upscale нет (демо 27.10.2026): есть
    notional (котируемая сумма по цене входа) и size (базовый актив) — их частное.
    """
    direct = from_fp9(pick(pos, ('entryPrice', 'avgEntryPrice', 'openPrice')))
    if direct:
        return direct
    size, notional = from_fp9(pick(pos, ('size', 'baseSize'))), from_fp9(pos.get('notional'))
    return notional / size if size and notional else None


def closed_result(info):
    """
    Итог закрытой позиции из GET /positions/{idx} (демо 27.10.2026): pnl — до
    комиссий, fee — вход и выход, funding — фандинг (плюс — получен). Чистый итог =
    pnl − fee + funding. Цена выхода — exchangedQuote ÷ exchangedBase события closePosition.
    """
    pnl = from_fp9(pick(info, ('realizedPnl', 'pnl')))
    net = None if pnl is None else pnl - (from_fp9(info.get('fee')) or 0) + (from_fp9(info.get('funding')) or 0)
    exit_price = from_fp9(pick(info, ('closePrice', 'exitPrice')))
    quote, base = from_fp9(info.get('exchangedQuote')), from_fp9(info.get('exchangedBase'))
    if exit_price is None and info.get('eventName') == 'closePosition' and quote and base:
        exit_price = quote / base
    return net, exit_price


def status_from_account(acc):
    """Объект счёта из /accounts/with-risk-status → плоский снимок (Decimal)."""
    rs = acc.get('riskStatus') or {}
    f = from_fp9
    out = {
        'accountId': acc.get('accountId'),
        'type': acc.get('type'),
        'phase': acc.get('currentPhase'),
        'status': acc.get('status'),
        'tradingLockReason': acc.get('tradingLockReason'),
        'apiTrading': acc.get('apiTrading'),
        'antifraudStatus': acc.get('antifraudStatus'),
        'failureReason': acc.get('failureReason'),
        'initialAccountBalance': f(acc.get('initialAccountBalance')),
        'accountBalance': f(rs.get('currentBalance')) or f(acc.get('accountBalance')),
        'currentEquity': f(rs.get('currentEquity')),
        'dayStartEquity': f(rs.get('dayStartEquity')) or f(acc.get('dayStartEquity')),
        'maxEquity': f(rs.get('maxEquity')) or f(acc.get('maxEquity')),
        'maxDailyDrawdown': _rule_pct(acc.get('maxDailyDrawdown')),
        'maxTotalDrawdown': _rule_pct(acc.get('maxTotalDrawdown')),
        'maxTrailingTotalDrawdown': _rule_pct(acc.get('maxTrailingTotalDrawdown')),
        'drawdownBase': f(acc.get('drawdownBase')),
        'maxDrawdownStop': f(acc.get('maxDrawdownStop')),
        'profitTarget': _rule_pct(acc.get('profitTarget')),
        'minTradingDays': acc.get('minTradingDays'),
        'profitableDays': acc.get('profitableDays'),
        'tradingDays': rs.get('tradingDays'),
        'consistencyRuleApplies': rs.get('consistencyRuleApplies'),
        'consistencyRuleMet': rs.get('consistencyRuleMet'),
        'consistencyRuleRatio': _ratio_pct(rs.get('consistencyRuleRatio')),
        'consistencyRuleLimit': _ratio_pct(rs.get('consistencyRuleLimit')),
    }
    if isinstance(out['minTradingDays'], (list, tuple)):
        out['minTradingDays'] = out['minTradingDays'][0] if out['minTradingDays'] else None
    return out


def jsonable(status):
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in status.items()}


def _D(value):
    return sizing.D(value)


class Follower:
    def __init__(self, db, feed, notifier, client_factory=None, now=time.time):
        self.db = db
        self.feed = feed
        self.notify = notifier
        self.now = now
        self._client_factory = client_factory or (lambda key: UpscaleClient(key))
        self._clients = {}
        self._markets = {}
        self._locks = {}
        self._last_equity = {}
        self._leverage_ok = {}          # (счёт, рынок) → плечо, которое Upscale принял

    # ── доступ к счёту ─────────────────────────────────────────────────────
    def lock(self, account_id):
        return self._locks.setdefault(account_id, asyncio.Lock())

    def client(self, acc):
        key_id = acc.get('key_id')
        if not key_id:
            return None
        if key_id not in self._clients:
            k = self.db.key(key_id)
            if not k or k.get('status') != 'ok':
                return None
            self._clients[key_id] = self._client_factory(crypto.decrypt(k['enc']))
        return self._clients[key_id]

    def forget_client(self, key_id):
        self._clients.pop(key_id, None)

    async def markets(self, acc, client):
        cached = self._markets.get(acc['id'])
        if cached and self.now() - cached[1] < MARKETS_TTL_S:
            return cached[0]
        found = mk.Markets(await client.markets(acc['upscale_id']))
        self._markets[acc['id']] = (found, self.now())
        return found

    async def account_status(self, acc, client):
        for item in await client.accounts():
            if str(item.get('accountId')) == str(acc['upscale_id']):
                return status_from_account(item)
        raise UpscaleError(404, 'account_not_found', 'счёт не виден ключу (API выключен?)')

    async def _key_failed(self, acc, err):
        """Ключ не работает: счёт выключается, участник и админ узнают."""
        if not err.key_dead:
            return
        self.db.set_key(acc['key_id'], status='dead', last_error=err.code)
        self.forget_client(acc['key_id'])
        self.db.set_account(acc['id'], enabled=0, last_error=err.code)
        self.db.audit('system', 'key_dead', {'account': acc['id'], 'code': err.code})
        await self.notify.user(acc['tg_id'], f'⚠️ Ключ Upscale перестал работать ({err.code}). '
                                             f'Счёт отключён от копирования. Подключите заново: /start')
        await self.notify.admin(f'Ключ участника {acc["tg_id"]} не работает ({err.code}), '
                                f'счёт #{acc["id"]} выключен')

    # ── кто получает сделку ────────────────────────────────────────────────
    def eligible(self, acc, user):
        """Копируются все сделки счёта-мастера — выбора стратегий нет (решение владельца 27.10.2026)."""
        if not user or user.get('status') != 'active' or user.get('paused'):
            return False
        if not acc.get('enabled') or acc.get('paused') or not acc.get('key_id'):
            return False
        level = user.get('level') or 'demo'
        if level == 'observer':
            return False
        return not (level == 'demo' and acc.get('kind') != 'demo')

    # ── события ────────────────────────────────────────────────────────────
    async def handle(self, ev):
        kind = ev.get('kind')
        self.db.master_event(ev)
        handler = getattr(self, f'on_{kind}', None)
        if handler is None:
            return
        try:
            await handler(ev)
        except Exception as exc:                       # noqa: BLE001
            log(f'исполнитель: событие {kind} {ev.get("key")} — {exc!r}')
            self.db.audit('system', 'error', {'event': kind, 'key': ev.get('key'), 'error': repr(exc)})

    async def on_freeze(self, ev):
        await self.notify.admin(f'🧊 Лента счёта-источника заморожена: {ev.get("reason")}. Новых '
                                f'входов нет, позиции участников под стопами Upscale. Снять — '
                                f'админка, «Источник».')

    async def on_stale(self, ev):
        await self.notify.admin(f'⏳ Книга счёта {self.feed.account} не отвечает дольше '
                                f'{config.SOURCE_STALE_S:.0f} с (бот перезапускается?) — новые '
                                f'входы остановлены.')

    async def on_fresh(self, ev):
        await self.notify.admin(f'✅ Книга счёта {self.feed.account} снова отвечает — входы разрешены.')

    async def on_master_status(self, ev):
        if ev.get('status') == 'active':
            await self.notify.admin(f'✅ Счёт-источник {self.feed.account} снова торгует.')
        else:
            await self.notify.admin(f'⛔ Счёт-источник {self.feed.account}: {ev.get("status")} — '
                                    f'новых входов у участников нет, открытые доводятся.')

    async def on_close_missing(self, ev):
        await self.notify.admin(f'❓ Позиция мастера {ev.get("key")} исчезла, а строки о закрытии '
                                f'нет {config.FEED_CLOSE_WAIT_MIN:.0f} мин. Участники не закрыты — '
                                f'проверьте счёт в боте.')

    def _entry_targets(self):
        if self.db.entries_paused() or not self.feed.entries_allowed():
            return []
        out = []
        for acc in self.db.accounts(enabled_only=True):
            user = self.db.user(acc['tg_id'])
            if self.eligible(acc, user):
                out.append((acc, user))
        return out

    async def on_new_order(self, ev):
        kind = 'stop_market' if ev['rec'].get('stop_entry') else 'limit'
        jobs = [self.enter(acc, user, ev, kind=kind) for acc, user in self._entry_targets()]
        if jobs:
            await asyncio.gather(*jobs)

    async def on_opened(self, ev):
        """
        Позиция мастера без заявки: ИИ бота одобрил вход, когда лимит уже за рынком.
        Входим по рынку, только пока цена мастера не за стопом и не за первой целью —
        иначе это уже не та сделка.
        """
        rec = ev['rec']
        price, stop = _D(rec.get('last_price')), _D(rec.get('stop_loss'))
        targets = [_D(t) for t in rec.get('targets') or [] if _D(t)]
        if price is None or stop is None or not targets:
            return
        long_ = rec['direction'] == 'LONG'
        inside = (stop < price < targets[0]) if long_ else (targets[0] < price < stop)
        if not inside:
            log(f'исполнитель: вход по рынку {ev["key"]} пропущен — цена {price} за стопом или целью')
            return
        market_ev = dict(ev, rec=dict(rec, limit_price=float(price)))
        jobs = [self.enter(acc, user, market_ev, kind='market') for acc, user in self._entry_targets()]
        if jobs:
            await asyncio.gather(*jobs)

    async def enter(self, acc, user, ev, kind='limit'):
        rec, key = ev['rec'], ev['key']
        async with self.lock(acc['id']):
            client = self.client(acc)
            if client is None:
                return
            try:
                status = self._recent_status(acc)
                if status is None:
                    status = await self.account_status(acc, client)
                    self.db.set_account(acc['id'], status_json=jsonable(status), status_at=now_iso())
                markets = await self.markets(acc, client)
            except UpscaleError as err:
                self.db.refuse(acc['id'], acc['tg_id'], key, rec.get('strategy'), rec.get('pair'),
                               f'счёт не прочитан: {err.code}')
                await self._key_failed(acc, err)
                return
            market = markets.find(rec['pair'])
            own_open = self.db.open_trades(account_id=acc['id'])
            other_sides = self._other_sides(acc, rec['pair'], market)
            market_id = str((market or {}).get('id'))
            options = mk.leverage_options(market or {}, config.DEFAULT_MAX_LEVERAGE)
            known = self._leverage_ok.get((acc['id'], market_id))
            if known is not None:
                options = [v for v in options if v <= known] or options[-1:]
            trade_id = None
            for i, lev in enumerate(options):
                decision = sizing.plan_entry(
                    order=rec, market=market, status=status,
                    rules={'risk_pct': self.feed.master_risk_pct()},
                    open_trades=own_open, other_sides=other_sides,
                    default_leverage=config.DEFAULT_MAX_LEVERAGE, buffer_pct=config.DD_BUFFER_PCT,
                    leverage=lev)
                if not decision.ok:
                    if trade_id is not None:
                        self.db.update_trade(trade_id, state='error', note=decision.reason[:300])
                    self.db.refuse(acc['id'], acc['tg_id'], key, rec.get('strategy'), rec.get('pair'),
                                   decision.reason)
                    return
                idem = new_idem_key()
                if trade_id is None:
                    trade_id, created = self.db.create_trade(
                        account_id=acc['id'], tg_id=acc['tg_id'], master_key=key,
                        strategy=rec.get('strategy'), pair=rec.get('pair'), market_id=market_id,
                        direction=rec['direction'], state='new', limit_price=decision.limit_price,
                        stop=decision.stop, initial_stop=decision.stop, targets=rec.get('targets'),
                        fractions=rec.get('fractions'), base_size=decision.base_size,
                        leverage=decision.leverage, risk_usd=decision.risk_usd, entry_idem=idem)
                    if not created:
                        return
                else:
                    self.db.update_trade(trade_id, leverage=decision.leverage, entry_idem=idem)
                body = entry_order(acc['upscale_id'], market['id'], rec['direction'],
                                   decision.limit_price, decision.base_size, decision.leverage,
                                   decision.margin, stop=decision.stop, take=decision.take,
                                   kind=kind)
                try:
                    resp = await client.create_order(body, idem) or {}
                except UpscaleError as err:
                    # Предел плеча зависит от вида счёта, которого Upscale не называет:
                    # большее не подошло — пробуем следующее.
                    if err.code == 'invalid_leverage' and i + 1 < len(options):
                        log(f'исполнитель: счёт #{acc["id"]} {rec["pair"]}: плечо {lev:g} не принято, '
                            f'пробую {options[i + 1]:g}')
                        continue
                    self.db.update_trade(trade_id, state='error', note=f'{err.code}: {err.message}'[:300])
                    self.db.refuse(acc['id'], acc['tg_id'], key, rec.get('strategy'), rec.get('pair'),
                                   f'Upscale отклонил заявку: {err.code}')
                    await self._key_failed(acc, err)
                    return
                self._leverage_ok[(acc['id'], market_id)] = lev
                self.db.update_trade(trade_id, state='pending', entry_order=str(resp.get('id') or ''))
                await self.notify.user(acc['tg_id'], self.text_placed(rec, decision, kind))
                return

    def _recent_status(self, acc, max_age_s=STATUS_FRESH_S):
        """Снимок счёта из сверки, если он свежий: вход без лишнего запроса к Upscale."""
        from copier.db import parse_iso
        fresh = self.db.account(acc['id']) or acc
        at = parse_iso(fresh.get('status_at'))
        status = self.db.account_status(fresh)
        if not at or not status.get('status') or self.now() - at.timestamp() > max_age_s:
            return None
        return status

    def _other_sides(self, acc, pair, market):
        """Стороны по этому рынку на других счетах участника: сделки копировщика и живые позиции."""
        sides = set()
        for t in self.db.open_trades(tg_id=acc['tg_id']):
            if t['account_id'] != acc['id'] and mk.base_asset(t['pair']) == mk.base_asset(pair):
                sides.add(t['direction'])
        market_id = str(market.get('id')) if market else None
        for other in self.db.accounts(tg_id=acc['tg_id']):
            if other['id'] == acc['id']:
                continue
            for p in (self.db.account_status(other).get('positions') or []):
                if market_id and str(p.get('market')) == market_id:
                    sides.add(str(p.get('direction')).upper())
        return sides

    async def _for_master(self, key, fn, states=('new', 'pending', 'open')):
        jobs = []
        for t in self.db.trades_for_master(key, states):
            acc = self.db.account(t['account_id'])
            if acc:
                jobs.append(fn(acc, t))
        if jobs:
            await asyncio.gather(*jobs)

    async def on_order_gone(self, ev):
        async def job(acc, t):
            async with self.lock(acc['id']):
                client = self.client(acc)
                if client is None:
                    return
                if t['state'] in ('new', 'pending'):
                    await self._cancel_entry(client, t)
                    await self._sync(acc, client)
                    t = self.db.trade(t['id'])
                if t['state'] == 'open':
                    await self._close(acc, client, t, 'заявка мастера снята без налива')
                elif t['state'] in ('new', 'pending'):
                    self.db.update_trade(t['id'], state='canceled', closed_at=now_iso(),
                                         exit_reason='мастер снял заявку')
        await self._for_master(ev['key'], job)

    async def on_filled(self, ev):
        """
        Мастер налился — сразу сверить участников с этой сделкой: если их лимит на
        Upscale тоже налился, тейки и перенесённый стоп ставятся без ожидания
        очередной сверки (цены Upscale и бота различаются — налив видит только сверка).
        """
        seen = set()
        for t in self.db.trades_for_master(ev['key'], ('new', 'pending')):
            if t['account_id'] in seen:
                continue
            seen.add(t['account_id'])
            acc = self.db.account(t['account_id'])
            if acc:
                await self.sync_account(acc)

    async def on_stop_moved(self, ev):
        new_stop = _D(ev.get('new_stop'))
        if new_stop is None:
            return

        async def job(acc, t):
            old = _D(t['stop'])
            long_ = t['direction'] == 'LONG'
            if old is not None and ((long_ and new_stop <= old) or (not long_ and new_stop >= old)):
                log(f'исполнитель: стоп мастера {ev["key"]} уехал в сторону риска — не переношу')
                return
            async with self.lock(acc['id']):
                client = self.client(acc)
                if client is None:
                    return
                if t['state'] == 'open':
                    ok = await self._move_stop(acc, client, t, new_stop)
                    if ok:
                        await self.notify.user(acc['tg_id'], f'🛡 {t["strategy"]} {t["pair"]}: стоп '
                                               f'перенесён на {_fmt(new_stop)}'
                                               + (' (безубыток)' if ev.get('breakeven') else ''))
                else:
                    # Заявка ещё не налилась: стоп перенесётся при наливе (сверка).
                    self.db.update_trade(t['id'], stop=new_stop)
        await self._for_master(ev['key'], job)

    async def on_reduced(self, ev):
        """
        Остаток мастера уменьшился: тейк (why=target) или «половина» ИИ (why=ai_half).
        Доводим остаток участника до той же доли от его налива; если тейк участника
        на Upscale уже сработал — лишнего нет и ничего не делаем.
        """
        keep = _D(ev.get('remaining'))
        if keep is None or keep < 0:
            return
        what = 'цель взята' if ev.get('why') == 'target' else 'ИИ закрыл половину'

        async def job(acc, t):
            async with self.lock(acc['id']):
                client = self.client(acc)
                if client is None:
                    return
                pos = await self._position(acc, client, t)
                if pos is None:
                    return
                size = from_fp9(pick(pos, ('size', 'baseSize'))) or Decimal(0)
                filled = _D(t['filled_size']) or _D(t['base_size']) or size
                markets = await self.markets(acc, client)
                market = markets.find(t['pair'])
                excess = mk.round_down(size - filled * keep, mk.step(market or {}, 'size'))
                if excess <= 0:
                    return  # тейк участника уже сработал на Upscale
                body = close_order(acc['upscale_id'], t['market_id'], t['direction'],
                                   self._position_id(pos, t), excess)
                try:
                    await client.create_order(body, new_idem_key())
                    await self.notify.user(acc['tg_id'], f'🎯 {t["strategy"]} {t["pair"]}: {what}, '
                                                         f'закрыта часть позиции')
                except UpscaleError as err:
                    log(f'исполнитель: часть {t["pair"]} не закрыта — {err}')
        await self._for_master(ev['key'], job, states=('open',))

    async def on_closed(self, ev):
        trade_row = ev.get('trade') or {}
        code = str(trade_row.get('exit_reason') or '')
        reason = 'мастер закрыл: ' + EXIT_REASONS.get(code, EXIT_REASONS.get(code[:2], code or '—'))

        async def job(acc, t):
            async with self.lock(acc['id']):
                client = self.client(acc)
                if client is None:
                    return
                if t['state'] in ('new', 'pending'):
                    await self._cancel_entry(client, t)
                    await self._sync(acc, client)
                    t = self.db.trade(t['id'])
                    if t['state'] in ('new', 'pending'):
                        self.db.update_trade(t['id'], state='canceled', closed_at=now_iso(),
                                             exit_reason=reason)
                        return
                if t['state'] == 'open':
                    await self._close(acc, client, t, reason)
        await self._for_master(ev['key'], job)

    async def on_ai_note(self, ev):
        """Решение ИИ бота по паре — тем участникам, у кого по ней сделка копировщика."""
        pair = ev.get('pair')
        if not pair:
            return
        text = '🤖 ' + str(ev.get('title') or 'ИИ')
        lines = [str(x) for x in (ev.get('lines') or [])][:3]
        if lines:
            text += '\n' + '\n'.join(lines)
        told = set()
        for t in self.db.open_trades():
            if t['pair'] == pair and t['tg_id'] not in told:
                told.add(t['tg_id'])
                await self.notify.user(t['tg_id'], text[:1000])

    # ── действия на счёте ──────────────────────────────────────────────────
    async def _cancel_entry(self, client, t):
        if not t.get('entry_order'):
            return
        try:
            await client.cancel_order(t['entry_order'])
        except UpscaleError as err:
            if err.code not in ('order_not_found', 'order_not_active'):
                log(f'исполнитель: заявка {t["pair"]} не снята — {err}')

    async def _position(self, acc, client, t, positions=None):
        if positions is None:
            positions = await client.active_positions(acc['upscale_id'])
        for p in positions or []:
            pid = str(pick(p, ('idx', 'id', 'positionId')) or '')
            if t.get('position_id') and pid == str(t['position_id']):
                return p
        for p in positions or []:
            if (str(pick(p, ('market', 'marketId'))) == str(t['market_id'])
                    and str(p.get('direction')).upper() == t['direction']):
                return p
        return None

    @staticmethod
    def _position_id(pos, t):
        return str(pick(pos, ('idx', 'id', 'positionId')) or t.get('position_id') or '')

    async def _close(self, acc, client, t, reason):
        pos = await self._position(acc, client, t)
        if pos is None:
            await self._sync(acc, client)
            return
        size = from_fp9(pick(pos, ('size', 'baseSize')))
        body = close_order(acc['upscale_id'], t['market_id'], t['direction'],
                           self._position_id(pos, t), size)
        try:
            await client.create_order(body, new_idem_key())
        except UpscaleError as err:
            if err.code != 'position_not_available':
                log(f'исполнитель: {t["pair"]} не закрыта — {err}')
                await self.notify.admin(f'❗ Не закрылась позиция участника {acc["tg_id"]} '
                                        f'{t["pair"]}: {err.code}')
                return
        self.db.update_trade(t['id'], exit_reason=reason)
        await self._sync(acc, client)

    async def _move_stop(self, acc, client, t, new_stop):
        orders = await client.active_orders(acc['upscale_id'])
        stop_order = None
        for o in orders or []:
            if str(o.get('type')) not in ('stop', 'stop_market'):
                continue
            if t.get('stop_order') and str(o.get('id')) == str(t['stop_order']):
                stop_order = o
                break
            linked = (_ids(o.get('positionId')) & {str(t.get('position_id'))}) or \
                     (_ids(o.get('parentOrderId')) & {str(t.get('entry_order'))})
            if linked:
                stop_order = o
        if stop_order is None:
            log(f'исполнитель: стоп {t["pair"]} у участника {acc["tg_id"]} не найден')
            await self.notify.admin(f'❗ Не найден стоп участника {acc["tg_id"]} {t["pair"]} — '
                                    f'перенос не сделан')
            return False
        try:
            resp = await client.update_trigger(stop_order['id'], new_stop, new_idem_key()) or {}
        except UpscaleError as err:
            log(f'исполнитель: стоп {t["pair"]} не перенесён — {err}')
            return False
        # Upscale на изменение создаёт НОВЫЙ ордер с новым id, прежний — canceled_by_update
        # (демо 27.10.2026): дальше переносим уже новый.
        self.db.update_trade(t['id'], stop=new_stop, stop_order=str(resp.get('id') or stop_order['id']))
        return True

    async def _place_takes(self, acc, client, t, pos):
        targets = json.loads(t['targets'] or '[]')
        if len(targets) <= 1 or t.get('take_orders'):
            return
        markets = await self.markets(acc, client)
        market = markets.find(t['pair']) or {}
        size = from_fp9(pick(pos, ('size', 'baseSize'))) or _D(t['base_size'])
        parts = sizing.split_targets(size, targets, json.loads(t['fractions'] or '[]'),
                                     mk.step(market, 'size'))
        placed = []
        for price, part in parts:
            idem = new_idem_key()
            body = close_order(acc['upscale_id'], t['market_id'], t['direction'],
                               self._position_id(pos, t), part,
                               trigger=mk.round_price(price, mk.step(market, 'price')))
            try:
                resp = await client.create_order(body, idem) or {}
                placed.append({'id': resp.get('id'), 'price': str(price), 'size': str(part)})
            except UpscaleError as err:
                log(f'исполнитель: тейк {t["pair"]} @ {price} не поставлен — {err}')
                placed.append({'id': None, 'price': str(price), 'size': str(part),
                               'error': err.code})
        self.db.update_trade(t['id'], take_orders=placed)

    # ── сверка ─────────────────────────────────────────────────────────────
    async def sync_account(self, acc):
        async with self.lock(acc['id']):
            client = self.client(acc)
            if client is None:
                return
            await self._sync(acc, client)

    async def _sync(self, acc, client):
        try:
            status = await self.account_status(acc, client)
            orders = await client.active_orders(acc['upscale_id'])
            positions = await client.active_positions(acc['upscale_id'])
        except UpscaleError as err:
            self.db.set_account(acc['id'], last_error=err.code)
            await self._key_failed(acc, err)
            return
        await self._status_changes(acc, status)
        status['positions'] = [{'market': pick(p, ('market', 'marketId')),
                                'direction': str(p.get('direction')).upper(),
                                'size': str(from_fp9(pick(p, ('size', 'baseSize'))))}
                               for p in positions or []]
        self.db.set_account(acc['id'], status_json=jsonable(status), status_at=now_iso(),
                            phase=status.get('phase'), last_error=None)
        self._equity_snapshot(acc, status)

        active_ids = {str(o.get('id')) for o in orders or []}
        for t in self.db.open_trades(account_id=acc['id']):
            if t['state'] in ('new', 'pending'):
                pos = await self._position(acc, client, t, positions)
                if pos is not None:
                    await self._opened(acc, client, t, pos)
                elif t.get('entry_order') and t['entry_order'] not in active_ids and \
                        not self._fresh(t):
                    self.db.update_trade(t['id'], state='canceled', closed_at=now_iso(),
                                         exit_reason=t.get('exit_reason') or 'заявка снята на Upscale')
            elif t['state'] == 'open':
                pos = await self._position(acc, client, t, positions)
                if pos is None:
                    await self._closed(acc, client, t)


    def _fresh(self, t):
        from copier.db import parse_iso
        made = parse_iso(t.get('created_at'))
        return made is not None and self.now() - made.timestamp() < ORDER_GRACE_S

    async def _opened(self, acc, client, t, pos):
        entry = position_entry(pos)
        size = from_fp9(pick(pos, ('size', 'baseSize')))
        self.db.update_trade(t['id'], state='open', position_id=self._position_id(pos, t),
                             entry_price=entry, filled_size=size, opened_at=now_iso())
        t = self.db.trade(t['id'])
        await self.notify.user(acc['tg_id'], f'✅ {t["strategy"]} {t["pair"]} {_side(t)}: заявка '
                                             f'налилась' + (f' по {_fmt(entry)}' if entry else ''))
        await self._place_takes(acc, client, t, pos)
        # Мастер перенёс стоп, пока заявка ждала: догоняем на наливе.
        if _D(t['stop']) != _D(t['initial_stop']):
            await self._move_stop(acc, client, t, _D(t['stop']))

    async def _closed(self, acc, client, t):
        pnl, exit_price = None, None
        if t.get('position_id'):
            try:
                info = await client.position(t['position_id']) or {}
                pnl, exit_price = closed_result(info)
            except UpscaleError as err:
                log(f'исполнитель: итог {t["pair"]} не прочитан — {err}')
        risk = _D(t['risk_usd'])
        r = (pnl / risk) if (pnl is not None and risk) else None
        reason = t.get('exit_reason') or 'стоп или тейк Upscale'
        self.db.update_trade(t['id'], state='closed', closed_at=now_iso(), pnl_usd=pnl,
                             pnl_r=None if r is None else round(r, 3), exit_price=exit_price,
                             exit_reason=reason)
        await self.notify.user(acc['tg_id'], self.text_closed(t, pnl, r, reason))

    async def _status_changes(self, acc, status):
        """Антифрод, блокировки, провал фазы — участнику и админу, один раз на смену."""
        prev = self.db.account_status(acc)
        checks = (('antifraudStatus', 'отметка антифрода Upscale'),
                  ('tradingLockReason', 'блокировка торговли'),
                  ('failureReason', 'счёт провалил фазу'),
                  ('phase', 'фаза счёта'))
        for field, title in checks:
            old, new = prev.get(field), status.get(field)
            if not prev or old == new:
                continue
            if field == 'antifraudStatus' and (new in (None, 'normal')):
                continue
            if field in ('tradingLockReason', 'failureReason') and not new:
                continue
            text = f'Счёт #{acc["id"]}: {title} — {new}'
            await self.notify.user(acc['tg_id'], '⚠️ ' + text)
            await self.notify.admin(f'⚠️ участник {acc["tg_id"]}: {text}')
            self.db.audit('system', 'account_change', {'account': acc['id'], field: new})

    def _equity_snapshot(self, acc, status):
        last = self._last_equity.get(acc['id'], 0)
        if self.now() - last < config.EQUITY_SNAPSHOT_MIN * 60:
            return
        self._last_equity[acc['id']] = self.now()
        eq = status.get('currentEquity')
        if eq is None:
            return
        self.db.add_equity(acc['id'], float(status.get('accountBalance') or 0), float(eq),
                           float(status.get('dayStartEquity') or 0))

    # ── управление ─────────────────────────────────────────────────────────
    async def close_account(self, acc, actor):
        """Закрыть всё на счёте и поставить его на паузу (иначе следующий сетап откроет снова)."""
        async with self.lock(acc['id']):
            client = self.client(acc)
            if client is None:
                return False, 'нет рабочего ключа'
            self.db.set_account(acc['id'], paused=1)
            # Сначала сверка: налившаяся заявка должна стать позицией, иначе после
            # закрытия она выглядела бы снятой без налива.
            await self._sync(acc, client)
            try:
                await client.close_all(acc['upscale_id'], idem_key=new_idem_key())
            except UpscaleError as err:
                return False, err.code
            reason = f'закрыто вручную ({actor})'
            for t in self.db.open_trades(account_id=acc['id']):
                if t['state'] == 'open':
                    self.db.update_trade(t['id'], exit_reason=reason)
                else:
                    self.db.update_trade(t['id'], state='canceled', closed_at=now_iso(),
                                         exit_reason=reason)
            await self._sync(acc, client)
            self.db.audit(actor, 'close_all', {'account': acc['id']})
            return True, ''

    async def close_trade(self, trade_id, actor, reason=None):
        t = self.db.trade(trade_id)
        acc = self.db.account(t['account_id']) if t else None
        if not t or not acc:
            return False, 'сделка не найдена'
        async with self.lock(acc['id']):
            client = self.client(acc)
            if client is None:
                return False, 'нет рабочего ключа'
            if t['state'] in ('new', 'pending'):
                await self._cancel_entry(client, t)
                self.db.update_trade(t['id'], state='canceled', closed_at=now_iso(),
                                     exit_reason=reason or f'снято вручную ({actor})')
            elif t['state'] == 'open':
                await self._close(acc, client, t, reason or f'закрыто вручную ({actor})')
            self.db.audit(actor, 'close_trade', {'trade': trade_id})
            return True, ''

    async def set_stop(self, trade_id, new_stop, actor):
        """Перенос стопа открытой позиции — только в сторону уменьшения риска (проверяет код)."""
        t = self.db.trade(trade_id)
        acc = self.db.account(t['account_id']) if t else None
        if not t or not acc or t['state'] != 'open':
            return False, 'позиции нет'
        new_stop = _D(new_stop)
        old = _D(t['stop'])
        long_ = t['direction'] == 'LONG'
        if old is not None and ((long_ and new_stop <= old) or (not long_ and new_stop >= old)):
            return False, 'стоп отодвигается — нельзя'
        async with self.lock(acc['id']):
            client = self.client(acc)
            if client is None:
                return False, 'нет рабочего ключа'
            ok = await self._move_stop(acc, client, t, new_stop)
        if ok:
            self.db.audit(actor, 'set_stop', {'trade': trade_id, 'stop': str(new_stop)})
        return ok, '' if ok else 'стоп не перенесён'

    # ── тексты ─────────────────────────────────────────────────────────────
    @staticmethod
    def text_placed(rec, d, kind='limit'):
        what = {'market': 'вход по рынку (ИИ одобрил)', 'stop_market': 'стоп-заявка выставлена'}.get(
            kind, 'заявка выставлена')
        lines = [f'📌 {rec.get("strategy")} {rec.get("pair")} {_side(rec)} — {what}',
                 f'вход {_fmt(d.limit_price)} · стоп {_fmt(d.stop)}',
                 'цели ' + ', '.join(_fmt(t) for t in rec.get('targets') or []),
                 f'риск ${d.risk_usd:.2f} · размер {_fmt(d.base_size)} · плечо {d.leverage:g}']
        if rec.get('why'):
            lines.append(f'почему: {rec["why"][:200]}')
        return '\n'.join(lines)

    @staticmethod
    def text_closed(t, pnl, r, reason):
        icon = '🟢' if (pnl or 0) > 0 else ('🔴' if (pnl or 0) < 0 else '⚪️')
        result = (f'{pnl:+.2f} $' if pnl is not None else 'итог уточняется')
        if r is not None:
            result += f' ({r:+.2f}R)'
        return f'{icon} {t["strategy"]} {t["pair"]} {_side(t)} закрыта: {result}\n{reason}'


def _side(rec):
    return 'ЛОНГ' if str(rec.get('direction')).upper() == 'LONG' else 'ШОРТ'


def _fmt(value):
    if value is None:
        return '—'
    v = Decimal(str(value))
    if v == 0:
        return '0'
    return f'{v:.{max(2, 6 - len(str(int(abs(v)))))}f}'.rstrip('0').rstrip('.')
