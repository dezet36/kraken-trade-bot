"""
Что код обязан отвергнуть, даже когда модель уверена и объяснение складное.

ЗДЕСЬ ПРОВЕРЯЕТСЯ ВТОРОЙ РУБЕЖ. Первый — грамматика, она не даёт назвать
несуществующий уровень. Но законный идентификатор ещё не означает осмысленный
сетап: для лонга можно выбрать стоп выше входа, и оба уровня будут настоящими.

Поле `why` на любой из этих ошибок будет выглядеть убедительно. Именно поэтому
решение принимается по числам, а текст идёт в журнал для чтения человеком.

Проверки СЧИТАЮТ, а не сверяются с текстом исходника: ошибка здесь будет в
формуле, а формулу поиск по строкам не ловит.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import llm_decide as dec


# Уровни подобраны так, чтобы РАЗУМНЫЙ сетап проходил все пороги. Первая
# версия этого не обеспечивала: лонг от 100 со стопом 94 к цели 103 давал
# отношение 0.5, и предохранитель справедливо отвергал его как плохой. Ошибка
# была в наборе данных, а выглядела как поломка проверяемого кода.
#
#   лонг:  вход L3 100, стоп ЗА L4 98 (код отступает 0.3%: 97.706, риск
#          2.294), цель L2 107 (ход 7) -> R:R 3.05
#   шорт:  вход L2 107, стоп за L1 109 (109.327, риск 2.327), цель L3 100
#          (ход 7) -> R:R 3.0
LEVELS = [
    {'id': 'L1', 'price': 109.0, 'touches': 3, 'kind': 'скопление максимумов'},
    {'id': 'L2', 'price': 107.0, 'touches': 2, 'kind': 'верх зоны ордер-блок / скопление максимумов'},
    {'id': 'L3', 'price': 100.0, 'touches': 4, 'kind': 'низ зоны ордер-блок / скопление минимумов'},
    {'id': 'L4', 'price': 98.0, 'touches': 5, 'kind': 'скопление минимумов'},
    {'id': 'L5', 'price': 94.0, 'touches': 2, 'kind': 'пивот-минимум'},
]

ALL_TRUE = {'poi': True, 'vp': True, 'der': True, 'smc': True, 'flow': True}

# Стоп, который поставит код: уровень L4 минус буфер охоты (0.3% без ATR).
STOP = 98.0 * (1 - dec.stop_hunt_pct() / 100)
RISK = 100.0 - STOP


def answer(**over):
    """Ответ модели: разумный лонг от L3 со стопом L5 к целям L2 и L1."""
    import json
    body = {'d': 'enter', 'side': 'LONG', 'entry': 'L3', 'stop': 'L4',
            'tp': ['L2', 'L1'], 'inval': 'L4', 'cf': dict(ALL_TRUE),
            'p': 0.6, 'why': 'скопление снизу свежее', 'risk': 'слив OI'}
    body.update(over)
    return json.dumps(body, ensure_ascii=False)


def verdict(**over):
    return dec.check(dec.parse(answer(**over), LEVELS), LEVELS)


class TestTheGoodSetupPasses:

    def test_a_sound_entry_is_accepted(self):
        out = verdict()
        assert out['ok'], out.get('gate')
        assert out['side'] == 'LONG'
        assert out['entry'] == 100.0
        assert out['stop_level'] == 98.0
        assert out['stop'] == pytest.approx(STOP)
        assert out['targets'] == [107.0, 109.0]

    def test_the_numbers_are_computed_not_taken_from_the_model(self):
        """
        R:R и ожидание считает код. Модель их не присылает вовсе — и не должна:
        на арифметике она ошибается, а порог, проверенный по её же ошибке,
        не защищает ни от чего.
        """
        out = verdict()
        # Вход 100, стоп 97.706, первая цель 107: риск 2.294, ход 7.
        assert out['rr'] == pytest.approx(7 / RISK, abs=0.01)
        assert out['cost_r'] == pytest.approx(
            config.ENTRY_COST_ROUND_TRIP / (RISK / 100), abs=1e-4)
        # EV считается по НАШЕЙ доле дошедших до цели, и пока своих исходов
        # мало — не считается вовсе (см. TestExpectedValueUsesOurOwnStatistics).
        assert out['ev'] in ('', None) or isinstance(out['ev'], float)


class TestGeometryTheGrammarCannotSee:
    """
    Идентификатор законен, а сетап бессмыслен. Грамматика знает список
    уровней, но не знает, какой из них выше.
    """

    def test_a_long_with_the_stop_above_entry_is_refused(self):
        out = verdict(entry='L3', stop='L1', tp=['L2'])
        assert not out['ok']
        assert out['gate'] == 'геометрия неверна'

    def test_a_long_with_a_target_below_entry_is_refused(self):
        out = verdict(entry='L3', stop='L5', tp=['L4'])   # цель 97 ниже входа 100
        assert not out['ok']
        assert out['gate'] == 'геометрия неверна'

    def test_a_short_mirrors_the_rule(self):
        ok = verdict(side='SHORT', entry='L2', stop='L1', tp=['L3', 'L4'])
        assert ok['ok'], ok.get('gate')
        bad = verdict(side='SHORT', entry='L3', stop='L5', tp=['L4'])
        assert not bad['ok']
        assert bad['gate'] == 'геометрия неверна'


class TestCostsDecide:
    """
    Издержки зависят от тесноты стопа. Постоянная величина вместо формулы —
    та самая ошибка готового промта, занижавшая расход в сотню раз.
    """

    def test_a_tight_stop_costs_more_in_r(self):
        wide = dec.cost_in_r(100.0, 94.0)     # стоп 6%
        tight = dec.cost_in_r(100.0, 99.7)    # стоп 0.3%
        assert tight > wide * 15

    def test_the_formula_matches_the_arithmetic(self):
        # Стоп 0.3% при ставке туда-обратно 0.075% даёт 0.25R.
        assert dec.cost_in_r(100.0, 99.7) == pytest.approx(
            config.ENTRY_COST_ROUND_TRIP / 0.003, rel=1e-6)

    def test_a_stop_tighter_than_the_floor_is_refused(self, monkeypatch):
        # Лонг со стопом 3% от входа: при минимуме 1.5% проходит.
        monkeypatch.setattr(dec.llm_context, 'min_stop_pct', lambda atr_pct=None: 1.5)
        assert verdict()['ok']

        # Тот же сетап при минимуме 5% — уже нет.
        monkeypatch.setattr(dec.llm_context, 'min_stop_pct', lambda atr_pct=None: 5.0)
        tight = verdict()
        assert not tight['ok']
        assert tight['gate'] == 'стоп теснее минимального'


class TestExpectedValue:

    def test_a_losing_setup_is_refused(self, monkeypatch):
        """
        Сетап, который при нашей доле цели 0.20 не окупается:
        0.20 x 3.05 - 0.80 - 0.033 = -0.22. Доля берётся из наших исходов,
        а не из числа модели — её p с 23.09.2026 на решение не влияет.
        """
        monkeypatch.setattr(dec, 'empirical_p', lambda *a, **k: (0.20, 40))
        out = verdict()
        assert not out['ok']
        assert out['gate'] == 'ожидание не положительно'

    def test_the_model_probability_no_longer_decides(self, monkeypatch):
        """Модель может назвать хоть 0.20, хоть 0.75 — ворота смотрят на своё."""
        monkeypatch.setattr(dec, 'empirical_p', lambda *a, **k: (0.60, 40))
        assert verdict(p=0.20)['ok'], 'решает наша доля, а не слово модели'

    def test_costs_are_subtracted(self):
        """Ожидание без вычета издержек было бы систематически завышено."""
        with_costs = dec.expected_value(0.5, 2.0, 0.25)
        without = dec.expected_value(0.5, 2.0, 0.0)
        assert without - with_costs == pytest.approx(0.25)

    def test_the_refusal_keeps_the_numbers(self, monkeypatch):
        """
        Отказ по ожиданию обязан сохранить числа: без них нельзя потом
        проверить, правильно ли предохранитель отсекал.
        """
        monkeypatch.setattr(dec, 'empirical_p', lambda *a, **k: (0.20, 40))
        out = verdict()
        assert out['gate'] == 'ожидание не положительно'
        assert 'ev' in out and 'rr' in out and 'cost_r' in out


class TestConfluenceIsCountedByCode:
    """
    Оценку X/5 модель себе натянет: объяснение подгоняется под уже принятое
    решение. Поэтому она отмечает признаки, а считает их код.
    """

    def test_three_of_five_is_refused(self):
        out = verdict(cf={'poi': True, 'vp': True, 'der': True,
                          'smc': False, 'flow': False})
        assert not out['ok']
        assert out['gate'] == 'мало конфлюенса'
        assert out['votes'] == 3

    def test_four_of_five_passes(self):
        out = verdict(cf={'poi': True, 'vp': True, 'der': True,
                          'smc': True, 'flow': False})
        assert out['ok'], out.get('gate')
        assert out['votes'] == 4

    def test_the_vote_survives_a_refusal(self):
        """Счёт факторов нужен и у отказа — иначе разбирать нечего."""
        out = verdict(cf={'poi': False, 'vp': False, 'der': False,
                          'smc': False, 'flow': False})
        assert out['votes'] == 0
        assert out['confluence'] == {f: False for f in dec.FACTORS}


class TestRefusalIsAFirstClassAnswer:

    def test_a_skip_is_not_an_error(self):
        out = dec.check(dec.parse(
            '{"d":"skip","cf":{"poi":false,"vp":false,"der":false,'
            '"smc":false,"flow":false},"why":"стоп не окупается"}', LEVELS),
            LEVELS)
        assert not out['ok']
        assert out['gate'] == 'модель пропустила'
        assert 'стоп не окупается' in out['detail']

    def test_every_refusal_has_a_name(self):
        """
        Безымянный отказ превращает разбор в гадание — так уже вышло с зоной B
        и развилкой THIN_STOP, которые пришлось выбросить неизмеренными.
        """
        cases = [
            verdict(entry='L3', stop='L1', tp=['L2']),
            verdict(cf={f: False for f in dec.FACTORS}),
            dec.check(None, LEVELS),
        ]
        for out in cases:
            assert not out['ok']
            assert out['gate'], 'отказ без имени'


class TestBrokenAnswers:

    def test_a_non_json_answer_is_refused_not_raised(self):
        assert dec.parse('не json', LEVELS) is None
        out = dec.check(None, LEVELS)
        assert not out['ok']
        assert out['gate'] == 'ответ не разобран'

    def test_an_unknown_level_id_is_refused(self):
        """
        Грамматика такого не пропустит, но вызов может пройти и без неё —
        например, при отладке. Второй рубеж обязан устоять сам.
        """
        out = verdict(entry='L9')
        assert not out['ok']
        assert out['gate'] == 'уровень не найден'

    def test_an_empty_target_list_is_refused(self):
        out = verdict(tp=[])
        assert not out['ok']
        assert out['gate'] == 'уровень не найден'


class TestTheWholePass:

    def make_df(self):
        import numpy as np
        import pandas as pd
        idx = np.arange(400)
        closes = 100 + 4 * np.sin(idx / 23 * 2 * np.pi) + idx * 0.004
        return pd.DataFrame({
            'timestamp': pd.to_datetime(idx * 3_600_000 + 1_700_000_000_000,
                                        unit='ms'),
            'open': closes, 'high': closes + closes * 0.002,
            'low': closes - closes * 0.002, 'close': closes,
            'volume': np.full(400, 100.0),
        })

    def test_the_model_sees_the_grammar_for_these_levels(self, monkeypatch):
        # Без предела стопа: на синтетике уровни стоят теснее 1.5%, и
        # грамматика с расстояниями честно оставила бы один отказ.
        monkeypatch.setattr(dec.llm_context, 'min_stop_pct', lambda atr_pct=None: 0.0)
        seen = {}

        def ask(prompt, grammar, max_tokens):
            seen['prompt'] = prompt
            seen['grammar'] = grammar
            return '{"d":"skip","cf":{"poi":false,"vp":false,"der":false,' \
                   '"smc":false,"flow":false},"why":"нет"}'

        out = dec.decide('BTCUSDT', self.make_df(), ask)
        assert 'УРОВНИ' in seen['prompt']
        for level in out['levels']:
            # Уровень в грамматике — номером с ценой из таблицы.
            assert dec.llm_context.label(level['id'], level['price']) in seen['grammar']

    def test_a_failing_model_does_not_raise(self):
        def boom(prompt, grammar, max_tokens):
            raise RuntimeError('модель не загружена')

        out = dec.decide('BTCUSDT', self.make_df(), boom)
        assert not out['ok']
        assert out['gate'] == 'модель недоступна'

    def test_thin_data_refuses_before_calling_the_model(self):
        import numpy as np
        import pandas as pd

        def never(prompt, grammar, max_tokens):
            raise AssertionError('модель вызвана без разметки')

        flat = pd.DataFrame({
            'timestamp': pd.to_datetime(np.arange(20) * 3_600_000, unit='ms'),
            'open': np.full(20, 100.0), 'high': np.full(20, 100.0),
            'low': np.full(20, 100.0), 'close': np.full(20, 100.0),
            'volume': np.full(20, 1.0),
        })
        out = dec.decide('BTCUSDT', flat, never)
        assert not out['ok']
        assert out['gate'] == 'нет разметки'


class TestATruncatedAnswerIsNamedApart:
    """
    Обрубок и мусор — разные поломки, и чинятся они в разных местах.

    С 18 на 19 сентября 2026 журнал сервера 119 раз подряд написал «модель
    вернула не JSON». Имя было честным и бесполезным: по нему выходило, что
    модель отвечает чепухой, и чинить полезли бы промт. На деле окно контекста
    было 2048 при вопросе в 1703 токена, ответ обрывался на 345-м, и JSON не
    закрывался. Модель отвечала правильно — ей не давали договорить.

    Отличить можно по одному признаку: грамматика заканчивает ответ скобкой.
    """

    def test_an_unfinished_json_is_called_truncated(self):
        cut = '{"regime":"боковик","analysis":"уровень 82300 держал цену три'
        assert dec.parse(cut, LEVELS) is None
        out = dec.check(None, LEVELS, cut)
        assert out['gate'] == 'ответ обрезан'
        assert 'окно контекста' in out['detail']

    def test_the_tail_is_kept_so_the_break_point_is_visible(self):
        cut = '{"regime":"тренд вверх","analysis":"скопление минимумов сви'
        out = dec.check(None, LEVELS, cut)
        assert 'сви' in out['detail'], 'по детали не видно, где оборвалось'

    def test_real_rubbish_keeps_its_own_name(self):
        out = dec.check(None, LEVELS, 'извините, я не могу помочь}')
        assert out['gate'] == 'ответ не разобран'

    def test_an_empty_answer_is_neither(self):
        """
        Пустота — это не обрубок: модель не вернула ни одного токена, и
        причина другая. Сваливать их в одно имя значит снова чинить не там.
        """
        out = dec.check(None, LEVELS, '   ')
        assert out['gate'] == 'ответ пуст'

    def test_silence_about_the_raw_text_stays_unnamed(self):
        """
        Не сказали, что ответила модель, — не выдумываем. Пустая строка и
        несказанное это разные вещи.
        """
        out = dec.check(None, LEVELS)
        assert out['gate'] == 'ответ не разобран'


class TestTheAnswerLimitComesFromTheSettings:
    """
    Предел длины ответа обязан быть ОДИН и лежать в одном месте.

    Здесь стояло своё число — 400, — и strategy_llm звал decide без этого
    параметра. Боевой путь получал 400 всегда, чем бы ни был LLM_MAX_TOKENS:
    поднятый до 1200 предел не доходил до модели вовсе. Поднимали его дважды,
    глядя на обрезанные ответы, и дважды без толку.
    """

    def test_decide_does_not_impose_a_limit_of_its_own(self):
        seen = {}

        def ask(prompt, grammar, max_tokens):
            seen['max_tokens'] = max_tokens
            return '{"d":"skip","cf":{"poi":false,"vp":false,"der":false,' \
                   '"smc":false,"flow":false},"why":"нет"}'

        dec.decide('BTCUSDT', TestTheWholePass().make_df(), ask)
        assert seen['max_tokens'] is None, (
            'decide навязывает свой предел — настройка снова не дойдёт')


class TestTheTriggerIsExecutable:
    """
    Условие входа — объект, который исполняет код, а не текст для журнала.

    Пока условие было строкой, модель писала «дождаться закрытия выше L3», а
    код ставил лимит немедленно: половина её логики не доходила до сделки.
    """

    def test_an_object_trigger_resolves_to_a_price(self):
        out = verdict(trigger={'when': 'close_above', 'level': 'L2',
                               'note': 'и объём выше медианы'})
        assert out['ok']
        assert out['trigger_when'] == 'close_above'
        assert out['trigger_level'] == 107.0
        assert out['trigger_id'] == 'L2'
        assert out['trigger'] == 'close_above L2: и объём выше медианы'

    def test_a_string_trigger_still_means_now(self):
        """Старые ответы и проверки отвечают строкой — это «сейчас»."""
        out = verdict(trigger='возврат в уровень')
        assert out['trigger_when'] == 'now'
        assert out['trigger_level'] is None
        assert out['trigger'] == 'возврат в уровень'

    def test_a_trigger_on_an_unknown_level_falls_back_to_now(self):
        out = verdict(trigger={'when': 'close_below', 'level': 'L9', 'note': 'x'})
        assert out['trigger_when'] == 'now'
        assert 'не найден' in out['trigger']

    def test_now_needs_no_level(self):
        out = verdict(trigger={'when': 'now', 'note': 'лимит на уровень'})
        assert out['trigger_when'] == 'now'
        assert out['trigger'] == 'лимит на уровень'

    def test_the_grammar_only_allows_known_conditions(self):
        import llm_grammar
        text = llm_grammar.build([lv['id'] for lv in LEVELS])
        assert 'close_above' in text and 'close_below' in text and 'now' in text
        assert 'touch' not in text


class TestTheCriticSecondOpinion:
    """
    Второе мнение — только на «войти», тем же движком, другой ролью.

    Отклонение — отказ с именем; всё сказанное аналитиком остаётся в
    вердикте. Поломка критика — не подтверждение и не отказ.
    """

    def _ask_pair(self, analyst, critic):
        """Первый вызов — аналитик, второй — критик."""
        calls = []

        def ask(prompt, grammar, max_tokens):
            calls.append((prompt, grammar))
            return analyst if len(calls) == 1 else critic
        return ask, calls

    def test_a_confirmed_plan_passes_with_the_opinion_attached(self, monkeypatch):
        monkeypatch.setattr(config, 'LLM_CRITIC', True)
        # Проверки кода подменяем готовым «прошёл»: на синтетических свечах
        # уровни не те, что в LEVELS, а здесь проверяется вызов критика.
        ready = verdict()
        monkeypatch.setattr(dec, 'check', lambda parsed, levels, answer=None, market=None, min_stop=None, atr_pct=None: ready)
        ask, calls = self._ask_pair(
            answer(),
            '{"verdict":"confirm","issues":"возражений нет","worst":"—"}')
        out = dec.decide('BTCUSDT', TestTheWholePass().make_df(), ask)
        assert len(calls) == 2, 'критика не позвали'
        assert 'ПЛАН АНАЛИТИКА' in calls[1][0]
        assert 'verdict' in calls[1][1] and 'confirm' in calls[1][1]
        assert out['ok'] and out['critic']['verdict'] == 'confirm'

    def test_the_critic_can_be_switched_off(self, monkeypatch):
        monkeypatch.setattr(config, 'LLM_CRITIC', False)
        ready = verdict()
        monkeypatch.setattr(dec, 'check', lambda parsed, levels, answer=None, market=None, min_stop=None, atr_pct=None: ready)
        ask, calls = self._ask_pair(answer(), 'не должно быть вызвано')
        out = dec.decide('BTCUSDT', TestTheWholePass().make_df(), ask)
        assert len(calls) == 1 and out['ok'] and 'critic' not in out

    def test_the_panel_toggle_silences_the_critic(self, monkeypatch):
        """.env разрешает, оператор выключил с панели — критика нет."""
        import settings_store
        monkeypatch.setattr(config, 'LLM_CRITIC', True)
        monkeypatch.setattr(settings_store, 'critic_enabled', lambda: False)
        ready = verdict()
        monkeypatch.setattr(dec, 'check', lambda parsed, levels, answer=None, market=None, min_stop=None, atr_pct=None: ready)
        ask, calls = self._ask_pair(answer(), 'не должно быть вызвано')
        out = dec.decide('BTCUSDT', TestTheWholePass().make_df(), ask)
        assert len(calls) == 1 and out['ok'] and 'critic' not in out

    def test_a_rejected_plan_is_a_named_refusal(self):
        v = verdict()
        assert v['ok']
        out = dec.review(v, 'разметка', lambda p, g, m:
                         '{"verdict":"reject","issues":"стоп под равными минимумами",'
                         '"worst":"стоп снимут первым"}')
        assert out['ok'] is False
        assert out['gate'] == 'критик отклонил'
        assert out['detail'] == 'стоп снимут первым'
        assert out['critic']['verdict'] == 'reject'
        # Всё, что сказал аналитик, осталось: по нему потом видно, где спор.
        assert out['entry'] == 100.0 and out['analysis'] == v['analysis']

    def test_a_broken_critic_neither_confirms_nor_rejects(self):
        v = verdict()

        def boom(p, g, m):
            raise RuntimeError('модель не загрузилась')
        out = dec.review(v, 'разметка', boom)
        assert out['ok'] is True
        assert out['critic']['verdict'] == 'broken'

        out = dec.review(verdict(), 'разметка', lambda p, g, m: 'не json')
        assert out['ok'] is True and out['critic']['verdict'] == 'broken'

    def test_the_critic_is_not_called_on_a_refusal(self, monkeypatch):
        monkeypatch.setattr(config, 'LLM_CRITIC', True)
        calls = []

        def ask(prompt, grammar, max_tokens):
            calls.append(prompt)
            return answer(d='skip')
        dec.decide('BTCUSDT', TestTheWholePass().make_df(), ask)
        assert len(calls) == 1

    def test_the_plan_text_names_what_the_critic_must_see(self):
        text = dec.plan_text(verdict(trigger={'when': 'close_above', 'level': 'L2',
                                              'note': 'x'}))
        assert 'Направление: LONG' in text
        assert 'Вход: L3 100' in text and 'Стоп: L4 97' in text
        assert 'close_above L2' in text
        assert 'Разбор:' in text


class TestGeometryAgainstLiquidityIsCode:
    """
    Стоп вплотную под скоплением чужих стопов снимут вместе с ними — это
    арифметика, и платить за неё пять минут модели незачем. Препятствия на
    пути к цели входа не запрещают, но уходят критику и в журнал.
    """

    MARKET = {
        'smc': {'untapped': [{'price': 97.8, 'side': 'SSL', 'source': 'SWING'},
                             {'price': 110.0, 'side': 'BSL', 'source': 'SWING'}],
                'equal_levels': [{'price': 104.0, 'source': 'EQH', 'side': 'BSL'}]},
        'book': {'walls_above': [{'price': 103.0, 'volume_x': 8.0, 'dist_pct': 3.0}],
                 'walls_below': []},
        'pois': [{'type': 'ORDER_BLOCK', 'direction': 'BEARISH', 'top': 105.5,
                  'bottom': 105.0, 'touches': 0, 'bars_ago': 3, 'inside': False}],
        'liquidations': {'above': [{'from': 106.0, 'to': 106.4, 'share_pct': 40.0,
                                    'dist_pct': 6.2, 'side': 'шорты'}], 'below': []},
    }

    def test_a_stop_just_under_a_pool_is_refused(self):
        # Вход L3 100, стоп за L4 98 → 97.706; пул стопов лонгов на 97.8,
        # в 0.1% выше стопа — снимут одним ходом. Сам L4 пулом не считается:
        # за него стоп и спрятан.
        out = dec.check(dec.parse(answer(), LEVELS), LEVELS, market=self.MARKET)
        assert out['ok'] is False
        assert out['gate'] == 'стоп в скоплении стопов'
        assert '97.8' in out['detail']

    def test_a_stop_safely_below_the_pool_passes(self):
        market = {'smc': {'untapped': [{'price': 98.5, 'side': 'SSL', 'source': 'SWING'}]}}
        out = dec.check(dec.parse(answer(), LEVELS), LEVELS, market=market)
        assert out['ok'] is True

    def test_obstacles_between_entry_and_target_are_listed_not_refused(self):
        market = dict(self.MARKET)
        market['smc'] = {'untapped': [{'price': 110.0, 'side': 'BSL', 'source': 'SWING'}],
                         'equal_levels': [{'price': 104.0, 'source': 'EQH', 'side': 'BSL'}]}
        out = dec.check(dec.parse(answer(), LEVELS), LEVELS, market=market)
        assert out['ok'] is True
        joined = '; '.join(out['obstacles'])
        assert 'плита 103' in joined
        assert 'order_block 105..105.5 против' in joined
        assert 'скопление 104 (EQH)' in joined
        assert 'ликвидации 106..106.4' in joined
        assert '110' not in joined, 'за целью — не препятствие'
        assert 'Между входом и первой целью' in dec.plan_text(out)

    def test_no_snapshot_no_checks(self):
        out = dec.check(dec.parse(answer(), LEVELS), LEVELS, market=None)
        assert out['ok'] is True and out['obstacles'] == []


class TestDistancesAreInTheGrammar:
    """
    Стоп ближе минимума и цель ближе min_rr × минимум невыразимы, а не
    отвергаемы: 19.09.2026 два первых плана «войти» умерли на этих
    предохранителях.
    """

    def test_a_stop_closer_than_the_minimum_is_not_offered(self):
        import llm_grammar
        ids = ['L1', 'L2', 'L3', 'L4']
        prices = [110.0, 103.0, 100.0, 99.5]          # L4 в 0.5% под L3
        text = llm_grammar.build(ids, prices=prices, min_stop_pct=1.5, min_rr=2.0)
        names = [l.split(' ')[0] for l in text.splitlines() if l.startswith('long-l2-')]
        assert sorted(names) == ['long-l2-l3', 'long-l2-l4']   # оба стопа дальше 1.5%
        # Лонг от L3: единственный стоп L4 слишком близко — ветки нет вовсе.
        assert not any(l.startswith('long-l3-') for l in text.splitlines())

    def test_a_target_closer_than_rr_times_minimum_is_not_offered(self):
        import llm_grammar
        ids = ['L1', 'L2', 'L3', 'L4']
        prices = [115.0, 104.0, 100.0, 97.0]          # стоп L4 3%: цель нужна ≥ 6%
        text = llm_grammar.build(ids, prices=prices, min_stop_pct=1.5, min_rr=2.0)
        # Лонг от L3 (100) со стопом L4 (97, 3%): цель должна быть дальше 6% —
        # L2 (102) не годится, L1 (104) годится.
        line = next(l for l in text.splitlines() if l.startswith('t-long-l3-l4'))
        assert 'L2' not in line and 'L1 (115)' in line, 'цель пишется номером с ценой'

    def test_without_prices_nothing_is_restricted(self):
        import llm_grammar
        a = llm_grammar.build(['L1', 'L2', 'L3'])
        b = llm_grammar.build(['L1', 'L2', 'L3'], prices=[100, 99, 98], min_stop_pct=0)
        # Без цен уровни пишутся голым номером, с ценами — «L1 (100)»;
        # набор планов при этом одинаков.
        strip = lambda t: t.replace(' (100)', '').replace(' (99)', '').replace(' (98)', '')
        assert a == strip(b) and 'L1 (100)' in b


class TestATriggerAgainstTheIdeaIsRefused:

    def test_long_after_close_below_the_entry_is_refused(self):
        out = verdict(trigger={'when': 'close_below', 'level': 'L3', 'note': 'x'})
        assert out['ok'] is False and out['gate'] == 'условие противоречит входу'

    def test_long_after_close_above_a_higher_level_is_fine(self):
        out = verdict(trigger={'when': 'close_above', 'level': 'L2', 'note': 'x'})
        assert out['ok'] is True

    def test_short_after_close_above_the_entry_is_refused(self):
        out = verdict(d='enter', side='SHORT', entry='L2', stop='L1', tp=['L3'], inval='L1',
                      trigger={'when': 'close_above', 'level': 'L2', 'note': 'x'})
        assert out['ok'] is False and out['gate'] == 'условие противоречит входу'


class TestBiasIsAlwaysThere:
    """Куда рынок — и при отказе: так отказ становится проверяемым."""

    def test_bias_is_parsed_on_enter_and_skip(self):
        out = dec.parse(answer(bias='up'), LEVELS)
        assert out['bias'] == 'up'
        out = dec.parse(answer(d='skip', bias='down'), LEVELS)
        assert out['bias'] == 'down'
        assert dec.check(out, LEVELS)['bias'] == 'down'

    def test_an_unknown_bias_is_empty_not_wrong(self):
        assert dec.parse(answer(bias='sideways'), LEVELS)['bias'] == ''

    def test_the_grammar_demands_bias_in_both_answers(self):
        import llm_grammar
        for text in (llm_grammar.build(['L1', 'L2', 'L3']), llm_grammar.build([])):
            assert 'bias' in text and 'flat' in text


class TestDynamicBuffers:
    """
    Порог «стоп под скоплением» и минимальный стоп зависят от размаха:
    фиксированные 0.25% и 1.5% тесны для монеты с ATR 3% и щедры для BTC.
    """

    def test_the_hunt_threshold_grows_with_atr(self):
        assert dec.stop_hunt_pct(None) == pytest.approx(0.3)
        assert dec.stop_hunt_pct(0.7) == pytest.approx(0.37)
        assert dec.stop_hunt_pct(3.0) == pytest.approx(0.6)

    def test_a_pool_0_4pct_above_the_stop_is_hunted_on_a_wide_market(self):
        market = {'smc': {'untapped': [{'price': 97.4, 'side': 'SSL', 'source': 'SWING'}]}}
        # стоп 97 (L4), пул 97.4: gap 0.41% — при ATR 0.7 (порог 0.37%) проходит,
        # при ATR 3 (порог 0.6%) — отказ.
        assert dec.stop_in_liquidity('LONG', 97.0, market, atr_pct=0.7) == ''
        assert 'снимут' in dec.stop_in_liquidity('LONG', 97.0, market, atr_pct=3.0)

    def test_the_minimum_stop_follows_half_atr(self, monkeypatch):
        import llm_context
        assert llm_context.min_stop_pct(None) == pytest.approx(1.5)
        assert llm_context.min_stop_pct(0.8) == pytest.approx(1.5)
        assert llm_context.min_stop_pct(4.0) == pytest.approx(2.0)
        out = dec.check(dec.parse(answer(), LEVELS), LEVELS, min_stop=4.0)
        assert out['gate'] == 'стоп теснее минимального'


class TestEntryOnAPoolIsRefused:
    """
    20.09.2026 BNB: шорт «на EQL, где стопы лонгов». Пул по ходу сделки —
    цель или место выноса, вход от него законен только после sweep_reclaim.
    """

    LEVELS = [
        {'id': 'L1', 'price': 109.0, 'touches': 3, 'kind': 'скопление максимумов'},
        {'id': 'L2', 'price': 107.0, 'touches': 2, 'kind': 'пивот-максимум'},
        {'id': 'L3', 'price': 100.0, 'touches': 4, 'kind': 'равные экстремумы EQL / нетронутое скопление'},
        {'id': 'L4', 'price': 98.0, 'touches': 5, 'kind': 'скопление минимумов'},
        {'id': 'L5', 'price': 94.0, 'touches': 2, 'kind': 'пивот-минимум'},
    ]

    def test_a_short_from_equal_lows_is_refused(self):
        out = dec.check(dec.parse(answer(side='SHORT', entry='L3', stop='L2', tp=['L5'], inval='L2'),
                                  self.LEVELS), self.LEVELS)
        assert out['ok'] is False and out['gate'] == 'вход на пуле стопов'
        assert 'EQL' in out['detail']

    def test_a_short_from_equal_lows_is_refused_even_after_a_sweep(self):
        # Вынос минимумов с возвратом вверх — это разворот ВВЕРХ; шорт после
        # него — продажа в дно. BNB 20.09, второй заход.
        out = dec.check(dec.parse(answer(side='SHORT', entry='L3', stop='L2', tp=['L5'], inval='L2',
                                         trigger={'when': 'sweep_reclaim', 'level': 'L3', 'note': ''}),
                                  self.LEVELS), self.LEVELS)
        assert out['gate'] == 'вход на пуле стопов' and 'дно выноса' in out['detail']

    def test_a_long_from_equal_lows_after_a_sweep_and_reclaim_is_valid(self):
        levels = [
            {'id': 'L1', 'price': 109.0, 'touches': 3, 'kind': 'скопление максимумов'},
            {'id': 'L2', 'price': 107.0, 'touches': 2, 'kind': 'пивот-максимум'},
            {'id': 'L3', 'price': 100.0, 'touches': 4, 'kind': 'равные экстремумы EQL'},
            {'id': 'L4', 'price': 98.0, 'touches': 5, 'kind': 'пивот-минимум'},
            {'id': 'L5', 'price': 94.0, 'touches': 2, 'kind': 'пивот-минимум'},
        ]
        plain = dec.check(dec.parse(answer(entry='L3', stop='L4', tp=['L2']), levels), levels)
        assert plain['gate'] == 'вход на пуле стопов'
        swept = dec.check(dec.parse(answer(entry='L3', stop='L4', tp=['L2'],
                                           trigger={'when': 'sweep_reclaim', 'level': 'L3', 'note': ''}),
                                    levels), levels)
        assert swept['gate'] != 'вход на пуле стопов', swept.get('detail')

    def test_a_long_from_equal_highs_is_always_refused(self):
        levels = [
            {'id': 'L1', 'price': 109.0, 'touches': 3, 'kind': 'скопление максимумов'},
            {'id': 'L2', 'price': 107.0, 'touches': 2, 'kind': 'равные экстремумы EQH'},
            {'id': 'L3', 'price': 100.0, 'touches': 4, 'kind': 'пивот-минимум'},
            {'id': 'L4', 'price': 98.0, 'touches': 5, 'kind': 'пивот-минимум'},
            {'id': 'L5', 'price': 94.0, 'touches': 2, 'kind': 'пивот-минимум'},
        ]
        out = dec.check(dec.parse(answer(entry='L2', stop='L3', tp=['L1'],
                                         trigger={'when': 'sweep_reclaim', 'level': 'L2', 'note': ''}),
                                  levels), levels)
        assert out['gate'] == 'вход на пуле стопов' and 'вершине выноса' in out['detail']

    def test_a_zone_edge_that_coincides_with_a_pool_is_fine(self):
        out = verdict()                      # L3 — низ зоны ордер-блок / скопление
        assert out['ok'], out.get('gate')


class TestTheAnalysisIsSixFields:
    def test_six_fields_become_numbered_paragraphs(self):
        parts = {'direction': 'вверх', 'liquidity': 'стопы под 98', 'structure': 'ОБ 100',
                 'flow': 'дельта плюс', 'conflicts': 'ОИ падает', 'plan': 'от 100 к 107'}
        text = dec.analysis_text(parts)
        assert text.startswith('1. Куда рынок: вверх') and '6. План: за что и куда: от 100 к 107' in text
        out = dec.parse(answer(analysis=parts), LEVELS)
        assert out['analysis_parts'] == parts and '3. Структура и зоны: ОБ 100' in out['analysis']

    def test_a_plain_string_still_works(self):
        assert dec.analysis_text('строкой') == 'строкой'


class TestTheJustificationMustMatchThePlan:
    """BNB 20.09: в tp_why — L12 746.5, в плане — L20: грамматика не пустила
    близкую цель, модель взяла первую разрешённую и продолжила писать о своей."""

    def test_a_target_justified_by_another_level_is_refused(self):
        out = verdict(tp_why='L5 (94.0) — скопление минимумов, магнит')
        assert out['ok'] is False and out['gate'] == 'обоснование не о том плане'
        assert 'L5' in out['detail'] and 'L2' in out['detail']

    def test_the_price_counts_as_a_mention(self):
        out = verdict(tp_why='цель у 94.0 — скопление минимумов')
        assert out['gate'] == 'обоснование не о том плане'

    def test_a_matching_justification_passes(self):
        out = verdict(tp_why='L2 (107) — скопление максимумов, магнит', stop_why='за L4 98 — низ скопления')
        assert out['ok'], out.get('gate')

    def test_prose_without_levels_is_not_checked(self):
        out = verdict(tp_why='ближайший магнит сверху', stop_why='за защищающей структурой')
        assert out['ok'], out.get('gate')


class TestThinkingBeforeTheAnswer:
    """Мысль в <think> отделяется от JSON, идёт в вердикт и не путает
    проверку на обрубок."""

    def test_the_thought_is_split_off_and_kept(self):
        raw = '<think>стоп за свингом дня, цель EQL</think>\n' + answer()
        parsed = dec.parse(raw, LEVELS)
        assert parsed['thought'] == 'стоп за свингом дня, цель EQL'
        out = dec.check(parsed, LEVELS)
        assert out['ok'] and out['thought'].startswith('стоп за')

    def test_without_a_thought_nothing_changes(self):
        assert dec.split_thought(answer()) == ('', answer())
        assert dec.parse(answer(), LEVELS)['thought'] == ''

    def test_a_cut_answer_after_a_thought_is_still_a_cut(self):
        assert dec.truncated('<think>x</think>{"d":"enter",') is True
        assert dec.truncated('<think>x</think>' + answer()) is False


class TestTheGrammarAdmitsAThought:
    def test_the_think_block_is_optional_and_bounded(self):
        import llm_grammar as g
        text = g.build(['L1', 'L2', 'L3', 'L4', 'L5', 'L6'], prices=[120, 112, 106, 100, 95, 90],
                       min_stop_pct=1.5, min_rr=2.0, think_chars=4000)
        head = text.splitlines()[:5]
        assert head[0] == 'root     ::= think answer | answer'
        assert 'tchar{0,4000}' in head[1] and head[4].startswith('answer   ::= enter | skip')

    def test_zero_budget_means_no_block(self):
        import llm_grammar as g
        text = g.build(['L1', 'L2', 'L3', 'L4', 'L5', 'L6'], prices=[120, 112, 106, 100, 95, 90],
                       min_stop_pct=1.5, min_rr=2.0, think_chars=0)
        assert text.startswith('root     ::= enter | skip')


class TestLabelledLevelsAreParsedToIds:
    def test_ids_lose_the_price_and_prices_resolve(self):
        import json
        import llm_context
        levels = [{'id': 'L1', 'price': 110.0}, {'id': 'L2', 'price': 100.0}, {'id': 'L3', 'price': 95.0}]
        answer = json.dumps({'regime': 'r', 'analysis': 'a', 'bias': 'up', 'd': 'enter', 'side': 'LONG',
                             'entry': 'L2 (100)', 'stop': 'L3 (95)', 'tp': ['L1 (110)'], 'inval': 'L3 (95)',
                             'trigger': {'when': 'close_above', 'level': 'L2 (100)', 'note': 'n'},
                             'cf': {}, 'p': 0.6, 'why': 'w'})
        out = dec.parse(answer, levels)
        assert out['ids'] == {'entry': 'L2', 'stop': 'L3', 'tp': ['L1'], 'inval': 'L3'}
        assert out['entry'] == 100.0 and out['stop'] == 95.0 and out['targets'] == [110.0]
        assert out['trigger_level'] == 100.0 and out['trigger_id'] == 'L2'
        assert llm_context.level_id_of('L11 (2615)') == 'L11' and llm_context.level_id_of('L11') == 'L11'
        assert llm_context.label('L11', 2615) == 'L11 (2615)' and llm_context.label('L4', 2709.71) == 'L4 (2709.71)'


class TestThePlanParagraphMustNameTheSameStop:
    """
    LTC 21.09.2026: разбор — «стоп за L5 (59.99)», план — L7 (грамматика не
    пустила стоп в 0.2% от входа), stop_why задним числом объяснил L7.
    Разбор пишется до решения — расхождение с ним и есть подмена.
    """

    def _levels(self):
        return [{'id': 'L1', 'price': 63.294}, {'id': 'L4', 'price': 60.13}, {'id': 'L5', 'price': 59.99},
                {'id': 'L7', 'price': 59.33}]

    def test_a_stop_moved_by_the_grammar_is_caught_via_the_plan_paragraph(self):
        parsed = {'ids': {'entry': 'L4', 'stop': 'L7', 'tp': ['L1']},
                  'stop_why': 'За L7 (59.33) — максимум недели', 'tp_why': 'L1 (63.294) — ликвидации шортов',
                  'analysis_parts': {'plan': 'Вход в зону имбаланса L4..L5 по ретесту. Стоп за L5 (59.99). Цель L1 (63.294).'}}
        out = dec.justification_mismatch(parsed, self._levels())
        assert out and 'analysis.plan' in out and 'L5' in out

    def test_a_consistent_plan_passes(self):
        parsed = {'ids': {'entry': 'L4', 'stop': 'L7', 'tp': ['L1']},
                  'stop_why': 'За L7 (59.33)', 'tp_why': 'L1 (63.294)',
                  'analysis_parts': {'plan': 'Вход L4, стоп за L7 (59.33), цель L1.'}}
        assert dec.justification_mismatch(parsed, self._levels()) == ''

    def test_a_plan_paragraph_without_a_stop_level_is_not_judged(self):
        parsed = {'ids': {'entry': 'L4', 'stop': 'L7', 'tp': ['L1']},
                  'stop_why': 'За L7', 'tp_why': 'L1',
                  'analysis_parts': {'plan': 'Лонг от имбаланса к пулу шортов, стоп за структурой.'}}
        assert dec.justification_mismatch(parsed, self._levels()) == ''


class TestExpectedValueUsesOurOwnStatistics:
    """
    Вероятность берётся из НАШИХ исходов, а не из числа, которое назвала
    модель. Модель ставила 0.65 в 41 плане из 68 — это константа, и ворота
    «ожидание не положительно» с ней не сработали ни разу за 179 разборов
    (при p ≥ 0.55 и R:R ≥ 2.5 EV всегда ≥ 0.7). По факту доля планов,
    дошедших до цели раньше стопа, — 0.19 на 47 наблюдениях.
    """

    def _file(self, tmp_path, rows):
        import csv
        path = tmp_path / 'out.csv'
        cols = ['at', 'gate', 'side', 'hit_tp1', 'hit_sl', 'tp_hours', 'sl_hours']
        with open(path, 'w', encoding='utf-8', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, '') for c in cols})
        return str(path)

    def test_too_few_outcomes_means_no_probability(self, tmp_path, monkeypatch):
        rows = [{'at': '2026-09-24', 'side': 'LONG', 'hit_tp1': '1', 'tp_hours': '3'}] * 5
        monkeypatch.setattr(dec, 'llm_outcomes_path', lambda: self._file(tmp_path, rows))
        p, n = dec.empirical_p(since='2026-09-01')
        assert p is None and n == 5, 'на пяти исходах доля — шум, а не оценка'

    def test_it_counts_only_targets_reached_before_the_stop(self, tmp_path, monkeypatch):
        rows = ([{'at': '2026-09-24', 'side': 'LONG', 'hit_tp1': '1', 'hit_sl': '0', 'tp_hours': '3'}] * 10
                + [{'at': '2026-09-24', 'side': 'LONG', 'hit_tp1': '1', 'hit_sl': '1',
                    'tp_hours': '9', 'sl_hours': '4'}] * 10        # стоп был раньше — не в зачёт
                + [{'at': '2026-09-24', 'side': 'LONG', 'hit_tp1': '0', 'hit_sl': '1', 'sl_hours': '2'}] * 20)
        monkeypatch.setattr(dec, 'llm_outcomes_path', lambda: self._file(tmp_path, rows))
        p, n = dec.empirical_p(since='2026-09-01')
        assert n == 40 and p == pytest.approx(0.25)

    def test_plans_from_the_old_rules_do_not_count(self, tmp_path, monkeypatch):
        rows = [{'at': '2026-09-20', 'side': 'SHORT', 'hit_tp1': '0', 'hit_sl': '1'}] * 50
        monkeypatch.setattr(dec, 'llm_outcomes_path', lambda: self._file(tmp_path, rows))
        p, n = dec.empirical_p(since='2026-09-23')
        assert (p, n) == (None, 0), 'та геометрия к этой отношения не имеет'

    def test_refusals_and_plans_without_a_side_are_skipped(self, tmp_path, monkeypatch):
        rows = ([{'at': '2026-09-24', 'gate': 'мало конфлюенса', 'hit_tp1': '1'}] * 40
                + [{'at': '2026-09-24', 'side': '', 'hit_tp1': '1'}] * 40)
        monkeypatch.setattr(dec, 'llm_outcomes_path', lambda: self._file(tmp_path, rows))
        assert dec.empirical_p(since='2026-09-01') == (None, 0)

    def test_without_a_probability_the_gate_cannot_fire(self, tmp_path, monkeypatch):
        """Нет своих наблюдений — нет и приговора: ворота молчат, EV пуст."""
        monkeypatch.setattr(dec, 'empirical_p', lambda *a, **k: (None, 3))
        out = verdict()
        assert out['gate'] != 'ожидание не положительно'
        assert out.get('ev') in ('', None)
        assert out.get('p_n') == 3

    def test_with_a_bad_probability_the_gate_fires(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dec, 'empirical_p', lambda *a, **k: (0.10, 44))
        out = verdict()
        assert out['gate'] == 'ожидание не положительно'
        assert '0.10' in out['detail'] and '44' in out['detail']


class TestARefusedPlanGoesBackToTheModel:
    """
    Отказ по суждению — не приговор, а замечание: код возвращает его модели
    и просит перестроить план.

    ОТКУДА. За 206 разборов код отказал 77 раз — семьдесят семь выброшенных
    пятнадцатиминутных разборов, где модель прочитала рынок и ошиблась в
    одном месте плана. Памяти между вызовами у неё нет, поэтому отказ её
    ничему не учит; возврат с причиной — единственный способ дать исправить.
    Попытка ровно одна.
    """

    def _decide(self, answers, **over):
        """Прогон decide с очередью ответов модели."""
        import pandas as pd
        import numpy as np
        asked = []

        def ask(prompt, grammar, max_tokens):
            asked.append(prompt)
            return answers[len(asked) - 1]

        n = 300
        close = 100 + np.cumsum(np.random.default_rng(3).normal(0, 0.4, n))
        df = pd.DataFrame({
            'timestamp': pd.date_range('2026-01-01', periods=n, freq='h'),
            'open': close, 'high': close + 0.5, 'low': close - 0.5,
            'close': close, 'volume': np.ones(n) * 10,
        })
        out = dec.decide('BTCUSDT', df, ask, **over)
        return out, asked

    def test_a_fixable_refusal_is_returned_with_its_reason(self):
        """Второй вопрос содержит и прошлый ответ, и причину отказа."""
        bad = answer(stop='L2')                    # стоп выше входа у лонга
        good = answer()
        out, asked = self._decide([bad, good])
        assert len(asked) == 2, 'модель не спросили второй раз'
        assert 'ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ' in asked[1]
        assert 'КОД ОТВЕРГ ЭТОТ ПЛАН' in asked[1]
        assert out.get('revised_from', '').startswith('геометрия неверна')
        assert out['raw'] == good

    def test_the_second_answer_is_the_one_that_counts(self):
        """В вердикт идёт второй ответ, а первый остаётся только в пометке."""
        bad, good = answer(stop='L2'), answer()
        out, _ = self._decide([bad, good])
        assert out['raw'] == good
        assert out['gate'] != 'геометрия неверна', 'вердикт остался от первого ответа'

    def test_only_one_retry(self):
        """Две ошибки подряд — второй отказ окончателен, третьего вопроса нет."""
        out, asked = self._decide([answer(stop='L2'), answer(stop='L2')])
        assert len(asked) == 2
        assert not out['ok'] and out['gate'] == 'геометрия неверна'

    def test_a_model_decision_is_not_second_guessed(self):
        """«Пропускаю» — решение модели, а не ошибка плана: не переспрашиваем."""
        out, asked = self._decide([answer(d='skip', why='нечего')])
        assert len(asked) == 1
        assert out['gate'] == 'модель пропустила'

    def test_the_question_is_repeated_so_the_prefix_stays_cached(self):
        """
        Второй вопрос начинается тем же текстом: сервер держит префикс в
        кэше, и переделка стоит минуты вместо пятнадцати.
        """
        _, asked = self._decide([answer(stop='L2'), answer()])
        assert asked[1].startswith(asked[0][:2000])

    def test_a_broken_second_answer_keeps_the_first_refusal(self, monkeypatch):
        import pandas as pd
        import numpy as np

        def ask(prompt, grammar, max_tokens):
            if 'ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ' in prompt:
                raise RuntimeError('модель молчит')
            return answer(stop='L2')

        n = 300
        close = 100 + np.cumsum(np.random.default_rng(3).normal(0, 0.4, n))
        df = pd.DataFrame({
            'timestamp': pd.date_range('2026-01-01', periods=n, freq='h'),
            'open': close, 'high': close + 0.5, 'low': close - 0.5,
            'close': close, 'volume': np.ones(n) * 10,
        })
        out = dec.decide('BTCUSDT', df, ask)
        assert out['gate'] == 'геометрия неверна', 'поломка переделки не должна менять вердикт'

    def test_the_rejected_plan_is_kept_whole(self):
        """
        Пара «что модель написала сперва» и «что после замечания» — материал
        для обучения предпочтениям. Второй ответ уходит в журнал как `raw`,
        первый обязан сохраниться рядом, иначе пара распадается.
        """
        bad, good = answer(stop='L2'), answer()
        out, _ = self._decide([bad, good])
        assert out['rejected_raw'] == bad
        assert out['raw'] == good
        assert out['revised_from'].startswith('геометрия неверна')

    def test_without_a_revision_there_is_nothing_to_keep(self):
        """Переделки не было — поле пустое, пары нет."""
        out, asked = self._decide([answer(d='skip', why='нечего')])
        assert len(asked) == 1
        assert not out.get('rejected_raw') and not out.get('revised_from')
