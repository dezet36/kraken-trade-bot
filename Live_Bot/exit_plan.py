"""
План выхода из позиции: какие цели и какими долями закрывать.

Отдельный модуль, потому что план нужен двум исполнителям — боевому
trade_manager и фантомному paper_broker, — а тянуть один в другой нельзя:
paper_broker намеренно не содержит и не импортирует код, умеющий отправлять
ордера на биржу.

Почему план вообще стал данными, а не настройкой. Раньше доли закрытия брались
из глобального `config.TP_CLOSE_FRACTIONS`, откалиброванного под фибо-стратегию
(одна цель, 100%). SMC при этом рассчитывает три цели с долями 25/25/50, и её
план молча подменялся чужим: позиция закрывалась целиком на первой цели. По
бэктесту это ровно та конфигурация, которая уходит в минус (−9.1% против
+40.6% на трёх целях) — основную прибыль SMC приносят дальние цели при
винрейте около 25%. С двумя одновременно работающими стратегиями одна
глобальная настройка обслуживать обе не может в принципе.
"""

import config

# Запасной план — только для сигналов без своего (старые pending-ордера,
# позиции, восстановленные из состояния предыдущей версии бота).
DEFAULT_FRACTIONS = list(getattr(config, 'TP_CLOSE_FRACTIONS', [1.0]))
LEGACY_TP_KEYS = ['take_profit_1', 'take_profit_2'][:len(DEFAULT_FRACTIONS)]


def tp_plan(params):
    """
    Возвращает (цели, доли) для конкретной позиции.

    Доли всегда суммируются ровно в 1.0: иначе часть позиции либо останется
    висеть после последней цели, либо будет закрыта дважды.
    """
    targets = [float(t) for t in (params.get('tp_targets') or [])]
    fractions = [float(f) for f in (params.get('tp_fractions') or [])]

    if not targets:
        targets = [float(params[key]) for key in LEGACY_TP_KEYS
                   if params.get(key) is not None]
        fractions = list(DEFAULT_FRACTIONS)

    if not targets:
        return [], []

    if fractions:
        targets = targets[:len(fractions)]
    fractions = fractions[:len(targets)] or [1.0]
    fractions[-1] += 1.0 - sum(fractions)
    return targets, fractions


def wants_breakeven(params):
    """Переносить ли стоп в безубыток. У SMC безубыток выключен намеренно."""
    return bool(params.get('breakeven_after_tp',
                           getattr(config, 'BREAKEVEN_AT_B', True)))


def direction_cap(params):
    """
    Максимум позиций стратегии в одну сторону (0 = без ограничения).

    Тоже настройка стратегии, а не бота: несколько лонгов на криптопарах —
    это одна ставка с умноженным риском, но насколько она допустима, зависит
    от винрейта конкретной стратегии. У SMC он около 25%, и коррелированные
    лонги там складываются в глубокую просадку; фибо-модель считалась без
    ограничения вовсе. Одно глобальное число обслужить обе не может.
    """
    value = params.get('max_same_direction')
    if value is None:
        value = getattr(config, 'MAX_SAME_DIRECTION', 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def cooldown_hours(strategy):
    """
    Пауза по паре после выхода — настройка СТРАТЕГИИ, а не бота.

    Книги у стратегий раздельные, горизонты разные: уровни держат позицию
    часы и переоценивают ситуацию быстро, SMC тянет до дальних целей. Общее
    число обслуживало обе плохо, а после появления третьей стратегии стало
    просто неверным: замер уровней делался на шести часах, живой бот брал
    двенадцать из конфига, и торговал бы не то, что измерено.
    """
    if strategy == 'LEVELS':
        try:
            from levels import params as levels_params
            return float(levels_params.COOLDOWN_HOURS)
        except Exception:                          # noqa: BLE001
            pass
    return float(getattr(config, 'COOLDOWN_HOURS', 12))


def tps_completed(remaining_size, original_size, fractions, tolerance=0.01):
    """
    Сколько целей уже отработало — по остатку позиции на бирже.

    Нужно при восстановлении после рестарта: бот видит на бирже уменьшившийся
    размер и должен понять, с какой цели продолжать. Прежняя формула делила
    закрытую часть на фиксированные 0.5 и на плане 25/25/50 давала неверный
    ответ — позиция продолжалась не с той цели, то есть остаток закрывался по
    чужому уровню.
    """
    if original_size <= 0 or remaining_size > original_size or not fractions:
        return 0
    closed = 1.0 - remaining_size / original_size
    done, accumulated = 0, 0.0
    for index, fraction in enumerate(fractions[:-1]):
        accumulated += fraction
        if closed >= accumulated - tolerance:
            done = index + 1
    return done
