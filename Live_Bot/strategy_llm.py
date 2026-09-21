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

import os
import threading
import time

import config
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
# Повод, с которым пару отдали модели, и отказ, которым это кончилось:
# пара -> подпись повода (llm_urgency.signature) / {'at', 'sig'}.
_asked_sig = {}
_refused = {}
_reasons = {}           # пара -> поводы из последнего построения очереди

# Взведённые условия входа: пара -> план. Модель сказала «войти после
# закрытия часа выше L3» — план лежит здесь, и каждый цикл код смотрит на
# последнюю ЗАКРЫТУЮ свечу.
#
# ПЕРЕЖИВАЕТ ПЕРЕЗАПУСК. Сначала план жил в процессе — «честнее, чем
# исполнить его по разметке, которой уже нет». На деле план — это цены:
# вход, стоп, цели и уровень условия, — а разметка нужна была модели, не
# коду. Условие ждёт до 12 часов, выкатки идут по несколько раз в день, и
# каждая молча стирала план, о котором человеку уже пришло сообщение.
# Срок считается от взведения, а не от перезапуска.
_armed = {}
_ARMED_FILE = os.path.join(config.DATA_DIR, 'llm_armed.json')
_armed_loaded = False

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
        # Прошлые разборы этой пары читаются здесь, в потоке: файл журнала
        # растёт на строку за разбор, и чтение его целиком — секунды, а не
        # минуты. В цикле этому не место, в потоке — самое оно.
        history = llm_journal.last(80)
        verdict = llm_decide.decide(pair, df, llm_local.ask, market=market,
                                    history=history)
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


def _asked_recently(pair, now=None, factor=1.0):
    """Спрашивали ли об этой паре недавно. Окно — LLM_REASK_AFTER_MIN × factor."""
    minutes = int(config.__dict__.get('LLM_REASK_AFTER_MIN', 0) or 0) * factor
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
    import llm_urgency
    _asked_sig[pair] = llm_urgency.signature(_reasons.get(pair, ()))
    # Метки живут дольше окна повтора: по ним _stale решает, что пару
    # давно не разбирали. Выбрасываем только совсем старые.
    import llm_urgency
    keep_sec = max(minutes * 60, llm_urgency.STALE_HOURS * 3600 * 2)
    for key, at in list(_asked.items()):
        if now - at >= keep_sec:
            _asked.pop(key, None)


def _cached_context(pair):
    try:
        import strategy_smc
        return strategy_smc.cached_context(pair)
    except Exception:                              # noqa: BLE001
        return None


def _queue(pairs, context_of=_cached_context, skip=()):
    """
    Пары в порядке обхода: от курсора по кругу, недавно разобранные — в конец.

    Курсор сдвигается тем, кто пару отправил (см. scan_for_setups), а не
    здесь: очередь — это только порядок, отправка может и не состояться.

    skip — пары, о которых спрашивать сейчас незачем: план уже взведён,
    позиция или заявка уже стоит. 20.09.2026 DOT разобрали шесть раз за
    ночь с одним и тем же планом — и шесть раз прислали его человеку.
    """
    skip = set(skip or ())
    pairs = [p for p in (pairs or []) if p and p not in skip]
    if not pairs:
        return []
    start = _cursor % len(pairs)
    ring = pairs[start:] + pairs[:start]

    # СОБЫТИЯ ВПЕРЁД. Свежий слом, всплеск ликвидаций, цена у уровня, скачок
    # ОИ — такая пара идёт впереди круга, и окно повтора для неё вдвое
    # короче. Считается кодом из кэша SMC и файлов, без запросов к бирже.
    import llm_urgency
    ranked = llm_urgency.rank(ring, context_of)
    out, idle, same = [], [], []
    for pair, points, why in ranked:
        _reasons[pair] = list(why or ())
        # ТОТ ЖЕ ПОВОД ПОСЛЕ ОТКАЗА — НЕ ПОВОД. 21.09.2026 ETH разобрали пять
        # раз за 2.5 часа: цена стояла у того же уровня, ответ был тот же
        # («стоп в скоплении стопов»), и каждый раз это стоило 15 минут
        # модели. Пара вернётся, как только повод станет новым (другой
        # уровень, слом, всплеск) — или по плановому проходу через
        # STALE_HOURS: отсеянной навсегда она стать не может.
        last = _refused.get(pair)
        if last and not _stale(pair) and llm_urgency.same_reason(
                last['at'], last['sig'], why):
            same.append(f"{pair} ({', '.join(why)})")
            continue
        if points >= llm_urgency.URGENT:
            if not _asked_recently(pair, factor=0.5):
                out.append(pair)
            continue
        if _asked_recently(pair):
            continue
        # РАЗБОР ПО ПОВОДУ. Нет события и цена не у уровня — модели там
        # делать нечего; пара подождёт повода или планового прохода раз в
        # STALE_HOURS. Освобождает половину времени модели под пары, где
        # сетап возможен сейчас.
        if points >= llm_urgency.REASON or _stale(pair):
            out.append(pair)
        else:
            idle.append(pair)
    if same:
        log(f'   {NAME}: тот же повод, что при отказе, ждут нового — {"; ".join(same)}')
    if idle:
        log(f'   {NAME}: без повода, ждут события — {", ".join(idle)}')
    return out


