"""
PlatformManager — главный мульти-тенант цикл (копи-трейдинг по подписке).

Один проход цикла:
  1. refresh_accounts(): обновить реестр активных юзеров (подписка/триал + ключи).
  2. Для каждого активного юзера: вести его позиции и pending-ордера (его ключами).
  3. Сканировать рынок ОДИН раз (общий keyless market-client) → список кандидатов.
  4. Для каждого кандидата построить сигнал ОДИН раз и исполнить на счёте каждого
     активного + не-на-паузе юзера, у кого есть свободный слот и нет этой пары.

Размер позиции считается индивидуально в LiveTradeManager.execute_trade по балансу
каждого юзера. Сбой одного юзера изолирован try/except и не роняет общий цикл.
"""

import os
import json
import traceback
from datetime import datetime, timezone

import config
import db
import telegram_notify as tg
from exchange import make_market_client
from pair_scanner import get_liquid_pairs, scan_for_setups
from strategy import analyze_market
from user_account import UserAccount
from logger import log

# Номинальный баланс для расчёта геометрии сигнала (rr-гейт от баланса не зависит;
# реальный размер позиции пересчитывается в execute_trade по балансу юзера).
_NOMINAL_BALANCE = 10000.0


class _ScanCtx:
    """Заглушка trade_manager для рыночного скана: кулдаун нейтрален.

    Кулдаун — персональный (проверяется в execute_trade каждого юзера), поэтому
    общий скан рынка не должен отбрасывать пары по чьему-то одному кулдауну.
    """
    def check_cooldown(self, pair):
        return True


def _user_signal(sig: dict) -> dict:
    """Личная копия общего сигнала под одного юзера (params изолируем от мутаций)."""
    s = dict(sig)
    s['params'] = dict(sig['params'])
    return s


def _fmt_until_short(dt) -> str:
    try:
        return dt.astimezone(timezone.utc).strftime('%d.%m.%Y')
    except Exception:
        return str(dt)


