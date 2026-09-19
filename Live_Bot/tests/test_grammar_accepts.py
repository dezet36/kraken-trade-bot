"""
Грамматика ПРИНИМАЕТ ответы, которых от неё ждут, и ОТВЕРГАЕТ те, что не
должна — проверено разборщиком GBNF без модели, на десятках образцов.

До этого файла проверки сверяли имена правил и форму повторений. Ошибка
`(0, 2)` вместо `{0,2}` прошла бы и их — и проходила, пока не упала на
сервере. Здесь грамматика применяется к строкам, и ошибка такого рода
ломает разбор уже тут.
"""

import json
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm_grammar as gr
from gbnf_check import Grammar

RNG = random.Random(20260919)

PRICES = {4: [110.0, 105.0, 100.0, 94.0],
          6: [120.0, 112.0, 106.0, 100.0, 95.0, 90.0],
          9: [130.0, 124.0, 118.0, 112.0, 106.0, 100.0, 95.0, 90.0, 85.0],
          12: [140.0, 134.0, 128.0, 122.0, 116.0, 110.0, 104.0, 100.0, 95.0, 90.0, 85.0, 80.0]}


def ids(n):
    return [f'L{i}' for i in range(1, n + 1)]


def grammar(n, min_stop=1.5, min_rr=2.0):
    text = gr.build(ids(n), prices=PRICES[n], min_stop_pct=min_stop, min_rr=min_rr)
    return Grammar(text), text


def words(n_chars):
    """Русский текст заданной длины без кавычек и управляющих символов."""
    pool = 'цена растёт дельта отрицательная стоп за уровнем цель на скоплении '
    out = ''
    while len(out) < n_chars:
        out += pool
    return out[:n_chars].strip()


def skip_answer(n, bias='up', analysis_len=300):
    return json.dumps({'regime': 'тренд вверх, по сломам', 'analysis': words(analysis_len),
                       'bias': bias, 'd': 'skip',
                       'cf': {'poi': True, 'vp': False, 'der': True, 'smc': False, 'flow': False},
                       'why': 'нет уровня за что'}, ensure_ascii=False, separators=(',', ':'))


def enter_answer(n, side='LONG', entry='L3', stop='L4', tp=('L1',), inval=None,
                 when='now', level=None, bias='up', p='0.62', trig_note='возврат в уровень'):
    trigger = {'when': when, 'note': trig_note}
    if when != 'now':
        trigger = {'when': when, 'level': level, 'note': trig_note}
    body = {'regime': 'тренд вверх', 'analysis': words(400), 'bias': bias, 'd': 'enter',
            'side': side, 'entry': entry, 'stop': stop, 'tp': list(tp), 'inval': inval or stop,
            'trigger': trigger,
            'cf': {'poi': True, 'vp': True, 'der': True, 'smc': True, 'flow': False},
            'p': float(p), 'why': 'за скоплением, к экстремуму дня', 'risk': 'слив ОИ',
            'alt': 'пробой вниз отменяет идею'}
    # Компактно, как пишет модель под грамматикой: у "d":"skip" пробела нет.
    text = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
    # json пишет 0.9 как 0.9 — грамматика требует ровно две цифры
    return text.replace(f'"p":{float(p)}', f'"p":{p}')


