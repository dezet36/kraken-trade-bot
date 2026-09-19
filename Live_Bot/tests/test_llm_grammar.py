"""
Грамматика ответа: что она разрешает и чего разрешить не может.

ЗАЧЕМ ЭТО ПРОВЕРЯТЬ ОТДЕЛЬНО. Грамматика — единственное, что мешает модели
назвать цену, которой нет в данных. Просьба в промпте таким препятствием не
является: выдуманный уровень выглядит как настоящий, и объяснение к нему будет
не хуже. Если грамматика соберётся с ошибкой, запрет исчезнет молча.

Ошибку в ней llama.cpp сообщает только в момент вызова — то есть уже в бою, на
живом сетапе. Поэтому она собирается и разбирается здесь, без модели.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_grammar as gr


class TestOnlyTheGivenLevelsAreAllowed:
    """Главное свойство: сослаться можно лишь на то, что посчитал код."""

    def test_every_given_level_is_allowed(self):
        ids = ['L1', 'L2', 'L3', 'L4']
        text = gr.build(ids)
        for level in ids:
            assert f'"\\"{level}\\""' in text

    def test_a_level_beyond_the_list_is_absent(self):
        """
        На сетапе с четырьмя уровнями ссылка на L9 обязана быть невозможна.
        Общей грамматики «L1..L12» быть не может ровно поэтому.
        """
        text = gr.build(['L1', 'L2', 'L3', 'L4'])
        assert '"\\"L5\\""' not in text
        assert '"\\"L9\\""' not in text

    def test_the_allowed_set_follows_the_setup(self):
        small = gr.build(['L1', 'L2'])
        large = gr.build([f'L{i}' for i in range(1, 13)])
        assert small != large
        assert '"\\"L12\\""' in large
        assert '"\\"L12\\""' not in small


class TestNothingReferencesAMissingRule:
    """
    Опечатка в имени правила делает грамматику нерабочей, а узнать об этом
    можно только при вызове модели. Поэтому имена сверяются здесь.
    """

    @pytest.mark.parametrize('count', [0, 1, 2, 3, 7, 12])
    def test_all_rules_resolve(self, count):
        text = gr.build([f'L{i}' for i in range(1, count + 1)])
        missing = gr.referenced(text) - set(gr.rules_of(text))
        assert not missing, f'ссылки без определения: {sorted(missing)}'

    def test_character_classes_are_not_mistaken_for_rules(self):
        """
        Проверка выше однажды сообщала о «неопределённых правилах» x00 и x1f.
        Это куски символьного класса, а не имена — ложная тревога отправляла
        искать ошибку, которой нет.
        """
        text = gr.build(['L1'])
        assert 'x00' not in gr.referenced(text)
        assert 'x1f' not in gr.referenced(text)


class TestRefusalIsAlwaysAvailable:
    """
    Отказ — полноценный ответ. Большинство сетапов брать не надо, и если
    грамматика отказ не разрешает, модель вынуждена придумать вход.
    """

    def test_skip_exists_alongside_entry(self):
        text = gr.build(['L1', 'L2'])
        assert 'skip' in gr.rules_of(text)
        assert 'enter' in gr.rules_of(text)

    def test_without_levels_only_refusal_remains(self):
        """
        Уровней нет — значит ссылаться не на что. Разрешить вход здесь
        означало бы разрешить ссылку на несуществующее.
        """
        text = gr.build([])
        assert '\\"skip\\"' in text
        assert '\\"enter\\"' not in text
        assert 'side' not in gr.rules_of(text)

    def test_refusal_still_reports_confluence(self):
        """
        Отказ без разбора факторов бесполезен для последующей проверки: по
        нему нельзя будет понять, ЧТО именно не сошлось.
        """
        text = gr.build([])
        assert 'cf' in gr.rules_of(text)


class TestTheExitPlanIsAlwaysComplete:

    def test_at_least_one_target_is_required(self):
        """
        Пустой список целей — это вход без единого выхода. Через повторение
        целей грамматика была бы короче, но разрешила бы именно это.
        """
        text = gr.build(['L1', 'L2'])
        tps = [l for l in text.splitlines() if l.startswith('tps')][0]
        assert '"[" "]"' not in tps
        assert tps.count('lv') >= 1

    def test_targets_are_capped(self):
        text = gr.build(['L1', 'L2', 'L3', 'L4'])
        tps = [l for l in text.splitlines() if l.startswith('tps')][0]
        longest = max(part.count('lv') for part in tps.split('|'))
        assert longest == gr.MAX_TARGETS

    def test_an_entry_carries_stop_and_invalidation(self):
        text = gr.build(['L1', 'L2'])
        enter = [l for l in text.splitlines() if l.startswith('enter')][0]
        for field in ('entry', 'stop', 'tp', 'inval'):
            assert f'\\"{field}\\"' in enter


class TestTheNumbersAreBounded:

    def test_certainty_is_impossible(self):
        """
        Вероятность ограничена 0.00-0.99 намеренно. Единица говорит не о
        сетапе, а о том, что модель себя не откалибровала.
        """
        text = gr.build(['L1'])
        prob = [l for l in text.splitlines() if l.startswith('prob')][0]
        assert '"1.' not in prob
        assert '"0."' in prob

    def test_the_explanation_is_length_capped(self):
        """
        Без предела модель на медленном процессоре уходит в рассуждение на
        сотни токенов — это минуты счёта ради текста, который не дочитают.
        """
        text = gr.build(['L1'])
        why = [l for l in text.splitlines() if l.startswith('why')][0]
        assert f'{{1,{gr.WHY_CHARS}}}' in why


class TestItMatchesTheContextBuilder:
    """
    Грамматика и разметка обязаны говорить об одних и тех же уровнях.
    Разойдясь, они дадут ответ, который не с чем сопоставить.
    """

    def test_ids_from_the_builder_are_accepted(self):
        import numpy as np
        import pandas as pd

        import llm_context

        idx = np.arange(400)
        closes = 100 + 4 * np.sin(idx / 23 * 2 * np.pi) + idx * 0.004
        df = pd.DataFrame({
            'timestamp': pd.to_datetime(idx * 3_600_000 + 1_700_000_000_000,
                                        unit='ms'),
            'open': closes, 'high': closes + closes * 0.002,
            'low': closes - closes * 0.002, 'close': closes,
            'volume': np.full(400, 100.0),
        })
        out = llm_context.build('BTCUSDT', df)
        ids = [level['id'] for level in out['levels']]
        assert ids, 'разметка не дала ни одного уровня'

        text = gr.build(ids)
        for level_id in ids:
            assert f'"\\"{level_id}\\""' in text
        assert not (gr.referenced(text) - set(gr.rules_of(text)))


class TestTheLongestAnswerStillFits:
    """
    Пределы полей, предел ответа и окно контекста — три числа про одно.

    Разойдясь, они режут ответ на полуслове, и по обрубку не видно, что
    виновато. 19 сентября 2026 так и вышло: окно 2048 при вопросе 1703, и 119
    ответов подряд оборвались, не закрыв JSON.

    Здесь эта связь записана как правило, а не как совпадение настроек.
    """

    # Русский текст в токенайзере Qwen — около 2.5 знаков на токен. Замерено
    # на настоящем ответе: 1400 знаков ушли в 528 токенов.
    CHARS_PER_TOKEN = 2.5

    def worst_answer_tokens(self):
        import llm_grammar as gr
        chars = (gr.REGIME_CHARS + gr.ANALYSIS_CHARS + gr.TRIGGER_CHARS
                 + gr.WHY_CHARS + gr.RISK_CHARS + gr.ALT_CHARS)
        # Плюс сама разметка JSON: имена полей, скобки, идентификаторы целей.
        return chars / self.CHARS_PER_TOKEN + 120

    def test_it_fits_the_answer_limit(self):
        import config
        assert self.worst_answer_tokens() < config.LLM_MAX_TOKENS

    def test_it_fits_the_context_window(self):
        """Вопрос замерен на сервере: около 1700 токенов вместе с разметкой."""
        import config
        assert 1700 + self.worst_answer_tokens() < config.LLM_CTX

    def test_the_short_fields_cannot_ramble_for_minutes(self):
        """
        Поле why на 800 знаков выродилось в двадцать повторов одной мысли и
        стоило двух минут счёта. Каждое поле, кроме разбора, отвечает на один
        вопрос — предложения-двух ему хватает.
        """
        import llm_grammar as gr
        for name in ('REGIME_CHARS', 'TRIGGER_CHARS', 'WHY_CHARS',
                     'RISK_CHARS', 'ALT_CHARS'):
            assert getattr(gr, name) <= 400, name
