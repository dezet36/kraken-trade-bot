"""
Запуск маркет-мейкера вместе с ботом, отдельным потоком.

ПОЧЕМУ ПОТОК, А НЕ ВСТРОЕННЫЙ ВЫЗОВ В ЦИКЛ БОТА. У бота свой такт, привязанный
к пятиминутным свечам биржи; у маркет-мейкера свой, привязанный к жизни стакана.
Смешав их, мы получили бы либо ленивое котирование, либо лишние опросы биржи —
и, что хуже, сбой одного останавливал бы другое.

ПАДЕНИЕ МАРКЕТ-МЕЙКЕРА НЕ ДОЛЖНО ОСТАНАВЛИВАТЬ ТОРГОВЛЮ НА БИРЖЕ, и наоборот.
Это разные площадки, разные деньги и разные риски. Поток ловит всё, пишет в
журнал и продолжает со следующего такта: остановиться молча — худшее, что он
может сделать, потому что заявки останутся висеть.

ВЫКЛЮЧЕН ПО УМОЛЧАНИЮ. Маркет-мейкер запускается только при PM_AUTOSTART=1.
Причина не в осторожности вообще, а в том, что бумажный прогон пока не дал
исполнений: включать по умолчанию то, о чём нечего сказать числами, значит
навязывать пользователю непроверенное.
"""

import os
import threading
import time

_thread = None
_state = {'running': False, 'cycles': 0, 'last_error': None,
          'last_at': None, 'stopping': False}

# Сон между тактами прерываемый. Обычный time.sleep держал бы поток до конца
# такта — до полуминуты, — а всё это время на Polymarket стоят наши заявки.
# При закрытии программы это ровно то время, которое они висят без присмотра.
_wake = threading.Event()


def autostart_enabled():
    return (os.getenv('PM_AUTOSTART', '') or '').strip().lower() in ('1', 'true', 'да')


def status():
    """Состояние потока. alive отвечает на вопрос «работает ли ПРЯМО СЕЙЧАС»."""
    out = dict(_state)
    out['alive'] = bool(_thread is not None and _thread.is_alive())
    out['autostart'] = autostart_enabled()
    return out


def stop():
    """
    Просит поток завершиться и снимает заявки.

    Останавливать поток насильно нельзя: он может быть в середине отправки, и
    прерывание оставило бы заявку на бирже без записи о ней. Поэтому ставится
    признак, а поток выходит на следующем такте, сняв за собой всё.
    """
    _state['stopping'] = True
    _wake.set()
    return True


def _loop(poll_seconds):
    from logger import log

    from . import engine, executor, mm, params, wallet

    try:
        markets = mm.select_markets()
    except Exception as exc:                                # noqa: BLE001
        log(f"⚠️ Polymarket: рынки не отобрались ({exc}) — маркет-мейкер не запущен")
        _state['running'] = False
        _state['last_error'] = str(exc)[:200]
        return

    maker = engine.PaperMaker()
    live = wallet.status()['can_trade_live']
    log(f"◈ Polymarket: маркет-мейкер запущен, рынков {len(markets)}, "
        f"режим {'ЖИВЫЕ ДЕНЬГИ' if live else 'бумага'}, "
        f"капитал ${maker.bankroll:,.0f}")
    _state['running'] = True

    try:
        while True:
            if _state.get('stopping'):
                log('◈ Polymarket: остановка по запросу')
                return
            try:
                before = maker.mark_to_market({})
                out = mm.step(maker, markets, live=live,
                              day_loss=max(0.0, -before['pnl']))
                _state['cycles'] += 1
                # Отметка живости на замке: без неё замок службы протухал бы
                # через пять минут, и вторая копия сочла бы его брошенным.
                mm._touch_lock()
                _state['last_at'] = engine._stamp()
                _state['last_error'] = None
                if out['fills']:
                    log(f"◈ Polymarket: исполнений {len(out['fills'])}, "
                        f"капитал ${out['report']['equity']:,.2f}")
            except Exception as exc:                        # noqa: BLE001
                # Ошибка одного такта не должна останавливать поток: заявки
                # останутся висеть, а мы перестанем их вести.
                _state['last_error'] = str(exc)[:200]
                log(f"⚠️ Polymarket: такт не прошёл ({str(exc)[:120]})")
            _wake.wait(poll_seconds)
    finally:
        _state['running'] = False
        if live:
            log(f"◈ Polymarket: снимаю заявки — {executor.cancel_all()}")


def start(poll_seconds=None, force=False):
    """
    Поднимает маркет-мейкер фоновым потоком. Возвращает True, если запущен.

    force=True — запуск ПО ПРЯМОМУ ДЕЙСТВИЮ человека, минуя PM_AUTOSTART.
    Переменная отвечает на вопрос «поднимать ли самому при старте бота», а
    нажатие кнопки — это уже ответ, и спрашивать второй раз незачем. Без этого
    на сервере с собранным приложением запустить маркет-мейкер было бы нечем:
    консоли там нет, а совет «выполните команду из папки» к делу не относится.

    Повторный вызов ничего не делает: два потока котировали бы одни и те же
    рынки, перебивая заявки друг друга и удваивая размер.
    """
    global _thread
    if not force and not autostart_enabled():
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _state['last_error'] = None

    # ЗАМОК БЕРЁТСЯ И ЗДЕСЬ. Проверка на живой поток ловит только повтор внутри
    # приложения, а маркет-мейкер запускается ещё и командой из консоли — и это
    # ДРУГОЙ процесс с тем же файлом состояния. Два таких пишут вперемешку:
    # заявки разного размера, число рынков скачет от цикла к циклу, позиции
    # одного затираются другим. Ровно это уже случалось, и замок был поставлен
    # в командный запуск — но служба его не спрашивала, и дыра осталась
    # открытой ровно наполовину.
    from . import mm, params
    if not mm._single_instance():
        _state['last_error'] = ('маркет-мейкер уже работает в другом процессе '
                                '— второй запуск отменён')
        try:
            from logger import log as _say
            _say(f"⚠️ Polymarket: {_state['last_error']}")
        except Exception:                                   # noqa: BLE001
            pass
        return False
    seconds = int(poll_seconds or params.MM_POLL_SECONDS)
    _state['stopping'] = False
    _wake.clear()
    try:
        _thread = threading.Thread(target=_loop, args=(seconds,), daemon=True,
                                   name='polymarket-mm')
        _thread.start()
    except Exception as exc:                                # noqa: BLE001
        # Молчаливый отказ здесь неотличим от «кнопка не работает»: человек
        # жмёт, поток не поднимается, и на экране ровно ничего.
        _state['last_error'] = f'{type(exc).__name__}: {str(exc)[:160]}'
        return False
    return True