def _stale(pair, now=None):
    """Пару давно (или никогда) не разбирали — плановый проход без повода."""
    import llm_urgency
    now = now if now is not None else time.time()
    at = _asked.get(pair)
    return at is None or (now - at) >= llm_urgency.STALE_HOURS * 3600


def armed():
    """Взведённые условия — для панели: пара, условие, уровень, возраст."""
    now = time.time()
    return [{'pair': pair, 'when': p['verdict'].get('trigger_when'),
             'level': p['verdict'].get('trigger_level'),
             'side': p['verdict'].get('side'),
             'minutes': int((now - p['armed_at']) / 60)}
            for pair, p in _armed.items()]


def current_setups(broker=None, now=None):
    """
    Живые сетапы ИИ для человека — то, что ещё может сбыться:
      armed   — планы, ждущие условия (снимаются по TTL или по цели без входа);
      pending — лимитные заявки, ждущие цену (живут LLM_TRIGGER_TTL_H);
      open    — открытые позиции.
    broker — фантомный брокер со snapshot(); без него — только планы.
    """
    now = now if now is not None else time.time()
    _load_armed()
    ttl_min = int(config.__dict__.get('LLM_TRIGGER_TTL_H', 0) or 12) * 60
    out = {'armed': [], 'pending': [], 'open': []}
    for pair, plan in sorted(_armed.items()):
        v = plan['verdict']
        minutes = int((now - plan['armed_at']) / 60)
        out['armed'].append({
            'pair': pair, 'side': v.get('side'), 'entry': v.get('entry'), 'stop': v.get('stop'),
            'targets': list(v.get('targets') or []), 'rr': v.get('rr'), 'p': v.get('p'),
            'when': v.get('trigger_when'), 'level': v.get('trigger_level'),
            'minutes': minutes, 'left_min': max(0, ttl_min - minutes),
            'why': v.get('why', ''), 'stop_why': v.get('stop_why', ''), 'tp_why': v.get('tp_why', ''),
            'bias': v.get('bias', ''),
        })
    snap = {}
    if broker is not None and hasattr(broker, 'snapshot'):
        try:
            snap = broker.snapshot() or {}
        except Exception as exc:                   # noqa: BLE001
            log(f'   {NAME}: состояние брокера для списка сетапов не прочитано — {exc}')
    out['pending'] = [o for o in snap.get('pending') or [] if o.get('strategy') == NAME]
    out['open'] = [o for o in snap.get('open') or [] if o.get('strategy') == NAME]
    return out


def _arm(pair, verdict):
    """Откладывает вход до условия. Новый план по паре сменяет старый."""
    _armed[pair] = {'verdict': verdict, 'armed_at': time.time()}
    _save_armed()
    log(f'   {NAME} {pair}: вход отложен до условия '
        f'{verdict.get("trigger_when")} {verdict.get("trigger_level"):.6g} '
        f'({verdict.get("side")} от {verdict.get("entry"):.6g})')


