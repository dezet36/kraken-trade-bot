"""
Пятая стратегия: модель судит сетапы и торгует их на своём депозите.

ОТКУДА БЕРУТСЯ ИДЕИ. Не из воздуха. Сканеры четырёх стратегий за цикл находят
сетапы — их модель и разбирает. Это первый шаг из двух: сначала фильтр над
готовыми идеями, потом самостоятельный поиск. Конвейер общий, меняется только
то, кто дёргает модель.

ПОЧЕМУ ЭТО НЕ НАРУШАЕТ ГЛАВНОГО ПРАВИЛА ПРОЕКТА. В bot.py прямо записано:
стратегия не должна молча получать чужих кандидатов и торговать их под своим
именем — так однажды уровни месяц торговали сетапы фибо, и разбор оказался
недостоверным.

Разница в слове «молча». Здесь заимствование — сам замысел, оно объявлено в
названии модуля, и в журнале у каждой сделки записано, чей сетап модель взяла
за основу. Уровни же получали чужое по недосмотру ветки else.

И решение всё равно СВОЁ: модель заново выбирает направление, вход, стоп и
цели из разметки, а донорский сетап служит поводом посмотреть на эту пару
именно сейчас. От исходного кандидата в сделке может не остаться ни одного
уровня.

ЧТО ЭТО ДАЁТ ДЛЯ ЗАМЕРА. Фильтр сравнивается напрямую: те же сетапы с моделью
и без неё — это почти парный замер, а не сравнение двух разных выборок. У
самостоятельного поиска такого преимущества не будет.

ЦЕНА ВРЕМЕНИ. Одно решение занимает две-четыре минуты на процессоре сервера.
Поэтому разбирается не всё подряд: берутся лучшие кандидаты по оценке сканера,
не больше MAX_PER_CYCLE за цикл. Иначе цикл бота растянется на полчаса, а
торговый цикл ждать не должен.
"""

import time

import config
import llm_context
import llm_decide
import llm_journal
import llm_local
from logger import log

NAME = 'LLM'

# Сколько сетапов за цикл отдаём модели. Четыре по три минуты — это двенадцать
# минут счёта; больше цикл терпеть не станет.
MAX_PER_CYCLE = int(config.__dict__.get('LLM_MAX_PER_CYCLE', 0) or 4)

# Предел не по числу, а по времени. Число сетапов ничего не обещает: один
# разбор занял 165 секунд, а на модели покрупнее займёт вдвое больше, и четыре
# «недолгих» разбора растянут пятиминутный цикл на двадцать минут.
BUDGET_SEC = int(config.__dict__.get('LLM_CYCLE_BUDGET_SEC', 0) or 300)

# Когда модель уже отвечала об ЭТОМ сетапе — когда именно. Ключ описан в
# _fingerprint. Живёт в процессе: перезапуск бота законно спрашивает заново.
_asked = {}


def _fresh(pool):
    """
    Кандидаты всех стратегий одним списком, лучшие впереди.

    Дубли по паре убираются: две стратегии часто находят сетап на одной паре в
    один цикл, и разбирать её дважды — значит потратить шесть минут там, где
    хватит трёх. Остаётся кандидат с большей оценкой.
    """
    best = {}
    for donor, candidates in (pool or {}).items():
        for candidate in candidates or []:
            pair = candidate.get('pair')
            if not pair:
                continue
            candidate = dict(candidate)
            candidate['donor'] = donor
            known = best.get(pair)
            if known is None or (candidate.get('score') or 0) > (known.get('score') or 0):
                best[pair] = candidate
    return sorted(best.values(),
                  key=lambda c: (-(c.get('score') or 0), -(c.get('rr') or 0)))


def _fingerprint(candidate):
    """
    Метка сетапа: пара и цены, ради которых модель и зовётся.

    Своего номера у кандидата нет, и быть не может: сканер находит его заново
    каждый цикл. А заявка висит часами — 19 сентября один и тот же
    SHIB1000USDT LONG с одними и теми же ценами разбирался 119 раз подряд по
    165 секунд. Данные те же, ответ тот же, процессор занят.

    Цены округляются до значащих цифр, а не до копеек: у SHIB и у BTC разный
    порядок, и общего числа знаков после запятой для них не существует.
    """
    params = (candidate.get('signal') or {}).get('params') or {}

    def mark(value):
        try:
            return f'{float(value):.6g}'
        except (TypeError, ValueError):
            return '—'

    return (f"{candidate.get('pair')}|{mark(params.get('entry'))}"
            f"|{mark(params.get('stop_loss'))}")


