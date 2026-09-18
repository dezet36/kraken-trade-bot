"""
Во что обошлась боевая сделка: комиссии и фандинг.

ЗАЧЕМ ЭТОТ ФАЙЛ ПОЯВИЛСЯ. Боевой путь не знал своих издержек вообще. Поиск
слов «fee» и «funding» по trade_manager, bot и exchange не давал ни одного
совпадения, а итог сделки считался так:

    pnl = realized + (exit_price - entry) * remaining

Это ГРЯЗНЫЙ результат — до вычета того, что забрала биржа. Бумажный двойник
комиссии считал с самого начала, и именно он показал, чем всё решается: на
364 сделках сервера за 5–29 августа 2026 грязный итог был +$34.94, комиссии
−$661.03, чистый −$616.77. То есть весь убыток — это издержки, и боевой
журнал единственного, что имеет значение, не записывал.

ОТКУДА БЕРЁТСЯ ЧИСЛО. Сначала спрашиваем биржу: сколько она удержала на самом
деле. Это истина, а не наша модель тарифа. Если биржа не ответила или не умеет
отдавать историю исполнений — считаем оценку по ставкам из config. В журнал
вместе с суммой уходит колонка fees_source: «exchange» или «estimate». Без неё
через месяц будет не отличить замеренное от посчитанного, а смешивать их в
одной колонке и потом делать выводы — ровно тот способ ошибиться, из-за
которого пришлось переделывать разбор августа.

ПОЧЕМУ СЧИТАЕМ ОТ ФАКТОВ СДЕЛКИ, А НЕ НАКОПЛЕНИЕМ. Напрашивалось списывать
комиссию в момент каждого исполнения и складывать в позицию. Так нельзя:
positions_state.json не хранит ни realized_pnl, ни tp_hit, и после перезапуска
бота позиция пересобирается из данных биржи — накопленное обнулилось бы молча,
а сделка получила бы заниженные издержки. Поэтому оценка выводится в момент
закрытия из геометрии сделки: цена входа, размер, сколько целей отработало,
цена выхода. Перезапуск на это не влияет.

ЧЕГО ЗДЕСЬ НЕТ. Комиссии за неисполненные и отменённые ордера — их не берут.
И разбора того, по какой цене реально закрылась каждая часть: оценка считает
части по плановым ценам целей. Когда биржа отвечает, это неважно — берётся
факт; когда не отвечает, число и помечено как оценка.
"""

import config
from exit_plan import tp_plan
from logger import log

# Столько исполнений тянем с биржи за раз. Одна сделка — это вход, до четырёх
# частичных фиксаций и выход, то есть с запасом.
_TRADES_LIMIT = 50


def _is_long(position) -> bool:
    return position['signal']['setup']['type'] == 'LONG'


def _entry_is_taker(position) -> bool:
    """
    Вход рыночным ордером или лимитным.

    Разница в тарифе почти втрое (0.055% против 0.02%), поэтому гадать нельзя.
    Отложенный лимит оставляет в позиции _lifecycle.entry_mode == 'LIMIT';
    у рыночного входа там 'MARKET'. Если следа нет — считаем худший случай:
    занизить издержки хуже, чем завысить, потому что заниженные создают
    видимость преимущества, которого нет.
    """
    mode = (position.get('_lifecycle') or {}).get('entry_mode')
    return str(mode).upper() != 'LIMIT'


