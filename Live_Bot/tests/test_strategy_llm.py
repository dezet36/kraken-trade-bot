"""
Пятая стратегия: чьи сетапы берёт, что с ними делает и чего делать не должна.

ГЛАВНОЕ, ЧТО СТЕРЕГУТ ЭТИ ПРОВЕРКИ — ПОРЯДОК И ЧЕСТНОСТЬ ЗАИМСТВОВАНИЯ.

В bot.py записано правило: стратегия не должна молча получать чужих кандидатов
и торговать их под своим именем. Уровни однажды месяц торговали сетапы фибо
через ветку else, и месяц наблюдений оказался недостоверным.

Пятая стратегия чужих кандидатов получает — но это её замысел, а не недосмотр.
Значит проверять надо два свойства: что заимствование ВИДНО в журнале (иначе
разобрать сделку потом будет нечем) и что решение всё-таки своё (иначе это не
пятая стратегия, а переименованная чужая).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_llm


def donor_candidate(pair='BTCUSDT', score=5, donor_signal=None):
    """Кандидат в том виде, в каком его отдаёт сканер чужой стратегии."""
    return {
        'pair': pair,
        'score': score,
        'rr': 2.5,
        'df_1h': 'свечи',
        'signal': donor_signal if donor_signal is not None else {
            'trading_pair': pair,
            'strategy': 'LEVELS',
            'setup': {'type': 'SHORT', 'size': 5.0, 'end_price': 100.0},
            'trigger': {'zone': 'LEVEL'},
            'params': {'entry': 100.0, 'stop_loss': 102.0,
                       'take_profit_1': 95.0, 'take_profit_2': 95.0,
                       'risk_pct': 0.5, 'position_size': 10.0,
                       'risk_amount': 50.0, 'rr': 2.5},
        },
    }


def approving_verdict(**over):
    out = {
        'ok': True, 'gate': '', 'detail': '',
        'side': 'LONG', 'entry': 100.0, 'stop': 97.0,
        'targets': [107.0, 109.0], 'inval': 97.0,
        'p': 0.58, 'rr': 2.33, 'ev': 0.9, 'cost_r': 0.025,
        'votes': 4, 'confluence': {'poi': True, 'vp': True, 'der': True,
                                   'smc': True, 'flow': False},
        'regime': 'боковик', 'analysis': 'подробный разбор',
        'trigger': 'возврат в уровень', 'why': 'скопление снизу свежее',
        'risk': 'слив открытого интереса', 'alt': 'пробой вниз отменяет идею',
        'ids': {'entry': 'L5', 'stop': 'L7'},
    }
    out.update(over)
    return out


class TestItDoesNotInventSetups:
    """
    Пустой пул означает, что разбирать нечего. Модель не выдумывает сетапы на
    ровном месте, и это правильно: её задача — судить, а не фантазировать.
    """

    def test_an_empty_pool_gives_nothing(self, monkeypatch):
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        assert strategy_llm.scan_for_setups({}, gate=None) == []

    def test_without_a_model_it_stands_idle(self, monkeypatch):
        """
        Модели нет — стратегия простаивает, а НЕ торгует чужие сетапы как свои.
        Иначе в журнале появились бы сделки LLM, которых модель не видела.
        """
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: False)
        out = strategy_llm.scan_for_setups({'LEVELS': [donor_candidate()]},
                                           gate=None)
        assert out == []


class TestTheBorrowingIsVisible:
    """
    Заимствование обязано быть видно в журнале. Невидимое — это ровно та
    подмена, из-за которой месяц наблюдений по уровням пришлось выбросить.
    """

    def test_the_donor_is_recorded(self):
        signal = strategy_llm._reshape(donor_candidate(), approving_verdict())
        assert signal['llm']['donor'] != ''

    def test_the_trade_is_marked_as_llm(self):
        signal = strategy_llm._reshape(donor_candidate(), approving_verdict())
        assert signal['strategy'] == 'LLM'

    def test_the_analysis_reaches_the_signal(self):
        """
        Разбор — главная ценность записи: по нему потом видно, НА ЧЁМ модель
        ошиблась, а не только что ошиблась.
        """
        signal = strategy_llm._reshape(donor_candidate(), approving_verdict())
        for field in ('regime', 'analysis', 'trigger', 'why', 'risk', 'alt'):
            assert signal['llm'][field], f'поле {field} потеряно'

    def test_the_probability_is_kept_for_calibration(self):
        """
        Вероятность нужна, чтобы позже проверить калибровку: выигрывают ли
        сетапы, названные «0.58», действительно в 58% случаев.
        """
        signal = strategy_llm._reshape(donor_candidate(), approving_verdict())
        assert signal['llm']['p'] == 0.58


class TestTheDecisionIsItsOwn:
    """
    Если бы стратегия просто повторяла чужой сетап, это была бы переименованная
    чужая стратегия, а не пятая.
    """

    def test_levels_come_from_the_model_not_the_donor(self):
        candidate = donor_candidate()
        signal = strategy_llm._reshape(candidate, approving_verdict())
        params = signal['params']
        donor = candidate['signal']['params']

        assert params['entry'] == 100.0
        assert params['stop_loss'] == 97.0 != donor['stop_loss']
        assert params['tp_targets'] == [107.0, 109.0]

    def test_the_direction_can_be_reversed(self):
        """
        Донор нашёл шорт, модель решила лонг. Так бывает, и сделка обязана
        уйти в журнал лонгом — иначе исполнение разойдётся с разбором.
        """
        candidate = donor_candidate()
        assert candidate['signal']['setup']['type'] == 'SHORT'
        signal = strategy_llm._reshape(candidate, approving_verdict(side='LONG'))
        assert signal['setup']['type'] == 'LONG'

    def test_the_exit_plan_matches_the_targets(self):
        signal = strategy_llm._reshape(donor_candidate(), approving_verdict())
        assert len(signal['params']['tp_fractions']) == \
               len(signal['params']['tp_targets'])
        assert sum(signal['params']['tp_fractions']) == pytest.approx(1.0)

    def test_breakeven_is_off(self):
        """
        Модель сама называет уровень инвалидации. Подтянутый стоп выбивал бы
        позицию раньше, чем идея опровергнута.
        """
        signal = strategy_llm._reshape(donor_candidate(), approving_verdict())
        assert signal['params']['breakeven_after_tp'] is False
        assert signal['params']['invalidation'] == 97.0


class TestOneSetupPerPairPerCycle:
    """
    Две стратегии часто находят сетап на одной паре в один цикл. Разбирать её
    дважды — значит потратить шесть минут там, где хватит трёх, а решение
    выйдет одно и то же: разметка-то одна.
    """

    def test_duplicates_collapse_to_the_best(self):
        pool = {'FIBO': [donor_candidate('BTCUSDT', score=3)],
                'SMC': [donor_candidate('BTCUSDT', score=9)]}
        ready = strategy_llm._fresh(pool)
        assert len(ready) == 1
        assert ready[0]['score'] == 9
        assert ready[0]['donor'] == 'SMC'

    def test_different_pairs_survive(self):
        pool = {'FIBO': [donor_candidate('BTCUSDT')],
                'SMC': [donor_candidate('ETHUSDT')]}
        assert len(strategy_llm._fresh(pool)) == 2

    def test_the_best_go_first(self):
        pool = {'FIBO': [donor_candidate('AUSDT', score=1),
                         donor_candidate('BUSDT', score=7)]}
        ready = strategy_llm._fresh(pool)
        assert [c['pair'] for c in ready] == ['BUSDT', 'AUSDT']


class TestTheCycleIsNotHeldUp:
    """
    ЦИКЛ НЕ ЖДЁТ МОДЕЛЬ — ЭТО ГЛАВНОЕ СВОЙСТВО ПЯТОЙ СТРАТЕГИИ.

    Один разбор занимает на сервере четыре-пять минут, и быстрой модели там
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

        strategy_llm.scan_for_setups({'FIBO': [donor_candidate()]}, gate=None,
                                     candles=lambda pair: [0] * 500)
        assert real_time.time() - started < 0.7, 'цикл дождался модель'
        strategy_llm.join(5)

    def test_one_setup_at_a_time(self, monkeypatch):
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

        many = [donor_candidate(f'P{i}USDT', score=20 - i) for i in range(20)]
        for _ in range(3):                       # три цикла подряд
            strategy_llm.scan_for_setups({'FIBO': many}, gate=None,
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
        cycle({'FIBO': [donor_candidate()]})
        assert written, 'отказ модели не записан'
        assert written[0][0] == 'LLM'


class TestItRunsLast:
    """
    Пятая стратегия разбирает кандидатов, найденных остальными за ЭТОТ цикл.
    Встань она раньше — пул к её очереди был бы пуст, и она молча простаивала
    бы каждый цикл, ничем не выдавая причины.
    """

    def test_llm_is_last_in_the_order(self):
        import paper_broker
        import settings_store

        assert paper_broker.STRATEGIES[-1] == 'LLM'
        assert settings_store.STRATEGIES[-1] == 'LLM'

    def test_both_lists_agree(self):
        import paper_broker
        import settings_store

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
        """
        Колонка, осмысленная для одной стратегии, не повод заводить второй
        журнал. У остальных четырёх она просто пуста.
        """
        import paper_broker
        empty = paper_broker._llm_columns(None)
        assert set(empty) == {c for c in paper_broker.COLUMNS
                              if c.startswith('llm_')}
        assert all(value == '' for value in empty.values())


class FakeClock:
    """Часы, которые идут, только когда их двигают: время здесь — предмет проверки."""

    def __init__(self, start=1000.0):
        self.now = start

    def time(self):
        return self.now


@pytest.fixture(autouse=True)
def _forget_previous_setups():
    """
    Состояние модуля глобальное: память о сетапах, очередь и занятость потока.

    Не почистив, вторая проверка получила бы ответ первой и прошла бы по
    ошибке — или, хуже, заняла бы «модель» потоком, который уже не нужен.
    """
    strategy_llm.join(10)
    strategy_llm._asked.clear()
    strategy_llm._done.clear()
    strategy_llm._busy = None
    yield
    strategy_llm.join(10)
    strategy_llm._asked.clear()
    strategy_llm._done.clear()
    strategy_llm._busy = None


def cycle(pool, candles=lambda pair: [0] * 500):
    """
    Один оборот бота: отдать сетап, дождаться модель, забрать вердикт.

    Ожидание здесь — свойство ПРОВЕРКИ, а не бота: в жизни вердикт забирает
    следующий цикл через несколько минут, и ждать его никто не будет.
    """
    strategy_llm.scan_for_setups(pool, gate=None, candles=candles)
    strategy_llm.join(15)
    return strategy_llm.scan_for_setups(pool, gate=None, candles=candles)


def refusing_decide(recorder=None):
    """Модель, которая всегда отказывается, и запоминает, о чём её спросили."""
    def decide(pair, *args, **kwargs):
        if recorder is not None:
            recorder.append(pair)
        return {'ok': False, 'gate': 'модель пропустила', 'detail': ''}
    return decide


class TestTheSameSetupIsNotReExaminedEveryCycle:
    """
    Заявка висит часами, сканер находит её каждый цикл, ответ при тех же
    данных тот же.

    19 сентября 2026 один и тот же SHIB1000USDT LONG с одними и теми же
    ценами разбирался 119 раз подряд по 165 секунд: за девять часов на
    повторные ответы ушло больше двух часов счёта, и всё это время торговый
    цикл ждал.
    """

    def test_the_second_cycle_does_not_ask_again(self, monkeypatch):
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide(asked))

        for _ in range(3):
            strategy_llm.scan_for_setups({'FIBO': [donor_candidate()]},
                                         gate=None,
                                         candles=lambda pair: [0] * 500)
            strategy_llm.join(15)
        assert asked == ['BTCUSDT'], f'модель спросили {len(asked)} раза'

    def test_a_changed_price_is_a_new_setup(self, monkeypatch):
        """
        Тот же инструмент с другим входом — другая идея, и её надо разобрать.
        Иначе память превратилась бы в запрет на пару, а не на сетап.
        """
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide(asked))

        strategy_llm.scan_for_setups({'FIBO': [donor_candidate()]}, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)           # дожидаемся: модель разбирает по одному

        moved = donor_candidate()
        moved['signal']['params']['entry'] = 111.0
        strategy_llm.scan_for_setups({'FIBO': [moved]}, gate=None,
                                     candles=lambda pair: [0] * 500)
        assert len(asked) == 2, 'сдвинутый вход не признан новым сетапом'

    def test_after_the_window_it_is_asked_again(self, monkeypatch):
        """
        Рынок вокруг сетапа меняется: через час те же цены стоят в другой
        обстановке, и ответ может стать другим. Запрет навсегда был бы враньём.
        """
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide(asked))

        strategy_llm.scan_for_setups({'FIBO': [donor_candidate()]}, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)           # модель разбирает по одному
        minutes = strategy_llm.config.LLM_REASK_AFTER_MIN
        for key in list(strategy_llm._asked):
            strategy_llm._asked[key] -= minutes * 60 + 1

        strategy_llm.scan_for_setups({'FIBO': [donor_candidate()]}, gate=None,
                                     candles=lambda pair: [0] * 500)
        assert len(asked) == 2


