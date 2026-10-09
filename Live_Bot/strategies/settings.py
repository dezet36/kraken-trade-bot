"""
Настройки оператора, которые принадлежат СТРАТЕГИЯМ (реорганизация, этап 2,
08.10.2026): правила решений, а не деньги.

    min_stop_pct  минимальная дистанция стопа, % цены — фильтр сетапа у ФИБО и
                  SMC (strategy_profile.MIN_STOP_KNOB_READERS): тесная структура
                  не окупает шум и комиссии, такой сетап не берётся;
    critic        второе мнение о плане модели (ИИ).

Деньги — риск на сделку, депозит, стороны, пределы позиций — у счёта
(accounts/paper.py), стратегия их не знает.

Значения лежат в runtime_settings.json (каталог данных) в разделах стратегий.
Пишет их панель и Telegram через settings_store; здесь — правила полей и
чтение. Файл перечитывается, только если изменился: правка с панели действует
со следующего сетапа, без перезапуска. Нет файла или он нечитаем — значения по
умолчанию: торговля от файла настроек не зависит.
"""

import json
import os
import threading

FILE_NAME = 'runtime_settings.json'
FIELDS = ('min_stop_pct', 'critic')

# Границы разумного — их же проверяет панель при записи.
LIMITS = {'min_stop_pct': (0.1, 20.0)}

_lock = threading.Lock()
_cache = {'key': None, 'data': {}}


def path():
    # config — заново при каждом вызове: тесты перезагружают его, и каталог
    # данных, схваченный при импорте, был бы чужим.
    from infra import config
    return os.path.join(config.DATA_DIR, FILE_NAME)


def defaults():
    """Поля стратегии, когда оператор их не задавал."""
    from infra import config
    return {'min_stop_pct': round(float(config.MIN_SL_PERCENT) * 100, 3), 'critic': True}


def clamp(field, value, fallback):
    low, high = LIMITS[field]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    if value != value:                       # NaN
        return fallback
    return min(max(value, low), high)


def section(item, base=None):
    """Поля стратегии из записанного раздела поверх base (по умолчанию —
    defaults()). Мусор в поле не принимается: остаётся прежнее значение."""
    out = dict(defaults() if base is None else base)
    item = item if isinstance(item, dict) else {}
    if 'min_stop_pct' in item:
        out['min_stop_pct'] = clamp('min_stop_pct', item['min_stop_pct'], out['min_stop_pct'])
    if 'critic' in item:
        out['critic'] = bool(item['critic'])
    return out


def _stored():
    """Записанные разделы; {} — файла нет или он нечитаем."""
    p = path()
    try:
        stamp = os.path.getmtime(p)
    except OSError:
        return {}
    key = (p, stamp)
    with _lock:
        if _cache['key'] == key:
            return _cache['data']
        try:
            with open(p, encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        _cache['key'], _cache['data'] = key, data
        return data


def min_stop_pct(strategy):
    """Минимальная дистанция стопа в ДОЛЯХ цены (0.008 = 0.8%)."""
    try:
        from strategies import registry
        if registry.get(strategy) is None:
            from infra import config
            return float(config.MIN_SL_PERCENT)
        return float(section(_stored().get(strategy))['min_stop_pct']) / 100.0
    except Exception:                              # noqa: BLE001
        from infra import config
        return float(config.MIN_SL_PERCENT)


def critic_enabled():
    """
    Второе мнение о плане модели: включено ли оператором. Тумблер на панели —
    на время; .env (config.LLM_CRITIC) выключает жёстко — это проверяет
    llm_decide.critic_enabled.
    """
    try:
        return bool(section(_stored().get('LLM'))['critic'])
    except Exception:                              # noqa: BLE001
        from infra import config
        return bool(getattr(config, 'LLM_CRITIC', True))