def estimate(position, exit_price):
    """
    Оценка издержек по ставкам из config. Возвращает (комиссии, фандинг).

    Считается из геометрии сделки, поэтому не зависит ни от накопленного
    состояния, ни от того, перезапускался ли бот.
    """
    params = position.get('params') or {}
    entry = float(position.get('entry_price') or 0)
    size = float(params.get('position_size') or 0)
    if entry <= 0 or size <= 0:
        return 0.0, 0.0

    maker = config.PAPER_FEE_MAKER
    taker = config.PAPER_FEE_TAKER

    # Вход: весь размер по цене входа.
    fees = size * entry * (taker if _entry_is_taker(position) else maker)

    # Промежуточные цели закрываются БИРЖЕВЫМИ ЛИМИТНЫМИ ордерами — мейкер.
    # Доли считаются от НАЧАЛЬНОГО размера: так их считает и _check_tp_levels.
    targets, fractions = tp_plan(params)
    for i in range(min(int(position.get('tp_hit') or 0), max(len(targets) - 1, 0))):
        fees += size * fractions[i] * targets[i] * maker

    # Остаток — по ставке тейкера. Последняя цель, стоп, тайм-стоп и ручное
    # закрытие уходят через _close_all, а он шлёт РЫНОЧНЫЙ ордер. Исключение
    # одно: внешнее закрытие, когда сработал биржевой лимит на последней цели, —
    # там по факту мейкер. Отдельной ветки под него здесь нет намеренно: отличить
    # заполненный лимит от сработавшего стопа по одному слову 'EXT' нельзя, а
    # когда биржа отвечает, оценка и не используется — берётся факт.
    remaining = float(position.get('remaining_size') or 0)
    if remaining > 0 and exit_price:
        fees += remaining * float(exit_price) * taker

    funding = 0.0
    if getattr(config, 'PAPER_FUNDING', False):
        opened = position.get('entry_time')
        closed = position.get('exit_time')
        if opened and closed:
            hours = (closed - opened).total_seconds() / 3600
            periods = int(hours // 8)
            if periods > 0:
                # Ставку платит длинная сторона при положительном фандинге и
                # получает при отрицательном; для короткой — наоборот.
                sign = 1 if _is_long(position) else -1
                notional = size * entry
                funding = sign * config.PAPER_FUNDING_RATE_8H * notional * periods

    return fees, funding


def _actual_fees(client, pair, since_ms):
    """
    Сколько биржа удержала на самом деле. None, если спросить не удалось.

    Суммируются комиссии всех исполнений по паре с момента входа. Ноль —
    подозрительный ответ (исполнений не может не быть, вход-то состоялся),
    поэтому он тоже считается неудачей и уводит в оценку.
    """
    try:
        import exchange as ex
        if not ex.supports(client, 'fetchMyTrades'):
            return None
        fills = client.fetch_my_trades(pair, since=since_ms, limit=_TRADES_LIMIT)
    except Exception as e:                             # noqa: BLE001
        log(f"   {pair}: история исполнений недоступна — {e}")
        return None

    total = 0.0
    seen = False
    for fill in fills or []:
        fee = fill.get('fee') or {}
        cost = fee.get('cost')
        if cost is None:
            for part in fill.get('fees') or []:
                if part.get('cost') is not None:
                    cost = part['cost']
                    break
        if cost is None:
            continue
        try:
            total += abs(float(cost))
            seen = True
        except (TypeError, ValueError):
            continue
    return total if seen else None


def _actual_funding(client, pair, since_ms):
    """
    Фактически уплаченный фандинг. None, если спросить не удалось.

    В отличие от комиссий ноль здесь ЗАКОННЫЙ: сделка короче восьми часов
    фандинга не платит вовсе. Поэтому пустой ответ — это ноль, а не отказ, и
    неудачей считается только исключение.
    """
    try:
        import exchange as ex
        if not ex.supports(client, 'fetchFundingHistory'):
            return None
        rows = client.fetch_funding_history(pair, since=since_ms, limit=_TRADES_LIMIT)
    except Exception:                                  # noqa: BLE001
        return None

    total = 0.0
    for row in rows or []:
        amount = row.get('amount')
        if amount is None:
            continue
        try:
            # Знак у бирж означает движение по счёту: списание отрицательное.
            # Издержка — величина со знаком «плюс = заплатили», поэтому
            # переворачиваем.
            total += -float(amount)
        except (TypeError, ValueError):
            continue
    return total


def settle(position, exit_price, client, pair):
    """
    Итоговые издержки сделки.

    Возвращает словарь: fees_usd, funding_usd, fees_source. Ничего не бросает —
    сбой учёта не должен мешать закрытию позиции.
    """
    try:
        est_fees, est_funding = estimate(position, exit_price)
    except Exception as e:                             # noqa: BLE001
        log(f"⚠️ Не удалось оценить издержки {pair}: {e}")
        return {'fees_usd': 0.0, 'funding_usd': 0.0, 'fees_source': 'none'}

    since_ms = None
    opened = position.get('entry_time')
    if opened is not None:
        try:
            # Секунда назад: биржа отдаёт исполнения строго позже since, а
            # исполнение входа приходится ровно на entry_time.
            since_ms = int(opened.timestamp() * 1000) - 1000
        except Exception:                              # noqa: BLE001
            since_ms = None

    fees, funding, source = est_fees, est_funding, 'estimate'
    if client is not None and since_ms is not None:
        real_fees = _actual_fees(client, pair, since_ms)
        if real_fees is not None:
            fees, source = real_fees, 'exchange'
            real_funding = _actual_funding(client, pair, since_ms)
            if real_funding is not None:
                funding = real_funding

    return {'fees_usd': round(fees, 4),
            'funding_usd': round(funding, 4),
            'fees_source': source}
