"""
Панель не ходит в сеть, а такт идёт часто.

ЖАЛОБА: «приложение постоянно глючит, когда получает данные с полимаркета».

ПРИЧИНА БЫЛА В АРХИТЕКТУРЕ, а не в сети. Панель дёргала биржу прямо в
обработчике HTTP-запроса: остаток, заявки, сделки, стаканы под оценку позиций.
На холодном кэше это секунды ожидания, а иногда и таймаут — приложение
выглядело подвисающим ровно в тот момент, когда на него смотрят.

Между тем торговый поток и так говорит с биржей каждый такт. Пусть он и греет
кэш, а панель только читает готовое.

ЗАОДНО ТАКТ СТАЛ ЧАЩЕ. Тридцать секунд — это окно, в котором рынок успевает
пройти сквозь нашу заявку до того, как мы её снимем. Укоротить его мешала
ЛЕНТА: по запросу на каждый рынок с заявками, при двадцати пяти рынках — двадцать
пять запросов на такт. В живом режиме она нужна только для мнения модели, а
позиции и деньги ведёт биржа.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import polymarket  # noqa: E402
from polymarket import params  # noqa: E402


class TestThePanelReadsAWarmCache:

    def test_the_trading_thread_refreshes_it(self):
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        body = text[text.index('def _loop('):text.index('def start(')]
        assert '_pm.exchange_view(force=True)' in body

    def test_the_refresh_never_breaks_the_cycle(self):
        """Кэш — удобство: его сбой не имеет права остановить торговлю."""
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        spot = text.index('_pm.exchange_view(force=True)')
        assert 'except Exception' in text[spot:spot + 200]

    def test_the_cache_outlives_a_cycle(self):
        """
        Иначе панель регулярно попадает на холодный кэш и идёт в сеть сама —
        с секундами ожидания и случайными таймаутами.
        """
        assert polymarket._EXCHANGE_TTL > params.MM_POLL_SECONDS * 2


class TestTheCycleIsCheapEnoughToBeFrequent:

    def test_the_tape_is_skipped_in_live_mode(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index('need_tape = []')
        block = text[spot:spot + 400]
        assert 'if not live or want_shadow:' in block

    def test_paper_mode_always_needs_the_tape(self):
        """
        В бумаге исполнение определяется ТОЛЬКО по ленте — без неё не будет
        ни одного исполнения, и весь бумажный прогон станет пустым.
        """
        import inspect

        from polymarket import mm

        assert 'want_shadow' in inspect.signature(mm.step).parameters
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index('need_tape = []')
        assert 'not live' in text[spot:spot + 200]

    def test_the_model_is_still_checked_sometimes(self):
        """Сверка модели с делом не должна исчезнуть совсем."""
        assert 1 <= params.MM_SHADOW_EVERY <= 100

    def test_the_cycle_got_shorter(self):
        assert params.MM_POLL_SECONDS <= 15, 'окно, в котором нас подбирают'
