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
#   лонг:  вход L3 100, стоп L4 97 (риск 3), цель L2 107 (ход 7) -> R:R 2.33
#   шорт:  вход L2 107, стоп L1 109 (риск 2), цель L3 100 (ход 7) -> R:R 3.50
LEVELS = [
    {'id': 'L1', 'price': 109.0, 'touches': 3, 'kind': 'скопление максимумов'},
    {'id': 'L2', 'price': 107.0, 'touches': 2, 'kind': 'скопление максимумов'},
    {'id': 'L3', 'price': 100.0, 'touches': 4, 'kind': 'скопление минимумов'},
    {'id': 'L4', 'price': 97.0, 'touches': 5, 'kind': 'скопление минимумов'},
    {'id': 'L5', 'price': 94.0, 'touches': 2, 'kind': 'пивот-минимум'},
]

ALL_TRUE = {'poi': True, 'vp': True, 'der': True, 'smc': True, 'flow': True}


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
        assert out['stop'] == 97.0
        assert out['targets'] == [107.0, 109.0]

    def test_the_numbers_are_computed_not_taken_from_the_model(self):
        """
        R:R и ожидание считает код. Модель их не присылает вовсе — и не должна:
        на арифметике она ошибается, а порог, проверенный по её же ошибке,
        не защищает ни от чего.
        """
        out = verdict()
        # Вход 100, стоп 97, первая цель 107: риск 3, ход 7.
        assert out['rr'] == pytest.approx(7 / 3, abs=0.01)
        assert out['cost_r'] == pytest.approx(
            config.ENTRY_COST_ROUND_TRIP / 0.03, rel=1e-6)
        assert out['ev'] == pytest.approx(
            0.6 * (7 / 3) - 0.4 - out['cost_r'], abs=0.01)


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
        monkeypatch.setattr(dec.llm_context, 'min_stop_pct', lambda: 1.5)
        assert verdict()['ok']

        # Тот же сетап при минимуме 5% — уже нет.
        monkeypatch.setattr(dec.llm_context, 'min_stop_pct', lambda: 5.0)
        tight = verdict()
        assert not tight['ok']
        assert tight['gate'] == 'стоп теснее минимального'


class TestExpectedValue:

    def test_a_losing_setup_is_refused(self):
        """
        Тот же сетап, что проходит при вероятности 0.6, при 0.30 обязан
        отсеяться: 0.30 x 2.33 - 0.70 - 0.025 = -0.026.
        """
        out = verdict(p=0.30)
        assert not out['ok']
        assert out['gate'] == 'ожидание не положительно'

    def test_costs_are_subtracted(self):
        """Ожидание без вычета издержек было бы систематически завышено."""
        with_costs = dec.expected_value(0.5, 2.0, 0.25)
        without = dec.expected_value(0.5, 2.0, 0.0)
        assert without - with_costs == pytest.approx(0.25)

    def test_the_refusal_keeps_the_numbers(self):
        """
        Отказ по ожиданию обязан сохранить числа: без них нельзя потом
        проверить, правильно ли предохранитель отсекал.
        """
        out = verdict(p=0.30)
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

    def test_the_model_sees_the_grammar_for_these_levels(self):
        seen = {}

        def ask(prompt, grammar, max_tokens):
            seen['prompt'] = prompt
            seen['grammar'] = grammar
            return '{"d":"skip","cf":{"poi":false,"vp":false,"der":false,' \
                   '"smc":false,"flow":false},"why":"нет"}'

        out = dec.decide('BTCUSDT', self.make_df(), ask)
        assert 'УРОВНИ' in seen['prompt']
        for level in out['levels']:
            assert f'"\\"{level["id"]}\\""' in seen['grammar']

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
        monkeypatch.setattr(dec, 'check', lambda parsed, levels, answer=None: ready)
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
        monkeypatch.setattr(dec, 'check', lambda parsed, levels, answer=None: ready)
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