def _asked_recently(mark, now=None):
    """Спрашивали ли об этом сетапе недавно. Окно — LLM_REASK_AFTER_MIN."""
    minutes = int(config.__dict__.get('LLM_REASK_AFTER_MIN', 0) or 0)
    if minutes <= 0:
        return False
    now = now if now is not None else time.time()
    at = _asked.get(mark)
    return at is not None and (now - at) < minutes * 60


def _remember(mark, now=None):
    """Отмечает разбор и выбрасывает протухшие метки, чтобы не копить их."""
    minutes = int(config.__dict__.get('LLM_REASK_AFTER_MIN', 0) or 0)
    now = now if now is not None else time.time()
    _asked[mark] = now
    if minutes > 0:
        for key, at in list(_asked.items()):
            if now - at >= minutes * 60:
                _asked.pop(key, None)


def _reshape(candidate, verdict):
    """
    Собирает из вердикта модели готовый сигнал на депозите этой стратегии.

    Берётся СТРУКТУРА донорского сигнала — setup, trigger, разметка, — а
    уровни подставляются выбранные моделью. Так сохраняется всё, что умеет
    остальной код: размер позиции, план выхода, график закрытой сделки.
    """
    donor = candidate.get('signal') or {}
    signal = dict(donor)
    signal['trading_pair'] = candidate['pair']
    signal['strategy'] = NAME

    params = dict(donor.get('params') or {})
    targets = list(verdict['targets'])
    params.update({
        'entry': verdict['entry'],
        'stop_loss': verdict['stop'],
        'take_profit_1': targets[0],
        'take_profit_2': targets[-1],
        'tp_targets': targets,
        'tp_fractions': _fractions(len(targets)),
        'rr': verdict['rr'],
        'sl_distance': abs(verdict['entry'] - verdict['stop']),
        # Безубыток выключен: модель сама называет уровень инвалидации, и
        # подтянутый стоп выбивал бы позицию раньше, чем идея опровергнута.
        'be_level': None,
        'breakeven_after_tp': False,
        'invalidation': verdict.get('inval'),
    })
    signal['params'] = params

    setup = dict(donor.get('setup') or {})
    setup['type'] = verdict['side']
    signal['setup'] = setup

    # Всё, что модель сказала, уходит в сигнал и дальше в журнал. Без этого
    # решение нельзя будет разобрать: останутся цены без объяснения.
    signal['llm'] = {
        # ДОНОРА БЕРЁМ ИЗ ДВУХ МЕСТ. Обычно его проставляет _fresh, но если
        # _reshape позовут кандидатом в обход отбора, запись о заимствовании
        # окажется пустой — а она и есть то, что отличает объявленное
        # заимствование от молчаливой подмены. Имя стратегии знает и сам
        # донорский сигнал, оттуда и берём запасным путём.
        'donor': (candidate.get('donor')
                  or (candidate.get('signal') or {}).get('strategy', '')),
        'regime': verdict.get('regime', ''),
        'analysis': verdict.get('analysis', ''),
        'trigger': verdict.get('trigger', ''),
        'why': verdict.get('why', ''),
        'risk': verdict.get('risk', ''),
        'alt': verdict.get('alt', ''),
        'p': verdict.get('p'),
        'votes': verdict.get('votes'),
        'confluence': verdict.get('confluence', {}),
        'rr': verdict.get('rr'),
        'ev': verdict.get('ev'),
        'cost_r': verdict.get('cost_r'),
        'model': llm_local.last_stats().get('model', ''),
        'ids': verdict.get('ids', {}),
    }
    return signal


def _fractions(count):
    """Доли выхода по целям. Ближняя цель забирает больше — она надёжнее."""
    if count <= 1:
        return [1.0]
    if count == 2:
        return [0.6, 0.4]
    return [0.5, 0.3, 0.2]