def _save_armed():
    """Взведённые планы — на диск. Отказ записи торговле не мешает."""
    try:
        import json
        tmp = _ARMED_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(_armed, fh, ensure_ascii=False, default=str)
        os.replace(tmp, _ARMED_FILE)
    except Exception as exc:                       # noqa: BLE001
        log(f'   {NAME}: взведённые планы не сохранены — {exc}')


def _load_armed():
    """Планы с диска — один раз за запуск; просроченные снимет проверка."""
    global _armed_loaded
    if _armed_loaded:
        return
    _armed_loaded = True
    try:
        import json
        if not os.path.exists(_ARMED_FILE):
            return
        with open(_ARMED_FILE, encoding='utf-8') as fh:
            stored = json.load(fh)
        for pair, plan in (stored or {}).items():
            if pair not in _armed and plan.get('verdict') and plan.get('armed_at'):
                _armed[pair] = plan
        if stored:
            log(f'   {NAME}: взведённых планов с прошлого запуска — {len(stored)}')
    except Exception as exc:                       # noqa: BLE001
        log(f'   {NAME}: взведённые планы не прочитаны — {exc}')


def _target_reached(verdict, bars):
    """
    Дошла ли цена до первой цели по закрытым свечам после взведения.
    -> строка «цена дошла до цели 1.015 (максимум 1.0212)» или ''.
    """
    targets = verdict.get('targets') or []
    if not targets:
        return ''
    target = float(targets[0])
    is_long = verdict.get('side') == 'LONG'
    for _closed_at, _o, high, low, _c, _v in bars:
        if (is_long and high >= target) or (not is_long and low <= target):
            extreme = high if is_long else low
            return f'цена дошла до цели {target:.6g} ({"максимум" if is_long else "минимум"} {extreme:.6g})'
    return ''


def _closed_bars(df, since_ms):
    """
    Закрытые часовые свечи, закрывшиеся ПОСЛЕ момента since_ms, старые → новые.

    Биржа отдаёт последней текущую, ещё идущую свечу — она отбрасывается.
    Возвращает список (закрыта_в_мс, open, high, low, close, volume).
    """
    try:
        if df is None or len(df) < 3:
            return []
        step = int((df['timestamp'].iloc[-1] - df['timestamp'].iloc[-2]).total_seconds() * 1000)
        out = []
        for i in range(max(0, len(df) - 60), len(df) - 1):
            row = df.iloc[i]
            closed_at = int(row['timestamp'].timestamp() * 1000) + step
            if closed_at <= since_ms:
                continue
            out.append((closed_at, float(row['open']), float(row['high']),
                        float(row['low']), float(row['close']), float(row['volume'])))
        return out
    except Exception:                              # noqa: BLE001
        return []


def _median_volume(df, bars=48):
    try:
        import numpy as np
        return float(np.median(df['volume'].values[-bars - 1:-1]))
    except Exception:                              # noqa: BLE001
        return 0.0