class TestTheVerdictComesBackNextCycle:
    """
    Разбор идёт сбоку: цикл отдал сетап и ушёл, вердикт забирает следующий.

    Задержка в пять минут для сетапа, живущего часами, ничего не меняет.
    Остановка бота на пять минут меняет всё — см. TestTheCycleIsNotHeldUp.
    """

    def test_the_signal_appears_on_the_following_cycle(self, monkeypatch):
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *args, **kwargs: approving_verdict())

        pool = {'FIBO': [donor_candidate()]}
        first = strategy_llm.scan_for_setups(pool, gate=None,
                                             candles=lambda pair: [0] * 500)
        assert first == [], 'вердикт пришёл в тот же цикл — значит ждали'

        strategy_llm.join(15)
        second = strategy_llm.scan_for_setups(pool, gate=None,
                                              candles=lambda pair: [0] * 500)
        assert len(second) == 1
        assert second[0]['signal']['strategy'] == 'LLM'

    def test_an_answer_about_a_setup_that_is_gone_is_still_collected(self,
                                                                     monkeypatch):
        """
        Пул следующего цикла может оказаться пустым — сетап отработал или
        протух. Вердикт о нём всё равно надо забрать: он уже оплачен пятью
        минутами счёта, и отказ модели должен попасть в журнал.
        """
        written = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *args, **kwargs: {
                                'ok': False, 'gate': 'мало конфлюенса',
                                'detail': '2 из 5'})

        import refused
        monkeypatch.setattr(refused, 'record',
                            lambda *args, **kwargs: written.append(args))

        strategy_llm.scan_for_setups({'FIBO': [donor_candidate()]}, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        strategy_llm.scan_for_setups({}, gate=None,
                                     candles=lambda pair: [0] * 500)
        assert written, 'отказ потерялся вместе с сетапом'

    def test_a_stale_verdict_is_refused_by_name(self, monkeypatch):
        """
        Вердикт приходит к разметке пятиминутной давности — это нормально. Но
        застрявший поток может принести его через полчаса, к разметке, которой
        больше нет: торговать по ней значит торговать вчерашним днём, а
        выглядеть это будет как свежее решение модели.
        """
        written = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda *args, **kwargs: approving_verdict())

        import refused
        monkeypatch.setattr(refused, 'record',
                            lambda *args, **kwargs: written.append(args))

        strategy_llm.scan_for_setups({'FIBO': [donor_candidate()]}, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)
        # Отматываем отметку отправки на час назад.
        with strategy_llm._work_lock:
            strategy_llm._done[:] = [(c, v, at - 3600)
                                     for c, v, at in strategy_llm._done]
        out = strategy_llm.scan_for_setups({}, gate=None,
                                           candles=lambda pair: [0] * 500)
        assert out == [], 'устаревший вердикт взяли в работу'
        assert any('устарел' in row[2] for row in written), written