def _refuse(signal_like, verdict):
    """Пишет отказ модели в общий журнал отказов."""
    try:
        import refused
        refused.record(NAME, signal_like, f"ИИ: {verdict.get('gate', '')}",
                       verdict.get('detail') or verdict.get('why', ''),
                       verdict.get('cost_r', ''))
    except Exception:                              # noqa: BLE001
        pass


def scan_for_setups(pool, gate, client=None, balance=None, candles=None):
    """
    Разбирает чужие сетапы моделью и возвращает СВОИ.

    pool — словарь «стратегия -> кандидаты», собранный за этот цикл. Пустой
    словарь означает, что разбирать нечего: модель не выдумывает сетапы на
    пустом месте, и это правильно.

    candles(pair) -> df — откуда брать свечи. Отдельным параметром, чтобы
    проверки обходились без сети.
    """
    if not llm_local.available():
        log(f'   {NAME}: модель недоступна — стратегия простаивает')
        return []

    ready = _fresh(pool)
    if not ready:
        log(f'   {NAME}: чужих сетапов за цикл не нашлось, разбирать нечего')
        return []

    if candles is None:
        import exchange

        def candles(pair):
            return exchange.fetch_ohlcv('1h', limit=500, symbol=pair,
                                        client=client)

    out = []
    started = time.time()
    longest = 0.0
    for number, candidate in enumerate(ready[:MAX_PER_CYCLE]):
        pair = candidate['pair']

        mark = _fingerprint(candidate)
        if _asked_recently(mark):
            log(f'   {NAME} {pair}: тот же сетап уже разобран, '
                f'спрошу не раньше чем через '
                f'{config.__dict__.get("LLM_REASK_AFTER_MIN", 60)} мин')
            continue

        # ВРЕМЯ ПРОВЕРЯЕТСЯ ПЕРЕД РАЗБОРОМ, А НЕ ПОСЛЕ. Остановиться на
        # полпути нельзя: модель отвечает целиком или никак. Поэтому решение
        # начинать принимается по самому долгому разбору этого цикла — если
        # следующий окажется таким же, уложимся ли мы в бюджет.
        spent = time.time() - started
        if number and spent + longest > BUDGET_SEC:
            left = ready[:MAX_PER_CYCLE][number:]
            log(f'   {NAME}: бюджет цикла {BUDGET_SEC} с исчерпан за '
                f'{spent:.0f} с — {len(left)} сетапов остались без разбора')
            for skipped in left:
                _refuse(skipped.get('signal') or {'trading_pair': skipped['pair']},
                        {'gate': 'не хватило времени цикла',
                         'detail': f'разбор занимает {longest:.0f} с, '
                                   f'бюджет {BUDGET_SEC} с'})
            break

        try:
            df = candles(pair)
        except Exception as exc:                   # noqa: BLE001
            log(f'   {NAME} {pair}: свечи не получены — {exc}')
            continue
        if df is None or len(df) < 100:
            continue

        at = time.time()
        verdict = llm_decide.decide(pair, df, llm_local.ask)
        longest = max(longest, time.time() - at)
        _remember(mark)
        llm_journal.record(pair, candidate.get('donor', ''), verdict,
                           llm_local.last_stats())

        if not verdict['ok']:
            log(f'   {NAME} {pair}: отказ — {verdict["gate"]}'
                + (f' ({verdict["detail"]})' if verdict.get('detail') else ''))
            _refuse(candidate.get('signal') or {'trading_pair': pair}, verdict)
            continue

        log(f'   {NAME} {pair}: {verdict["side"]} от {verdict["entry"]:.6g}, '
            f'R:R {verdict["rr"]}, EV {verdict["ev"]}, '
            f'конфлюенс {verdict["votes"]}/5 (сетап от {candidate["donor"]})')
        out.append({
            'pair': pair,
            'signal': _reshape(candidate, verdict),
            'score': verdict.get('votes', 0),
            'rr': verdict['rr'],
            'poi_type': 'LLM',
            'df_1h': candidate.get('df_1h'),
        })
    return out