def condition_met(when, level, side, bars, median_volume=0.0):
    """
    Наступило ли условие входа на свечах, закрывшихся после взведения.

    Возвращает описание для журнала или ''. Семь условий, и каждое —
    арифметика по закрытым свечам, без модели:
      close_above / close_below            закрытие за уровнем
      close_*_with_volume                  то же при объёме ≥ 1.5× медианы
      retest                               закрытие за уровнем по ходу сделки,
                                           потом касание уровня с той стороны
      sweep_reclaim                        экстремум за уровнем против сделки,
                                           закрытие обратно не позже 3 свечей
    """
    if not bars or level is None:
        return ''
    level = float(level)
    long = side == 'LONG'
    if when in ('close_above', 'close_above_with_volume'):
        for closed_at, o, h, l, c, v in bars:
            if c > level and (when == 'close_above' or (median_volume and v >= 1.5 * median_volume)):
                return f'час закрылся по {c:.6g} выше {level:.6g}' + (
                    f' на объёме ×{v / median_volume:.1f}' if 'volume' in when else '')
        return ''
    if when in ('close_below', 'close_below_with_volume'):
        for closed_at, o, h, l, c, v in bars:
            if c < level and (when == 'close_below' or (median_volume and v >= 1.5 * median_volume)):
                return f'час закрылся по {c:.6g} ниже {level:.6g}' + (
                    f' на объёме ×{v / median_volume:.1f}' if 'volume' in when else '')
        return ''
    if when == 'retest':
        broke = False
        for closed_at, o, h, l, c, v in bars:
            if not broke:
                broke = c > level if long else c < level
                continue
            touched = l <= level if long else h >= level
            held = c >= level if long else c <= level
            if touched and held:
                return f'пробой {level:.6g} и возврат к нему (закрытие {c:.6g})'
            if (c < level) if long else (c > level):
                broke = False                      # пробой не удержался — ждём заново
        return ''
    if when == 'sweep_reclaim':
        swept_at = None
        for i, (closed_at, o, h, l, c, v) in enumerate(bars):
            beyond = l < level if long else h > level
            if swept_at is None and beyond:
                swept_at = i
            if swept_at is not None and i - swept_at <= 3:
                back = c > level if long else c < level
                if back:
                    return f'вынос за {level:.6g} и возврат за {i - swept_at + 1} св. (закрытие {c:.6g})'
            elif swept_at is not None and i - swept_at > 3:
                swept_at = i if beyond else None
        return ''
    return ''


def _busy_pairs(pairs, gate):
    """
    Пары, по которым модель спрашивать незачем: взведённый план, своя
    позиция или заявка, а при «одна пара — одна позиция» — и чужая.
    """
    busy = set(_armed)
    if gate is None:
        return busy
    broker = getattr(gate, '_broker', None)
    exclusive = bool(getattr(config, 'PAPER_EXCLUSIVE_PAIRS', False))
    for pair in pairs or ():
        try:
            if gate.has_position_or_order(pair):
                busy.add(pair)
            elif exclusive and broker is not None and broker.pair_taken_by(pair):
                busy.add(pair)
        except Exception:                          # noqa: BLE001
            continue
    return busy


def _check_armed(candles):
    """
    Проверяет взведённые условия по свечам, закрывшимся после взведения.

    Считаются только свечи ПОСЛЕ взведения: если цена уже стояла выше
    уровня, когда модель просила «закрытие выше», это не подтверждение, а
    то самое положение, которое её и не устроило.
    """
    out = []
    _load_armed()
    ttl = int(config.__dict__.get('LLM_TRIGGER_TTL_H', 0) or 12) * 3600
    now = time.time()
    for pair, plan in list(_armed.items()):
        verdict = plan['verdict']
        if now - plan['armed_at'] > ttl:
            _armed.pop(pair, None)
            _save_armed()
            log(f'   {NAME} {pair}: условие входа не наступило за {ttl // 3600} ч — снято')
            _refuse(pair, {'gate': 'условие не наступило',
                           'detail': f'{verdict.get("trigger_when")} '
                                     f'{verdict.get("trigger_level")} за {ttl // 3600} ч'})
            continue
        try:
            df = candles(pair)
        except Exception as exc:                   # noqa: BLE001
            log(f'   {NAME} {pair}: свечи для условия не получены — {exc}')
            continue
        bars = _closed_bars(df, plan['armed_at'] * 1000)
        if not bars:
            continue
        # ЦЕЛЬ ДОСТИГНУТА ДО ВХОДА — ПЛАН СНИМАЕТСЯ. 21.09.2026 SUI: LONG от
        # 0.93988 по retest, цена без отката ушла с 0.96 на 1.02 — выше первой
        # цели 1.015, а план висел взведённым и ждал бы откат ещё 10 часов.
        # Входить у цели поздно; и это исход, который надо считать: условие
        # модели оказалось строже рынка.
        missed = _target_reached(verdict, bars)
        if missed:
            _armed.pop(pair, None)
            _save_armed()
            log(f'   {NAME} {pair}: цель достигнута без входа — {missed}; план снят')
            _refuse(pair, {'gate': 'цель достигнута без входа',
                           'detail': f'{verdict.get("trigger_when")} '
                                     f'{verdict.get("trigger_level")} не наступило, а {missed}'})
            _refused[pair] = {'at': time.time(), 'sig': frozenset()}
            try:
                import telegram_notify as tg
                tg.llm_setup_rejected(pair, verdict.get('side', ''), float(verdict.get('entry') or 0),
                                      'цель достигнута без входа',
                                      f'условие {verdict.get("trigger_when")} '
                                      f'{verdict.get("trigger_level")} не наступило, а {missed}')
            except Exception:                      # noqa: BLE001
                pass
            continue
        met = condition_met(verdict['trigger_when'], verdict['trigger_level'],
                            verdict['side'], bars, _median_volume(df))
        if not met:
            continue
        _armed.pop(pair, None)
        _save_armed()
        log(f'   {NAME} {pair}: условие наступило — {met}; '
            f'{verdict["side"]} от {verdict["entry"]:.6g}')
        out.append({
            'pair': pair,
            'signal': _reshape(pair, verdict, df),
            'score': verdict.get('votes', 0),
            'rr': verdict['rr'],
            'poi_type': 'LLM',
            'df_1h': df,
        })
    return out


