"""
Фантомный брокер: сделки считаются по реальным свечам, но на бирже не
открываются.

Зачем отдельный модуль, а не флаг внутри trade_manager. LiveTradeManager
пронизан вызовами биржи (create_order, set_leverage, trading-stop), и любой
пропущенный флаг означал бы реальный ордер. Здесь класс физически не умеет
торговать: ccxt-клиент используется только для чтения свечей и открыт БЕЗ
ключей. Отправить ордер этим кодом нельзя даже по ошибке.

Что даёт фантомный режим, чего не даёт бэктест:
    * решения принимаются на живом рынке в реальном времени, без права
      подсмотреть будущее;
    * обе стратегии могут держать ОДНУ И ТУ ЖЕ пару одновременно — на бирже
      это невозможно, а для честного сравнения необходимо: иначе более частая
      стратегия отбирает сетапы у второй;
    * у каждой стратегии свой виртуальный депозит, поэтому доходность
      сравнивается в процентах, а не в абсолютных долларах.

Модель исполнения (сознательно пессимистичная — завышенный результат хуже
заниженного):
    * вход  — GTC-лимит, как в бою; заполняется, когда 5-минутная свеча
      КАСАЕТСЯ цены лимита; комиссия мейкера;
    * выход — комиссия тейкера; на стоповых выходах (SL/BE/тайм-стоп) ещё и
      проскальзывание против нас;
    * если в одной свече задеты и стоп, и тейк — считаем, что сработал СТОП.
      По OHLC порядок внутри свечи неизвестен, и выбор в свою пользу — самый
      частый способ нарисовать себе несуществующую доходность;
    * фандинг списывается на границах 8-часовых интервалов по реальной ставке
      биржи (при недоступности — по PAPER_FUNDING_RATE_8H).

Задержка: обрабатываются только ЗАКРЫТЫЕ 5-минутные свечи, поэтому событие
фиксируется с опозданием до 5 минут. Цикл бота идёт с тем же шагом.
"""

import csv
import json
import math
import os
import shutil
from datetime import datetime, timezone

import config
import glossary
from exit_plan import cooldown_hours, direction_cap, tp_plan, wants_breakeven
import settings_store as settings
from logger import log

STRATEGIES = ('FIBO', 'SMC', 'LEVELS')

BAR_TF = '5m'
BAR_MS = 5 * 60 * 1000
FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000

STATE_FILE   = os.path.join(config.DATA_DIR, 'paper_state.json')
JOURNAL_CSV  = os.path.join(config.DATA_DIR, 'paper_trades.csv')
JOURNAL_JSON = os.path.join(config.DATA_DIR, 'paper_trades.jsonl')

# Колонки выгрузки. Порядок — «как читает человек»: что за сделка, по каким
# уровням, чем кончилась, сколько съели издержки, почему вообще открылась.
COLUMNS = [
    'trade_id', 'strategy', 'pair', 'direction', 'zone', 'htf_trend',
    'open_time', 'entry_price', 'planned_entry', 'entry_wait_min',
    'stop_loss', 'tp1', 'tp2', 'rr', 'risk_usd', 'position_size',
    'notional_usd', 'leverage_eff',
    'close_time', 'exit_price', 'exit_reason', 'tps_hit', 'duration_min',
    'gross_pnl_usd', 'fees_usd', 'funding_usd', 'pnl_usd', 'pnl_r', 'pnl_pct',
    'balance_before', 'balance_after', 'result',
    'mfe_price', 'mae_price', 'mfe_r', 'mae_r', 'breakeven_set',
    'why', 'confluence', 'poi_type', 'factors', 'sweep',
    'impulse_pct', 'score', 'proximity', 'htf_strength',
    # Человеческие формулировки — чтобы выгрузку можно было читать в Excel,
    # не держа в голове словарь технических имён. Технические имена остались
    # в колонке factors: на них считается разбор вкладов.
    'exit_reason_ru', 'confirmed_ru', 'missing_ru',
]


def _fmt_p(price: float) -> str:
    """Цена с разумным числом знаков: BTC и SHIB в одном логе."""
    if price >= 1000:   return f"{price:.2f}"
    if price >= 1:      return f"{price:.4f}"
    if price >= 0.01:   return f"{price:.6f}"
    if price >= 0.0001: return f"{price:.8f}"
    return f"{price:.10f}"


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec='seconds')


def _norm(symbol: str) -> str:
    return (symbol or '').replace('/', '').replace(':USDT', '').replace(':', '').upper()


# ── Гейт сканера ─────────────────────────────────────────────────────────────

class StrategyGate:
    """
    Переходник под интерфейс, который ожидают сканеры (check_cooldown,
    has_position_or_order).

    Существует ради одной вещи: в фантомном режиме занятость пары считается
    ОТДЕЛЬНО для каждой стратегии. Позиция SMC по BTCUSDT не должна закрывать
    фибо-стратегии вход в ту же пару — на бирже так нельзя, а здесь можно, и
    именно это делает сравнение честным.
    """

    def __init__(self, broker, strategy):
        self._broker = broker
        self._strategy = strategy

    def check_cooldown(self, pair):
        return self._broker.check_cooldown(self._strategy, pair)

    def has_position_or_order(self, pair):
        return self._broker.has_position_or_order(self._strategy, pair)


# ── Брокер ───────────────────────────────────────────────────────────────────

