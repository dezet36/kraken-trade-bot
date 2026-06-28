"""
Точка входа МУЛЬТИ-ТЕНАНТ платформы (копи-трейдинг по подписке).

Гоняет PlatformManager.cycle() по расписанию: ведёт позиции всех активных
подписчиков, сканирует рынок один раз за цикл и исполняет общий сигнал на счёте
каждого активного юзера его ключами и по его балансу.

Legacy одно-юзерный бот (`bot.py`) остаётся рабочим до завершения онбординга (фаза B).
Запуск платформы: `python platform_bot.py`.

Требует переменные окружения:
  PLATFORM_SECRET_KEY — Fernet-ключ для дешифровки API-ключей юзеров (обязателен).
  PLATFORM_DB         — путь к SQLite (по умолчанию Live_Bot/platform.db).
"""

import os
import sys
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler

import config
import db
from platform_manager import PlatformManager
from logger import log

CYCLE_MINUTES = int(os.getenv('PLATFORM_CYCLE_MINUTES', 5))

platform = None


def _check_secret_key():
    """Fail-fast: без мастер-ключа ключи юзеров не расшифровать — запуск бессмыслен."""
    if not os.getenv('PLATFORM_SECRET_KEY'):
        log("❌ PLATFORM_SECRET_KEY не задан — платформа не может расшифровать ключи юзеров.")
        log("   Сгенерируй: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"")
        sys.exit(1)


def platform_cycle():
    """Один проход главного цикла; ошибка цикла не должна ронять планировщик."""
    log("\n" + "=" * 60)
    log(f"ПЛАТФОРМА — ЦИКЛ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)
    try:
        platform.cycle()
    except Exception as e:
        import traceback
        log(f"❌ Ошибка цикла платформы: {e}")
        log(traceback.format_exc())


def main():
    global platform

    _check_secret_key()

    log("=" * 60)
    mode_label = "LIVE" if config.TRADING_MODE == 'LIVE' else "DEMO"
    log(f"ПЛАТФОРМА (мульти-тенант) — {mode_label}")
    log("=" * 60)

    db.init_db()
    platform = PlatformManager()

    n_users  = len(db.list_users())
    n_active = len(db.list_active_users())
    log(f"\nПул пар:        {len(config.TRADING_PAIRS_POOL)} пар")
    log(f"Макс. позиций:  {config.MAX_ACTIVE_PAIRS} (на юзера)")
    log(f"Риск/сделка:    {config.RISK_PER_TRADE}% (на юзера)")
    log(f"Триал:          {db.get_int_setting('trial_days', 7)} дн.")
    log(f"Подписка:       ${db.get_int_setting('sub_price_usd', 30)} / "
        f"{db.get_int_setting('sub_days', 30)} дн.")
    log(f"Пользователей:  {n_users} (активных с ключами: {n_active})")
    log(f"Интервал цикла: {CYCLE_MINUTES} мин")

    scheduler = BlockingScheduler()
    scheduler.add_job(
        platform_cycle, 'interval', minutes=CYCLE_MINUTES, next_run_time=datetime.now()
    )

    log("\nПлатформа запущена! Ctrl+C для остановки.\n")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        log("\n\nПлатформа остановлена пользователем")


if __name__ == "__main__":
    main()
