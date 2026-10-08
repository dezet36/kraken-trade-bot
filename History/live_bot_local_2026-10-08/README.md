# Локальные данные Live_Bot, убранные из папки кода 08.10.2026

Реорганизация проекта, этап 0 (docs/Архитектура_модули_2026-10-08.md). Здесь —
файлы, которые лежали в `Live_Bot/` рядом с кодом и не использовались: бот на
сервере пишет данные в `/opt/kraken/bot_data`, локально бот не запускается
(memory: на ПК только KrakenRemote).

- данные бумажного счёта, журналы и состояние локального запуска за август 2026
  (`paper_*`, `refused.csv`, `runtime_settings.json`, `settings_history.jsonl`,
  `positions_state.json`, `pending_orders.json`, `cooldown_state.json`, …);
- `positioning/` — локальный сбор позиционирования за август;
- `polymarket_data/` — данные прошлого проекта Polymarket (маркет-мейкер); код
  проекта удалён раньше;
- `app_window/`, `window_profile/`, `window.json`, `running_app.json` — профиль
  окна старого локального приложения (удалено 20.09.2026);
- `bot_log.txt`, `remote.log`, `selftest.log`, `errors.json`, `leak_trace.txt` —
  старые журналы.

Вернуть любой файл — перенести обратно в `Live_Bot/`.