class PaperBroker:

    def __init__(self, client, strategies=None, start_balance=None):
        self.client = client
        self.strategies = tuple(strategies or STRATEGIES)
        self.state_file = STATE_FILE
        self.journal_csv = JOURNAL_CSV
        self.journal_jsonl = JOURNAL_JSON

        self.trade_history = []      # для Telegram-панели: закрытые за сессию
        self.daily_pnl = 0.0
        self._funding_cache = {}     # pair -> (ставка, время запроса ms)

        self.state = self._load_state(start_balance)

    # ── Состояние ────────────────────────────────────────────────────────────

    def _blank_state(self, start_balance):
        start_balance = start_balance or config.PAPER_START_BALANCES
        balances = {}
        for name in self.strategies:
            balances[name] = float(start_balance.get(name, config.PAPER_START_BALANCE))
        return {
            'version': 1,
            'started_at': datetime.now().isoformat(timespec='seconds'),
            'start_balance': dict(balances),
            'balance': dict(balances),
            'next_trade_id': 1,
            'cooldown':  {name: {} for name in self.strategies},
            'positions': {name: {} for name in self.strategies},
            'pending':   {name: {} for name in self.strategies},
        }

    def _load_state(self, start_balance):
        if not os.path.exists(self.state_file):
            state = self._blank_state(start_balance)
            self._save_state(state)
            log(f"👻 Фантомный счёт создан: " +
                ", ".join(f"{k} ${v:,.0f}" for k, v in state['start_balance'].items()))
            return state

        try:
            with open(self.state_file, 'r', encoding='utf-8') as fh:
                state = json.load(fh)
        except Exception as exc:
            log(f"⚠️ paper_state.json нечитаем ({exc}) — эксперимент начат заново")
            state = self._blank_state(start_balance)
            self._save_state(state)
            return state

        start_balance = start_balance or config.PAPER_START_BALANCES

        # Стратегия могла добавиться после старта — доводим структуру,
        # НЕ трогая уже накопленные балансы.
        for key in ('start_balance', 'balance'):
            state.setdefault(key, {})
        for key in ('cooldown', 'positions', 'pending'):
            state.setdefault(key, {})
        for name in self.strategies:
            if name not in state['start_balance']:
                initial = float(start_balance.get(name, config.PAPER_START_BALANCE))
                state['start_balance'][name] = initial
                state['balance'][name] = initial
            for key in ('cooldown', 'positions', 'pending'):
                state[key].setdefault(name, {})
        state.setdefault('next_trade_id', 1)

        # Депозит в .env поменяли на ходу — молча пересчитывать нельзя:
        # доходность в процентах считается от НАЧАЛЬНОГО депозита, и подмена
        # базы задним числом исказила бы весь эксперимент.
        for name in self.strategies:
            wanted = float(start_balance.get(name, config.PAPER_START_BALANCE))
            stored = float(state['start_balance'][name])
            if abs(wanted - stored) > 0.01:
                log(f"⚠️ {name}: в настройках депозит ${wanted:,.0f}, но эксперимент "
                    f"начат с ${stored:,.0f}. Оставляю начальный. "
                    f"Чтобы начать заново — PAPER_RESET=true")
        return state

    def _save_state(self, state=None):
        state = self.state if state is None else state
        try:
            tmp = self.state_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(state, fh, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp, self.state_file)
        except Exception as exc:
            log(f"⚠️ Не удалось сохранить paper_state.json: {exc}")

    @staticmethod
    def archive_previous():
        """
        Убирает прошлый эксперимент в архив (PAPER_RESET=true).

        Файлы переименовываются, а не удаляются: история фантомных сделок —
        это данные для анализа стратегии, и потерять их из-за одной строки в
        .env было бы обидно.
        """
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        moved = []
        for path in (STATE_FILE, JOURNAL_CSV, JOURNAL_JSON):
            if os.path.exists(path):
                target = f"{path}.{stamp}.bak"
                try:
                    shutil.move(path, target)
                    moved.append(os.path.basename(target))
                except Exception as exc:
                    log(f"⚠️ Не удалось убрать в архив {path}: {exc}")
        if moved:
            log(f"🗄 Прошлый фантомный прогон в архиве: {', '.join(moved)}")
        return moved

    # ── Запросы состояния ────────────────────────────────────────────────────

    def balance(self, strategy):
        return float(self.state['balance'].get(strategy, 0.0))

    def start_balance(self, strategy):
        return float(self.state['start_balance'].get(strategy, 0.0))

    def positions(self, strategy):
        return self.state['positions'].get(strategy, {})

    def pending(self, strategy):
        return self.state['pending'].get(strategy, {})

    def has_position_or_order(self, strategy, pair):
        key = _norm(pair)
        return key in self.positions(strategy) or key in self.pending(strategy)

    def check_cooldown(self, strategy, pair):
        last = self.state['cooldown'].get(strategy, {}).get(_norm(pair))
        if not last:
            return True
        hours = (_now_ms() - int(last)) / 3_600_000
        return hours >= cooldown_hours(strategy)

    def portfolio_risk(self):
        """
        Сколько сейчас под риском по всему портфелю: сумма и доля депозита.

        Считаются и открытые позиции, и ожидающие ордера: ордер, который
        вот-вот нальётся, — это уже принятый риск, и не учитывать его
        значило бы обходить собственный предел.
        """
        amount, deposit = 0.0, 0.0
        for strategy in self.strategies:
            deposit += float(self.balance(strategy) or 0)
            for book in (self.positions(strategy), self.pending(strategy)):
                for item in book.values():
                    amount += float(item.get('risk_amount') or 0)
        pct = (amount / deposit * 100) if deposit > 0 else 0.0
        return amount, pct, deposit

    def portfolio_slots(self):
        """Позиций и ордеров по всем стратегиям вместе."""
        return sum(len(self.positions(s)) + len(self.pending(s))
                   for s in self.strategies)

    def _portfolio_room(self, strategy, params):
        """Пропускает ли предел портфеля ещё одну сделку."""
        max_positions = settings.portfolio_max_positions()
        if max_positions and self.portfolio_slots() >= max_positions:
            return False, (f'предел портфеля: занято '
                           f'{self.portfolio_slots()}/{max_positions} позиций')

        limit = settings.portfolio_risk_pct()
        if not limit:
            return True, ''
        used, pct, deposit = self.portfolio_risk()
        if deposit <= 0:
            return True, ''
        risk_pct = float(params.get('risk_pct') or config.RISK_PER_TRADE)
        addition = self.balance(strategy) * risk_pct / 100
        after = (used + addition) / deposit * 100
        if after > limit:
            return False, (f'предел портфеля: под риском {pct:.1f}%, '
                           f'сделка добавит до {after:.1f}% при пределе {limit:.1f}%')
        return True, ''

    def slots_used_by(self, strategy):
        return len(self.positions(strategy)) + len(self.pending(strategy))

    def _direction_count(self, strategy, direction):
        """Сколько позиций и ордеров стратегии смотрят в одну сторону."""
        return sum(1 for book in (self.positions(strategy), self.pending(strategy))
                   for item in book.values() if item['direction'] == direction)

    def free_slots(self, strategy):
        budget = config.SLOTS_PER_STRATEGY or config.MAX_ACTIVE_PAIRS
        return budget - self.slots_used_by(strategy)

    def equity(self, strategy):
        """Баланс + нереализованный результат по открытым позициям."""
        total = self.balance(strategy)
        for pos in self.positions(strategy).values():
            total += self._unrealised(pos)
        return total

    @staticmethod
    def _unrealised(pos):
        """
        Плавающий результат позиции.

        Комиссии и фандинг вычитаются: уже уплаченное — это деньги, которых на
        счёте нет, и показывать их как часть капитала значит завышать депозит.
        """
        price = pos.get('last_price') or pos['entry_price']
        sign = 1 if pos['direction'] == 'LONG' else -1
        return (pos.get('realized_pnl', 0.0)
                + sign * (price - pos['entry_price']) * pos['size']
                - pos.get('fees_paid', 0.0)
                - pos.get('funding_paid', 0.0))

    # ── Совместимость с Telegram-панелью ─────────────────────────────────────

    @property
    def exchange(self):
        return self.client

    def get_real_balance(self):
        return sum(self.equity(name) for name in self.strategies)

    def get_open_pairs(self):
        pairs = set()
        for name in self.strategies:
            pairs |= set(self.positions(name).keys()) | set(self.pending(name).keys())
        return pairs

    def get_active_count(self):
        return sum(len(self.positions(name)) for name in self.strategies)

    def _load_pending_orders(self):
        out = {}
        for name in self.strategies:
            for pair, po in self.pending(name).items():
                out[f"{pair} [{name}]"] = po
        return out

    @property
    def active_positions(self):
        """
        Позиции в форме, которую понимает Telegram-панель.

        Ключ содержит стратегию, потому что одна и та же пара может быть
        открыта обеими — в живом режиме такого ключа не бывает.
        """
        out = {}
        for name in self.strategies:
            for pair, pos in self.positions(name).items():
                out[f"{pair} [{name}]"] = [self._as_live_position(pos)]
        return out

    @staticmethod
    def _as_live_position(pos):
        targets = pos['targets']
        return {
            'status': 'OPEN',
            'entry_price': pos['entry_price'],
            'entry_time': datetime.fromisoformat(pos['opened_at']),
            'tp_hit': pos.get('tp_hit', 0),
            'breakeven_set': pos.get('breakeven_set', False),
            'trailing_active': False,
            'signal': {'setup': {'type': pos['direction']},
                       'trigger': {'zone': pos.get('zone', '—')}},
            'params': {
                'stop_loss': pos['stop_loss'],
                'take_profit_1': targets[0],
                'take_profit_2': targets[1] if len(targets) > 1 else targets[0],
                'position_size': pos['size'],
            },
        }

    def get_stats_dict(self):
        from trade_manager import compute_stats
        rows = read_journal()
        return compute_stats(rows) if rows else None

    def gate(self, strategy):
        return StrategyGate(self, strategy)

    def reset_at(self, strategy):
        """Момент последнего перезапуска стратегии (или None)."""
        return (self.state.get('reset_at') or {}).get(strategy)

    def set_deposit(self, strategy, deposit, restart=False):
        """
        Задаёт депозит, с которого стратегия торгует.

        Возвращает (получилось, сообщение).

        Пока стратегия не сделала ни одной сделки, депозит меняется свободно.
        Дальше — только вместе с перезапуском, потому что доходность считается
        в процентах от стартовой базы: подменить базу и оставить прежние сделки
        значит получить процент, которого не было.

        Перезапуск НЕ удаляет историю. Он ставит отметку времени, с которой
        начинается новый отсчёт; прежние сделки остаются в журнале и в
        выгрузке, просто не участвуют в текущей статистике стратегии.
        """
        if strategy not in self.strategies:
            return False, f'Стратегия {strategy} не запущена'
        try:
            deposit = float(deposit)
        except (TypeError, ValueError):
            return False, 'Депозит должен быть числом'
        if deposit <= 0:
            return False, 'Депозит должен быть больше нуля'
        if abs(deposit - self.start_balance(strategy)) < 0.01 and not restart:
            return True, 'Депозит не изменился'

        untouched = (not self.slots_used_by(strategy)
                     and abs(self.balance(strategy) - self.start_balance(strategy)) < 0.01)

        if not untouched and not restart:
            return False, ('По стратегии уже есть сделки или позиции. Сменить '
                           'депозит можно только вместе с перезапуском отсчёта.')

        if restart and not untouched:
            self.state['positions'][strategy] = {}
            self.state['pending'][strategy] = {}
            self.state['cooldown'][strategy] = {}
            self.state.setdefault('reset_at', {})[strategy] = _iso(_now_ms())
            log(f"🔄 {strategy}: отсчёт начат заново с ${deposit:,.2f}. "
                f"Прежние сделки остались в журнале, но в статистику не входят.")
        else:
            log(f"⚙️ {strategy}: стартовый депозит ${deposit:,.2f}")

        self.state['start_balance'][strategy] = deposit
        self.state['balance'][strategy] = deposit
        self._save_state()
        return True, f'{strategy}: депозит ${deposit:,.2f}'

    def apply_settings(self, settings):
        """Применяет депозит из настроек, когда это возможно без перезапуска."""
        for name in self.strategies:
            wanted = (settings.get(name) or {}).get('deposit')
            if wanted is None:
                continue
            ok, message = self.set_deposit(name, wanted)
            if not ok:
                log(f"⚠️ {name}: {message}")

    # ── Открытие ─────────────────────────────────────────────────────────────

    def open(self, strategy, signal):
        """
        Ставит фантомный лимитный ордер по сигналу стратегии.

        Размер считается от ТЕКУЩЕГО виртуального депозита этой стратегии, а не
        от того, что насчитал сканер: депозиты двух стратегий расходятся со
        временем, и риск должен следовать за своим.
        """
        pair = _norm(signal['trading_pair'])
        params = signal['params']
        setup = signal['setup']
        direction = setup['type']
        is_long = direction == 'LONG'

        if self.has_position_or_order(strategy, pair):
            log(f"   [{strategy}] {pair}: уже есть фантомная позиция/ордер")
            return False
        if not self.check_cooldown(strategy, pair):
            log(f"   [{strategy}] {pair}: кулдаун активен")
            return False

        # Предел на ВЕСЬ портфель — единственная проверка, которая смотрит
        # за пределы своей стратегии. Каждая соблюдает свой лимит слотов, и
        # при трёх стратегиях по шесть позиций под риском оказывается 9%
        # депозита, хотя ни одна правил не нарушила.
        allowed, why = self._portfolio_room(strategy, params)
        if not allowed:
            log(f"   [{strategy}] {pair}: {why}")
            return False

        # Направленный кэп берём из сигнала: у стратегий он разный. Считаем
        # только СВОИ позиции — книги у стратегий раздельные, и ограничение
        # одной не должно молча урезать вторую.
        cap = direction_cap(params)
        if cap > 0 and self._direction_count(strategy, direction) >= cap:
            log(f"   [{strategy}] {pair}: направление {direction} занято "
                f"{cap}/{cap} — направленный кэп")
            return False

        entry = float(params['entry'])
        stop = float(params['stop_loss'])
        sl_dist = abs(entry - stop)
        if sl_dist <= 0:
            log(f"   [{strategy}] {pair}: нулевая дистанция стопа — пропуск")
            return False

        balance = self.balance(strategy)
        if balance <= 0:
            log(f"   [{strategy}] {pair}: депозит обнулён — торговля остановлена")
            return False

        risk_pct = float(params.get('risk_pct') or config.RISK_PER_TRADE)
        risk_amount = balance * (risk_pct / 100)
        size = risk_amount / sl_dist
        if size > config.MAX_POSITION_SIZE_UNITS:
            size = config.MAX_POSITION_SIZE_UNITS
            risk_amount = size * sl_dist

        # Тот же предохранитель, что в бою: позиция не может стоить больше
        # депозита с плечом. Без него мелкий депозит «торговал» бы объёмами,
        # которые биржа не пропустит.
        notional = size * entry
        if notional > balance * config.LEVERAGE:
            log(f"   [{strategy}] {pair}: объём ${notional:,.0f} > депозит×плечо "
                f"${balance * config.LEVERAGE:,.0f} — пропуск")
            return False

        # План выхода берём у стратегии, а не собираем из take_profit_1/2:
        # у фибо одна цель на 100%, у SMC три с долями 25/25/50, и подменять
        # один план другим значит торговать не ту стратегию, что проверялась.
        targets, fractions = tp_plan(params)
        if not targets:
            log(f"   [{strategy}] {pair}: у сигнала нет целей — пропуск")
            return False

        offset = config.LIMIT_ENTRY_OFFSET_PCT if config.USE_LIMIT_ENTRY else 0.0
        limit_price = entry * (1 + offset) if is_long else entry * (1 - offset)

        now = _now_ms()
        record = {
            'strategy': strategy,
            'pair': pair,
            'direction': direction,
            'planned_entry': entry,
            'limit_price': limit_price,
            'stop_loss': stop,
            'targets': targets,
            'fractions': fractions,
            'be_level': params.get('be_level'),
            'breakeven_after_tp': bool(params.get(
                'breakeven_after_tp', getattr(config, 'BREAKEVEN_AT_B', True))),
            'risk_amount': risk_amount,
            'size': size,
            'rr': float(params.get('rr') or 0),
            'invalidation': self._invalidation(strategy, signal, is_long),
            'placed_ts': now,
            # UTC, как и время открытия позиции: иначе две метки в одном
            # состоянии живут в разных часовых поясах, и разница вылезает
            # позже как «позиция открыта 3 часа назад, а ордер стоял 6».
            'placed_at': _iso(now),
            'expires_ts': now + int(config.PENDING_ORDER_MAX_HOURS * 3_600_000),
            'last_ts': now - BAR_MS,      # начинаем со свечи, идущей сейчас
            'balance_before': balance,
            'context': self._context(strategy, signal),
        }

        self.state['pending'][strategy][pair] = record
        # Кулдаун ставим при ВЫСТАВЛЕНИИ лимита, как в бою: иначе один и тот же
        # сетап переоткрывался бы каждые 5 минут, пока лимит ждёт цену.
        self.state['cooldown'][strategy][pair] = now
        self._save_state()

        log(f"   👻 [{strategy}] {pair} {direction}: лимит ${_fmt_p(limit_price)} | "
            f"стоп ${_fmt_p(stop)} | цель ${_fmt_p(targets[0])} | RR {record['rr']:.2f}")
        return True

    @staticmethod
    def _invalidation(strategy, signal, is_long):
        """
        Уровень, за которым сетап перестаёт существовать и лимит снимается.

        У фибо это 88.6%-уровень коррекции (так же считает живой бот), у SMC —
        стоп: заход цены за зону означает, что зона не отработала.
        """
        setup = signal['setup']
        if strategy == 'FIBO' and setup.get('size'):
            end_price, size = float(setup['end_price']), float(setup['size'])
            return (end_price - size * config.ZONE_B_TOP if is_long
                    else end_price + size * config.ZONE_B_TOP)
        return float(signal['params']['stop_loss'])

    @staticmethod
    def _context(strategy, signal):
        """
        «Почему открылась» — то, что потом читают в дашборде и выгрузке.

        Хранится в двух видах сразу: технические имена факторов нужны разбору
        вкладов, человеческие — чтобы понять сделку с одного взгляда. Список
        НЕсработавших подтверждений не менее важен сработавших: именно он
        объясняет, почему две внешне одинаковые сделки разошлись.
        """
        scan = signal.get('scan') or {}
        smc = signal.get('smc') or {}
        setup = signal['setup']
        raw_factors = smc.get('factors') or scan.get('factors')

        ctx = {
            'zone': signal['trigger'].get('zone', '—'),
            'htf_trend': signal.get('htf_trend', '—'),
            'score': scan.get('score'),
            'proximity': scan.get('proximity'),
            'htf_strength': scan.get('htf_strength'),
            'confluence': smc.get('confluence') or scan.get('confluence'),
            'poi_type': smc.get('poi_type') or scan.get('poi_type'),
            'sweep': smc.get('sweep') or scan.get('sweep'),
        }
        try:
            ctx['impulse_pct'] = round(setup['size'] / setup['end_price'] * 100, 2)
        except Exception:
            ctx['impulse_pct'] = None

        ok, missing = glossary.confirmations(raw_factors)
        ctx['confirmed'] = ok
        ctx['missing'] = missing
        ctx['factors'] = (list(raw_factors) if isinstance(raw_factors, (list, tuple))
                          else [k for k, v in (raw_factors or {}).items() if v])

        # Фразу собираем через разделитель, а не предлогами: русские падежи
        # потребовали бы склонять каждое название из словаря («в зона A»).
        side = glossary.direction(setup['type']).capitalize()
        if strategy == 'SMC':
            parts = [side, glossary.poi_type(ctx['poi_type']),
                     f"подтверждения на {ctx['confluence']}"]
        else:
            impulse = f"{ctx['impulse_pct']}%" if ctx['impulse_pct'] is not None else '—'
            parts = [side, glossary.zone(ctx['zone']),
                     f"тренд 4H {glossary.trend(ctx['htf_trend'])}",
                     f"импульс {impulse}"]
        ctx['why'] = ' · '.join(parts)
        return ctx

    # ── Симуляция ────────────────────────────────────────────────────────────

    def update(self):
        """
        Прокручивает все фантомные ордера и позиции по свечам, появившимся с
        прошлого вызова. Вызывается раз в цикл бота.
        """
        pairs = {}
        for strategy in self.strategies:
            for pair, rec in list(self.pending(strategy).items()):
                pairs[pair] = min(pairs.get(pair, rec['last_ts']), rec['last_ts'])
            for pair, rec in list(self.positions(strategy).items()):
                pairs[pair] = min(pairs.get(pair, rec['last_ts']), rec['last_ts'])

        if not pairs:
            return

        log(f"\n👻 Фантомный счёт: пар в работе — {len(pairs)}")
        changed = False
        for pair, since in pairs.items():
            candles = self._fetch_candles(pair, since)
            if not candles:
                continue
            funding = self._funding_rate(pair)
            for strategy in self.strategies:
                if self._advance(strategy, pair, candles, funding):
                    changed = True

        if changed:
            self._save_state()

    MAX_BARS = 500   # предел выдачи биржи за один запрос

    def _fetch_candles(self, pair, since_ts):
        """Закрытые 5-минутные свечи новее since_ts."""
        now = _now_ms()
        needed = math.ceil((now - since_ts) / BAR_MS) + 2
        # Бот стоял дольше, чем помещается в один запрос. Молча взять последние
        # 500 свечей нельзя: стоп, сработавший в пропущенном промежутке, был бы
        # потерян, и позиция «продолжилась» бы с неверной цены.
        if needed > self.MAX_BARS:
            hours = (now - since_ts) / 3_600_000
            log(f"   ⚠️ {pair}: перерыв {hours:.1f}ч длиннее окна выдачи "
                f"({self.MAX_BARS} свечей ≈ {self.MAX_BARS * 5 / 60:.0f}ч) — "
                f"часть событий не восстановить, результат сделки будет неточным")
        bars = max(3, min(needed, self.MAX_BARS))
        try:
            raw = self.client.fetch_ohlcv(pair, BAR_TF, limit=bars)
        except Exception as exc:
            log(f"   ⚠️ {pair}: свечи недоступны — {exc}")
            return []
        # Последняя свеча ещё формируется: её high/low не окончательны.
        return [c for c in (raw or []) if c[0] > since_ts and c[0] + BAR_MS <= now]

    def _funding_rate(self, pair):
        """Ставка фандинга за 8ч. При недоступности — значение из настроек."""
        if not config.PAPER_FUNDING:
            return 0.0
        cached = self._funding_cache.get(pair)
        now = _now_ms()
        if cached and now - cached[1] < 4 * 3_600_000:
            return cached[0]
        rate = config.PAPER_FUNDING_RATE_8H
        try:
            info = self.client.fetch_funding_rate(pair)
            value = info.get('fundingRate')
            if value is not None:
                rate = float(value)
        except Exception:
            pass   # биржа не отдала ставку — считаем по типовой
        self._funding_cache[pair] = (rate, now)
        return rate

    def _advance(self, strategy, pair, candles, funding_rate):
        """Прогоняет свечи через ордер/позицию одной стратегии по одной паре."""
        touched = False
        for ts, _open, high, low, close, _vol in candles:
            pending = self.pending(strategy).get(pair)
            if pending and ts > pending['last_ts']:
                pending['last_ts'] = ts
                # Цена нужна дашборду, чтобы показать, далеко ли ордеру до
                # заполнения: висящий лимит в двух процентах от рынка и в
                # десятых долях процента — совершенно разные новости.
                pending['last_price'] = close
                touched = True
                self._process_pending(strategy, pair, pending, ts, high, low)

            position = self.positions(strategy).get(pair)
            if position and ts > position['last_ts']:
                position['last_ts'] = ts
                position['last_price'] = close
                touched = True
                self._apply_funding(position, ts, funding_rate)
                self._process_position(strategy, pair, position, ts, high, low, close)
        return touched

    def _process_pending(self, strategy, pair, order, ts, high, low):
        is_long = order['direction'] == 'LONG'
        limit = order['limit_price']

        # Заполнение проверяем ПЕРВЫМ: чтобы цена дошла до инвалидации, она
        # обязана была пройти через лимит, который лежит ближе к рынку.
        filled = (low <= limit) if is_long else (high >= limit)
        if filled:
            self._fill(strategy, pair, order, ts, limit)
            return

        if ts >= order['expires_ts']:
            self._drop_pending(strategy, pair,
                               f"лимит не заполнен за {config.PENDING_ORDER_MAX_HOURS:.0f}ч")
            return

        # Страховка на случай сетапа, у которого уровень инвалидации окажется
        # БЛИЖЕ к рынку, чем лимит. Пока обе стратегии ставят его дальше, и
        # эта ветка не срабатывает: цена не может дойти до инвалидации, не
        # задев по дороге лимит, — а тогда выше уже случился вход.
        inv = order.get('invalidation')
        if inv:
            broken = (low <= inv) if is_long else (high >= inv)
            if broken:
                self._drop_pending(strategy, pair, f"сетап разрушен (${_fmt_p(inv)})")
                return

        target = order['targets'][0]
        gone = (high >= target) if is_long else (low <= target)
        if gone:
            self._drop_pending(strategy, pair, "цена дошла до цели без нас")

    def _drop_pending(self, strategy, pair, reason):
        self.state['pending'][strategy].pop(pair, None)
        log(f"   👻 [{strategy}] {pair}: ордер снят — {reason}")

    def _fill(self, strategy, pair, order, ts, price):
        """Лимит заполнен: превращаем ордер в позицию, списываем комиссию мейкера."""
        self.state['pending'][strategy].pop(pair, None)

        size = order['size']
        fee = size * price * config.PAPER_FEE_MAKER
        trade_id = self.state['next_trade_id']
        self.state['next_trade_id'] = trade_id + 1

        position = {
            'trade_id': trade_id,
            'strategy': strategy,
            'pair': pair,
            'direction': order['direction'],
            'entry_price': price,
            'planned_entry': order['planned_entry'],
            'size': size,
            'initial_size': size,
            'stop_loss': order['stop_loss'],
            'initial_stop': order['stop_loss'],
            'targets': order['targets'],
            'fractions': order['fractions'],
            'be_level': order['be_level'],
            'breakeven_after_tp': order.get('breakeven_after_tp', True),
            'risk_amount': order['risk_amount'],
            'rr': order['rr'],
            'opened_ts': ts,
            'opened_at': _iso(ts),
            'placed_ts': order['placed_ts'],
            # На единицу раньше свечи входа: свеча заполнения обязана быть
            # обработана и как свеча позиции — в ней уже может стоять стоп.
            'last_ts': ts - 1,
            'last_price': price,
            'tp_hit': 0,
            'realized_pnl': 0.0,
            'fees_paid': fee,
            'funding_paid': 0.0,
            'funding_ts': ts,
            'breakeven_set': False,
            'mfe_price': price,
            'mae_price': price,
            'balance_before': order['balance_before'],
            'zone': order['context'].get('zone', '—'),
            'context': order['context'],
        }
        self.state['positions'][strategy][pair] = position
        log(f"   👻 [{strategy}] {pair} {order['direction']}: ВХОД @ ${_fmt_p(price)} "
            f"({int((ts - order['placed_ts']) / 60000)} мин ожидания)")

    def _apply_funding(self, position, ts, rate):
        """Списывает фандинг за каждый пройденный 8-часовой интервал."""
        if not rate:
            position['funding_ts'] = ts
            return
        last = position.get('funding_ts', position['opened_ts'])
        periods = (ts // FUNDING_INTERVAL_MS) - (last // FUNDING_INTERVAL_MS)
        if periods <= 0:
            return
        notional = position['size'] * position['last_price']
        # Положительная ставка: лонги платят шортам.
        sign = 1 if position['direction'] == 'LONG' else -1
        position['funding_paid'] = position.get('funding_paid', 0.0) + sign * rate * notional * periods
        position['funding_ts'] = ts

    def _process_position(self, strategy, pair, pos, ts, high, low, close):
        is_long = pos['direction'] == 'LONG'

        pos['mfe_price'] = max(pos['mfe_price'], high) if is_long else min(pos['mfe_price'], low)
        pos['mae_price'] = min(pos['mae_price'], low) if is_long else max(pos['mae_price'], high)

        max_hold = getattr(config, 'MAX_POSITION_HOLD_HOURS', 0)
        if max_hold and (ts - pos['opened_ts']) / 3_600_000 > max_hold:
            self._close(strategy, pair, pos, ts, close, 'TIME', slip=True)
            return

        # Безубыток — только если его заказала сама стратегия. У SMC он
        # выключен: подтянутый стоп выбивает позицию шумом до дальних целей.
        if pos.get('breakeven_after_tp', True) and not pos['breakeven_set']:
            be = pos.get('be_level')
            if be:
                crossed = (high >= be) if is_long else (low <= be)
                if crossed:
                    pos['stop_loss'] = pos['entry_price']
                    pos['breakeven_set'] = True

        # Стоп проверяем РАНЬШЕ тейка: порядок событий внутри свечи по OHLC
        # неизвестен, и трактовка в свою пользу завышает результат.
        stop = pos['stop_loss']
        stopped = (low <= stop) if is_long else (high >= stop)
        if stopped:
            self._close(strategy, pair, pos, ts, stop, 'BE' if pos['breakeven_set'] else 'SL',
                        slip=True)
            return

        while pos['tp_hit'] < len(pos['targets']):
            level = pos['targets'][pos['tp_hit']]
            reached = (high >= level) if is_long else (low <= level)
            if not reached:
                break
            index = pos['tp_hit']
            if index < len(pos['targets']) - 1:
                self._take_partial(pos, index, level)
                if pos.get('breakeven_after_tp', True) and not pos['breakeven_set']:
                    pos['stop_loss'] = pos['entry_price']
                    pos['breakeven_set'] = True
            else:
                self._close(strategy, pair, pos, ts, level, f'TP{index + 1}', slip=False)
                return

    def _take_partial(self, pos, index, level):
        portion = min(pos['size'], pos['initial_size'] * pos['fractions'][index])
        sign = 1 if pos['direction'] == 'LONG' else -1
        pos['realized_pnl'] += sign * (level - pos['entry_price']) * portion
        pos['fees_paid'] += portion * level * config.PAPER_FEE_TAKER
        pos['size'] = max(0.0, pos['size'] - portion)
        pos['tp_hit'] = index + 1
        log(f"   👻 [{pos['strategy']}] {pos['pair']}: TP{index + 1} @ ${_fmt_p(level)}, "
            f"закрыто {pos['fractions'][index] * 100:.0f}%")

    def _close(self, strategy, pair, pos, ts, price, reason, slip):
        """Закрывает остаток позиции и пишет сделку в журнал."""
        is_long = pos['direction'] == 'LONG'
        exit_price = price
        if slip and config.PAPER_SLIPPAGE_PCT:
            # Проскальзывание всегда против нас — на стопе рынок уже бежит.
            move = price * config.PAPER_SLIPPAGE_PCT
            exit_price = price - move if is_long else price + move

        sign = 1 if is_long else -1
        remaining = pos['size']
        gross = pos['realized_pnl'] + sign * (exit_price - pos['entry_price']) * remaining
        fees = pos['fees_paid'] + remaining * exit_price * config.PAPER_FEE_TAKER
        funding = pos.get('funding_paid', 0.0)
        net = gross - fees - funding

        balance_before = self.balance(strategy)
        balance_after = balance_before + net
        self.state['balance'][strategy] = balance_after
        self.state['positions'][strategy].pop(pair, None)
        self.state['cooldown'][strategy][pair] = ts

        self.daily_pnl += net
        self.trade_history.append({'pnl': net, 'exit_time': datetime.now(), 'pair': pair,
                                   'strategy': strategy})

        row = self._journal_row(pos, ts, exit_price, reason, gross, fees, funding, net,
                                balance_before, balance_after)
        _write_journal(row)

        icon = '🟢' if net > 0 else ('⚪' if net == 0 else '🔴')
        log(f"   👻 [{strategy}] {pair}: {icon} {reason} @ ${_fmt_p(exit_price)} | "
            f"${net:+.2f} ({net / pos['risk_amount']:+.2f}R) | депозит ${balance_after:,.2f}")

    def _journal_row(self, pos, ts, exit_price, reason, gross, fees, funding, net,
                     balance_before, balance_after):
        ctx = pos.get('context', {})
        sl_dist = abs(pos['entry_price'] - pos['initial_stop'])
        sign = 1 if pos['direction'] == 'LONG' else -1
        targets = pos['targets']
        factors = ctx.get('factors')

        row = {
            'trade_id': pos['trade_id'],
            'strategy': pos['strategy'],
            'pair': pos['pair'],
            'direction': pos['direction'],
            'zone': pos.get('zone', '—'),
            'htf_trend': ctx.get('htf_trend', '—'),
            'open_time': pos['opened_at'],
            'entry_price': round(pos['entry_price'], 8),
            'planned_entry': round(pos['planned_entry'], 8),
            'entry_wait_min': int((pos['opened_ts'] - pos['placed_ts']) / 60000),
            'stop_loss': round(pos['initial_stop'], 8),
            'tp1': round(targets[0], 8),
            'tp2': round(targets[1], 8) if len(targets) > 1 else '',
            'rr': round(pos['rr'], 3),
            'risk_usd': round(pos['risk_amount'], 2),
            'position_size': round(pos['initial_size'], 8),
            'notional_usd': round(pos['initial_size'] * pos['entry_price'], 2),
            'leverage_eff': (round(pos['initial_size'] * pos['entry_price'] / balance_before, 2)
                             if balance_before else ''),
            'close_time': _iso(ts),
            'exit_price': round(exit_price, 8),
            'exit_reason': reason,
            'tps_hit': pos['tp_hit'],
            'duration_min': int((ts - pos['opened_ts']) / 60000),
            'gross_pnl_usd': round(gross, 4),
            'fees_usd': round(fees, 4),
            'funding_usd': round(funding, 4),
            'pnl_usd': round(net, 4),
            'pnl_r': round(net / pos['risk_amount'], 3) if pos['risk_amount'] else 0,
            'pnl_pct': round(net / balance_before * 100, 3) if balance_before else 0,
            'balance_before': round(balance_before, 2),
            'balance_after': round(balance_after, 2),
            'result': 'WIN' if net > 0 else ('LOSS' if net < 0 else 'BREAKEVEN'),
            'mfe_price': round(pos['mfe_price'], 8),
            'mae_price': round(pos['mae_price'], 8),
            'mfe_r': round(sign * (pos['mfe_price'] - pos['entry_price']) / sl_dist, 3) if sl_dist else '',
            'mae_r': round(sign * (pos['mae_price'] - pos['entry_price']) / sl_dist, 3) if sl_dist else '',
            'breakeven_set': pos['breakeven_set'],
            'why': ctx.get('why', ''),
            'exit_reason_ru': glossary.exit_reason(reason),
            'confirmed_ru': '; '.join(ctx.get('confirmed') or []),
            'missing_ru': '; '.join(ctx.get('missing') or []),
            'confluence': ctx.get('confluence', ''),
            'poi_type': ctx.get('poi_type', ''),
            'factors': ', '.join(factors) if isinstance(factors, (list, tuple)) else (factors or ''),
            'sweep': ctx.get('sweep', ''),
            'impulse_pct': ctx.get('impulse_pct', ''),
            'score': round(ctx['score'], 1) if isinstance(ctx.get('score'), (int, float)) else '',
            'proximity': ctx.get('proximity', ''),
            'htf_strength': ctx.get('htf_strength', ''),
        }
        return row

    # ── Действия оператора ───────────────────────────────────────────────────

    def cancel_pending(self, strategy, pair):
        """Снимает ожидающий ордер. Возвращает (получилось, сообщение)."""
        pair = _norm(pair)
        if pair not in self.pending(strategy):
            return False, f'{pair}: ожидающего ордера нет'
        self.state['pending'][strategy].pop(pair, None)
        self._save_state()
        log(f"🖐 [{strategy}] {pair}: ордер снят оператором")
        return True, f'{pair}: ордер снят'

    def move_to_breakeven(self, strategy, pair):
        """
        Переносит стоп во вход.

        Действие только снижающее риск: стоп двигается ТОЛЬКО в сторону входа
        и никогда от него. Отодвинуть стоп дальше через дашборд нельзя — это
        увеличение риска, и странице без пароля такого делать не положено.
        """
        pair = _norm(pair)
        pos = self.positions(strategy).get(pair)
        if not pos:
            return False, f'{pair}: позиции нет'

        entry = pos['entry_price']
        is_long = pos['direction'] == 'LONG'
        # Двигаем, только если это ужимает риск
        if (is_long and pos['stop_loss'] >= entry) or (not is_long and pos['stop_loss'] <= entry):
            return False, f'{pair}: стоп уже в безубытке или ближе'

        pos['stop_loss'] = entry
        pos['breakeven_set'] = True
        self._save_state()
        log(f"🖐 [{strategy}] {pair}: стоп переведён в безубыток оператором")
        return True, f'{pair}: стоп в безубытке'

    def close_one(self, strategy, pair):
        """Закрывает одну фантомную позицию по текущей цене."""
        pair = _norm(pair)
        pos = self.positions(strategy).get(pair)
        if not pos:
            return False, f'{pair}: позиции нет'
        price = pos.get('last_price') or pos['entry_price']
        self._close(strategy, pair, pos, _now_ms(), price, 'MANUAL', slip=True)
        self._save_state()
        return True, f'{pair}: позиция закрыта'

    def close_all(self, strategy):
        """Закрывает все позиции стратегии и снимает её ордера."""
        closed = 0
        for pair in list(self.positions(strategy)):
            if self.close_one(strategy, pair)[0]:
                closed += 1
        cancelled = len(self.pending(strategy))
        self.state['pending'][strategy] = {}
        self._save_state()
        log(f"🖐 [{strategy}]: закрыто позиций {closed}, снято ордеров {cancelled}")
        return True, f'{strategy}: закрыто {closed}, снято {cancelled}'

    # ── Ручное закрытие (Telegram /close) ────────────────────────────────────

    def close_position_by_pair(self, trading_pair):
        """Закрывает фантомную позицию по паре во ВСЕХ стратегиях, что её держат."""
        key = _norm(trading_pair).split(' ')[0]
        closed, price = False, 0.0
        for strategy in self.strategies:
            pos = self.positions(strategy).get(key)
            if not pos:
                continue
            price = pos.get('last_price') or pos['entry_price']
            self._close(strategy, key, pos, _now_ms(), price, 'MANUAL', slip=True)
            closed = True
        if closed:
            self._save_state()
        return closed, price

    # ── Сводка для дашборда ──────────────────────────────────────────────────

    def snapshot(self):
        """Живое состояние счетов и открытых позиций (без обращений к бирже)."""
        out = {'started_at': self.state.get('started_at'), 'strategies': {},
               'open': [], 'pending': []}
        for strategy in self.strategies:
            start = self.start_balance(strategy)
            equity = self.equity(strategy)
            out['strategies'][strategy] = {
                'start_balance': round(start, 2),
                'balance': round(self.balance(strategy), 2),
                'equity': round(equity, 2),
                'return_pct': round((equity - start) / start * 100, 2) if start else 0.0,
                'pending': len(self.pending(strategy)),
                # Сделки до этой отметки в статистику стратегии не входят
                'reset_at': self.reset_at(strategy),
            }
            for pair, pos in self.positions(strategy).items():
                out['open'].append(self._position_view(strategy, pair, pos))
            for pair, order in self.pending(strategy).items():
                out['pending'].append(self._pending_view(strategy, pair, order))

        out['open'].sort(key=lambda item: item['opened'])
        out['pending'].sort(key=lambda item: item['opened'])
        return out

    def _position_view(self, strategy, pair, pos):
        """Открытая позиция для дашборда: что зафиксировано, что ещё в рынке."""
        price = pos.get('last_price') or pos['entry_price']
        risk = pos['risk_amount'] or 1
        sign = 1 if pos['direction'] == 'LONG' else -1

        realized = pos.get('realized_pnl', 0.0)
        floating = sign * (price - pos['entry_price']) * pos['size']
        costs = pos.get('fees_paid', 0.0) + pos.get('funding_paid', 0.0)
        total = realized + floating - costs

        targets = pos['targets']
        final = targets[-1]
        # Где цена между стопом и последней целью: 0 — у стопа, 1 — у цели.
        span = abs(final - pos['stop_loss'])
        progress = (abs(price - pos['stop_loss']) / span) if span else 0.0

        return {
            'strategy': strategy,
            'pair': pair,
            'direction': pos['direction'],
            'zone': pos.get('zone', '—'),
            'entry': pos['entry_price'],
            'price': price,
            'stop': pos['stop_loss'],
            'targets': list(targets),
            'fractions': list(pos.get('fractions') or []),
            'tp1': targets[0],
            'tp2': targets[1] if len(targets) > 1 else None,
            'rr': pos['rr'],
            'risk': risk,
            'size': pos['size'],
            'size_left_pct': (round(pos['size'] / pos['initial_size'] * 100, 1)
                              if pos.get('initial_size') else 100.0),
            'opened': pos['opened_at'],
            'tp_hit': pos.get('tp_hit', 0),
            'breakeven': pos.get('breakeven_set', False),
            'realized': round(realized, 2),
            'floating': round(floating, 2),
            'costs': round(costs, 2),
            'unrealised': round(total, 2),
            'unrealised_r': round(total / risk, 2),
            'progress': round(min(max(progress, 0.0), 1.0), 4),
            'mfe_r': round(sign * (pos['mfe_price'] - pos['entry_price'])
                           / abs(pos['entry_price'] - pos['initial_stop']), 2)
                     if pos['entry_price'] != pos['initial_stop'] else 0,
            'why': (pos.get('context') or {}).get('why', ''),
            'confirmed': (pos.get('context') or {}).get('confirmed', []),
            'missing': (pos.get('context') or {}).get('missing', []),
        }

    def _pending_view(self, strategy, pair, order):
        """
        Ордер, ожидающий активации.

        Главное здесь — далеко ли цене до лимита и сколько ордеру осталось
        жить: висящий сутками лимит в трёх процентах от рынка почти наверняка
        истечёт, и это видно только по этим двум числам.
        """
        price = order.get('last_price')
        limit = order['limit_price']
        now = _now_ms()
        return {
            'strategy': strategy,
            'pair': pair,
            'direction': order['direction'],
            'zone': (order.get('context') or {}).get('zone', '—'),
            'entry': limit,
            'price': price,
            'distance_pct': (round(abs(limit - price) / price * 100, 2)
                             if price else None),
            'stop': order['stop_loss'],
            'targets': list(order['targets']),
            'tp1': order['targets'][0],
            'rr': order['rr'],
            'risk': order['risk_amount'],
            'invalidation': order.get('invalidation'),
            'opened': order['placed_at'],
            'waiting_min': int((now - order['placed_ts']) / 60000),
            'expires_in_min': max(0, int((order['expires_ts'] - now) / 60000)),
            'pending': True,
            'why': (order.get('context') or {}).get('why', ''),
            'confirmed': (order.get('context') or {}).get('confirmed', []),
            'missing': (order.get('context') or {}).get('missing', []),
        }


# ── Журнал ───────────────────────────────────────────────────────────────────

def _write_journal(row):
    """Пишет сделку в CSV (для Excel) и в JSONL (полный дамп)."""
    try:
        fresh = not os.path.exists(JOURNAL_CSV) or os.path.getsize(JOURNAL_CSV) == 0
        with open(JOURNAL_CSV, 'a', encoding='utf-8', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction='ignore')
            if fresh:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        log(f"⚠️ Не удалось записать фантомную сделку в CSV: {exc}")
    try:
        with open(JOURNAL_JSON, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass


def read_journal(path=None):
    """Все закрытые фантомные сделки (список словарей)."""
    path = path or JOURNAL_CSV
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except Exception as exc:
        log(f"⚠️ Не читается журнал фантомных сделок: {exc}")
        return []
