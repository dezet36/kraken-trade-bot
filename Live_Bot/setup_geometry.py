"""
Разметка сетапа для графиков: зоны и уровни, по которым принято решение.

ЖИЛА ЭТА ФУНКЦИЯ ВНУТРИ БУМАЖНОГО БРОКЕРА, и это было не на месте. Разметка
нужна ДВУМ рисовальщикам: графику сделки в панели и картинке, которая уходит в
Telegram. Второй её не получал вовсе и годами рисовал одни и те же три линии
плана для всех четырёх стратегий — вход, стоп, цель, — отчего по картинке
нельзя было понять, на чём стратегия вообще сработала.

Скопировать сюда её вторую версию было нельзя: две реализации одной разметки
разошлись бы при первой же правке, и на двух графиках одной сделки оказались бы
разные зоны. В этом проекте такое уже случалось — у стратегии уровней замер и
бой имели РАЗНЫЕ реализации, и стоило это месяца недостоверных наблюдений.

Форма общая для всех стратегий: полосы и линии. Рисовальщику незачем знать,
ордер-блок перед ним или зона Фибоначчи.
"""

import config
import glossary


def build(strategy, signal):
    """
    Разметка, по которой стратегия принимала решение: зоны и уровни.

    Без неё график сделки — свечи с тремя линиями плана, и по нему нельзя
    проверить главное: вошли ли там, где собирались. Зона коррекции,
    ордер-блок, уровень — это и есть сам сетап; цена входа лишь его
    следствие.

    Разметка СОХРАНЯЕТСЯ вместе со сделкой, а не считается заново при
    показе. Параметры стратегий меняются, и пересчёт нарисовал бы зоны,
    которых в момент входа не было, — разбор сделки превратился бы в
    разбор сегодняшних настроек.

    Форма общая для всех стратегий: полосы и линии. Рисовальщику незачем
    знать, ордер-блок перед ним или зона Фибоначчи.
    """
    bands, lines = [], []
    setup = signal.get('setup') or {}
    out = {'bands': bands, 'lines': lines}

    def band(low, high, label, main=False):
        """
        Полоса на графике. main — ЗОНА, В КОТОРУЮ ВХОДИМ.

        Разделение появилось потому, что все полосы рисовались одинаково
        бледными, и на картинке нельзя было отличить зону сделки от
        контекста. У SMC это ордер-блок против имбаланса, у Фибоначчи —
        зона A, где стоит лимит, против зоны B, которая только показывает
        границу инвалидации.
        """
        if low and high and float(high) != float(low):
            bands.append({'bottom': min(float(low), float(high)),
                          'top': max(float(low), float(high)),
                          'label': label, 'main': bool(main)})

    def _iso_time(value):
        """Метка времени в строгом ISO с зоной либо None."""
        if not value:
            return None
        try:
            import pandas as pd
            stamp = pd.Timestamp(value)
            if stamp.tzinfo is not None:
                stamp = stamp.tz_convert('UTC').tz_localize(None)
            return stamp.strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception:                      # noqa: BLE001
            return None

    def leg(label_from, label_to):
        """
        Сам импульс: две горизонтали по его краям и, если есть время, —
        отрезок из точки A в точку B.

        Горизонтали остаются: по ним читаются цены. Но именно ОТРЕЗОК
        отвечает на вопрос «по какому сетапу вошли» — видно, откуда куда
        и за сколько сходила цена, а вход стоит на откате от него.

        Время начала едет отдельным полем: по нему дашборд разворачивает
        окно графика назад, до начала импульса. Без него график начинался
        от входа, и разметка висела в воздухе — зоны есть, а движения,
        которое их породило, за левым краем.
        """
        if not (setup.get('start_price') and setup.get('end_price')):
            return
        lines.append({'price': float(setup['start_price']), 'label': label_from})
        lines.append({'price': float(setup['end_price']), 'label': label_to})
        start_at = _iso_time(setup.get('start_time'))
        end_at = _iso_time(setup.get('end_time'))
        if start_at:
            out['from'] = start_at
            if end_at:
                out['leg'] = {
                    'from': start_at, 'from_price': float(setup['start_price']),
                    'to': end_at, 'to_price': float(setup['end_price']),
                }

    if strategy == 'FIBO':
        za, zb = signal.get('zone_a') or {}, signal.get('zone_b') or {}
        # Границы в подписи берутся ИЗ КОНФИГА, а не пишутся руками.
        # Написанная руками подпись зоны B утверждала «61.8–88.6%», тогда
        # как зона стоит на 78.6–88.6%: на графике всё было нарисовано
        # правильно, а прочитать с него можно было неверное число.
        def _pct(value):
            return f'{value * 100:.1f}'.rstrip('0').rstrip('.')

        # Зона A — главная: именно в ней стоит лимит. Зона B показывает,
        # где сетап перестаёт быть действительным.
        band(za.get('bottom'), za.get('top'),
             f'зона A · {_pct(config.ZONE_A_BOTTOM)}–{_pct(config.ZONE_A_TOP)}%',
             main=True)
        band(zb.get('bottom'), zb.get('top'),
             f'зона B · {_pct(config.ZONE_B_BOTTOM)}–{_pct(config.ZONE_B_TOP)}%')
        leg('начало импульса', 'конец импульса')
    elif strategy == 'SMC':
        smc = signal.get('smc') or {}
        band(smc.get('poi_bottom'), smc.get('poi_top'),
             glossary.poi_type(smc.get('poi_type')), main=True)
        # Имбаланс — вторая половина основания сделки. Он участвует в
        # отборе, но на графике его не было вовсе.
        band(smc.get('fvg_bottom'), smc.get('fvg_top'), 'имбаланс (FVG)')
        leg('начало движения', 'конец движения')
        # ОТ ЧЕГО СТРОИЛАСЬ СТРУКТУРА. Ордер-блок показывает, ГДЕ вход;
        # пробитый уровень и снятая ликвидность отвечают, ПОЧЕМУ вообще
        # эта сторона. Оба значились в факторах отбора и оба до графика не
        # доходили — на картинке было видно следствие без причины.
        if smc.get('structure_level'):
            lines.append({
                'price': float(smc['structure_level']),
                'label': (glossary.structure_event(smc.get('structure_type'))
                          + ' · пробитый уровень'),
            })
        if smc.get('sweep_price'):
            lines.append({
                'price': float(smc['sweep_price']),
                'label': (glossary.liquidity_side(smc.get('sweep_side'))
                          + ' · снята'),
            })
    elif strategy == 'LEVELS':
        lv = signal.get('levels') or {}
        if lv.get('level'):
            count = lv.get('touches')
            lines.append({
                'price': float(lv['level']),
                'label': f"уровень · касаний {count}" if count else 'уровень',
                # Главная линия сетапа: у этой стратегии уровень — не
                # вспомогательная разметка, а сам повод для сделки.
                # Рисовалась она тем же бледным пунктиром, что и края
                # импульса у соседей, и на графике её приходилось искать.
                'main': True,
            })
        # Сетап этой стратегии — сам уровень, а уровень сделан касаниями.
        # Поэтому её график разворачивается до ПЕРВОГО касания, а не до
        # начала какого-то движения: движения тут нет вовсе. Без этого
        # окно начиналось за час до входа, подпись обещала «касаний 3», а
        # проверить это на картинке было нечем.
        start_at = _iso_time(setup.get('start_time'))
        if start_at:
            out['from'] = start_at
        points = [p for p in (setup.get('touches_at') or [])
                  if p.get('at') and p.get('price')]
        if points:
            out['touches'] = points
    elif strategy == 'RSIBB':
        bb = signal.get('rsibb') or {}
        # Канал целиком — это и есть сетап: цена вышла за край, цель на
        # середине. Без обеих границ на графике видно только вход и цель,
        # а откуда они взялись — нет.
        band(bb.get('lower'), bb.get('upper'),
             f"канал Боллинджера · RSI {bb['rsi']:.0f}" if bb.get('rsi')
             is not None else 'канал Боллинджера')
        if bb.get('mid'):
            lines.append({'price': float(bb['mid']),
                          'label': 'средняя линия — цель', 'main': True})
        if bb.get('band'):
            lines.append({
                'price': float(bb['band']),
                'label': 'полоса — вход',
                # Главная линия сетапа: именно на ней стоит лимитная
                # заявка, и вся арифметика издержек держится на том, что
                # цена приходит к ней сама.
                'main': True,
            })
        start_at = _iso_time(setup.get('start_time'))
        if start_at:
            out['from'] = start_at
    return out