class TestValidAnswersAreAccepted:

    @pytest.mark.parametrize('n', [4, 6, 9, 12])
    def test_skips_with_every_bias(self, n):
        g, _ = grammar(n)
        for bias in ('up', 'down', 'flat'):
            assert g.accepts(skip_answer(n, bias)), (n, bias)

    @pytest.mark.parametrize('n', [4, 6, 9, 12])
    def test_a_sample_of_geometrically_sound_entries(self, n):
        """
        Пятьдесят с лишним случайных планов: вход, стоп по свою сторону
        дальше 1.5%, цели по другую сторону дальше 3% — все приняты.
        """
        g, _ = grammar(n)
        prices = PRICES[n]
        levels = ids(n)
        accepted = 0
        for _ in range(240):
            side = RNG.choice(['LONG', 'SHORT'])
            e = RNG.randrange(n)
            pe = prices[e]
            if side == 'LONG':
                stops = [i for i in range(e + 1, n) if (pe - prices[i]) / pe * 100 >= 1.5]
            else:
                stops = [i for i in range(0, e) if (prices[i] - pe) / pe * 100 >= 1.5]
            if not stops:
                continue
            s = RNG.choice(stops)
            need = 2.0 * abs(pe - prices[s]) / pe * 100        # R:R от ЭТОГО стопа
            if side == 'LONG':
                tps = [i for i in range(0, e) if (prices[i] - pe) / pe * 100 >= need]
            else:
                tps = [i for i in range(e + 1, n) if (pe - prices[i]) / pe * 100 >= need]
            if not tps:
                continue
            targets = RNG.sample(tps, k=min(len(tps), RNG.randint(1, 3)))
            when = RNG.choice(['now', 'close_above', 'close_below'])
            level = RNG.choice(levels)
            text = enter_answer(n, side, levels[e], levels[s], tuple(levels[t] for t in targets),
                                when=when, level=level, bias=RNG.choice(['up', 'down', 'flat']),
                                p=RNG.choice(['0.51', '0.62', '0.75', '0.90']))
            assert g.accepts(text), text[:200]
            accepted += 1
        # У четырёх уровней сочетаний мало — часть выборки пуста законно.
        assert accepted >= (10 if n == 4 else 25), f'принято всего {accepted}'

    def test_fields_at_their_length_limits(self):
        g, _ = grammar(6)
        text = json.dumps({'regime': words(gr.REGIME_CHARS), 'analysis': words(gr.ANALYSIS_CHARS),
                           'bias': 'flat', 'd': 'skip',
                           'cf': {'poi': False, 'vp': False, 'der': False, 'smc': False, 'flow': False},
                           'why': words(gr.WHY_CHARS)}, ensure_ascii=False, separators=(',', ':'))
        assert g.accepts(text)

    def test_the_critic_answer(self):
        g = Grammar(gr.critic())
        assert g.accepts('{"verdict":"reject","issues":"стоп под пулом","worst":"снимут"}')
        assert g.accepts('{"verdict": "confirm", "issues": "нет", "worst": "нет"}')
        assert not g.accepts('{"verdict":"maybe","issues":"x","worst":"y"}')


class TestInvalidAnswersAreRejected:

    def test_a_price_outside_the_list(self):
        g, _ = grammar(6)
        assert not g.accepts(enter_answer(6, 'LONG', 'L9', 'L5', ('L1',)))

    def test_stop_on_the_wrong_side(self):
        g, _ = grammar(6)
        assert not g.accepts(enter_answer(6, 'LONG', 'L3', 'L2', ('L1',)))
        assert not g.accepts(enter_answer(6, 'SHORT', 'L3', 'L4', ('L6',)))

    def test_target_on_the_wrong_side(self):
        g, _ = grammar(6)
        assert not g.accepts(enter_answer(6, 'LONG', 'L3', 'L5', ('L6',)))

    def test_stop_closer_than_the_minimum(self):
        g, _ = grammar(6)
        # L3 106 → L4 100: 5.7% — годится; но при пределе 6% — нет.
        strict, _ = grammar(6, min_stop=6.0)
        assert g.accepts(enter_answer(6, 'LONG', 'L3', 'L4', ('L1',)))
        assert not strict.accepts(enter_answer(6, 'LONG', 'L3', 'L4', ('L1',)))

    def test_target_closer_than_rr_times_the_stop(self):
        # Лонг L4 100, стоп L5 95 (5%): при R:R 2 цель должна быть дальше 10% —
        # L3 106 (6%) не годится, L1 120 (20%) годится. Это и был случай ETH
        # 19.09: стоп 4.7%, цель 4%, «R:R 0.86».
        g, _ = grammar(6)
        assert not g.accepts(enter_answer(6, 'LONG', 'L4', 'L5', ('L3',)))
        assert g.accepts(enter_answer(6, 'LONG', 'L4', 'L5', ('L1',)))

    def test_unknown_trigger_and_bias(self):
        g, _ = grammar(6)
        assert not g.accepts(enter_answer(6, when='touch', level='L3'))
        assert not g.accepts(skip_answer(6, bias='sideways'))

    def test_text_over_the_limit(self):
        g, _ = grammar(6)
        assert not g.accepts(skip_answer(6, analysis_len=gr.ANALYSIS_CHARS + 50))

    def test_certainty_is_impossible(self):
        g, _ = grammar(6)
        assert not g.accepts(enter_answer(6, p='1.00'))
        assert not g.accepts(enter_answer(6, p='0.00'))

    def test_the_bug_of_the_day_is_caught(self):
        """`(0, 2)` вместо `{0,2}` — разборщик такого не знает и падает."""
        text = gr.build(ids(4)).replace('{0,2}', '(0, 2)')
        with pytest.raises(ValueError):
            Grammar(text)
