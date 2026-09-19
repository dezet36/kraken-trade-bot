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

ЦЕНА ВРЕМЕНИ, И ПОЧЕМУ РАЗБОР УШЁЛ В СВОЙ ПОТОК. Один разбор занимает на этом
процессоре четыре-пять минут, и быстрой модели тут нет: замер 19 сентября 2026
дал 1.8 токена в секунду у восьмимиллиардной и 1.6 у тридцатипятимиллиардной
MoE — второй умнее, но не быстрее. При цикле в пять минут это означало бы, что
бот половину времени стоит и ждёт пятую стратегию.

Ждать нельзя не из-за упущенных входов — заявки висят часами. Дело в ведении
позиций: стопы, цели и перевод в безубыток проверяются РАЗ В ЦИКЛ, и растянув
цикл вдвое, мы вдвое огрубляем управление сделками остальных четырёх
стратегий. В этом проекте такое уже кончилось испорченным журналом: дыра в
свечах выглядела рыночным движением, и разбор двадцати трёх сделок дал вывод,
который оказался следом простоя.

Поэтому модель работает СБОКУ: цикл отдаёт ей один сетап и идёт дальше, а
готовый вердикт забирает следующим циклом. Задержка в пять минут для сетапа,
живущего часами, ничего не меняет; остановка бота на пять минут меняет всё.
"""

import threading
import time

import config
import llm_context
import llm_decide
import llm_journal
import llm_local
import settings_store as settings
from logger import log

NAME = 'LLM'

# Сколько кандидатов за цикл вообще рассматриваем. Разбирается из них один:
# поток занят минутами, и очередь длиннее единицы означала бы вердикты о
# сетапах, которых к их приходу уже не будет.
MAX_PER_CYCLE = int(config.__dict__.get('LLM_MAX_PER_CYCLE', 0) or 4)

# Когда модель уже отвечала об ЭТОМ сетапе — когда именно. Ключ описан в
# _fingerprint. Живёт в процессе: перезапуск бота законно спрашивает заново.
_asked = {}

# ── Разбор идёт сбоку от цикла ───────────────────────────────────────────────
# Поток один и на всех: вторая модель в памяти — это ещё пять гигабайт и
# вдвое меньше ядер каждому, то есть оба разбора вдвое медленнее вместо
# выигрыша. llama_cpp к тому же не рассчитан на параллельные вызовы.
_work_lock = threading.Lock()
_busy = None            # метка сетапа, который разбирается прямо сейчас
_done = []              # готовые вердикты, ждут ближайшего цикла
_thread = None


def busy():
    """Занята ли модель прямо сейчас. Для панели и проверок."""
    with _work_lock:
        return _busy


def join(timeout=None):
    """
    Дождаться текущего разбора. Нужен проверкам и остановке бота.

    В торговом цикле не зовётся никогда: он на то и не ждёт.
    """
    thread = _thread
    if thread is not None:
        thread.join(timeout)


def _run(candidate, df, submitted):
    """Разбор одного сетапа. Идёт в своём потоке, минутами."""
    global _busy
    pair = candidate['pair']
    try:
        verdict = llm_decide.decide(pair, df, llm_local.ask)
        llm_journal.record(pair, candidate.get('donor', ''), verdict,
                           llm_local.last_stats())
    except Exception as exc:                       # noqa: BLE001
        # Поток не имеет права унести с собой причину: без этой строки
        # стратегия молча перестала бы отвечать, и выглядело бы это как
        # «модель ничего не находит».
        log(f'⚠️ {NAME} {pair}: разбор оборвался — {exc}')
        verdict = {'ok': False, 'gate': 'разбор оборвался', 'detail': str(exc)[:200]}
    with _work_lock:
        _done.append((candidate, verdict, submitted))
        _busy = None


def _submit(candidate, df):
    """Отдаёт сетап модели. False — она занята предыдущим."""
    global _busy, _thread
    with _work_lock:
        if _busy is not None:
            return False
        _busy = _fingerprint(candidate)
    _thread = threading.Thread(target=_run, args=(candidate, df, time.time()),
                               name='llm-decide', daemon=True)
    _thread.start()
    return True


def _harvest():
    """Забирает готовые вердикты и очищает очередь."""
    with _work_lock:
        out = list(_done)
        _done.clear()
    return out


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

    У ФИБО ГОТОВОГО СИГНАЛА НЕТ, и это не оплошность сканера: там сигнал
    достраивается позже, из свечей. Взять цены только из signal значило бы
    получить для всех его кандидатов одну метку «пара|—|—» — и пятая
    стратегия на час переставала бы смотреть на пару после ПЕРВОГО же сетапа
    на ней, считая следующий тем же самым. Поэтому запасной источник —
    разметка самого сетапа.
    """
    params = (candidate.get('signal') or {}).get('params') or {}
    setup = candidate.get('setup') or {}

    def mark(*values):
        for value in values:
            try:
                return f'{float(value):.6g}'
            except (TypeError, ValueError):
                continue
        return '—'

    return (f"{candidate.get('pair')}"
            f"|{mark(params.get('entry'), setup.get('end_price'))}"
            f"|{mark(params.get('stop_loss'), setup.get('start_price'))}")


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

    # РИСК У СТРАТЕГИИ СВОЙ, А НЕ ДОНОРСКИЙ. Доля риска приезжала вместе с
    # чужими параметрами, и сделка на депозите пятой стратегии шла с
    # настройкой четвёртой. У сетапов фибо её не было вовсе — там готового
    # сигнала нет, — и тогда бралась общая настройка бота.
    #
    # Размер позиции пересчитывает брокер по этой доле, текущему депозиту и
    # ДИСТАНЦИИ СТОПА, а стоп у модели свой: копировать донорский размер
    # значило бы рисковать не тем, что заявлено.
    params.pop('position_size', None)
    params.pop('risk_amount', None)
    params.update({
        'risk_pct': settings.risk_pct(NAME),
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
    Забирает готовые вердикты и отдаёт модели следующий сетап. Не ждёт.

    pool — словарь «стратегия -> кандидаты», собранный за этот цикл. Пустой
    словарь означает, что отдавать нечего: модель не выдумывает сетапы на
    пустом месте, и это правильно. Но забрать готовое надо и тогда — вердикт
    про ПРОШЛЫЙ сетап приходит независимо от того, нашлось ли что-то сейчас.

    candles(pair) -> df — откуда брать свечи. Отдельным параметром, чтобы
    проверки обходились без сети. Свечи берутся ЗДЕСЬ, в цикле, и передаются
    потоку готовыми: клиент биржи на параллельные обращения не рассчитан.
    """
    if not llm_local.available():
        log(f'   {NAME}: модель недоступна — стратегия простаивает')
        return []

    out = _collect(_harvest())

    ready = _fresh(pool)
    if not ready:
        if not out:
            log(f'   {NAME}: чужих сетапов за цикл не нашлось, разбирать нечего')
        return out

    if candles is None:
        import exchange

        def candles(pair):
            return exchange.fetch_ohlcv('1h', limit=500, symbol=pair,
                                        client=client)

    if busy():
        log(f'   {NAME}: модель занята прошлым сетапом, жду её')
        return out

    for candidate in ready[:MAX_PER_CYCLE]:
        pair = candidate['pair']
        mark = _fingerprint(candidate)
        if _asked_recently(mark):
            continue

        try:
            df = candles(pair)
        except Exception as exc:                   # noqa: BLE001
            log(f'   {NAME} {pair}: свечи не получены — {exc}')
            continue
        if df is None or len(df) < 100:
            continue

        # Метка ставится ПРИ ОТПРАВКЕ, а не по ответу: иначе следующий цикл
        # отдал бы тот же сетап второй раз, пока первый ещё разбирается. И
        # только если отправка удалась — запомнив неотправленный сетап, мы на
        # час перестали бы спрашивать о том, чего модель не видела.
        if _submit(candidate, df):
            _remember(mark)
            log(f'   {NAME} {pair}: отдал модели сетап от '
                f'{candidate.get("donor", "?")}, вердикт будет через '
                f'несколько минут')
        break
    else:
        log(f'   {NAME}: все сетапы цикла уже разобраны')
    return out


def _collect(finished):
    """
    Превращает готовые вердикты в сигналы. Отказы пишет в журнал отказов.

    ВОЗРАСТ ПРОВЕРЯЕТСЯ ЗДЕСЬ. Вердикт приходит к разметке пятиминутной
    давности — это нормально. Но если поток застрял или машина была занята,
    он может прийти через полчаса, к разметке, которой больше нет. Торговать
    по ней значит торговать вчерашним днём, а выглядеть это будет как свежее
    решение модели.
    """
    out = []
    limit = int(config.__dict__.get('LLM_VERDICT_MAX_AGE_MIN', 0) or 20) * 60
    for candidate, verdict, submitted in finished:
        pair = candidate['pair']
        age = time.time() - submitted

        # Поломка ответом не является: по такому сетапу спросить надо снова,
        # а не через час. Метку снимаем.
        if verdict.get('gate') in llm_decide.BROKEN_GATES:
            _asked.pop(_fingerprint(candidate), None)

        if not verdict.get('ok'):
            log(f'   {NAME} {pair}: отказ — {verdict["gate"]}'
                + (f' ({verdict["detail"]})' if verdict.get('detail') else ''))
            _refuse(candidate.get('signal') or {'trading_pair': pair}, verdict)
            continue

        if age > limit:
            log(f'   {NAME} {pair}: вердикт устарел на {age / 60:.0f} мин — '
                f'не беру')
            _refuse(candidate.get('signal') or {'trading_pair': pair},
                    {'gate': 'вердикт устарел',
                     'detail': f'ответ пришёл через {age / 60:.0f} мин при '
                               f'пределе {limit // 60}'})
            continue

        log(f'   {NAME} {pair}: {verdict["side"]} от {verdict["entry"]:.6g}, '
            f'R:R {verdict["rr"]}, EV {verdict["ev"]}, '
            f'конфлюенс {verdict["votes"]}/5 (сетап от {candidate["donor"]}, '
            f'разбор занял {age:.0f} с)')
        out.append({
            'pair': pair,
            'signal': _reshape(candidate, verdict),
            'score': verdict.get('votes', 0),
            'rr': verdict['rr'],
            'poi_type': 'LLM',
            'df_1h': candidate.get('df_1h'),
        })
    return out