# Свечи других таймфреймов для графика в сообщении: frames(pair, tf, limit).
# Ставится сканером на каждом цикле; в проверках остаётся пустым, и график
# рисуется по часовым, что на руках.
_frames = None


def _notify_setup(signal, df):
    """План принят — сообщение с графиком. Отказ отправки торговле не мешает."""
    try:
        import telegram_notify as tg
        tg.llm_setup_found(signal, df if hasattr(df, 'columns') else None,
                           frames=_frames)
    except Exception as exc:                       # noqa: BLE001
        log(f'   {NAME}: уведомление о сетапе не отправлено — {exc}')


def _notify_rejected(pair, verdict):
    try:
        import telegram_notify as tg
        tg.llm_setup_rejected(pair, verdict.get('side', ''), float(verdict.get('entry') or 0),
                              verdict.get('gate', ''), verdict.get('detail', ''))
    except Exception:                              # noqa: BLE001
        pass


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
            'analysis_parts': verdict.get('analysis_parts'),
            'trigger': verdict.get('trigger', ''),
            'why': verdict.get('why', ''),
            'stop_why': verdict.get('stop_why', ''),
            'tp_why': verdict.get('tp_why', ''),
            'stop_level': verdict.get('stop_level'),
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
            'trigger_when': verdict.get('trigger_when', 'now'),
            'trigger_level': verdict.get('trigger_level'),
            'trigger_id': verdict.get('trigger_id'),
            'critic': verdict.get('critic') or {},
            'bias': verdict.get('bias', ''),
            # Уровни, из которых модель выбирала: график рисует по ним
            # подписи «L3 · пивот-максимум · вход».
            'levels': [{'id': lv.get('id'), 'price': lv.get('price'), 'kind': lv.get('kind')}
                       for lv in (verdict.get('levels') or [])],
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
                    market=None, frames=None):
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
    global _cursor, _frames

    if not llm_local.available():
        log(f'   {NAME}: модель недоступна — стратегия простаивает')
        return []

    if candles is None:
        import exchange

        def candles(pair):
            return exchange.fetch_ohlcv('1h', limit=500, symbol=pair,
                                        client=client)

        if frames is None:
            def frames(pair, tf, limit):
                return exchange.fetch_ohlcv(tf, limit=limit, symbol=pair,
                                            client=client)
    _frames = frames

    out = _collect(_harvest())
    out += _check_armed(candles)

    if market is None:
        benchmark = {}

        def market(pair, df):
            # Свечи BTC для альткоина — один запрос на цикл, и только когда
            # он нужен: для самого BTC ориентир бессмыслен.
            btc = None
            if pair != 'BTCUSDT':
                if 'df' not in benchmark:
                    try:
                        benchmark['df'] = candles('BTCUSDT')
                    except Exception as exc:       # noqa: BLE001
                        log(f'   {NAME}: свечи BTC не получены — {exc}')
                        benchmark['df'] = None
                btc = benchmark['df']
            return llm_market.snapshot(pair, df, client=client, benchmark=btc)

    if busy():
        log(f'   {NAME}: модель занята парой {busy()}, жду её')
        return out

    queue = _queue(pairs, skip=_busy_pairs(pairs, gate))
    if not queue:
        log(f'   {NAME}: все пары разобраны недавно или заняты планами, жду')
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


