import config
import telegram_notify as tg
import trade_journal as journal
from logger import log, log_trade
from exchange import get_exchange, reset_exchange
from datetime import datetime
from collections import defaultdict
import traceback
import json
import os
import csv
import time

COOLDOWN_FILE        = os.path.join(os.path.dirname(__file__), 'cooldown_state.json')
POSITIONS_FILE       = os.path.join(os.path.dirname(__file__), 'positions_state.json')
PENDING_ORDERS_FILE  = os.path.join(os.path.dirname(__file__), 'pending_orders.json')

# Доли закрытия на каждом TP уровне (по умолчанию ОДИН тейк -18% = [1.0]).
# TP_KEYS выводятся из числа долей: [1.0] -> 1 тейк, [0.5,0.5] -> 2 тейка (совместимость).
TP_CLOSE_FRACTIONS = getattr(config, 'TP_CLOSE_FRACTIONS', [1.0])
TP_KEYS = ['take_profit_1', 'take_profit_2'][:len(TP_CLOSE_FRACTIONS)]
N_TP    = len(TP_KEYS)


def _fmt_p(p: float) -> str:
    """Adaptive price formatter — handles BTC ($60000) and SHIB ($0.000012) alike."""
    if p >= 1000:   return f"{p:.2f}"
    if p >= 1:      return f"{p:.4f}"
    if p >= 0.01:   return f"{p:.6f}"
    if p >= 0.0001: return f"{p:.8f}"
    return f"{p:.10f}"