class PlatformManager:
    def __init__(self):
        db.init_db()
        self.accounts = {}     # telegram_id -> UserAccount (живут между циклами)
        self._fingerprints = {}  # telegram_id -> (exchange, api_key_enc) для детекта смены ключей
        self.market_client = make_market_client(config.EXCHANGE_NAME)

    # ── Реестр юзеров (активные + «manage-only» с открытыми позициями) ────────
    def refresh_accounts(self):
        """Синхронизирует реестр UserAccount с БД.

        В реестр попадают: (1) активные подписчики с ключами и (2) истёкшие/забаненные,
        у кого ОСТАЛИСЬ открытые позиции — их ведём до TP/SL, но новые сделки им не
        открываем (acc.active=False). Без доступа и без позиций — в реестр не берём.
        """
        keep_ids = set()

        for u in db.list_users():
            tid = u['telegram_id']
            if not db.has_keys(u):
                continue
            active = db.is_active(u)
            if not active and not self._has_open_positions(tid):
                continue  # нет доступа и нечего доводить — пропускаем
            keep_ids.add(tid)

            fp = (u.get('exchange'), u.get('api_key_enc'))
            cached = self.accounts.get(tid)
            if cached is None or self._fingerprints.get(tid) != fp:
                acc = UserAccount(u)
                if acc.ok:
                    acc.active = active
                    self.accounts[tid] = acc
                    self._fingerprints[tid] = fp
                else:
                    keep_ids.discard(tid)
                    self.accounts.pop(tid, None)
                    self._fingerprints.pop(tid, None)
            else:
                cached.active = active   # обновляем статус подписки

        # Убираем тех, кого больше не ведём (нет доступа и нет открытых позиций)
        for tid in list(self.accounts):
            if tid not in keep_ids:
                self.accounts.pop(tid, None)
                self._fingerprints.pop(tid, None)

    @staticmethod
    def _has_open_positions(telegram_id) -> bool:
        """Есть ли у юзера сохранённые открытые позиции (state/<id>/positions_state.json)."""
        path = os.path.join(os.path.dirname(__file__), 'state', str(telegram_id),
                            'positions_state.json')
        try:
            with open(path, 'r') as f:
                return bool(json.load(f))
        except Exception:
            return False

    # ── Напоминания об окончании подписки ─────────────────────────────────────
    def check_expiries(self):
        """Раз в цикл: напоминание за N дней до конца и уведомление в момент истечения.
        Идемпотентность — через users.last_reminded_at (не чаще раза в ~сутки)."""
        reminder_days = db.get_int_setting('expiry_reminder_days', 3)
        now = datetime.now(timezone.utc)
        for u in db.list_users():
            if not db.has_keys(u) or u.get('status') == 'banned':
                continue
            until = db.access_until(u)
            if not until:
                continue
            tid  = u['telegram_id']
            last = db._parse(u.get('last_reminded_at'))
            try:
                days_left = (until - now).days
            except Exception:
                continue

            if until > now and 0 <= days_left <= reminder_days:
                # Напоминаем не чаще раза в ~23ч
                if last is None or (now - last).total_seconds() > 23 * 3600:
                    tg.subscription_expiring(days_left, _fmt_until_short(until), telegram_id=tid)
                    db.set_user_field(tid, 'last_reminded_at', now.isoformat())
            elif until <= now:
                # Один раз после фактического истечения
                if last is None or last < until:
                    tg.subscription_expired(telegram_id=tid)
                    db.set_user_field(tid, 'last_reminded_at', now.isoformat())

    # ── Рыночный скан (один раз за цикл) ──────────────────────────────────────
    def scan_market(self):
        """Ищет сетапы по общему пулу пар через keyless market-client. Список кандидатов."""
        try:
            liquid = get_liquid_pairs(self.market_client)
        except Exception as e:
            log(f"⚠️ Ошибка получения ликвидных пар: {e}")
            return []
        if not liquid:
            return []
        try:
            return scan_for_setups(liquid, _ScanCtx(), client=self.market_client)
        except Exception as e:
            log(f"⚠️ Ошибка скана сетапов: {e}")
            return []

    # ── Один проход главного цикла ────────────────────────────────────────────
    def cycle(self):
        self.refresh_accounts()
        # Напоминания об окончании подписки (идемпотентно)
        try:
            self.check_expiries()
        except Exception as e:
            log(f"⚠️ Ошибка проверки подписок: {e}")

        total = len(self.accounts)
        n_active = sum(1 for a in self.accounts.values() if a.active)
        log(f"\n👥 В реестре: {total} (активных: {n_active}, ведём-позиции: {total - n_active})")
        if total == 0:
            return

        # 1. Ведение позиций и pending КАЖДОГО юзера в реестре (включая истёкших с позициями)
        for acc in list(self.accounts.values()):
            try:
                acc.manage()
            except Exception as e:
                log(f"⚠️ Юзер {acc.telegram_id}: ошибка ведения позиций — {e}")
                log(traceback.format_exc())

        # Новые входы возможны только если есть хоть один активный подписчик
        if n_active == 0:
            return

        # 2. Скан рынка ОДИН раз
        candidates = self.scan_market()
        log(f"🔍 Кандидатов после скана: {len(candidates)}")
        if not candidates:
            return

        # 3. Сигнал на кандидата строится один раз, исполняется на каждом подходящем счёте
        for cand in candidates:
            pair = cand['pair']
            try:
                signal = analyze_market(cand['df_1h'], None, pair, _NOMINAL_BALANCE)
            except Exception as e:
                log(f"⚠️ {pair}: ошибка построения сигнала — {e}")
                continue
            if not signal:
                continue
            signal['htf_trend'] = cand.get('htf_trend', 'NEUTRAL')

            for acc in self.accounts.values():
                try:
                    if not acc.active:        # истёкшая подписка — новые сделки не открываем
                        continue
                    if acc.is_paused():
                        continue
                    if not acc.has_free_slot():
                        continue
                    if acc.holds_pair(pair):
                        continue
                    acc.try_enter(_user_signal(signal), df_1h=cand['df_1h'])
                except Exception as e:
                    log(f"⚠️ Юзер {acc.telegram_id}: ошибка входа {pair} — {e}")
                    log(traceback.format_exc())

    # ── Удобные геттеры для команд Telegram (phase B) ─────────────────────────
    def get_account(self, telegram_id: int):
        return self.accounts.get(telegram_id)
