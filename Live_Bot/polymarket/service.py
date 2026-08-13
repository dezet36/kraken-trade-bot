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
_state = {'running': False, 'cycles': 0, 'last_error': None, 'last_at': None}


def autostart_enabled():
    return (os.getenv('PM_AUTOSTART', '') or '').strip().lower() in ('1', 'true', 'да')


def status():
    return dict(_state)


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
            try:
                before = maker.mark_to_market({})
                out = mm.step(maker, markets, live=live,
                              day_loss=max(0.0, -before['pnl']))
                _state['cycles'] += 1
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
            time.sleep(poll_seconds)
    finally:
        _state['running'] = False
        if live:
            log(f"◈ Polymarket: снимаю заявки — {executor.cancel_all()}")


def start(poll_seconds=None):
    """
    Поднимает маркет-мейкер фоновым потоком. Возвращает True, если запущен.

    Повторный вызов ничего не делает: два потока котировали бы одни и те же
    рынки, перебивая заявки друг друга и удваивая размер.
    """
    global _thread
    if not autostart_enabled():
        return False
    if _thread is not None and _thread.is_alive():
        return True
    from . import params
    seconds = int(poll_seconds or params.MM_POLL_SECONDS)
    _thread = threading.Thread(target=_loop, args=(seconds,), daemon=True,
                               name='polymarket-mm')
    _thread.start()
    return True
