"""
Пятая стратегия: модель сама разбирает рынок и торгует на своём депозите.

ОТКУДА БЕРУТСЯ ИДЕИ. Из самого рынка. Стратегия обходит ликвидные пары по
кругу, по одной за цикл, и по каждой модель получает полную разметку —
уровни, расстановку участников, профиль объёма, дельту, стакан, структуру —
и решает сама: есть ли здесь сделка, где вход, стоп и цели, и почему.

ТАК БЫЛО НЕ ВСЕГДА, И ПОЧЕМУ ПОМЕНЯЛИ. Первый вариант (19 сентября 2026) был
фильтром: модели отдавали только те пары, на которых сканеры четырёх других
стратегий за этот цикл нашли сетап. Замысел — почти парный замер: те же
сетапы с моделью и без. На деле вышло иначе. Сетапа донора модель не видела
вовсе — в разметке была только карта пары, — то есть судила не чужую идею, а
искала свою; а пары ей доставались лишь когда что-то находили другие: за
четыре часа четыре разбора, остальные циклы — «разбирать нечего». Фильтр
над чужими идеями, которых модель не видит, — не фильтр. Решено: модель —
самостоятельный аналитик, и очередь пар она получает независимо от того,
что нашли остальные.

ПОЧЕМУ ЭТО НЕ НАРУШАЕТ ГЛАВНОГО ПРАВИЛА ПРОЕКТА. В bot.py записано:
стратегия не должна молча получать чужих кандидатов и торговать их под своим
именем. Здесь чужих кандидатов нет вовсе: каждая сделка — от разметки до
уровней — собрана этой стратегией, и в журнале лежит её собственный разбор.

ЦЕНА ВРЕМЕНИ, И ПОЧЕМУ РАЗБОР УШЁЛ В СВОЙ ПОТОК. Один разбор занимает на этом
процессоре пять-семь минут, и быстрой модели тут нет: замер 19 сентября 2026
дал 1.8 токена в секунду у восьмимиллиардной и 1.6 у тридцатипятимиллиардной
MoE — второй умнее, но не быстрее. При цикле в пять минут это означало бы, что
бот половину времени стоит и ждёт пятую стратегию.

Ждать нельзя не из-за упущенных входов — заявки висят часами. Дело в ведении
позиций: стопы, цели и перевод в безубыток проверяются РАЗ В ЦИКЛ, и растянув
цикл вдвое, мы вдвое огрубляем управление сделками остальных четырёх
стратегий. В этом проекте такое уже кончилось испорченным журналом: дыра в
свечах выглядела рыночным движением, и разбор двадцати трёх сделок дал вывод,
который оказался следом простоя.

Поэтому модель работает СБОКУ: цикл отдаёт ей одну пару и идёт дальше, а
готовый вердикт забирает следующим циклом. Задержка в пять минут для идеи,
живущей часами, ничего не меняет; остановка бота на пять минут меняет всё.

СКОЛЬКО ЭТО ДАЁТ. Двадцать пар по семь минут — круг за два с половиной часа,
каждая пара разбирается заново раз в два-три часа. Для сделок с горизонтом в
часы и дни этого достаточно; чаще было бы незачем — разметка по часовым
свечам за это время почти не меняется.
"""

import threading
import time

import config
import llm_context
import llm_decide
import llm_journal
import llm_local
import llm_market
import settings_store as settings
from logger import log

NAME = 'LLM'

# Когда модель уже отвечала об ЭТОЙ паре — когда именно. Живёт в процессе:
# перезапуск бота законно спрашивает заново. Окно — LLM_REASK_AFTER_MIN: при
# двадцати парах круг длиннее часа и окно почти не срабатывает, но при пуле в
# две-три пары оно не даёт разбирать одну и ту же разметку без конца.
_asked = {}

# Где остановился обход: следующая пара берётся отсюда, а не с начала списка.
# Без курсора первая пара списка разбиралась бы каждый круг первой, а
# последние — никогда, если модель не успевает за цикл.
_cursor = 0

# ── Разбор идёт сбоку от цикла ───────────────────────────────────────────────
# Поток один и на всех: вторая модель в памяти — это ещё пять гигабайт и
# вдвое меньше ядер каждому, то есть оба разбора вдвое медленнее вместо
# выигрыша. llama_cpp к тому же не рассчитан на параллельные вызовы.
_work_lock = threading.Lock()
_busy = None            # пара, которая разбирается прямо сейчас
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


