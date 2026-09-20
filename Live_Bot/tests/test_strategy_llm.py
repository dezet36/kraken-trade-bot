"""
Пятая стратегия: как обходит пары, что делает с вердиктом и чего не должна.

ГЛАВНОЕ, ЧТО СТЕРЕГУТ ЭТИ ПРОВЕРКИ — ЦИКЛ НЕ ЖДЁТ МОДЕЛЬ, И РЕШЕНИЕ СВОЁ.

Модель разбирает одну пару пять-семь минут. Цикл отдаёт ей пару и уходит, а
вердикт забирает следующим оборотом: так стопы и цели остальных четырёх
стратегий по-прежнему проверяются раз в пять минут.

Чужих кандидатов здесь нет вовсе — первый вариант их получал, но сетапа
донора модель не видела, а пары ей доставались лишь когда что-то находили
другие. Теперь очередь пар своя, и каждая сделка от разметки до уровней
собрана этой стратегией.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_llm

PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']


def approving_verdict(**over):
    out = {
        'ok': True, 'gate': '', 'detail': '',
        'side': 'LONG', 'entry': 100.0, 'stop': 97.0,
        'targets': [107.0, 109.0], 'inval': 97.0,
        'p': 0.58, 'rr': 2.33, 'ev': 0.9, 'cost_r': 0.025,
        'votes': 4, 'confluence': {'poi': True, 'vp': True, 'der': True,
                                   'smc': True, 'flow': False},
        'regime': 'тренд вверх', 'analysis': 'откат к скоплению минимумов',
        'trigger': 'возврат в уровень', 'why': 'скопление снизу свежее',
        'risk': 'слив открытого интереса', 'alt': 'пробой вниз отменяет идею',
        'ids': {'entry': 'L5', 'stop': 'L7'},
    }
    out.update(over)
    return out


def refusing_decide(recorder=None):
    """Модель, которая всегда отказывается, и запоминает, о чём её спросили."""
    def decide(pair, *args, **kwargs):
        if recorder is not None:
            recorder.append(pair)
        return {'ok': False, 'gate': 'модель пропустила', 'detail': ''}
    return decide


def cycle(pairs, candles=lambda pair: [0] * 500):
    """
    Один оборот бота: отдать пару, дождаться модель, забрать вердикт.

    Ожидание здесь — свойство ПРОВЕРКИ, а не бота: в жизни вердикт забирает
    следующий цикл через несколько минут, и ждать его никто не будет.
    """
    strategy_llm.scan_for_setups(pairs, gate=None, candles=candles)
    strategy_llm.join(15)
    return strategy_llm.scan_for_setups(pairs, gate=None, candles=candles)


@pytest.fixture(autouse=True)
def _forget_previous_pairs(tmp_path):
    """
    Состояние модуля глобальное: память о парах, курсор, очередь и поток.

    Не почистив, вторая проверка получила бы ответ первой и прошла бы по
    ошибке — или, хуже, заняла бы «модель» потоком, который уже не нужен.
    Файл взведённых планов — во временной папке, чтобы проверки не читали
    планы настоящего бота и не писали в его папку.
    """
    strategy_llm.join(10)
    strategy_llm._asked.clear()
    strategy_llm._done.clear()
    strategy_llm._busy = None
    strategy_llm._cursor = 0
    strategy_llm._armed.clear()
    strategy_llm._ARMED_FILE = str(tmp_path / 'llm_armed.json')
    strategy_llm._armed_loaded = True
    yield
    strategy_llm.join(10)
    strategy_llm._asked.clear()
    strategy_llm._done.clear()
    strategy_llm._busy = None
    strategy_llm._cursor = 0


class TestItNeedsAModelAndPairs:

    def test_no_pairs_gives_nothing(self, monkeypatch):
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        assert strategy_llm.scan_for_setups([], gate=None) == []

    def test_without_a_model_it_stands_idle(self, monkeypatch):
        """
        Модели нет — стратегия простаивает и ничего не торгует. Иначе в
        журнале появились бы сделки LLM, которых модель не видела.
        """
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: False)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide(asked))
        assert strategy_llm.scan_for_setups(PAIRS, gate=None) == []
        assert asked == []


class TestItWalksThePairsInTurn:
    """
    Одна пара за цикл, по кругу. Без курсора первая пара списка разбиралась
    бы каждый раз первой, а последние — никогда.
    """

    def test_one_pair_per_cycle(self, monkeypatch):
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide(asked))
        strategy_llm.scan_for_setups(PAIRS, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        assert asked == ['BTCUSDT']

    def test_the_next_cycle_takes_the_next_pair(self, monkeypatch):
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide(asked))
        for _ in range(3):
            strategy_llm.scan_for_setups(PAIRS, gate=None,
                                         candles=lambda pair: [0] * 500)
            strategy_llm.join(15)
        assert asked == PAIRS

    def test_a_pair_without_candles_is_skipped_not_fatal(self, monkeypatch):
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide(asked))

        def candles(pair):
            if pair == 'BTCUSDT':
                raise RuntimeError('биржа не ответила')
            return [0] * 500
        strategy_llm.scan_for_setups(PAIRS, gate=None, candles=candles)
        strategy_llm.join(15)
        assert asked == ['ETHUSDT']

    def test_the_snapshot_failure_does_not_block_the_analysis(self, monkeypatch):
        """Снимок не обязателен: без него в разметке прочерки, но разбор идёт."""
        seen = {}
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda pair, df, ask, market=None, **k:
                            seen.setdefault('market', market) or
                            {'ok': False, 'gate': 'модель пропустила', 'detail': ''})

        def market(pair, df):
            raise RuntimeError('стакан не отдали')
        strategy_llm.scan_for_setups(PAIRS, gate=None,
                                     candles=lambda pair: [0] * 500,
                                     market=market)
        strategy_llm.join(15)
        assert seen['market'] is None

    def test_the_snapshot_reaches_the_model(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda pair, df, ask, market=None, **k:
                            seen.setdefault('market', market) or
                            {'ok': False, 'gate': 'модель пропустила', 'detail': ''})
        strategy_llm.scan_for_setups(PAIRS, gate=None,
                                     candles=lambda pair: [0] * 500,
                                     market=lambda pair, df: {'book': 'x'})
        strategy_llm.join(15)
        assert seen['market'] == {'book': 'x'}


class TestTheSignalIsItsOwn:
    """
    Сигнал собирается из вердикта с нуля: у него нет донора, из которого
    можно было бы взять структуру. Поля — те, что читает брокер.
    """

    def test_the_trade_is_marked_as_llm(self):
        signal = strategy_llm._reshape('BTCUSDT', approving_verdict())
        assert signal['strategy'] == 'LLM'
        assert signal['trading_pair'] == 'BTCUSDT'
        assert signal['setup']['type'] == 'LONG'
        assert signal['trigger']['zone'] == 'L5'

    def test_levels_come_from_the_model(self):
        params = strategy_llm._reshape('BTCUSDT', approving_verdict())['params']
        assert params['entry'] == 100.0
        assert params['stop_loss'] == 97.0
        assert params['tp_targets'] == [107.0, 109.0]
        assert params['take_profit_1'] == 107.0
        assert params['take_profit_2'] == 109.0
        assert params['sl_distance'] == pytest.approx(3.0)

    def test_the_direction_follows_the_verdict(self):
        signal = strategy_llm._reshape('BTCUSDT', approving_verdict(side='SHORT'))
        assert signal['setup']['type'] == 'SHORT'

    def test_the_risk_is_the_strategys_own(self, monkeypatch):
        # Подменяем В ТОМ объекте, которым пользуется стратегия: проверки
        # выгружают settings_store и импортируют заново, и «свежий»
        # settings_store и strategy_llm.settings бывают разными модулями.
        monkeypatch.setattr(strategy_llm.settings, 'risk_pct', lambda name: 0.7)
        params = strategy_llm._reshape('BTCUSDT', approving_verdict())['params']
        assert params['risk_pct'] == 0.7
        # Размер считает брокер по доле, депозиту и дистанции стопа.
        assert 'position_size' not in params
        assert 'risk_amount' not in params

    def test_the_exit_plan_matches_the_targets(self):
        params = strategy_llm._reshape('BTCUSDT', approving_verdict())['params']
        assert len(params['tp_fractions']) == len(params['tp_targets'])
        assert sum(params['tp_fractions']) == pytest.approx(1.0)

    def test_breakeven_is_off(self):
        """
        Модель сама называет уровень инвалидации. Подтянутый стоп выбивал бы
        позицию раньше, чем идея опровергнута.
        """
        params = strategy_llm._reshape('BTCUSDT', approving_verdict())['params']
        assert params['breakeven_after_tp'] is False
        assert params['invalidation'] == 97.0

    def test_the_analysis_reaches_the_signal(self):
        """
        Разбор — главная ценность записи: по нему потом видно, НА ЧЁМ модель
        ошиблась, а не только что ошиблась.
        """
        signal = strategy_llm._reshape('BTCUSDT', approving_verdict())
        for field in ('regime', 'analysis', 'trigger', 'why', 'risk', 'alt'):
            assert signal['llm'][field], f'поле {field} потеряно'
        assert signal['llm']['p'] == 0.58
        assert signal['llm']['donor'] == ''


class TestTheCycleIsNotHeldUp:
    """
    ЦИКЛ НЕ ЖДЁТ МОДЕЛЬ — ЭТО ГЛАВНОЕ СВОЙСТВО ПЯТОЙ СТРАТЕГИИ.

    Один разбор занимает на сервере пять-семь минут, и быстрой модели там
    нет: замер 19 сентября 2026 дал 1.8 токена в секунду у восьмимиллиардной и
    1.6 у тридцатипятимиллиардной MoE. При цикле в пять минут ожидание
    означало бы, что бот половину времени стоит.

    Опасность не в упущенных входах — заявки висят часами. Стопы, цели и
    перевод в безубыток проверяются РАЗ В ЦИКЛ: растянув цикл вдвое, мы вдвое
    огрубляем ведение сделок ОСТАЛЬНЫХ четырёх стратегий.
    """

    def test_the_cycle_returns_while_the_model_is_still_thinking(self,
                                                                 monkeypatch):
        import time as real_time

        started = real_time.time()

        def slow_decide(pair, df, ask, **kwargs):
            real_time.sleep(1.5)
            return {'ok': False, 'gate': 'модель пропустила', 'detail': ''}

        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide', slow_decide)

        strategy_llm.scan_for_setups(PAIRS, gate=None,
                                     candles=lambda pair: [0] * 500)
        assert real_time.time() - started < 0.7, 'цикл дождался модель'
        strategy_llm.join(5)

    def test_one_pair_at_a_time(self, monkeypatch):
        """
        Вторая модель в памяти — это ещё пять гигабайт и вдвое меньше ядер
        каждой: оба разбора вдвое медленнее вместо выигрыша. И llama_cpp на
        параллельные вызовы одного контекста не рассчитан.
        """
        import time as real_time

        asked = []

        def slow_decide(pair, df, ask, **kwargs):
            asked.append(pair)
            real_time.sleep(1.0)
            return {'ok': False, 'gate': 'модель пропустила', 'detail': ''}

        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide', slow_decide)

        many = [f'P{i}USDT' for i in range(20)]
        for _ in range(3):                       # три цикла подряд, модель занята
            strategy_llm.scan_for_setups(many, gate=None,
                                         candles=lambda pair: [0] * 500)
        assert asked == ['P0USDT'], asked
        strategy_llm.join(5)


class TestRefusalsAreRecorded:

    def test_a_refusal_goes_to_the_log(self, monkeypatch):
        """
        Отказы модели проверяются так же, как отказы предохранителей: без
        записи нельзя будет узнать, правильно ли она отсекала.
        """
        written = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *a, **k: {'ok': False,
                                             'gate': 'мало конфлюенса',
                                             'detail': '2 из 5', 'why': ''})

        import refused
        monkeypatch.setattr(refused, 'record',
                            lambda *a, **k: written.append(a))

        # Разбор идёт сбоку, поэтому отказ попадает в журнал следующим циклом.
        cycle(PAIRS)
        assert written, 'отказ модели не записан'
        assert written[0][0] == 'LLM'
        assert written[0][1]['trading_pair'] == 'BTCUSDT'


class TestItIsAStrategyLikeTheOthers:

    def test_both_lists_agree(self):
        import paper_broker
        import settings_store

        assert 'LLM' in paper_broker.STRATEGIES
        assert paper_broker.STRATEGIES == settings_store.STRATEGIES

    def test_it_has_its_own_budget(self):
        import config
        assert config.PAPER_START_BALANCES['LLM'] > 0


class TestTheJournalsStayInStep:
    """
    Колонки модели обязаны быть в ОБОИХ журналах. Боевой уже отставал от
    бумажного на двенадцать колонок — чинили там, куда смотрели.
    """

    def test_llm_columns_exist_in_both(self):
        import paper_broker
        import trade_journal

        paper = {c for c in paper_broker.COLUMNS if c.startswith('llm_')}
        live = {c for c in trade_journal.COLUMNS if c.startswith('llm_')}
        assert paper, 'в бумажном журнале нет колонок модели'
        assert paper == live, f'расходятся: {paper ^ live}'

    def test_a_non_llm_trade_leaves_them_empty(self):
        import paper_broker
        empty = paper_broker._llm_columns(None)
        assert set(empty) == {c for c in paper_broker.COLUMNS
                              if c.startswith('llm_')}
        assert all(value == '' for value in empty.values())


class TestThePairIsNotReExaminedEveryCycle:
    """
    Разметка по часовым свечам за пять минут не меняется, ответ при тех же
    данных тот же. 19 сентября 2026 одна и та же пара разбиралась 119 раз
    подряд по 165 секунд.
    """

    def test_a_small_pool_is_not_asked_again_within_the_window(self, monkeypatch):
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide(asked))

        for _ in range(3):
            strategy_llm.scan_for_setups(['BTCUSDT'], gate=None,
                                         candles=lambda pair: [0] * 500)
            strategy_llm.join(15)
        assert asked == ['BTCUSDT'], f'модель спросили {len(asked)} раза'

    def test_after_the_window_it_is_asked_again(self, monkeypatch):
        """
        Рынок меняется: через час та же пара стоит в другой обстановке, и
        ответ может стать другим. Запрет навсегда был бы враньём.
        """
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide(asked))

        strategy_llm.scan_for_setups(['BTCUSDT'], gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        import llm_urgency
        minutes = strategy_llm.config.LLM_REASK_AFTER_MIN
        # Окно повтора прошло, но повода нет — пара ждёт события…
        for key in list(strategy_llm._asked):
            strategy_llm._asked[key] -= minutes * 60 + 1
        strategy_llm.scan_for_setups(['BTCUSDT'], gate=None,
                                     candles=lambda pair: [0] * 500)
        assert len(asked) == 1
        # …или планового прохода раз в STALE_HOURS.
        for key in list(strategy_llm._asked):
            strategy_llm._asked[key] -= llm_urgency.STALE_HOURS * 3600
        strategy_llm.scan_for_setups(['BTCUSDT'], gate=None,
                                     candles=lambda pair: [0] * 500)
        assert len(asked) == 2

    def test_a_reason_reopens_the_pair_after_the_window(self, monkeypatch):
        """Событие (цена у уровня, слом, ликвидации) — и пара идёт снова."""
        import llm_urgency
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide', refusing_decide(asked))
        strategy_llm.scan_for_setups(['BTCUSDT'], gate=None, candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        for key in list(strategy_llm._asked):
            strategy_llm._asked[key] -= strategy_llm.config.LLM_REASK_AFTER_MIN * 60 + 1
        monkeypatch.setattr(llm_urgency, 'rank',
                            lambda pairs, context_of, now_ms=None: [(p, 2, ['цена у L5']) for p in pairs])
        strategy_llm.scan_for_setups(['BTCUSDT'], gate=None, candles=lambda pair: [0] * 500)
        assert len(asked) == 2

    def test_the_round_continues_past_recently_asked_pairs(self, monkeypatch):
        """Разобранная пара уступает очередь неразобранной, а не блокирует круг."""
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide(asked))
        for _ in range(4):
            strategy_llm.scan_for_setups(PAIRS, gate=None,
                                         candles=lambda pair: [0] * 500)
            strategy_llm.join(15)
        assert asked == PAIRS, asked


class TestTheVerdictComesBackNextCycle:
    """
    Разбор идёт сбоку: цикл отдал пару и ушёл, вердикт забирает следующий.
    """

    def test_the_signal_appears_on_the_following_cycle(self, monkeypatch):
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *args, **kwargs: approving_verdict())

        first = strategy_llm.scan_for_setups(PAIRS, gate=None,
                                             candles=lambda pair: [0] * 500)
        assert first == [], 'вердикт пришёл в тот же цикл — значит ждали'

        strategy_llm.join(15)
        second = strategy_llm.scan_for_setups(PAIRS, gate=None,
                                              candles=lambda pair: [0] * 500)
        assert len(second) == 1
        assert second[0]['pair'] == 'BTCUSDT'
        assert second[0]['signal']['strategy'] == 'LLM'
        assert second[0]['df_1h'] == [0] * 500, 'свечи для графика потеряны'

    def test_a_verdict_is_collected_even_when_the_pool_changed(self, monkeypatch):
        """Вердикт уже оплачен минутами счёта — забрать его надо в любом случае."""
        written = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *args, **kwargs: {
                                'ok': False, 'gate': 'мало конфлюенса',
                                'detail': '2 из 5'})

        import refused
        monkeypatch.setattr(refused, 'record',
                            lambda *args, **kwargs: written.append(args))

        strategy_llm.scan_for_setups(PAIRS, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        strategy_llm.scan_for_setups([], gate=None,
                                     candles=lambda pair: [0] * 500)
        assert written, 'отказ потерялся вместе со сменой пула'

    def test_a_stale_verdict_is_refused_by_name(self, monkeypatch):
        """
        Вердикт приходит к разметке пятиминутной давности — это нормально. Но
        застрявший поток может принести его через полчаса, к разметке, которой
        больше нет: торговать по ней значит торговать вчерашним днём.
        """
        written = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *args, **kwargs: approving_verdict())

        import refused
        monkeypatch.setattr(refused, 'record',
                            lambda *args, **kwargs: written.append(args))

        strategy_llm.scan_for_setups(PAIRS, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        # Отматываем отметку отправки на час назад.
        with strategy_llm._work_lock:
            strategy_llm._done[:] = [(p, d, v, at - 3600)
                                     for p, d, v, at in strategy_llm._done]
        out = strategy_llm.scan_for_setups([], gate=None,
                                           candles=lambda pair: [0] * 500)
        assert out == [], 'устаревший вердикт взяли в работу'
        assert any('устарел' in row[2] for row in written), written


class TestEveryAnswerIsWrittenDown:
    """
    Разбор — единственный продукт нескольких минут процессора, и до этого
    журнала он никуда не попадал: журнал сделок хранит только одобренные
    сетапы, журнал отказов — имя правила и двести знаков.
    """

    def test_a_refusal_is_recorded_too(self, monkeypatch):
        import llm_journal

        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *args, **kwargs: {
                                'ok': False, 'gate': 'мало конфлюенса',
                                'detail': '2 из 5', 'regime': 'боковик',
                                'analysis': 'уровень держал цену',
                                'confluence': {'poi': True}})

        strategy_llm.scan_for_setups(PAIRS, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)          # запись делает поток разбора

        rows = llm_journal.last()
        assert rows, 'разбор не записан'
        assert rows[0]['pair'] == 'BTCUSDT'
        assert rows[0]['gate'] == 'мало конфлюенса'
        assert rows[0]['analysis'] == 'уровень держал цену'

    def test_the_price_of_the_call_is_recorded(self, monkeypatch):
        import llm_journal

        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_local, 'last_stats',
                            lambda: {'model': 'Qwen3-8B-Q4_K_M.gguf',
                                     'ctx': 4096, 'prompt_tokens': 1703,
                                     'answer_tokens': 512, 'limit': 1200,
                                     'finish': 'stop', 'seconds': 165.0})
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide())

        strategy_llm.scan_for_setups(PAIRS, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)          # запись делает поток разбора
        row = llm_journal.last()[0]
        assert row['prompt_tokens'] == '1703'
        assert row['ctx'] == '4096'
        assert row['finish'] == 'stop'


class TestABrokenAnswerIsNotTakenForAJudgement:
    """
    Неисправность ответом не является.

    Запомнив её как разбор, бот на час перестал бы спрашивать о паре,
    которой модель не видела, — а в журнале это выглядело бы как «разобрана».
    """

    def test_a_truncated_answer_is_asked_about_again(self, monkeypatch):
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda pair, *a, **k: asked.append(pair) or
                            {'ok': False, 'gate': 'ответ обрезан',
                             'detail': 'окно контекста кончилось раньше ответа'})

        for _ in range(2):
            strategy_llm.scan_for_setups(['BTCUSDT'], gate=None,
                                         candles=lambda pair: [0] * 500)
            strategy_llm.join(15)
        assert len(asked) == 2, 'поломку запомнили как ответ модели'

    def test_the_list_of_broken_names_lives_in_one_place(self):
        import llm_decide
        assert 'ответ обрезан' in llm_decide.BROKEN_GATES
        assert 'мало конфлюенса' not in llm_decide.BROKEN_GATES


class TestAnArmedTriggerWaitsForTheBar:
    """
    «Войти после закрытия часа выше L3» — код ждёт, а не входит сразу.

    Считается только свеча, закрывшаяся ПОСЛЕ взведения: цена, уже стоящая
    выше уровня в момент разбора, — это положение, которое модель и не
    устроило, а не подтверждение.
    """

    def _df(self, closes, start_ms):
        import numpy as np
        import pandas as pd
        closes = np.asarray(closes, dtype=float)
        return pd.DataFrame({
            'timestamp': pd.to_datetime(
                np.arange(len(closes)) * 3_600_000 + start_ms, unit='ms'),
            'open': closes, 'high': closes + 0.1, 'low': closes - 0.1,
            'close': closes, 'volume': np.full(len(closes), 1.0)})

    def _arm(self, monkeypatch, when='close_above', level=101.0):
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *a, **k: approving_verdict(
                                trigger_when=when, trigger_level=level,
                                trigger_id='L3'))
        strategy_llm.scan_for_setups(['BTCUSDT'], gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        # Вердикт забирает следующий цикл — он же и взводит условие.
        return strategy_llm.scan_for_setups([], gate=None,
                                            candles=lambda pair: [0] * 500)

    def test_the_verdict_is_armed_not_traded(self, monkeypatch):
        out = self._arm(monkeypatch)
        assert out == []
        assert strategy_llm.armed()[0]['pair'] == 'BTCUSDT'
        assert strategy_llm.armed()[0]['when'] == 'close_above'

    def test_it_fires_on_a_bar_closed_above_after_arming(self, monkeypatch):
        import time as real_time
        self._arm(monkeypatch)
        armed_ms = int(strategy_llm._armed['BTCUSDT']['armed_at'] * 1000)
        # Свеча -2 закрылась ПОСЛЕ взведения по 102 > 101; свеча -1 ещё идёт.
        df = self._df([100, 100, 102, 103], start_ms=armed_ms - 2 * 3_600_000 + 1000)
        out = strategy_llm.scan_for_setups([], gate=None, candles=lambda pair: df)
        assert len(out) == 1
        assert out[0]['signal']['params']['entry'] == 100.0
        assert strategy_llm.armed() == []

    def test_a_bar_closed_before_arming_does_not_count(self, monkeypatch):
        self._arm(monkeypatch)
        armed_ms = int(strategy_llm._armed['BTCUSDT']['armed_at'] * 1000)
        # Все свечи закрылись до взведения — даже выше уровня.
        df = self._df([100, 102, 103, 104], start_ms=armed_ms - 10 * 3_600_000)
        out = strategy_llm.scan_for_setups([], gate=None, candles=lambda pair: df)
        assert out == []
        assert strategy_llm.armed()

    def test_a_bar_closed_below_does_not_fire_close_above(self, monkeypatch):
        self._arm(monkeypatch)
        armed_ms = int(strategy_llm._armed['BTCUSDT']['armed_at'] * 1000)
        df = self._df([100, 100, 100.5, 103], start_ms=armed_ms - 2 * 3_600_000 + 1000)
        out = strategy_llm.scan_for_setups([], gate=None, candles=lambda pair: df)
        assert out == []

    def test_it_expires_and_is_refused_by_name(self, monkeypatch):
        written = []
        import refused
        monkeypatch.setattr(refused, 'record', lambda *a, **k: written.append(a))
        self._arm(monkeypatch)
        strategy_llm._armed['BTCUSDT']['armed_at'] -= 13 * 3600
        out = strategy_llm.scan_for_setups([], gate=None,
                                           candles=lambda pair: [0] * 500)
        assert out == []
        assert strategy_llm.armed() == []
        assert any('условие не наступило' in row[2] for row in written), written

    def test_a_now_trigger_trades_immediately(self, monkeypatch):
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *a, **k: approving_verdict(trigger_when='now'))
        out = cycle(['BTCUSDT'])
        assert len(out) == 1
        assert strategy_llm.armed() == []


class TestSevenEntryConditionsAreCode:
    """
    «Возврат в зону» и «снятие стопов с возвратом» раньше оставались текстом в
    note и не исполнялись. Теперь это арифметика по закрытым свечам.
    """

    B = staticmethod(lambda o, h, l, c, v=1.0: (0, o, h, l, c, v))

    def test_close_above_needs_a_close_not_a_wick(self):
        assert strategy_llm.condition_met('close_above', 100, 'LONG',
                                          [self.B(99, 101, 98, 99.5)]) == ''
        assert 'выше' in strategy_llm.condition_met('close_above', 100, 'LONG',
                                                    [self.B(99, 101, 98, 100.5)])

    def test_close_with_volume_needs_the_volume(self):
        bars = [self.B(99, 101, 98, 100.5, v=1.2)]
        assert strategy_llm.condition_met('close_above_with_volume', 100, 'LONG', bars, 1.0) == ''
        bars = [self.B(99, 101, 98, 100.5, v=1.6)]
        assert 'объёме ×1.6' in strategy_llm.condition_met('close_above_with_volume', 100, 'LONG', bars, 1.0)

    def test_retest_is_break_then_touch_that_holds(self):
        bars = [self.B(99, 101, 98.5, 100.8),        # пробой вверх
                self.B(100.8, 101, 99.9, 100.3)]    # касание 100 и удержание
        assert 'ретест' not in strategy_llm.condition_met('retest', 100, 'LONG', bars[:1])
        assert 'возврат' in strategy_llm.condition_met('retest', 100, 'LONG', bars)
        # Касание, но закрытие ниже уровня — не ретест, а провал.
        failed = [self.B(99, 101, 98.5, 100.8), self.B(100.8, 101, 99.5, 99.7)]
        assert strategy_llm.condition_met('retest', 100, 'LONG', failed) == ''

    def test_sweep_reclaim_is_a_wick_beyond_then_close_back(self):
        bars = [self.B(100.5, 100.8, 99.2, 100.3)]  # вынос под 100 и закрытие выше
        assert 'вынос' in strategy_llm.condition_met('sweep_reclaim', 100, 'LONG', bars)
        late = [self.B(100.5, 100.8, 99.2, 99.6), self.B(99.6, 99.9, 99.1, 99.5),
                self.B(99.5, 99.9, 99.3, 99.6), self.B(99.6, 99.9, 99.3, 99.7),
                self.B(99.7, 100.5, 99.5, 100.2)]   # вернулась на 5-й свече — поздно
        assert strategy_llm.condition_met('sweep_reclaim', 100, 'LONG', late) == ''

    def test_short_mirrors_long(self):
        bars = [self.B(100, 101.2, 99.5, 99.7)]
        assert 'вынос' in strategy_llm.condition_met('sweep_reclaim', 101, 'SHORT', bars)
        assert 'ниже' in strategy_llm.condition_met('close_below', 100, 'SHORT', bars)

    def test_the_armed_plan_fires_on_a_retest(self, monkeypatch):
        import numpy as np
        import pandas as pd
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *a, **k: approving_verdict(trigger_when='retest',
                                                              trigger_level=101.0, trigger_id='L3'))
        strategy_llm.scan_for_setups(['BTCUSDT'], gate=None, candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        strategy_llm.scan_for_setups([], gate=None, candles=lambda pair: [0] * 500)
        armed_ms = int(strategy_llm._armed['BTCUSDT']['armed_at'] * 1000)
        closes = [100, 100, 101.8, 101.2, 101.5]
        df = pd.DataFrame({
            'timestamp': pd.to_datetime(np.arange(5) * 3_600_000 + armed_ms - 3_600_000 + 1000, unit='ms'),
            'open': closes, 'high': [c + 0.3 for c in closes],
            'low': [100, 100, 101.1, 100.9, 101.3], 'close': closes, 'volume': [1.0] * 5})
        out = strategy_llm.scan_for_setups([], gate=None, candles=lambda pair: df)
        assert len(out) == 1 and strategy_llm.armed() == []


class TestTheSetupIsAnnounced:
    """
    План принят — человеку уходит сообщение с монетой, входом, стопом,
    целями, условием и графиком; отклонённый код/критиком план — короткой
    строкой с причиной. Отправка не вмешивается в торговлю.
    """

    def test_an_accepted_plan_is_announced_with_the_numbers(self, monkeypatch):
        import telegram_notify as tg
        sent = {}
        monkeypatch.setattr(tg, '_allowed', lambda e: True)
        monkeypatch.setattr(tg, '_send', lambda text, chat_id=None: (sent.setdefault('text', text), True)[1])
        verdict = approving_verdict(trigger_when='close_above', trigger_level=103.0, trigger_id='L2',
                                    bias='up', critic={'verdict': 'confirm', 'worst': 'плита у цели'},
                                    levels=[{'id': 'L5', 'price': 100.0, 'kind': 'пивот'}])
        signal = strategy_llm._reshape('BTCUSDT', verdict)
        assert tg.llm_setup_found(signal, None) is True
        text = sent['text']
        for piece in ('BTCUSDT LONG', 'Вход:', '100', 'Стоп:', '97', '3.00%', 'Цель 1', '107',
                      'Цель 2', '109', 'R:R 2.33', 'вероятность 0.58', '4/5',
                      'close_above 103', 'Куда рынок: up', 'Критик: подтвердил', 'плита у цели'):
            assert piece in text, piece

    def test_a_rejected_plan_is_announced_briefly(self, monkeypatch):
        import telegram_notify as tg
        sent = {}
        monkeypatch.setattr(tg, '_allowed', lambda e: True)
        monkeypatch.setattr(tg, '_send', lambda text, chat_id=None: (sent.setdefault('text', text), True)[1])
        tg.llm_setup_rejected('ETHUSDT', 'SHORT', 2527.67, 'критик отклонил', 'стоп внутри кластера')
        assert 'отклонён' in sent['text'] and 'ETHUSDT SHORT' in sent['text'] and 'стоп внутри' in sent['text']

    def test_the_switch_silences_it(self, monkeypatch):
        import telegram_notify as tg
        monkeypatch.setattr(tg, '_allowed', lambda e: False)
        monkeypatch.setattr(tg, '_send', lambda *a, **k: (_ for _ in ()).throw(AssertionError('слать нельзя')))
        assert tg.llm_setup_found(strategy_llm._reshape('BTCUSDT', approving_verdict()), None) is False

    def test_the_collector_announces_and_the_signal_carries_the_plan(self, monkeypatch):
        import telegram_notify as tg
        announced = []
        monkeypatch.setattr(tg, 'llm_setup_found', lambda signal, df=None, **k: announced.append(signal) or True)
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *a, **k: approving_verdict(levels=[{'id': 'L5', 'price': 100.0, 'kind': 'пивот'}]))
        out = cycle(['BTCUSDT'])
        assert len(out) == 1 and len(announced) == 1
        assert announced[0]['llm']['levels'][0]['id'] == 'L5'
        assert announced[0]['llm']['trigger_when'] == 'now'


class TestArmedPlansSurviveARestart:
    """
    План с условием ждёт до 12 часов, а выкатки идут по несколько раз в
    день: каждая молча стирала план, о котором человеку уже пришло
    сообщение. Теперь план лежит на диске, срок — от взведения.
    """

    def test_an_armed_plan_is_read_back_after_a_restart(self):
        strategy_llm._arm('SOLUSDT', approving_verdict(trigger_when='close_below',
                                                       trigger_level=112.09, trigger_id='L4'))
        # «Перезапуск»: память пуста, файл остался.
        strategy_llm._armed.clear()
        strategy_llm._armed_loaded = False
        out = strategy_llm._check_armed(lambda pair: None)
        assert out == []
        assert 'SOLUSDT' in strategy_llm._armed
        assert strategy_llm._armed['SOLUSDT']['verdict']['trigger_level'] == 112.09

    def test_an_expired_plan_is_dropped_on_read(self, monkeypatch):
        strategy_llm._arm('SOLUSDT', approving_verdict(trigger_when='close_below',
                                                       trigger_level=112.09, trigger_id='L4'))
        strategy_llm._armed['SOLUSDT']['armed_at'] -= 13 * 3600
        strategy_llm._save_armed()
        strategy_llm._armed.clear()
        strategy_llm._armed_loaded = False
        strategy_llm._check_armed(lambda pair: None)
        assert strategy_llm._armed == {}


class TestBusyPairsAreNotReAsked:
    """
    Пара с взведённым планом, позицией или заявкой в очередь не идёт:
    ответ был бы тем же, а человеку прилетало бы то же сообщение.
    """

    def test_an_armed_pair_waits_for_its_condition(self, monkeypatch):
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda pair, *a, **k: asked.append(pair) or approving_verdict())
        strategy_llm._arm('BTCUSDT', approving_verdict(trigger_when='close_above',
                                                       trigger_level=103.0, trigger_id='L2'))
        strategy_llm.scan_for_setups(['BTCUSDT', 'ETHUSDT'], gate=None, candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        assert asked == ['ETHUSDT']

    def test_a_pair_with_a_position_is_skipped(self, monkeypatch):
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda pair, *a, **k: asked.append(pair) or approving_verdict())

        class Gate:
            def has_position_or_order(self, pair):
                return pair == 'BTCUSDT'
        strategy_llm.scan_for_setups(['BTCUSDT', 'ETHUSDT'], gate=Gate(), candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        assert asked == ['ETHUSDT']
