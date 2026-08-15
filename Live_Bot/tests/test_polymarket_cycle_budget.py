"""
Такт обязан заканчиваться, а отказы — затихать.

ЧТО НАБЛЮДАЛОСЬ НА ЖИВОМ СЧЁТЕ. Биржа отвечала с перебоями, и счётчик циклов
стоял на нуле пять минут подряд: заявки выставлялись, панель показывала ноль
тактов, снаружи бот выглядел мёртвым. Каждый рынок стоит нескольких обращений
по пять секунд каждое — десяток рынков растягивает такт на минуты.

И вторая половина той же беды: отвергнутая заявка повторялась КАЖДЫЙ такт.
Причина отказа редко меняется за полминуты — не тот счёт, не хватает денег,
рынок закрылся, — и за сутки журнал набрал 2480 ошибок против 125 удачных
заявок. Всё время цикла уходило на безнадёжные отправки.
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polymarket import params  # noqa: E402


class TestStepHasADeadline:

    def test_step_takes_a_deadline(self):
        import inspect

        from polymarket import mm

        assert 'deadline' in inspect.signature(mm.step).parameters

    def test_markets_left_over_are_counted_not_hidden(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        assert "skipped['не успели за такт'] = ran_out" in text
        assert "'ran_out': ran_out" in text

    def test_service_passes_a_budget(self):
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        assert 'params.MM_STEP_BUDGET_SECONDS' in text
        assert "out.get('ran_out')" in text, 'о нехватке времени говорим вслух'

    def test_the_budget_is_longer_than_a_tick(self):
        """Срок такта должен давать циклу шанс, а не резать его на входе."""
        assert params.MM_STEP_BUDGET_SECONDS >= params.MM_POLL_SECONDS


class TestRefusedOrdersBackOff:

    def test_backoff_doubles_and_has_a_ceiling(self):
        wait = params.MM_RETRY_SECONDS
        seen = []
        for _ in range(8):
            wait = min(wait, params.MM_RETRY_MAX_SECONDS)
            seen.append(wait)
            wait *= 2
        assert seen[0] == params.MM_RETRY_SECONDS
        assert max(seen) <= params.MM_RETRY_MAX_SECONDS
        assert seen[-1] > seen[0], 'отступ обязан расти'

    def test_step_skips_orders_that_are_still_waiting(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        assert "if order.get('retry_at') and time.time() < order['retry_at']:" in text

    def test_success_clears_the_backoff(self):
        """Заявка ушла — отступ забывается, иначе он копился бы вечно."""
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        assert "order.pop('retry_at', None)" in text
        assert "order.pop('retry_wait', None)" in text


class TestBothBooksAreTheSameBook:
    """
    ЗАМЕР, КОТОРЫЙ ЗАКРЫЛ ЦЕЛОЕ НАПРАВЛЕНИЕ РАБОТЫ.

    Была мысль выбирать сторону по более короткой очереди из двух токенов
    рынка. Замер по шести рынкам показал, что выбирать нечего: глубина на цене
    A в книге «ДА» и на цене (1-A) в книге «НЕТ» совпадает ДО КОНТРАКТА.

        Newsom      0.162: 6415 контрактов   |  0.838: 6415
        Buttigieg   0.053: 15722             |  0.947: 15722
        Путин       0.080: 219701            |  0.920: 219701

    Это одна и та же книга, показанная с двух сторон. Очередь у обеих
    сторон общая, и «выбрать короче» невозможно в принципе.
    """

    def test_the_finding_is_written_down(self):
        text = open(os.path.join(ROOT, 'polymarket', 'book.py'),
                    encoding='utf-8').read()
        assert 'ОДНА И ТА ЖЕ КНИГА' in text, \
            'иначе эту мысль предложат заново через месяц'