def _run(pair, df, market, submitted):
    """Разбор одной пары. Идёт в своём потоке, минутами."""
    global _busy
    try:
        verdict = llm_decide.decide(pair, df, llm_local.ask, market=market)
        llm_journal.record(pair, '', verdict, llm_local.last_stats())
    except Exception as exc:                       # noqa: BLE001
        # Поток не имеет права унести с собой причину: без этой строки
        # стратегия молча перестала бы отвечать, и выглядело бы это как
        # «модель ничего не находит».
        log(f'⚠️ {NAME} {pair}: разбор оборвался — {exc}')
        verdict = {'ok': False, 'gate': 'разбор оборвался', 'detail': str(exc)[:200]}
    with _work_lock:
        _done.append((pair, df, verdict, submitted))
        _busy = None


def _submit(pair, df, market=None):
    """Отдаёт пару модели. False — она занята предыдущей."""
    global _busy, _thread
    with _work_lock:
        if _busy is not None:
            return False
        _busy = pair
    _thread = threading.Thread(target=_run,
                               args=(pair, df, market, time.time()),
                               name='llm-decide', daemon=True)
    _thread.start()
    return True


def _harvest():
    """Забирает готовые вердикты и очищает очередь."""
    with _work_lock:
        out = list(_done)
        _done.clear()
    return out


def _asked_recently(pair, now=None):
    """Спрашивали ли об этой паре недавно. Окно — LLM_REASK_AFTER_MIN."""
    minutes = int(config.__dict__.get('LLM_REASK_AFTER_MIN', 0) or 0)
    if minutes <= 0:
        return False
    now = now if now is not None else time.time()
    at = _asked.get(pair)
    return at is not None and (now - at) < minutes * 60


def _remember(pair, now=None):
    """Отмечает разбор и выбрасывает протухшие метки, чтобы не копить их."""
    minutes = int(config.__dict__.get('LLM_REASK_AFTER_MIN', 0) or 0)
    now = now if now is not None else time.time()
    _asked[pair] = now
    if minutes > 0:
        for key, at in list(_asked.items()):
            if now - at >= minutes * 60:
                _asked.pop(key, None)


def _queue(pairs):
    """
    Пары в порядке обхода: от курсора по кругу, недавно разобранные — в конец.

    Курсор сдвигается тем, кто пару отправил (см. scan_for_setups), а не
    здесь: очередь — это только порядок, отправка может и не состояться.
    """
    pairs = [p for p in (pairs or []) if p]
    if not pairs:
        return []
    start = _cursor % len(pairs)
    ring = pairs[start:] + pairs[:start]
    fresh = [p for p in ring if not _asked_recently(p)]
    return fresh


def _reshape(pair, verdict, df=None):
    """
    Собирает из вердикта модели готовый сигнал на депозите этой стратегии.

    Всё с нуля: у этого сигнала нет донора, из которого можно было бы взять
    структуру. Поля — те, что читают paper_broker и trade_manager: пара,
    имя стратегии, тип сетапа, параметры входа и выхода.
    """
    targets = list(verdict['targets'])
    params = {
        # Размер позиции считает брокер по этой доле, текущему депозиту и
        # дистанции стопа. Здесь его не бывает.
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
    }
    return {
        'trading_pair': pair,
        'strategy': NAME,
        'setup': {'type': verdict['side']},
        # Зона в терминах остальных стратегий здесь одна: уровень, который
        # выбрала модель. Панель показывает её имя рядом со сделкой.
        'trigger': {'zone': (verdict.get('ids') or {}).get('entry') or 'LLM'},
        'params': params,
        # Всё, что модель сказала, уходит в сигнал и дальше в журнал. Без
        # этого решение нельзя будет разобрать: останутся цены без объяснения.
        'llm': {
            'donor': '',
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
        },
    }


def _fractions(count):
    """Доли выхода по целям. Ближняя цель забирает больше — она надёжнее."""
    if count <= 1:
        return [1.0]
    if count == 2:
        return [0.6, 0.4]
    return [0.5, 0.3, 0.2]


def _refuse(pair, verdict):
    """Пишет отказ модели в общий журнал отказов."""
    try:
        import refused
        refused.record(NAME, {'trading_pair': pair}, f"ИИ: {verdict.get('gate', '')}",
                       verdict.get('detail') or verdict.get('why', ''),
                       verdict.get('cost_r', ''))
    except Exception:                              # noqa: BLE001
        pass