_alerted = {}      # имя поломки -> когда сообщали. Не чаще раза в час на имя.


def _alert_broken(pair, verdict, now=None):
    """Сообщение в Telegram о поломке разбора, не чаще раза в час на имя."""
    now = now if now is not None else time.time()
    gate = verdict.get('gate', '')
    if now - _alerted.get(gate, 0) < 3600:
        return
    _alerted[gate] = now
    try:
        import telegram_notify as tg
        tg.error_alert(f'ИИ {pair}: {gate} — {str(verdict.get("detail", ""))[:200]}')
    except Exception:                              # noqa: BLE001
        pass


def _observe(pair, df, verdict):
    """Заводит наблюдение за исходом: куда пошла цена после вердикта."""
    try:
        import llm_outcomes
        from datetime import datetime, timezone
        price = float(df['close'].iloc[-1])
        ts = int(df['timestamp'].iloc[-1].timestamp() * 1000)
        llm_outcomes.watch(pair, verdict, price, ts,
                           at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    except Exception:                              # noqa: BLE001
        pass                                       # свечи не DataFrame — так в проверках


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
        _observe(pair, df, verdict)

        # Поломка ответом не является: по такой паре спросить надо снова,
        # а не через час. Метку снимаем — и говорим вслух: сто девятнадцать
        # одинаковых поломок подряд однажды девять часов никто не видел.
        if verdict.get('gate') in llm_decide.BROKEN_GATES:
            _asked.pop(pair, None)
            _alert_broken(pair, verdict)

        if not verdict.get('ok'):
            log(f'   {NAME} {pair}: отказ — {verdict["gate"]}'
                + (f' ({verdict["detail"]})' if verdict.get('detail') else ''))
            _refuse(pair, verdict)
            # Поломка — не отказ: повод не «отработан», спросим снова.
            if verdict.get('gate') not in llm_decide.BROKEN_GATES:
                _refused[pair] = {'at': time.time(),
                                  'sig': _asked_sig.get(pair, frozenset())}
            # Модель ПРЕДЛОЖИЛА сетап (есть направление и вход), а отклонил
            # код или критик — человеку это интересно: видно, что модель
            # ищет, и что её останавливает.
            if verdict.get('side') and verdict.get('entry'):
                _notify_rejected(pair, verdict)
            continue

        if age > limit:
            log(f'   {NAME} {pair}: вердикт устарел на {age / 60:.0f} мин — '
                f'не беру')
            _refuse(pair, {'gate': 'вердикт устарел',
                           'detail': f'ответ пришёл через {age / 60:.0f} мин при '
                                     f'пределе {limit // 60}'})
            continue

        _refused.pop(pair, None)
        signal = _reshape(pair, verdict, df)
        _notify_setup(signal, df)

        if verdict.get('trigger_when', 'now') != 'now' and verdict.get('trigger_level'):
            _arm(pair, verdict)
            continue

        log(f'   {NAME} {pair}: {verdict["side"]} от {verdict["entry"]:.6g}, '
            f'R:R {verdict["rr"]}, EV {verdict["ev"]}, '
            f'конфлюенс {verdict["votes"]}/5 (разбор занял {age:.0f} с)')
        out.append({
            'pair': pair,
            'signal': signal,
            'score': verdict.get('votes', 0),
            'rr': verdict['rr'],
            'poi_type': 'LLM',
            'df_1h': df,
        })
    return out
