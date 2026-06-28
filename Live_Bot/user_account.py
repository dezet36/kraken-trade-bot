"""
UserAccount — обёртка над одним пользователем платформы.

Связывает запись из БД (telegram_id, биржа, зашифрованные ключи) с:
  - ccxt-клиентом под его ключи (exchange.make_client, ключи дешифруются в памяти);
  - персональным LiveTradeManager (своё состояние позиций/pending/cooldown в state/<id>/,
    журнал сделок пишется в БД по telegram_id).

Экземпляр живёт между циклами в реестре PlatformManager — позиции и pending
хранятся в памяти менеджера, поэтому пересоздавать на каждый цикл не нужно.
"""

import config
import db
import crypto_security as crypto
from exchange import make_client
from trade_manager import LiveTradeManager
from logger import log


class UserAccount:
    def __init__(self, user: dict):
        self.telegram_id   = user['telegram_id']
        self.username      = user.get('username')
        self.exchange_name = (user.get('exchange') or 'bybit').lower()
        self.manager       = None
        self.error         = None
        # Подписка активна (обновляется PlatformManager.refresh_accounts на каждом цикле).
        # active=False -> «manage-only»: ведём открытые позиции, но новые НЕ открываем.
        self.active        = True
        self._build(user)

    def _build(self, user: dict):
        """Дешифрует ключи и строит exchange-клиент + персональный менеджер."""
        try:
            api_key    = crypto.decrypt(user.get('api_key_enc') or '')
            api_secret = crypto.decrypt(user.get('api_secret_enc') or '')
            if not api_key or not api_secret:
                self.error = "нет API-ключей"
                return
            client = make_client(self.exchange_name, api_key, api_secret, config.TRADING_MODE)
            self.manager = LiveTradeManager(exchange_client=client, telegram_id=self.telegram_id)
            log(f"👤 UserAccount {self.telegram_id} ({self.exchange_name}) готов")
        except Exception as e:
            self.error = str(e)
            log(f"❌ UserAccount {self.telegram_id}: не удалось построить — {e}")

    @property
    def ok(self) -> bool:
        return self.manager is not None

    # ── Свежие флаги из БД (меняются командами юзера/админа между циклами) ─────
    def is_paused(self) -> bool:
        u = db.get_user(self.telegram_id)
        return bool(u and u.get('paused'))

    def has_free_slot(self) -> bool:
        """Есть ли свободный слот под новую сделку (с учётом open + pending)."""
        if not self.ok:
            return False
        return self.manager.get_active_count() < config.MAX_ACTIVE_PAIRS

    def holds_pair(self, pair: str) -> bool:
        """Уже есть открытая/ожидающая позиция по паре."""
        return self.ok and pair in self.manager.get_open_pairs()

    # ── Делегаты для управления (используются циклом и командами Telegram) ────
    def manage(self):
        """Ведёт открытые позиции и проверяет pending-ордера на счёте юзера."""
        if not self.ok:
            return
        self.manager.manage_active_positions()
        self.manager.check_pending_orders()

    def try_enter(self, signal: dict, df_1h=None) -> bool:
        """Пробует открыть сделку по общему сигналу на счёте этого юзера."""
        if not self.ok:
            return False
        return self.manager.execute_trade(signal, df_1h=df_1h)

    def stats(self):
        return self.manager.get_stats_dict() if self.ok else None