class LiveTradeManager:
    def __init__(self, exchange_client=None, telegram_id=None):
        # Мульти-тенант: свой клиент и своё состояние на пользователя.
        # Legacy (telegram_id=None): клиент из .env, файлы состояния в модульной папке.
        self.telegram_id = telegram_id
        self.exchange = exchange_client if exchange_client is not None else get_exchange()

        if telegram_id is not None:
            state_dir = os.path.join(os.path.dirname(__file__), 'state', str(telegram_id))
            os.makedirs(state_dir, exist_ok=True)
            self.cooldown_file = os.path.join(state_dir, 'cooldown_state.json')
            self.positions_file = os.path.join(state_dir, 'positions_state.json')
            self.pending_file  = os.path.join(state_dir, 'pending_orders.json')
        else:
            self.cooldown_file = COOLDOWN_FILE
            self.positions_file = POSITIONS_FILE
            self.pending_file  = PENDING_ORDERS_FILE

        self.active_positions = defaultdict(list)
        self.last_trade_time = self._load_cooldown_state()
        self.trade_history = []
        self.daily_pnl = 0.0
        self.daily_date = datetime.now().date()
        self._ex_snapshot = None        # кэш снимка биржи (позиции+ордера) на цикл
        self._ex_snapshot_ts = 0.0
        self._restore_positions()       # W7: восстанавливаем позиции после перезапуска
        self._restore_pending_orders()  # W9+: восстанавливаем GTC ордера после перезапуска

    def _load_cooldown_state(self):
        """Загружает время последней сделки по парам из файла (сохраняется между перезапусками)."""
        state = defaultdict(lambda: None)
        try:
            if os.path.exists(self.cooldown_file):
                with open(self.cooldown_file, 'r') as f:
                    data = json.load(f)
                for pair, ts in data.items():
                    state[pair] = datetime.fromisoformat(ts)
                log(f"Кулдаун загружен: {len(data)} пар")
        except Exception as e:
            log(f"⚠️ Не удалось загрузить кулдаун: {e}")
        return state

    def _save_cooldown_state(self):
        """Сохраняет время последней сделки на диск."""
        try:
            data = {
                pair: ts.isoformat()
                for pair, ts in self.last_trade_time.items()
                if ts is not None
            }
            with open(self.cooldown_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            log(f"⚠️ Не удалось сохранить кулдаун: {e}")

    # ── W7: Восстановление позиций при перезапуске ───────────────────────────

    def _load_positions_state(self):
        """Загружает сохранённое состояние открытых позиций из JSON."""
        try:
            if os.path.exists(self.positions_file):
                with open(self.positions_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            log(f"⚠️ Не удалось загрузить positions_state.json: {e}")
        return {}

    def _save_position_state(self, trading_pair, position, params):
        """Сохраняет ключевые данные позиции для восстановления после краша."""
        try:
            state = self._load_positions_state()
            state[trading_pair] = {
                'pair':          trading_pair,
                'direction':     position['signal']['setup']['type'],
                'zone':          position['signal']['trigger'].get('zone', '—'),
                'htf_trend':     position['signal'].get('htf_trend', '—'),
                'entry_time':    position['entry_time'].isoformat(),
                'entry_price':   position['entry_price'],
                'stop_loss':     params['stop_loss'],
                'take_profit_1': params['take_profit_1'],
                'take_profit_2': params['take_profit_2'],
                'be_level':      params.get('be_level'),
                'position_size': params['position_size'],
                'risk_amount':   params['risk_amount'],
                'rr':            params.get('rr', 0),
                'sl_distance':   params.get('sl_distance', 0),
                'trail_distance': position.get('trail_distance', 0),
                'leverage':      params.get('leverage', config.LEVERAGE),
            }
            with open(self.positions_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log(f"⚠️ Не удалось сохранить состояние позиции {trading_pair}: {e}")

    def _remove_position_state(self, trading_pair):
        """Удаляет запись о закрытой позиции из файла состояния."""
        try:
            state = self._load_positions_state()
            if trading_pair in state:
                del state[trading_pair]
                with open(self.positions_file, 'w') as f:
                    json.dump(state, f, indent=2)
        except Exception as e:
            log(f"⚠️ Не удалось обновить positions_state.json: {e}")

    def _rebuild_position(self, exch_pos, saved):
        """Воссоздаёт position dict из данных биржи + сохранённого состояния."""
        pair        = exch_pos.get('symbol', saved.get('pair', '?'))
        curr_size   = abs(float(exch_pos.get('contracts', 0) or 0))
        orig_size   = float(saved['position_size'])
        entry_price = float(saved['entry_price'])
        direction   = saved['direction']
        is_long     = direction == 'LONG'

        # Вычисляем количество сработавших TP по оставшемуся размеру (W11: 2 тейка по 50%)
        if orig_size > 0 and curr_size <= orig_size:
            fraction_remaining = curr_size / orig_size
            tp_hit = max(0, min(N_TP, round((1.0 - fraction_remaining) / 0.5)))
        else:
            tp_hit = 0

        params = {
            'entry':         entry_price,
            'stop_loss':     float(saved['stop_loss']),
            'take_profit_1': float(saved['take_profit_1']),
            'take_profit_2': float(saved['take_profit_2']),
            'be_level':      float(saved['be_level']) if saved.get('be_level') is not None else None,
            'position_size': orig_size,
            'risk_amount':   float(saved.get('risk_amount', 0)),
            'rr':            float(saved.get('rr', 0)),
            'sl_distance':   float(saved.get('sl_distance', 0)),
            'leverage':      saved.get('leverage', config.LEVERAGE),
        }

        trail_distance  = float(saved.get('trail_distance', abs(entry_price - params['stop_loss'])))
        breakeven_set   = tp_hit >= 1
        trailing_active = tp_hit >= config.TRAIL_AFTER_TP

        trail_stop = None
        if trailing_active:
            try:
                mark = float(exch_pos.get('markPrice') or
                             exch_pos.get('info', {}).get('markPrice') or entry_price)
            except Exception:
                mark = entry_price
            trail_stop = (mark - trail_distance) if is_long else (mark + trail_distance)

        try:
            entry_dt = datetime.fromisoformat(saved['entry_time'])
        except Exception:
            entry_dt = datetime.now()

        signal = {
            'trading_pair': pair,
            'setup': {
                'type':        direction,
                'start_price': entry_price,
                'end_price':   entry_price,
                'size':        abs(float(saved.get('take_profit_2', entry_price)) - entry_price),
            },
            'trigger': {
                'zone':         saved.get('zone', '—'),
                'bos_level':    None,
                'volume_ratio': 0,
            },
            'params':     params,
            'htf_trend':  saved.get('htf_trend', '—'),
            'zone_a':     None,
            'zone_b':     None,
        }

        return {
            'order':           {'id': 'RECOVERED', 'symbol': pair},
            'signal':          signal,
            'params':          params,
            'entry_time':      entry_dt,
            'entry_price':     entry_price,
            'status':          'OPEN',
            'remaining_size':  curr_size,
            'realized_pnl':    0.0,
            'tp_hit':          tp_hit,
            'sl_order_id':     None,
            'tp_order_ids':    [],
            'breakeven_set':   breakeven_set,
            'trailing_active': trailing_active,
            'trail_stop':      trail_stop,
            'trail_distance':  trail_distance,
            '_recovered':      True,
        }

    def _restore_positions(self):
        """W7: загружает открытые позиции из positions_state.json и верифицирует с биржей."""
        saved_state = self._load_positions_state()
        if not saved_state:
            log("W7: нет сохранённых позиций для восстановления")
            return

        log(f"W7: найдено {len(saved_state)} сохранённых позиций, проверяем на бирже...")
        try:
            exch_positions = self.exchange.fetch_positions()
            exch_map = {}
            for ep in exch_positions:
                sym       = ep.get('symbol', '')
                contracts = abs(float(ep.get('contracts', 0) or 0))
                if contracts > 0:
                    exch_map[sym] = ep
        except Exception as e:
            log(f"⚠️ W7: не удалось загрузить позиции с биржи — {e}")
            tg.error_alert(f"W7 ошибка восстановления: {e}", telegram_id=self.telegram_id)
            return

        recovered = []
        for pair, saved in list(saved_state.items()):
            exch_pos = exch_map.get(pair)
            if exch_pos is None:
                log(f"W7: {pair} — позиция закрылась пока бот был выключен, удаляем запись")
                self._remove_position_state(pair)
                continue
            try:
                position = self._rebuild_position(exch_pos, saved)
                self.active_positions[pair].append(position)
                tp_h   = position['tp_hit']
                be_str = "✓" if position['breakeven_set'] else "✗"
                tr_str = "✓" if position['trailing_active'] else "✗"
                log(f"W7: ✅ {pair} {saved['direction']} восстановлена | "
                    f"TP hit: {tp_h} | BE: {be_str} | Trail: {tr_str}")
                recovered.append({
                    'pair':           pair,
                    'direction':      saved['direction'],
                    'tp_hit':         tp_h,
                    'breakeven_set':  position['breakeven_set'],
                    'trailing_active': position['trailing_active'],
                })
            except Exception as e:
                log(f"W7: ❌ ошибка восстановления {pair}: {e}")

        if recovered:
            tg.positions_restored(recovered, telegram_id=self.telegram_id)
            log(f"W7: восстановлено {len(recovered)} позиций")
        else:
            log("W7: ни одну позицию не удалось восстановить")

    # ─────────────────────────────────────────────────────────────────────────

    def test_connection(self):
        from exchange import test_connection
        return test_connection()

    def _reset_daily_pnl_if_needed(self):
        today = datetime.now().date()
        if today != self.daily_date:
            self.daily_pnl = 0.0
            self.daily_date = today

    def check_cooldown(self, trading_pair):
        if self.last_trade_time[trading_pair] is None:
            return True
        hours_since_last = (datetime.now() - self.last_trade_time[trading_pair]).total_seconds() / 3600
        return hours_since_last >= config.COOLDOWN_HOURS

    # ── Снимок реального состояния на бирже (источник истины) ────────────────
    def sync_exchange_state(self, max_age: float = 15.0):
        """Тянет с биржи реальные позиции (+ открытые ордера), кэширует на ~цикл.

        Возвращает {'positions': {норм_символ: pos}, 'pos_ok': bool, 'order_pairs': set}.
        pos_ok=False — биржа недоступна (тогда вызывающий код не должен считать «всё закрыто»)."""
        now = time.time()
        if self._ex_snapshot is not None and (now - self._ex_snapshot_ts) < max_age:
            return self._ex_snapshot

        pos_map, pos_ok = {}, True
        try:
            for p in self.exchange.fetch_positions():
                sz = abs(float(p.get('contracts', 0) or 0))
                if sz > 0:
                    pos_map[self._norm_symbol(p.get('symbol', ''))] = p
        except Exception as e:
            pos_ok = False
            log(f"⚠️ sync: не удалось получить позиции — {e}")

        order_pairs = set()
        try:   # best-effort: наши лимиты дополнительно покрыты pending-файлом
            for o in self.exchange.fetch_open_orders(params={'settleCoin': 'USDT'}):
                order_pairs.add(self._norm_symbol(o.get('symbol', '')))
        except Exception:
            pass

        self._ex_snapshot = {'positions': pos_map, 'pos_ok': pos_ok, 'order_pairs': order_pairs}
        self._ex_snapshot_ts = now
        return self._ex_snapshot

    def get_active_count(self):
        """Количество занятых слотов — по РЕАЛЬНОМУ состоянию биржи + наши pending-лимиты."""
        return len(self.get_open_pairs())

    def get_open_pairs(self):
        """Пары с реальной открытой позицией/ордером на бирже + наши pending-лимиты.
        Биржа — источник истины (чинит рассинхрон: дубли, «бот не видит позицию»)."""
        snap = self.sync_exchange_state()
        pairs = set()
        if snap['pos_ok']:
            pairs |= set(snap['positions'].keys())
        else:
            # биржа недоступна — падаем на in-memory, чтобы не открыть дубль
            pairs |= {self._norm_symbol(pair) for pair, ps in self.active_positions.items()
                      if any(p['status'] == 'OPEN' for p in ps)}
        pairs |= snap['order_pairs']
        pairs |= {self._norm_symbol(p) for p in self._load_pending_orders().keys()}
        return pairs

    def has_position_or_order(self, trading_pair) -> bool:
        """Есть ли на бирже позиция/ордер ИЛИ наш pending-лимит по паре (защита от дублей)."""
        return self._norm_symbol(trading_pair) in self.get_open_pairs()

    def get_direction_counts(self):
        """{'LONG': n, 'SHORT': n} — реальные позиции биржи + наши pending-лимиты.
        Биржа недоступна (pos_ok=False) — считаем по in-memory позициям (та же
        деградация, что в get_open_pairs: кэп — мягкий риск-шейпер, не инвариант)."""
        snap = self.sync_exchange_state()
        counts, seen = {'LONG': 0, 'SHORT': 0}, set()
        if snap['pos_ok']:
            for sym, p in snap['positions'].items():
                side = str(p.get('side', '')).lower()
                if side in ('long', 'short'):
                    counts[side.upper()] += 1
                    seen.add(sym)
        else:
            log("⚠️ dir-cap: биржа недоступна — направления по in-memory состоянию")
            for pair, ps in self.active_positions.items():
                open_ps = [p for p in ps if p['status'] == 'OPEN']
                if open_ps:
                    counts[open_ps[-1]['signal']['setup']['type']] += 1
                    seen.add(self._norm_symbol(pair))
        for pair, po in self._load_pending_orders().items():
            sym = self._norm_symbol(pair)
            if sym not in seen:   # дедуп: pending мог уже стать позицией
                counts['LONG' if po.get('side') == 'buy' else 'SHORT'] += 1
        return counts

    def get_real_balance(self):
        try:
            balance = self.exchange.fetch_balance()
            return balance.get('USDT', {}).get('total', 0) or 0
        except Exception:
            return 0

    def get_trading_balance(self):
        """База для расчёта размера позиции. Если юзер задал свой депозит (deposit_usd>0) —
        используем ЕГО «как есть» (по решению пользователя), иначе реальный баланс счёта."""
        if self.telegram_id is not None:
            try:
                import db
                user = db.get_user(self.telegram_id)
                dep = (user or {}).get('deposit_usd')
                if dep and float(dep) > 0:
                    return float(dep)
            except Exception:
                pass
        return self.get_real_balance()

    # ── W9: Рыночный вход (fallback) ─────────────────────────────────────────

    def _place_market_entry(self, trading_pair, side, position_size):
        """Открывает позицию рыночным ордером."""
        order = self.exchange.create_order(
            symbol=trading_pair,
            type='market',
            side=side,
            amount=position_size,
            params={'reduce_only': False},
        )
        log(f"📍 MARKET-ордер отправлен: {side.upper()} {position_size} {trading_pair}")
        return order

    # ── W9+: Система отложенных GTC лимитных ордеров (Вариант D) ─────────────

    @staticmethod
    def _serialize_for_json(obj):
        """Рекурсивно конвертирует объект в JSON-сериализуемую форму."""
        import pandas as pd
        if isinstance(obj, dict):
            return {k: LiveTradeManager._serialize_for_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [LiveTradeManager._serialize_for_json(i) for i in obj]
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, 'item'):          # numpy scalar (np.float64, np.int64 и т.д.)
            return obj.item()
        if isinstance(obj, float) and (obj != obj):   # NaN
            return None
        return obj

    def _load_pending_orders(self) -> dict:
        try:
            if os.path.exists(self.pending_file):
                with open(self.pending_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            log(f"⚠️ Не удалось загрузить pending_orders.json: {e}")
        return {}

    def _save_pending_order(self, pair, order_id, side, position_size,
                             limit_price, params, signal, invalidation_price):
        """Сохраняет GTC ордер для мониторинга в последующих циклах."""
        from datetime import timedelta
        try:
            pending = self._load_pending_orders()
            max_valid = datetime.now() + timedelta(hours=config.PENDING_ORDER_MAX_HOURS)
            pending[pair] = {
                'order_id':           order_id,
                'pair':               pair,
                'side':               side,
                'position_size':      position_size,
                'limit_price':        limit_price,
                'placed_at':          datetime.now().isoformat(),
                'max_valid_until':    max_valid.isoformat(),
                'params':             self._serialize_for_json(params),
                'signal':             self._serialize_for_json(signal),
                'invalidation_price': invalidation_price,
            }
            with open(self.pending_file, 'w') as f:
                json.dump(pending, f, indent=2)
            log(f"💾 Pending ордер {pair} сохранён (до {max_valid.strftime('%H:%M')})")
        except Exception as e:
            log(f"⚠️ Не удалось сохранить pending ордер {pair}: {e}")

    def _remove_pending_order(self, pair):
        try:
            pending = self._load_pending_orders()
            if pair in pending:
                del pending[pair]
                with open(self.pending_file, 'w') as f:
                    json.dump(pending, f, indent=2)
        except Exception as e:
            log(f"⚠️ Не удалось удалить pending ордер {pair}: {e}")

    def _cancel_pending_order(self, pair, order_id):
        """Отменяет ордер на бирже и удаляет из файла."""
        try:
            self.exchange.cancel_order(order_id, pair)
        except Exception:
            pass
        self._remove_pending_order(pair)
        log(f"🚫 Pending ордер {pair} отменён")

    def _restore_pending_orders(self):
        """При перезапуске: верифицируем GTC ордера с биржей, регистрируем заполненные."""
        pending = self._load_pending_orders()
        if not pending:
            return
        log(f"⏳ Найдено {len(pending)} pending ордеров при старте — верифицируем...")
        now = datetime.now()
        for pair, po in list(pending.items()):
            try:
                max_valid = datetime.fromisoformat(po['max_valid_until'])
                if now > max_valid:
                    log(f"   {pair}: pending истёк — отменяем")
                    self._cancel_pending_order(pair, po['order_id'])
                    continue
                status = self.exchange.fetch_order(po['order_id'], pair)
                s = status.get('status', '')
                if s == 'closed':
                    log(f"   {pair}: ордер исполнен пока бот был выключен — регистрируем")
                    self._register_filled_order(pair, po, status)
                elif s == 'canceled':
                    log(f"   {pair}: ордер отменён биржей — удаляем")
                    self._remove_pending_order(pair)
                else:
                    waited = int((now - datetime.fromisoformat(po['placed_at'])).total_seconds() / 60)
                    log(f"   {pair}: ордер @ ${_fmt_p(po['limit_price'])} ожидает {waited} мин")
            except Exception as e:
                log(f"   {pair}: ошибка проверки pending ордера при старте — {e}")

    def _register_filled_order(self, pair, pending_order, order_status):
        """
        Регистрирует заполненный GTC лимит как полноценную открытую позицию.
        Эквивалент нижней части execute_trade() для market-ордеров.
        """
        try:
            params  = dict(pending_order['params'])
            signal  = dict(pending_order['signal'])
            signal['trading_pair'] = pair   # гарантируем корректность пары
            side    = pending_order['side']
            is_long = (side == 'buy')

            actual_entry  = order_status.get('average') or order_status.get('price') or params['entry']
            actual_filled = order_status.get('filled') or params['position_size']

            params['entry']         = actual_entry
            params['position_size'] = actual_filled

            actual_sl_dist = abs(actual_entry - params['stop_loss'])
            min_dist       = actual_entry * config.MIN_SL_PERCENT

            if is_long and params['stop_loss'] >= actual_entry:
                params['stop_loss'] = actual_entry - min_dist
                actual_sl_dist = min_dist
                log(f"⚠️ SL скорректирован (LONG): ${params['stop_loss']:.6f}")
            elif not is_long and params['stop_loss'] <= actual_entry:
                params['stop_loss'] = actual_entry + min_dist
                actual_sl_dist = min_dist
                log(f"⚠️ SL скорректирован (SHORT): ${params['stop_loss']:.6f}")
            params['sl_distance'] = actual_sl_dist

            sl_order, tp_orders = self._place_sl_tp_orders(pair, side, actual_filled, params)

            position = {
                'order':           order_status,
                'signal':          signal,
                'params':          params,
                'entry_time':      datetime.now(),
                'entry_price':     actual_entry,
                'status':          'OPEN',
                'remaining_size':  actual_filled,
                'realized_pnl':    0.0,
                'tp_hit':          0,
                'sl_order_id':     sl_order.get('id') if sl_order else None,
                'tp_order_ids':    [o.get('id') if o else None for o in tp_orders],
                'breakeven_set':   False,
                'trailing_active': False,
                'trail_stop':      None,
                'trail_distance':  actual_sl_dist * config.TRAIL_DISTANCE_K,
            }

            balance_before = self.get_real_balance()
            journal.open_trade(position, {**signal, 'params': params}, balance_before)

            self.active_positions[pair].append(position)
            self._save_position_state(pair, position, params)  # W7

            self.last_trade_time[pair] = datetime.now()
            self._save_cooldown_state()

            # Удаляем из pending ДО Telegram (чтобы избежать двойной обработки)
            self._remove_pending_order(pair)

            waited_min = int(
                (datetime.now() - datetime.fromisoformat(pending_order['placed_at'])).total_seconds() / 60
            )
            signal_for_tg = dict(signal)
            signal_for_tg['params'] = params
            # График сетапа уже был отправлен при постановке лимита — здесь краткое
            # подтверждение заполнения без второго графика.
            tg.trade_opened(signal_for_tg, df_1h=None, telegram_id=self.telegram_id)

            log(f"✅ {pair}: GTC лимит исполнен @ ${_fmt_p(actual_entry)} "
                f"(ожидание {waited_min} мин) | Баланс: ${self.get_real_balance():.2f}")

        except Exception as e:
            log(f"❌ Ошибка регистрации заполненного ордера {pair}: {e}")
            log(f"📋 Детали:\n{traceback.format_exc()}")

    def check_pending_orders(self):
        """
        Вызывается в каждом цикле: проверяет статус всех GTC ордеров.
        Регистрирует заполненные, отменяет истёкшие или инвалидированные.
        """
        pending = self._load_pending_orders()
        if not pending:
            return
        log(f"\n⏳ Pending ордеров: {len(pending)} — проверяем...")
        now = datetime.now()

        for pair, po in list(pending.items()):
            order_id = po['order_id']
            try:
                # 1. Проверяем срок действия
                max_valid = datetime.fromisoformat(po['max_valid_until'])
                if now > max_valid:
                    log(f"   {pair}: pending истёк (>{config.PENDING_ORDER_MAX_HOURS:.0f}ч) — отменяем")
                    self._cancel_pending_order(pair, order_id)
                    continue

                # 2. Проверяем цену и два условия отмены сетапа
                try:
                    current_price = self.exchange.fetch_ticker(pair)['last']
                    inv   = po.get('invalidation_price', 0)
                    side  = po['side']
                    tp1   = po.get('params', {}).get('take_profit_1', 0)

                    if side == 'buy':
                        # Цена упала ниже 88.6% — сетап полностью инвалидирован
                        if inv and current_price < inv:
                            log(f"   {pair}: цена ${_fmt_p(current_price)} < инвалидации "
                                f"${_fmt_p(inv)} — сетап разрушен, отменяем")
                            self._cancel_pending_order(pair, order_id)
                            continue
                        # Цена уже прошла TP1 без нашего заполнения — поезд ушёл
                        if tp1 and current_price >= tp1:
                            log(f"   {pair}: цена ${_fmt_p(current_price)} достигла TP1 "
                                f"${_fmt_p(tp1)} без заполнения — отменяем")
                            self._cancel_pending_order(pair, order_id)
                            continue
                    else:  # sell
                        if inv and current_price > inv:
                            log(f"   {pair}: цена ${_fmt_p(current_price)} > инвалидации "
                                f"${_fmt_p(inv)} — сетап разрушен, отменяем")
                            self._cancel_pending_order(pair, order_id)
                            continue
                        if tp1 and current_price <= tp1:
                            log(f"   {pair}: цена ${_fmt_p(current_price)} достигла TP1 "
                                f"${_fmt_p(tp1)} без заполнения — отменяем")
                            self._cancel_pending_order(pair, order_id)
                            continue
                except Exception as e:
                    log(f"   {pair}: ошибка получения цены — {e}")

                # 3. Проверяем статус ордера на бирже
                status = self.exchange.fetch_order(order_id, pair)
                s = status.get('status', '')
                if s == 'closed':
                    log(f"   {pair}: ✅ GTC ордер ИСПОЛНЕН!")
                    self._register_filled_order(pair, po, status)
                elif s == 'canceled':
                    log(f"   {pair}: ордер отменён биржей — удаляем")
                    self._remove_pending_order(pair)
                else:
                    waited = int((now - datetime.fromisoformat(po['placed_at'])).total_seconds() / 60)
                    log(f"   {pair}: лимит @ ${_fmt_p(po['limit_price'])} ожидает {waited} мин")

            except Exception as e:
                log(f"   {pair}: ошибка проверки pending ордера — {e}")

    # ─────────────────────────────────────────────────────────────────────────

    def _place_sl_tp_orders(self, trading_pair, side, position_size, params):
        """
        SL — через position trading-stop (Bybit V5, уровень позиции).
        TP при одном тейке (-18%, 100%) — тоже на уровне позиции (закрывает всё).
        При нескольких TP (legacy) — limit reduce_only по долям для частичных фиксаций.
        Во всех случаях софт-монитор (_check_tp_levels) — первичный механизм закрытия,
        биржевой SL/TP — резервный (если бот не успел отработать цикл).
        """
        is_long     = (side == 'buy')
        sl_price    = params['stop_loss']
        close_side  = 'sell' if is_long else 'buy'

        sl_order  = None
        tp_orders = []   # список из 4 TP ордеров

        # ── SL через position trading-stop ───────────────────────────────────
        try:
            self.exchange.privatePostV5PositionTradingStop({
                'category':    'linear',
                'symbol':      trading_pair,
                'stopLoss':    str(sl_price),
                'slTriggerBy': 'LastPrice',
                'positionIdx': 0,
            })
            sl_order = {'id': 'position_sl'}
            log(f"SL выставлен @ ${sl_price:.4f}")
        except Exception as e:
            err = str(e)
            if '34040' in err or 'not modified' in err:
                sl_order = {'id': 'position_sl'}
                log(f"SL уже выставлен @ ${sl_price:.4f}")
            else:
                log(f"⚠️ SL не выставлен (программный SL активен): {e}")

        # ── TP ───────────────────────────────────────────────────────────────
        if N_TP == 1:
            # Один тейк -18% закрывает 100% — ведём на уровне позиции (Bybit V5).
            # Идемпотентно с TP, прикреплённым к ордеру входа: повторная установка
            # того же уровня => "not modified". Без дублирующего limit reduce_only.
            tp_price = params[TP_KEYS[0]]
            try:
                self.exchange.privatePostV5PositionTradingStop({
                    'category':    'linear',
                    'symbol':      trading_pair,
                    'takeProfit':  str(round(tp_price, 8)),
                    'tpTriggerBy': 'LastPrice',
                    'positionIdx': 0,
                })
                tp_orders.append({'id': 'position_tp'})
                log(f"TP выставлен @ ${tp_price:.4f} (закрывает 100%)")
            except Exception as e:
                err = str(e)
                if '34040' in err or 'not modified' in err:
                    tp_orders.append({'id': 'position_tp'})
                    log(f"TP уже выставлен @ ${tp_price:.4f}")
                else:
                    tp_orders.append(None)
                    log(f"⚠️ TP не выставлен (программный TP активен): {e}")
        else:
            # Legacy (W11): несколько TP — limit reduce_only по долям
            for i, key in enumerate(TP_KEYS):
                tp_price = params[key]
                tp_size  = round(position_size * TP_CLOSE_FRACTIONS[i], 6)
                try:
                    order = self.exchange.create_order(
                        symbol=trading_pair,
                        type='limit',
                        side=close_side,
                        amount=tp_size,
                        price=round(tp_price, 8),
                        params={'reduce_only': True, 'category': 'linear'},
                    )
                    tp_orders.append(order)
                    log(f"TP{i+1} limit @ ${tp_price:.4f} | размер: {tp_size:.6f}")
                except Exception as e:
                    tp_orders.append(None)
                    log(f"⚠️ TP{i+1} ордер не выставлен: {e}")

        return sl_order, tp_orders

    def execute_trade(self, signal, df_1h=None):
        trading_pair = signal['trading_pair']

        if not self.check_cooldown(trading_pair):
            hours_left = config.COOLDOWN_HOURS - (
                datetime.now() - self.last_trade_time[trading_pair]
            ).total_seconds() / 3600
            log(f"⏳ Кулдаун для {trading_pair}. Осталось {hours_left:.1f} ч")
            return False

        # Защита от дублей: на бирже уже есть позиция/ордер по паре ИЛИ наш pending-лимит
        if self.has_position_or_order(trading_pair):
            log(f"⏳ {trading_pair}: позиция/ордер уже есть на бирже — пропускаем (без дубля)")
            return False

        # Направленный кэп: не более MAX_SAME_DIRECTION позиций/ордеров в одну сторону
        # (снимок биржи уже прогрет проверкой выше — доп. API-запросов нет)
        if getattr(config, 'MAX_SAME_DIRECTION', 0) > 0:
            sig_dir = signal['setup']['type']            # 'LONG' / 'SHORT'
            dcnt = self.get_direction_counts()
            if dcnt.get(sig_dir, 0) >= config.MAX_SAME_DIRECTION:
                log(f"⛔ {trading_pair}: направление {sig_dir} занято "
                    f"{dcnt[sig_dir]}/{config.MAX_SAME_DIRECTION} — направленный кэп")
                return False

        self._reset_daily_pnl_if_needed()

        setup   = signal['setup']
        trigger = signal['trigger']

        # ── Расчёт размера: РИСК считаем от торгового депозита (свой депозит юзера или
        #    реальный баланс), а контроль маржи — по РЕАЛЬНОМУ балансу счёта. ──
        sizing_balance = self.get_trading_balance()   # база для риска (может быть задана юзером)
        real_balance   = self.get_real_balance()      # фактические средства на счёте
        if sizing_balance <= 0:
            log(f"❌ Депозит/баланс недоступен (${sizing_balance:.2f})")
            return False

        sig_sl_dist = abs(signal['params']['entry'] - signal['params']['stop_loss'])
        if sig_sl_dist <= 0:
            log(f"❌ SL дистанция = 0 — пропускаем")
            return False

        risk_amount   = sizing_balance * (config.RISK_PER_TRADE / 100)
        position_size = risk_amount / sig_sl_dist

        # W3: cap на максимальное количество юнитов (страховка от микроценовых пар)
        if position_size > config.MAX_POSITION_SIZE_UNITS:
            log(f"⚠️ Position size {position_size:.0f} > max {config.MAX_POSITION_SIZE_UNITS:.0f} — cap applied")
            position_size = config.MAX_POSITION_SIZE_UNITS

        if real_balance < risk_amount * 2:
            log(f"❌ Недостаточно средств! Баланс: ${real_balance:.2f}, риск: ${risk_amount:.2f}")
            return False

        # Копируем params и обновляем размер/риск под текущий баланс
        params = dict(signal['params'])
        params['risk_amount']   = risk_amount
        params['position_size'] = position_size
        params['leverage']      = config.LEVERAGE

        try:
            mode_text = "🔴 LIVE (РЕАЛЬНЫЕ ДЕНЬГИ)" if config.TRADING_MODE == 'LIVE' else "🟢 DEMO"
            log(f"\n{'='*60}")
            log(f" {mode_text}")
            log(f" ОТКРЫТИЕ СДЕЛКИ: {trading_pair}")
            log(f"{'='*60}")
            htf = signal.get('htf_trend', 'N/A')
            log(f"Тип: {setup['type']}  |  Зона: {trigger['zone']}  |  HTF: {htf}")
            log(f"Вход: ${_fmt_p(params['entry'])}  |  Стоп: ${_fmt_p(params['stop_loss'])}")
            if N_TP == 1:
                log(f"TP: ${_fmt_p(params['take_profit_1'])} (-18%, закрывает 100%)")
            else:
                log(f"TP1: ${_fmt_p(params['take_profit_1'])} (50%)  |  TP2: ${_fmt_p(params['take_profit_2'])} (50%)")
            if params.get('be_level'):
                log(f"Безубыток при пробое B: ${_fmt_p(params['be_level'])}")
            pos_value_usd = position_size * params['entry']
            log(f"Позиция: {position_size:.6f} (~${pos_value_usd:.2f})  |  Риск: ${risk_amount:.2f} ({config.RISK_PER_TRADE}%)  |  RR: 1:{params['rr']:.2f}")

            # Sanity check: position USD value must not exceed REAL balance × leverage
            max_pos_usd = real_balance * config.LEVERAGE
            if pos_value_usd > max_pos_usd:
                log(f"❌ Позиция ${pos_value_usd:.2f} > баланс×плечо ${max_pos_usd:.2f} — пропускаем {trading_pair}")
                return False

            side = 'buy' if setup['type'] == 'LONG' else 'sell'

            # Устанавливаем плечо явно перед входом
            try:
                self.exchange.set_leverage(config.LEVERAGE, trading_pair, params={'category': 'linear'})
                log(f"Плечо: {config.LEVERAGE}x")
            except Exception as e:
                err_s = str(e)
                log(f"⚠️ Не удалось установить плечо: {e}")
                if 'only support linear' in err_s or 'not support' in err_s.lower():
                    log(f"❌ {trading_pair} недоступен как фьючерс на этой бирже — пропускаем")
                    return False

            # W9+/W11: GTC лимитный ордер на границе зоны A (вход = params['entry'])
            if config.USE_LIMIT_ENTRY:
                offset = config.LIMIT_ENTRY_OFFSET_PCT

                # Вход уже равен 38.2%-границе зоны A. Небольшой offset гарантирует касание.
                if side == 'buy':
                    limit_price = round(params['entry'] * (1 + offset), 8)
                else:
                    limit_price = round(params['entry'] * (1 - offset), 8)

                ep  = setup.get('end_price', params['entry'])
                sz  = setup.get('size', 0)
                inv = (ep - sz * config.ZONE_B_TOP if side == 'buy'
                       else ep + sz * config.ZONE_B_TOP)
                # Bybit V5: прикрепляем стоп-лосс И тейк-профит прямо к ордеру входа —
                # позиция защищена и ведётся биржей в МОМЕНТ заполнения лимита, даже если бот
                # не успеет отработать цикл регистрации. Один TP -18% закрывает 100%.
                limit_params = {'reduce_only': False, 'timeInForce': 'GTC'}
                if getattr(self.exchange, 'id', '') == 'bybit':
                    limit_params['stopLoss']    = str(round(params['stop_loss'], 8))
                    limit_params['slTriggerBy'] = 'LastPrice'
                    if N_TP == 1:   # одиночный TP — биржа закроет всю позицию по нему
                        limit_params['takeProfit']  = str(round(params['take_profit_1'], 8))
                        limit_params['tpTriggerBy']  = 'LastPrice'
                try:
                    lim_order = self.exchange.create_order(
                        symbol=trading_pair, type='limit', side=side,
                        amount=position_size, price=limit_price,
                        params=limit_params,
                    )
                    self._save_pending_order(
                        trading_pair, lim_order.get('id'), side, position_size,
                        limit_price, params, signal, inv,
                    )
                    # Защита от дублей: ставим кулдаун уже при ВЫСТАВЛЕНИИ лимита,
                    # а не только при заполнении — даже если pending-файл не сохранится,
                    # повторного входа по этой паре в этом окне не будет.
                    self.last_trade_time[trading_pair] = datetime.now()
                    self._save_cooldown_state()
                    log(f"⏳ GTC LIMIT @ ${_fmt_p(limit_price)} | ID: {lim_order.get('id')} | "
                        f"Инвалидация @ ${_fmt_p(inv)}")
                    tg.limit_order_placed(trading_pair, side, limit_price,
                                          params['stop_loss'], config.PENDING_ORDER_MAX_HOURS,
                                          telegram_id=self.telegram_id,
                                          signal=signal, df_1h=df_1h)
                    return True
                except Exception as e:
                    log(f"⚠️ GTC LIMIT не удался: {e} — fallback на MARKET")

            order = self._place_market_entry(trading_pair, side, position_size)

            log(f"✅ ОРДЕР ИСПОЛНЕН! ID: {order.get('id', 'N/A')} | Статус: {order.get('status', 'unknown')}")

            # Обновляем params по фактическому заполнению (actual entry + filled size)
            actual_entry  = order.get('average') or order.get('price') or params['entry']
            actual_filled = order.get('filled') or position_size
            params['entry']         = actual_entry
            params['position_size'] = actual_filled

            # Фактическая SL-дистанция (могла измениться из-за проскальзывания на входе)
            actual_sl_dist = abs(actual_entry - params['stop_loss'])
            is_long_order  = (side == 'buy')
            min_dist       = actual_entry * config.MIN_SL_PERCENT

            if is_long_order and params['stop_loss'] >= actual_entry:
                params['stop_loss']   = actual_entry - min_dist
                actual_sl_dist        = min_dist
                log(f"⚠️ SL скорректирован (проскальзывание LONG): ${params['stop_loss']:.6f}")
            elif not is_long_order and params['stop_loss'] <= actual_entry:
                params['stop_loss']   = actual_entry + min_dist
                actual_sl_dist        = min_dist
                log(f"⚠️ SL скорректирован (проскальзывание SHORT): ${params['stop_loss']:.6f}")

            params['sl_distance'] = actual_sl_dist

            # Контроль фактического риска
            actual_risk = actual_filled * actual_sl_dist
            _base = sizing_balance if sizing_balance > 0 else 1
            log(f"Риск-контроль: цель ${risk_amount:.2f} ({config.RISK_PER_TRADE}%) | "
                f"фактически ${actual_risk:.2f} ({actual_risk / _base * 100:.2f}%)")

            # Выставляем SL + TP1-4 на бирже
            sl_order, tp_orders = self._place_sl_tp_orders(
                trading_pair, side, actual_filled, params
            )

            position = {
                'order':         order,
                'signal':        signal,
                'params':        params,
                'entry_time':    datetime.now(),
                'entry_price':   actual_entry,
                'status':        'OPEN',
                'remaining_size': actual_filled,
                'realized_pnl':  0.0,
                'tp_hit':        0,
                'sl_order_id':   sl_order.get('id') if sl_order else None,
                'tp_order_ids':  [o.get('id') if o else None for o in tp_orders],
                'breakeven_set': False,
                'trailing_active': False,
                'trail_stop':    None,
                'trail_distance': actual_sl_dist * config.TRAIL_DISTANCE_K,
            }

            # Журнал сделки — используем обновлённые params
            balance_before = self.get_real_balance()
            journal.open_trade(position, {**signal, 'params': params}, balance_before)

            self.active_positions[trading_pair].append(position)
            self._save_position_state(trading_pair, position, params)  # W7: сохраняем для восстановления
            self.last_trade_time[trading_pair] = datetime.now()
            self._save_cooldown_state()

            tg.trade_opened(signal, df_1h=df_1h, telegram_id=self.telegram_id)
            log(f"💰 Баланс после открытия: ${self.get_real_balance():.2f}")
            return True

        except Exception as e:
            log(f"❌ Ошибка отправки ордера: {e}")
            log(f"📋 Детали:\n{traceback.format_exc()}")
            reset_exchange()
            self.exchange = get_exchange()
            return False

    def _close_partial(self, trading_pair, position, fraction, reason):
        """Закрывает часть позиции рыночным ордером"""
        close_size = round(position['remaining_size'] * fraction, 6)
        if close_size <= 0:
            return

        side = 'sell' if position['signal']['setup']['type'] == 'LONG' else 'buy'

        try:
            self.exchange.create_order(
                symbol=trading_pair,
                type='market',
                side=side,
                amount=close_size,
                params={'reduce_only': True},
            )
            position['remaining_size'] = round(position['remaining_size'] - close_size, 6)
            log(f"📤 Частичное закрытие {reason}: {close_size:.6f} | Осталось: {position['remaining_size']:.6f}")
        except Exception as e:
            log(f"⚠️ Ошибка частичного закрытия {reason}: {e}")

    def _cancel_order(self, trading_pair, order_id):
        if not order_id:
            return
        try:
            self.exchange.cancel_order(order_id, trading_pair)
        except Exception:
            pass

    def _update_trail_stop(self, trading_pair, position, new_stop_price):
        """Обновляет SL позиции через Bybit position trading-stop."""
        try:
            self.exchange.privatePostV5PositionTradingStop({
                'category':    'linear',
                'symbol':      trading_pair,
                'stopLoss':    str(new_stop_price),
                'slTriggerBy': 'LastPrice',
                'positionIdx': 0,
            })
            log(f"   Трейлинг стоп → ${new_stop_price:.4f}")
        except Exception as e:
            if '34040' not in str(e) and 'not modified' not in str(e):
                log(f"   Ошибка обновления трейлинга: {e}")
        position['trail_stop'] = new_stop_price

    def _move_sl_to_breakeven(self, trading_pair, position):
        """Переносит стоп в безубыток через Bybit position trading-stop."""
        entry = position['entry_price']
        is_long = position['signal']['setup']['type'] == 'LONG'
        # Bybit требует SL строго по ту сторону entry: LONG — ниже, SHORT — выше
        offset = entry * 0.0002
        be_sl = (entry - offset) if is_long else (entry + offset)
        try:
            self.exchange.privatePostV5PositionTradingStop({
                'category':    'linear',
                'symbol':      trading_pair,
                'stopLoss':    str(round(be_sl, 8)),
                'slTriggerBy': 'LastPrice',
                'positionIdx': 0,
            })
            log(f"🔄 Стоп перенесён в безубыток @ ${be_sl:.6f}")
        except Exception as e:
            if '34040' not in str(e) and 'not modified' not in str(e):
                log(f"⚠️ Не удалось перенести стоп в безубыток: {e}")
        position['breakeven_set'] = True
        params = position['params']
        params['stop_loss'] = be_sl

    def manage_active_positions(self):
        total_positions = sum(len(p) for p in self.active_positions.values())
        if total_positions == 0:
            return

        log(f"\n📊 Активных позиций: {total_positions}")

        # Свежий снимок биржи на этот цикл (источник истины для реконсиляции/слотов)
        self._ex_snapshot = None
        ex_sizes = self._fetch_exchange_sizes()

        for trading_pair, positions in self.active_positions.items():
            if not positions:
                continue

            try:
                ticker = self.exchange.fetch_ticker(trading_pair)
                current_price = ticker['last']
                log(f"   {trading_pair}: ${current_price:.4f} ({len(positions)} поз.)")

                for position in positions[:]:
                    if position['status'] != 'OPEN':
                        continue
                    # Позицию закрыли вне бота (руками/ликвидация/биржевой SL)?
                    if self._is_closed_externally(trading_pair, position, ex_sizes):
                        self._handle_external_close(position, current_price, trading_pair)
                        continue
                    self._check_tp_levels(position, current_price, trading_pair)

            except Exception as e:
                log(f"⚠️ Ошибка получения цены для {trading_pair}: {e}")

    # ── Реконсиляция с биржей (ручное/внешнее закрытие) ──────────────────────

    @staticmethod
    def _norm_symbol(s: str) -> str:
        """Приводит символ к сравнимому виду: 'BTC/USDT:USDT' и 'BTCUSDT' -> 'BTCUSDT'."""
        return (s or '').replace('/', '').replace(':USDT', '').replace(':', '').upper()

    def _fetch_exchange_sizes(self):
        """Карта {нормализованный_символ: |contracts|} открытых позиций на бирже (из снимка).
        None — если биржа недоступна (тогда реконсиляцию пропускаем, чтобы НЕ закрыть
        позицию по ложному нулю)."""
        snap = self.sync_exchange_state()
        if not snap['pos_ok']:
            return None
        sizes = {}
        for sym, p in snap['positions'].items():
            try:
                sizes[sym] = abs(float(p.get('contracts', 0) or 0))
            except Exception:
                continue
        return sizes

    def _is_closed_externally(self, trading_pair, position, ex_sizes) -> bool:
        """True, если позиция фактически закрыта на бирже, а у нас ещё числится открытой."""
        if ex_sizes is None:
            return False
        # Свежей позиции даём время появиться в выдаче биржи (избегаем ложного закрытия)
        if (datetime.now() - position['entry_time']).total_seconds() < 120:
            return False
        remaining = position.get('remaining_size', 0.0) or 0.0
        if remaining <= 0:
            return False
        size_on_ex = ex_sizes.get(self._norm_symbol(trading_pair), 0.0)
        # Полное внешнее закрытие: на бирже осталось <10% от нашего остатка
        return size_on_ex < remaining * 0.10

    def _handle_external_close(self, position, exit_price, trading_pair):
        """Позиция закрыта вне бота: отменяем наши SL/TP-ордера и фиксируем сделку
        БЕЗ отправки рыночного закрытия (закрывать уже нечего)."""
        log(f"🔔 {trading_pair}: позиция закрыта на бирже вне бота — синхронизируем")
        self._cancel_order(trading_pair, position.get('sl_order_id'))
        for tp_oid in position.get('tp_order_ids', []):
            self._cancel_order(trading_pair, tp_oid)
        position['status'] = 'CLOSED_BY_EXT'
        self._finalize_trade(position, exit_price, 'EXT', trading_pair)

    def _check_tp_levels(self, position, current_price, trading_pair):
        params  = position['params']
        setup   = position['signal']['setup']
        tp_hit  = position['tp_hit']
        is_long = setup['type'] == 'LONG'

        # ── 0. W11: Безубыток при пробое уровня B импульса (0%, конец импульса) ─
        if getattr(config, 'BREAKEVEN_AT_B', True) and not position.get('breakeven_set'):
            be_level = params.get('be_level')
            if be_level:
                crossed = (is_long and current_price >= be_level) or \
                          (not is_long and current_price <= be_level)
                if crossed:
                    self._move_sl_to_breakeven(trading_pair, position)
                    log(f"   Пробит уровень B ${_fmt_p(be_level)} — SL в безубыток")

        # ── 1. Software SL — всегда активен (биржевой SL дополнительный backup) ─
        if is_long and current_price <= params['stop_loss']:
            reason = 'BE' if position.get('breakeven_set') else 'SL'
            self._close_all(position, current_price, reason, trading_pair)
            return
        elif not is_long and current_price >= params['stop_loss']:
            reason = 'BE' if position.get('breakeven_set') else 'SL'
            self._close_all(position, current_price, reason, trading_pair)
            return

        # ── 2. Проверяем следующий TP (W11: 2 тейка по 50%) ────────────────────
        if tp_hit < N_TP:
            tp_price  = params[TP_KEYS[tp_hit]]
            tp_reached = (is_long and current_price >= tp_price) or \
                         (not is_long and current_price <= tp_price)

            if tp_reached:
                tp_num = tp_hit + 1
                log(f"TP{tp_num} достигнут @ ${current_price:.4f} (цель ${tp_price:.4f})")

                if tp_hit < N_TP - 1:
                    # Биржевой limit-ордер закрывает долю от НАЧАЛЬНОГО размера позиции.
                    portion = round(params['position_size'] * TP_CLOSE_FRACTIONS[tp_hit], 6)
                    position['tp_hit'] += 1
                    position['remaining_size'] = max(0.0, round(position['remaining_size'] - portion, 6))

                    if is_long:
                        partial_pnl = (tp_price - position['entry_price']) * portion
                    else:
                        partial_pnl = (position['entry_price'] - tp_price) * portion
                    position['realized_pnl'] = position.get('realized_pnl', 0.0) + partial_pnl

                    remaining_pct = int(round(position['remaining_size'] / params['position_size'] * 100))
                    journal.record_tp_hit(position, tp_num, current_price)
                    tg.tp_hit(trading_pair, tp_num, setup['type'], current_price,
                              remaining_pct, position['realized_pnl'],
                              telegram_id=self.telegram_id)

                    # Подстраховка: после TP1 стоп в безубыток (если уровень B не сработал)
                    if not position['breakeven_set']:
                        self._move_sl_to_breakeven(trading_pair, position)
                else:
                    # Последний TP: биржевой limit-ордер закрывает остаток
                    self._close_all(position, current_price, f'TP{tp_num}', trading_pair)

    def _close_all(self, position, exit_price, reason, trading_pair):
        """Полностью закрывает позицию"""
        if position['remaining_size'] > 0:
            side = 'sell' if position['signal']['setup']['type'] == 'LONG' else 'buy'
            try:
                self.exchange.create_order(
                    symbol=trading_pair,
                    type='market',
                    side=side,
                    amount=position['remaining_size'],
                    params={'reduce_only': True},
                )
            except Exception as e:
                log(f"⚠️ Ошибка закрытия позиции: {e}")

        # Отменяем оставшиеся ордера
        self._cancel_order(trading_pair, position.get('sl_order_id'))
        for tp_oid in position.get('tp_order_ids', []):
            self._cancel_order(trading_pair, tp_oid)

        position['status'] = f'CLOSED_BY_{reason}'
        self._finalize_trade(position, exit_price, reason, trading_pair)

    def _finalize_trade(self, position, exit_price, reason, trading_pair):
        entry     = position['entry_price']
        remaining = position.get('remaining_size', 0.0)
        realized  = position.get('realized_pnl', 0.0)

        if position['signal']['setup']['type'] == 'LONG':
            pnl = realized + (exit_price - entry) * remaining
        else:
            pnl = realized + (entry - exit_price) * remaining

        position['pnl'] = pnl
        position['exit_price'] = exit_price
        position['exit_time'] = datetime.now()

        self.daily_pnl += pnl
        self.trade_history.append(position)

        trade_data = {
            'time': position['entry_time'].strftime('%Y-%m-%d %H:%M:%S'),
            'pair': trading_pair,
            'direction': position['signal']['setup']['type'],
            'entry': entry,
            'exit': exit_price,
            'pnl': round(pnl, 4),
            'zone': position['signal']['trigger']['zone'],
            'result': reason,
            'mode': config.TRADING_MODE,
        }
        log_trade(trade_data)
        log(f"📝 Сделка завершена ({reason}). PnL: ${pnl:+.4f} | Дневной PnL: ${self.daily_pnl:+.4f}")

        # Записываем в журнал (БД per-user при мульти-тенант, иначе CSV)
        balance_after = self.get_real_balance()
        journal.close_trade(position, exit_price, reason, pnl, balance_after, config,
                            telegram_id=self.telegram_id)
        self._remove_position_state(trading_pair)  # W7: удаляем из файла состояния

        duration_min = int((position['exit_time'] - position['entry_time']).total_seconds() / 60)
        tg.trade_closed(
            trading_pair,
            position['signal']['setup']['type'],
            reason,
            pnl,
            self.daily_pnl,
            self.get_real_balance(),
            entry=entry,
            exit_price=exit_price,
            duration_min=duration_min,
            tps_hit=position.get('tp_hit', 0),
            telegram_id=self.telegram_id,
        )

    def close_position_by_pair(self, trading_pair: str):
        """Вручную закрывает позицию по паре. Возвращает (success, price).
        Работает и для позиций, которых нет в памяти бота, но есть на бирже (рассинхрон)."""
        # 1) Есть отслеживаемая позиция — закрываем штатно (журнал/уведомления/отмена ордеров)
        for position in self.active_positions.get(trading_pair, []):
            if position['status'] == 'OPEN':
                try:
                    current_price = self.exchange.fetch_ticker(trading_pair)['last']
                    self._close_all(position, current_price, 'Manual', trading_pair)
                    self._ex_snapshot = None
                    return True, current_price
                except Exception as e:
                    log(f"Ошибка ручного закрытия {trading_pair}: {e}")
                    return False, 0.0

        # 2) Позиции в памяти нет — закрываем «сырую» позицию прямо с биржи (untracked)
        try:
            snap = self.sync_exchange_state(max_age=0)
            pos  = snap['positions'].get(self._norm_symbol(trading_pair))
            if not pos:
                return False, 0.0
            size = abs(float(pos.get('contracts', 0) or 0))
            if size <= 0:
                return False, 0.0
            is_long = (str(pos.get('side', '')).lower() == 'long')
            close_side = 'sell' if is_long else 'buy'
            # Отменяем висящие ордера и закрываем позицию рыночным reduce_only
            try:
                self.exchange.cancel_all_orders(trading_pair)
            except Exception:
                pass
            self.exchange.create_order(symbol=trading_pair, type='market', side=close_side,
                                       amount=size, params={'reduce_only': True})
            current_price = self.exchange.fetch_ticker(trading_pair)['last']
            self._ex_snapshot = None
            log(f"🖐 {trading_pair}: закрыта untracked-позиция вручную @ ${_fmt_p(current_price)}")
            return True, current_price
        except Exception as e:
            log(f"Ошибка закрытия untracked {trading_pair}: {e}")
            return False, 0.0

    def get_stats_dict(self) -> dict:
        """Статистика юзера: из БД (мульти-тенант) или CSV-журнала (legacy). None если пусто."""
        if self.telegram_id is not None:
            import db
            trades = db.get_user_trades(self.telegram_id)
        else:
            jf = journal.JOURNAL_FILE
            if not os.path.exists(jf):
                return None
            try:
                with open(jf, 'r', encoding='utf-8') as f:
                    trades = list(csv.DictReader(f))
            except Exception:
                return None
        return compute_stats(trades)

    def get_stats(self):
        if not self.trade_history:
            log("\n📊 Статистика: сделок ещё не было")
            return

        total = len(self.trade_history)
        winners = [t for t in self.trade_history if t.get('pnl', 0) > 0]
        total_pnl = sum(t.get('pnl', 0) for t in self.trade_history)
        win_rate = len(winners) / total * 100

        mode = config.TRADING_MODE
        log(f"\n{'='*60}")
        log(f" СТАТИСТИКА ТОРГОВЛИ ({mode})")
        log(f"{'='*60}")
        log(f"Всего сделок:   {total}")
        log(f"Прибыльных:     {len(winners)} ({win_rate:.1f}%)")
        log(f"Убыточных:      {total - len(winners)} ({100-win_rate:.1f}%)")
        log(f"Итоговый PnL:   ${total_pnl:+.4f}")
        log(f"Баланс:         ${self.get_real_balance():.2f}")
        log(f"{'='*60}")


def compute_stats(trades) -> dict:
    """Сводная статистика по списку сделок (строки журнала/БД). None если пусто.

    Вынесено из метода get_stats_dict, чтобы Telegram-команды могли считать прямо
    из БД (db.get_user_trades) без живого LiveTradeManager."""
    if not trades:
        return None

    def _f(t, key): return float(t.get(key, 0) or 0)

    total        = len(trades)
    wins         = sum(1 for t in trades if t.get('result') == 'WIN')
    total_pnl    = sum(_f(t, 'pnl_usd') for t in trades)
    gross_profit = sum(_f(t, 'pnl_usd') for t in trades if _f(t, 'pnl_usd') > 0)
    gross_loss   = abs(sum(_f(t, 'pnl_usd') for t in trades if _f(t, 'pnl_usd') < 0))

    today_str = datetime.now().strftime('%Y-%m-%d')
    today_pnl = sum(_f(t, 'pnl_usd') for t in trades
                    if t.get('close_time', '').startswith(today_str))

    pnls  = [(_f(t, 'pnl_usd'), t.get('pair', '?')) for t in trades]
    best  = max(pnls, key=lambda x: x[0])
    worst = min(pnls, key=lambda x: x[0])

    zone_a = [t for t in trades if t.get('zone') == 'Zone_A']
    zone_b = [t for t in trades if t.get('zone') == 'Zone_B']
    longs  = [t for t in trades if t.get('direction') == 'LONG']
    shorts = [t for t in trades if t.get('direction') == 'SHORT']

    durations    = [int(t.get('duration_min', 0) or 0) for t in trades]
    avg_duration = sum(durations) / len(durations) if durations else 0

    return {
        'total':         total,
        'wins':          wins,
        'losses':        total - wins,
        'win_rate':      wins / total * 100 if total > 0 else 0,
        'total_pnl':     total_pnl,
        'today_pnl':     today_pnl,
        'profit_factor': gross_profit / gross_loss if gross_loss > 0 else float('inf'),
        'best':          best,
        'worst':         worst,
        'avg_duration':  avg_duration,
        'zone_a':        (len(zone_a), sum(1 for t in zone_a if t.get('result') == 'WIN')),
        'zone_b':        (len(zone_b), sum(1 for t in zone_b if t.get('result') == 'WIN')),
        'longs':         (len(longs),  sum(1 for t in longs  if t.get('result') == 'WIN')),
        'shorts':        (len(shorts), sum(1 for t in shorts if t.get('result') == 'WIN')),
    }


if __name__ == "__main__":
    manager = LiveTradeManager()
    log("✅ Live Trade Manager инициализирован")
    log(f"💰 Баланс: ${manager.get_real_balance():.2f}")
