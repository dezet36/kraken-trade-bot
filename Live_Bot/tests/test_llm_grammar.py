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
        # Три уровня, а не два: вход, стоп и цель обязаны быть разными, и на
        # двух грамматика честно оставляет только отказ.
        text = gr.build(['L1', 'L2', 'L3'])
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
        text = gr.build(['L1', 'L2', 'L3'])
        lists = [l for l in text.splitlines() if l.startswith('tp-')]
        assert lists, 'правил списка целей нет вовсе'
        for rule in lists:
            assert '"[" ws "]"' not in rule
            assert '"[" "]"' not in rule

    def test_targets_are_capped(self):
        text = gr.build(['L1', 'L2', 'L3', 'L4'])
        for rule in [l for l in text.splitlines() if l.startswith('tp-')]:
            item = rule.split('::=')[0].strip()[3:]     # tp-long-l2 -> long-l2
            longest = max(part.count(f't-{item}') for part in rule.split('|'))
            assert longest == gr.MAX_TARGETS, rule

    def test_an_entry_carries_stop_and_invalidation(self):
        text = gr.build(['L1', 'L2', 'L3'])
        # Поля входа переехали в ветки плана: у каждой пары «направление +
        # уровень входа» свой набор допустимых стопов и целей.
        plans = [l for l in text.splitlines()
                 if l.startswith('long-') or l.startswith('short-')]
        assert plans, 'веток плана нет'
        for rule in plans:
            for field in ('side', 'entry', 'stop', 'tp', 'inval'):
                assert f'\\"{field}\\"' in rule, (field, rule)


class TestTheNumbersAreBounded:

    def test_certainty_is_impossible(self):
        """
        Вероятность ограничена 0.00-0.99 намеренно. Единица говорит не о
        сетапе, а о том, что модель себя не откалибровала.
        """
        text = gr.build(['L1', 'L2', 'L3'])
        prob = [l for l in text.splitlines() if l.startswith('prob')][0]
        assert '"1.' not in prob
        assert '"0."' in prob

    def test_the_explanation_is_length_capped(self):
        """
        Без предела модель на медленном процессоре уходит в рассуждение на
        сотни токенов — это минуты счёта ради текста, который не дочитают.
        """
        text = gr.build(['L1', 'L2', 'L3'])
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


class TestWrongGeometryIsUnsayable:
    """
    Для лонга цель НИЖЕ входа должна быть невозможна, а не отвергнута.

    ОТКУДА ЭТО. 19 сентября 2026, в первый же день, когда модель стала
    отвечать целиком, два вердикта из трёх умерли на предохранителе геометрии:
    для лонга она выбирала цели ниже входа. Текст задачи прямо требует
    обратного — восьмимиллиардная модель этого не держит. Со стороны выходило
    «стратегия работает, но никогда не торгует».

    Приём тот же, которым закрыты выдуманные цены: не просьба в промте, а
    вычёркивание недопустимых вариантов до выбора. Проверка в llm_decide
    остаётся вторым рубежом — на случай вызова без грамматики.

    ПОРЯДОК СПИСКА — ЭТО ЦЕНА: уровни приходят сверху вниз.
    """

    LEVELS = ['L1', 'L2', 'L3', 'L4', 'L5']

    def rule(self, text, name):
        return [l for l in text.splitlines() if l.startswith(name + ' ')][0]

    def test_a_long_can_only_target_levels_above_the_entry(self):
        text = gr.build(self.LEVELS)
        targets = self.rule(text, 't-long-l3')
        for above in ('L1', 'L2'):
            assert f'\\"{above}\\"' in targets, above
        for below in ('L4', 'L5'):
            assert f'\\"{below}\\"' not in targets, below

    def test_a_long_can_only_stop_below_the_entry(self):
        text = gr.build(self.LEVELS)
        stops = self.rule(text, 's-long-l3')
        for below in ('L4', 'L5'):
            assert f'\\"{below}\\"' in stops, below
        for above in ('L1', 'L2'):
            assert f'\\"{above}\\"' not in stops, above

    def test_a_short_is_the_mirror(self):
        text = gr.build(self.LEVELS)
        stops = self.rule(text, 's-short-l3')
        targets = self.rule(text, 't-short-l3')
        assert '\\"L2\\"' in stops and '\\"L4\\"' not in stops
        assert '\\"L4\\"' in targets and '\\"L2\\"' not in targets

    def test_the_invalidation_sits_with_the_stop(self):
        """
        Идея лонга умирает ПОД входом, а не над ним. Уровень инвалидации
        берётся из того же набора, что и стоп.
        """
        text = gr.build(self.LEVELS)
        plan = self.rule(text, 'long-l3')
        assert plan.count('s-long-l3') == 2, plan

    def test_the_topmost_level_has_no_long_branch(self):
        """
        Лонгу от самого верхнего уровня целиться некуда. Ветки быть не должно
        — иначе грамматика разрешит вход без единой цели.
        """
        text = gr.build(self.LEVELS)
        assert 'long-l1 ::=' not in text
        assert 'short-l5 ::=' not in text

    def test_two_levels_leave_only_a_refusal(self):
        """
        Вход, стоп и цель обязаны быть РАЗНЫМИ уровнями. На двух сделка не
        выражается, и грамматика честно оставляет только отказ.
        """
        text = gr.build(['L1', 'L2'])
        assert '\\"enter\\"' not in text
        assert '\\"skip\\"' in text

    def test_all_branches_of_a_full_setup_resolve(self):
        """Двенадцать уровней — это сорок веток; висячая ссылка убьёт вызов."""
        text = gr.build([f'L{i}' for i in range(1, 13)])
        assert not (gr.referenced(text) - set(gr.rules_of(text)))
        plans = self.rule(text, 'plan')
        assert plans.count('|') == 19, plans   # 10 лонгов + 10 шортов


