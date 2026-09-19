"""
Крошечный разборщик GBNF — ровно того подмножества, что пишет llm_grammar.

ЗАЧЕМ. Настоящий парсер грамматики — llama.cpp, и на машине разработки его
нет. Проверки сверяли имена правил и форму повторений, но не то, ПРИНИМАЕТ
ли грамматика ответы, которые от неё ждут, и ОТВЕРГАЕТ ли те, что не должна.
Этот модуль отвечает на оба вопроса без модели: разбирает текст грамматики
и матчит строку по правилам с откатом.

ЧТО ПОДДЕРЖИВАЕТСЯ: правила `name ::= ...`, альтернативы `|`,
последовательности, строки в кавычках с экранированием (\\" \\\\ \\n \\xHH),
классы символов `[...]` с диапазонами и отрицанием, ссылки на правила,
повторения `{m,n}`, `*`, `+`, `?` после класса, строки или ссылки. Скобок
llm_grammar не пишет — и разборщик их не знает намеренно: появятся — упадёт
здесь, а не на сервере.
"""

import re


class Grammar:
    def __init__(self, text):
        self.rules = {}
        for line in text.splitlines():
            if '::=' not in line:
                continue
            name, body = line.split('::=', 1)
            self.rules[name.strip()] = self._alternatives(body.strip())
        if 'root' not in self.rules:
            raise ValueError('нет правила root')

    # ── разбор текста грамматики ─────────────────────────────────────────

    def _alternatives(self, body):
        alts, depth_free = [], []
        cur = []
        i = 0
        while i < len(body):
            c = body[i]
            if c == '"':
                j = i + 1
                buf = []
                while j < len(body) and body[j] != '"':
                    if body[j] == '\\':
                        j += 1
                        e = body[j]
                        if e == 'n':
                            buf.append('\n')
                        elif e == 'x':
                            buf.append(chr(int(body[j + 1:j + 3], 16)))
                            j += 2
                        else:
                            buf.append(e)
                    else:
                        buf.append(body[j])
                    j += 1
                cur.append(('lit', ''.join(buf)))
                i = j + 1
            elif c == '[':
                j = i + 1
                negate = False
                if body[j] == '^':
                    negate = True
                    j += 1
                items = []
                while body[j] != ']':
                    ch = body[j]
                    if ch == '\\':
                        j += 1
                        e = body[j]
                        if e == 'n':
                            ch = '\n'
                        elif e == 'x':
                            ch = chr(int(body[j + 1:j + 3], 16))
                            j += 2
                        else:
                            ch = e
                    j += 1
                    if body[j] == '-' and body[j + 1] != ']':
                        j += 1
                        hi = body[j]
                        if hi == '\\':
                            j += 1
                            e = body[j]
                            if e == 'x':
                                hi = chr(int(body[j + 1:j + 3], 16))
                                j += 2
                            elif e == 'n':
                                hi = '\n'
                            else:
                                hi = e
                        j += 1
                        items.append((ch, hi))
                    else:
                        items.append((ch, ch))
                cur.append(('cls', negate, items))
                i = j + 1
            elif c == '|':
                alts.append(cur)
                cur = []
                i += 1
            elif c == '{':
                j = body.index('}', i)
                m, n = body[i + 1:j].split(',')
                cur[-1] = ('rep', cur[-1], int(m), int(n))
                i = j + 1
            elif c in '*+?':
                m, n = {'*': (0, None), '+': (1, None), '?': (0, 1)}[c]
                cur[-1] = ('rep', cur[-1], m, n)
                i += 1
            elif c.isspace():
                i += 1
            else:
                j = i
                while j < len(body) and (body[j].isalnum() or body[j] in '-_'):
                    j += 1
                if j == i:
                    raise ValueError(f'не разобрано: {body[i:i + 20]!r}')
                cur.append(('ref', body[i:j]))
                i = j
        alts.append(cur)
        return alts

    # ── матчинг ─────────────────────────────────────────────────────────

    def accepts(self, text):
        return any(end == len(text) for end in self._match_rule('root', text, 0))

    def _match_rule(self, name, text, pos):
        if name not in self.rules:
            raise ValueError(f'ссылка на неизвестное правило {name}')
        for alt in self.rules[name]:
            yield from self._match_seq(alt, 0, text, pos)

    def _match_seq(self, seq, idx, text, pos):
        if idx == len(seq):
            yield pos
            return
        for end in self._match_item(seq[idx], text, pos):
            yield from self._match_seq(seq, idx + 1, text, end)

    def _match_item(self, item, text, pos):
        kind = item[0]
        if kind == 'lit':
            lit = item[1]
            if text.startswith(lit, pos):
                yield pos + len(lit)
        elif kind == 'cls':
            if pos < len(text) and self._in_class(item, text[pos]):
                yield pos + 1
        elif kind == 'ref':
            yield from self._match_rule(item[1], text, pos)
        elif kind == 'rep':
            _, inner, lo, hi = item
            # Жадно: сначала самое длинное, откат к более коротким.
            ends = [pos]
            cur = pos
            count = 0
            while hi is None or count < hi:
                nxt = None
                for e in self._match_item(inner, text, cur):
                    nxt = e
                    break
                if nxt is None or nxt == cur:
                    break
                cur = nxt
                count += 1
                ends.append(cur)
            for k in range(len(ends) - 1, lo - 1, -1):
                yield ends[k]

    @staticmethod
    def _in_class(item, ch):
        _, negate, items = item
        hit = any(lo <= ch <= hi for lo, hi in items)
        return (not hit) if negate else hit