class TestEveryAnswerIsWrittenDown:
    """
    Разбор — единственный продукт двух-четырёх минут процессора, и до этого
    журнала он никуда не попадал: журнал сделок хранит только одобренные
    сетапы, журнал отказов — имя правила и двести знаков.

    19 сентября 2026 модель сделала 123 вызова и ни одной сделки. По журналу
    сделок её не существовало вовсе.
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

        strategy_llm.scan_for_setups({'FIBO': [donor_candidate()]}, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)          # запись делает поток разбора

        rows = llm_journal.last()
        assert rows, 'разбор не записан'
        assert rows[0]['gate'] == 'мало конфлюенса'
        assert rows[0]['analysis'] == 'уровень держал цену'
        assert rows[0]['donor'] == 'FIBO', 'не видно, чей сетап разбирали'

    def test_the_price_of_the_call_is_recorded(self, monkeypatch):
        """
        Токены и окно рядом с разбором. Без них поломку 19 сентября пришлось
        бы снова выводить сопоставлением двух файлов — что за девять часов
        так и не было сделано.
        """
        import llm_journal

        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_local, 'last_stats',
                            lambda: {'model': 'Qwen3-8B-Q4_K_M.gguf',
                                     'ctx': 4096, 'prompt_tokens': 1703,
                                     'answer_tokens': 512, 'limit': 1200,
                                     'finish': 'stop', 'seconds': 165.0})
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            refusing_decide())

        strategy_llm.scan_for_setups({'FIBO': [donor_candidate()]}, gate=None,
                                     candles=lambda pair: [0] * 500)
        strategy_llm.join(15)          # запись делает поток разбора
        row = llm_journal.last()[0]
        assert row['prompt_tokens'] == '1703'
        assert row['ctx'] == '4096'
        assert row['finish'] == 'stop'


class TestABrokenAnswerIsNotTakenForAJudgement:
    """
    Неисправность ответом не является.

    Запомнив её как разбор, бот на час перестал бы спрашивать о сетапе,
    которого модель не видела, — а в журнале это выглядело бы как «разобран».
    Уверенное враньё хуже отсутствия ответа.
    """

    def test_a_truncated_answer_is_asked_about_again(self, monkeypatch):
        asked = []
        monkeypatch.setattr(strategy_llm.llm_local, 'available', lambda: True)
        monkeypatch.setattr(strategy_llm.llm_decide, 'decide',
                            lambda pair, *a, **k: asked.append(pair) or
                            {'ok': False, 'gate': 'ответ обрезан',
                             'detail': 'окно контекста кончилось раньше ответа'})

        for _ in range(2):
            strategy_llm.scan_for_setups({'FIBO': [donor_candidate()]},
                                         gate=None,
                                         candles=lambda pair: [0] * 500)
            strategy_llm.join(15)
        assert len(asked) == 2, 'поломку запомнили как ответ модели'

    def test_the_list_of_broken_names_lives_in_one_place(self):
        """
        Имена поломок нужны в трёх местах: стратегии, панели и странице.
        Записанные трижды, они разойдутся — и поломка однажды покрасится в
        цвет обычного отказа.
        """
        import llm_decide
        assert 'ответ обрезан' in llm_decide.BROKEN_GATES
        assert 'мало конфлюенса' not in llm_decide.BROKEN_GATES


def fibo_candidate(pair='SUIUSDT', end_price=0.8, start_price=0.76, score=5):
    """
    Кандидат в том виде, в каком его отдаёт сканер ФИБО, — без готового
    сигнала.

    Так и задумано: у фибо сигнал достраивается позже, из свечей
    (bot._build_signal, ветка FIBO). Проверки, написанные по образцу уровней,
    этой формы не видели вовсе — а именно она и приходит чаще всех.
    """
    return {
        'pair': pair,
        'score': score,
        'df_1h': 'свечи',
        'zone': 'Zone_A',
        'htf_trend': 'BULLISH',
        'setup': {'type': 'LONG', 'start_price': start_price,
                  'end_price': end_price, 'size': abs(end_price - start_price)},
    }


class TestTheFiboShapeIsUnderstood:
    """
    У кандидатов фибо нет ключа signal, и это не оплошность сканера.

    19 сентября 2026 панель показала метку занятости «SUIUSDT|—|—»: цены не
    прочитались, потому что искали их только в signal. Для ВСЕХ сетапов фибо
    на одной паре метка выходила одинаковой, и после первого же разбора пара
    выпадала из поля зрения на час — следующий сетап считался тем же самым.
    """

    def test_two_setups_on_one_pair_differ(self):
        first = strategy_llm._fingerprint(fibo_candidate(end_price=0.80))
        second = strategy_llm._fingerprint(fibo_candidate(end_price=0.91))
        assert first != second, f'обе метки {first}'
        assert '—' not in first, first

    def test_the_levels_shape_still_works(self):
        """У уровней, SMC и боллинджера готовый сигнал есть — берём оттуда."""
        mark = strategy_llm._fingerprint(donor_candidate())
        assert '—' not in mark, mark
        assert '100' in mark


class TestTheRiskIsItsOwn:
    """
    Доля риска приезжала вместе с чужими параметрами: сделка на депозите пятой
    стратегии шла с настройкой четвёртой. У сетапов фибо её не было вовсе — и
    бралась общая настройка бота.

    Размер позиции брокер считает сам, по этой доле, текущему депозиту и
    ДИСТАНЦИИ СТОПА. Стоп у модели свой, поэтому донорский размер — это риск
    не тот, что заявлен.
    """

    def test_the_donor_risk_does_not_travel(self, monkeypatch):
        # Подменяем В ТОМ объекте, которым пользуется стратегия. Проверки
        # выгружают settings_store и импортируют заново, поэтому «свежий»
        # settings_store и strategy_llm.settings бывают РАЗНЫМИ модулями с
        # одним именем — патч одного мимо другого проходит молча.
        monkeypatch.setattr(strategy_llm.settings, 'risk_pct', lambda name: 0.7)
        candidate = donor_candidate()
        candidate['signal']['params']['risk_pct'] = 0.5
        signal = strategy_llm._reshape(candidate, approving_verdict())
        assert signal['params']['risk_pct'] == 0.7

    def test_the_donor_size_is_dropped(self):
        """
        Размер донора посчитан под ЕГО стоп. Оставить его значило бы рискнуть
        суммой, которой никто не назначал.
        """
        candidate = donor_candidate()
        candidate['signal']['params']['position_size'] = 10.0
        candidate['signal']['params']['risk_amount'] = 50.0
        signal = strategy_llm._reshape(candidate, approving_verdict())
        assert 'position_size' not in signal['params']
        assert 'risk_amount' not in signal['params']

    def test_a_fibo_donor_still_gets_a_workable_signal(self, monkeypatch):
        """
        Донор без готового сигнала — обычный случай, а не край. Сделка из него
        обязана собираться: пара, направление, вход, стоп, цели и доля риска.
        """
        monkeypatch.setattr(strategy_llm.settings, 'risk_pct', lambda name: 0.5)
        signal = strategy_llm._reshape(fibo_candidate(), approving_verdict())
        params = signal['params']
        assert signal['trading_pair'] == 'SUIUSDT'
        assert signal['strategy'] == 'LLM'
        assert signal['setup']['type'] == 'LONG'
        assert params['entry'] == 100.0 and params['stop_loss'] == 97.0
        assert params['tp_targets'] == [107.0, 109.0]
        assert params['risk_pct'] == 0.5