class TestRuleNamesAreParsable:
    """
    ИМЕНА ПРАВИЛ — ТОЛЬКО БУКВЫ, ЦИФРЫ И ДЕФИС, и узналось это дорого.

    Первая версия геометрических веток называла правила через подчёркивание —
    long_l2, s_long_l2. Все проверки прошли: они читают текст грамматики сами
    и о разборщике llama.cpp ничего не знают. На сервере же он читает имя до
    первого недопустимого знака и падает: «parse: error parsing grammar:
    expecting newline or end at _l2 | long_l3 | ...».

    Поймалось только в журнале службы, на живом боте. Поэтому набор знаков
    теперь проверяется здесь — это единственное, что можно проверить без самой
    llama.cpp, которой на машине разработки нет.
    """

    def test_no_rule_name_has_an_underscore(self):
        import re
        for count in (0, 3, 5, 12):
            text = gr.build([f'L{i}' for i in range(1, count + 1)])
            for name in gr.rules_of(text):
                assert re.fullmatch(r'[A-Za-z0-9-]+', name), name

    def test_references_match_the_same_charset(self):
        """Ссылка читается тем же разборщиком, что и определение."""
        import re
        text = gr.build([f'L{i}' for i in range(1, 8)])
        for name in gr.referenced(text):
            assert re.fullmatch(r'[A-Za-z0-9-]+', name), name


class TestTheTemplateDoesNotEatBraces:
    """
    Шаблон грамматики — f-строка, и одинарные фигурные скобки в ней —
    подстановка, а не текст. 19 сентября 2026 ограничение пробелов
    `{0,2}` ушло к llama.cpp как `(0, 2)`: парсер падал с «expecting ')'»,
    падение роняло процесс, и бот перезапускался на каждом разборе — с
    панели это выглядело как «нет связи с ботом».
    """

    @pytest.mark.parametrize('text', [
        gr.build(['L1', 'L2', 'L3', 'L4']), gr.build([]), gr.critic()])
    def test_repetitions_survive_formatting(self, text):
        import re
        assert '(0, 2)' not in text and '(1, ' not in text
        ws = [l for l in text.splitlines() if l.startswith('ws')]
        assert ws and ws[0].endswith('[ ' + chr(92) + 'n]{0,2}'), ws
        # Каждое повторение (после `]` или имени правила) — вида {m,n} с
        # числами. Фигурные скобки самого JSON здесь ни при чём: они стоят
        # внутри кавычек.
        for m in re.finditer(r'(?<=[\]A-Za-z0-9])\{[^}"]*\}', text):
            assert re.fullmatch(r'\{\d+,\d+\}', m.group()), m.group()


class TestTheRealParserAcceptsIt:
    """
    Единственная настоящая проверка грамматики — парсер llama.cpp. На машине
    разработки его нет, и проверка пропускается; на сервере она обязана
    проходить перед выкаткой: 19 сентября 2026 форма `(0, 2)` вместо `{0,2}`
    прошла все текстовые проверки и роняла процесс на каждом разборе.
    """

    @pytest.mark.parametrize('count', [0, 1, 2, 3, 5, 8, 12])
    def test_every_level_count_parses(self, count):
        llama = pytest.importorskip('llama_cpp')
        text = gr.build([f'L{i}' for i in range(1, count + 1)])
        llama.LlamaGrammar.from_string(text, verbose=False)

    def test_the_critic_grammar_parses(self):
        llama = pytest.importorskip('llama_cpp')
        llama.LlamaGrammar.from_string(gr.critic(), verbose=False)
