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

import traceback

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


class PlatformManager:
    def __init__(self):
        db.init_db()
        self.accounts = {}     # telegram_id -> UserAccount (живут между циклами)
        self._fingerprints = {}  # telegram_id -> (exchange, api_key_enc) для детекта смены ключей
        self.market_client = make_market_client(config.EXCHANGE_NAME)

    # ── Реестр активных юзеров ────────────────────────────────────────────────
    def refresh_accounts(self):
        """Синхронизирует реестр UserAccount со списком активных подписчиков из БД."""
        active = db.list_active_users()
        active_ids = set()

        for u in active:
            tid = u['telegram_id']
            active_ids.add(tid)
            fp = (u.get('exchange'), u.get('api_key_enc'))
            cached = self.accounts.get(tid)

            # Пересоздаём, если юзера ещё нет или он переподключил биржу/ключи
            if cached is None or self._fingerprints.get(tid) != fp:
                acc = UserAccount(u)
                if acc.ok:
                    self.accounts[tid] = acc
                    self._fingerprints[tid] = fp
                else:
                    # Не смогли построить клиент — выкидываем из активных
                    self.accounts.pop(tid, None)
                    self._fingerprints.pop(tid, None)

        # Убираем тех, кто больше не активен (истёк/забанен/отключил ключи)
        for tid in list(self.accounts):
            if tid not in active_ids:
                self.accounts.pop(tid, None)
                self._fingerprints.pop(tid, None)

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
        n_active = len(self.accounts)
        log(f"\n👥 Активных подписчиков с ключами: {n_active}")
        if n_active == 0:
            return

        # 1. Ведение позиций и pending каждого юзера (его ключами)
        for acc in list(self.accounts.values()):
            try:
                acc.manage()
            except Exception as e:
                log(f"⚠️ Юзер {acc.telegram_id}: ошибка ведения позиций — {e}")
                log(traceback.format_exc())

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