def scan_for_setups(pairs, gate, client=None, balance=None, candles=None,
                    market=None):
    """
    Забирает готовые вердикты и отдаёт модели следующую пару. Не ждёт.

    pairs — ликвидные пары этого цикла, те же, что получают остальные
    стратегии. Обходятся по кругу: одна пара за цикл, потому что модель
    разбирает одну за пять-семь минут и очередь длиннее единицы означала бы
    вердикты о разметке, которой к их приходу уже нет.

    candles(pair) -> df — откуда брать свечи. Отдельным параметром, чтобы
    проверки обходились без сети. Свечи берутся ЗДЕСЬ, в цикле, и передаются
    потоку готовыми: клиент биржи на параллельные обращения не рассчитан.

    market(pair, df) -> снимок — то же самое про стакан, дельту и структуру:
    llm_market.snapshot ходит на биржу, поэтому зовётся в цикле, а поток
    получает готовый словарь. Без снимка разбор законен — в разметке будут
    прочерки, — но без него модель отмечает профиль и поток вслепую.
    """
    global _cursor

    if not llm_local.available():
        log(f'   {NAME}: модель недоступна — стратегия простаивает')
        return []

    out = _collect(_harvest())

    if candles is None:
        import exchange

        def candles(pair):
            return exchange.fetch_ohlcv('1h', limit=500, symbol=pair,
                                        client=client)

    if market is None:
        def market(pair, df):
            return llm_market.snapshot(pair, df, client=client)

    if busy():
        log(f'   {NAME}: модель занята парой {busy()}, жду её')
        return out

    queue = _queue(pairs)
    if not queue:
        log(f'   {NAME}: все пары разобраны недавно, жду')
        return out

    for pair in queue:
        try:
            df = candles(pair)
        except Exception as exc:                   # noqa: BLE001
            log(f'   {NAME} {pair}: свечи не получены — {exc}')
            continue
        if df is None or len(df) < 100:
            continue

        # Снимок не обязателен, свечи обязательны: без уровней модели не из
        # чего выбирать, а без стакана она просто скажет «не измерено».
        try:
            facts = market(pair, df)
        except Exception as exc:                   # noqa: BLE001
            log(f'   {NAME} {pair}: снимок рынка не собран — {exc}')
            facts = None

        # Метка ставится ПРИ ОТПРАВКЕ, а не по ответу: иначе следующий цикл
        # отдал бы ту же пару второй раз, пока первая ещё разбирается. И
        # только если отправка удалась — запомнив неотправленную пару, мы на
        # час перестали бы спрашивать о том, чего модель не видела.
        if _submit(pair, df, facts):
            _remember(pair)
            _cursor = (list(pairs).index(pair) + 1) % max(1, len(pairs))
            log(f'   {NAME} {pair}: отдал модели на разбор, вердикт будет '
                f'через несколько минут')
        break
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
    for pair, df, verdict, submitted in finished:
        age = time.time() - submitted

        # Поломка ответом не является: по такой паре спросить надо снова,
        # а не через час. Метку снимаем.
        if verdict.get('gate') in llm_decide.BROKEN_GATES:
            _asked.pop(pair, None)

        if not verdict.get('ok'):
            log(f'   {NAME} {pair}: отказ — {verdict["gate"]}'
                + (f' ({verdict["detail"]})' if verdict.get('detail') else ''))
            _refuse(pair, verdict)
            continue

        if age > limit:
            log(f'   {NAME} {pair}: вердикт устарел на {age / 60:.0f} мин — '
                f'не беру')
            _refuse(pair, {'gate': 'вердикт устарел',
                           'detail': f'ответ пришёл через {age / 60:.0f} мин при '
                                     f'пределе {limit // 60}'})
            continue

        log(f'   {NAME} {pair}: {verdict["side"]} от {verdict["entry"]:.6g}, '
            f'R:R {verdict["rr"]}, EV {verdict["ev"]}, '
            f'конфлюенс {verdict["votes"]}/5 (разбор занял {age:.0f} с)')
        out.append({
            'pair': pair,
            'signal': _reshape(pair, verdict, df),
            'score': verdict.get('votes', 0),
            'rr': verdict['rr'],
            'poi_type': 'LLM',
            'df_1h': df,
        })
    return out
