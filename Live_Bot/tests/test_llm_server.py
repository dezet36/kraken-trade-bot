"""
Модель в llama-server: HTTP-клиент отвечает тем же, чем рабочий процесс —
текстом и статистикой, а поломки называет теми же именами.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import llm_local  # noqa: E402
import llm_server  # noqa: E402


class FakeLlamaServer(BaseHTTPRequestHandler):
    answer = '{"d":"skip"}'
    status = 200
    delay = 0.0
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == '/health':
            self._json({'status': 'ok'})
        elif self.path == '/props':
            self._json({'model_path': '/models/Qwen3.6-MTP.gguf', 'default_generation_settings': {'n_ctx': 12288}})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        import time
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/tokenize':
            # один токен на символ — границы совпадают всегда
            self._json({'tokens': [ord(ch) for ch in body['content']]}); return
        FakeLlamaServer.seen.append(body)
        if body.get('n_predict') == 0:
            self._json({'content': '', 'tokens_evaluated': len(body['prompt']), 'tokens_predicted': 0}); return
        time.sleep(FakeLlamaServer.delay)
        if FakeLlamaServer.status != 200:
            self.send_response(FakeLlamaServer.status); self.end_headers(); self.wfile.write(b'boom'); return
        if body.get('return_tokens'):
            # Фаза мысли: без грамматики, стоп по «</think>», токены обратно.
            assert 'grammar' not in body and '</think>' in body['stop']
            thought = FakeLlamaServer.thought
            self._json({'content': thought, 'tokens': [ord(ch) for ch in thought],
                        'stopping_word': '</think>' if FakeLlamaServer.thought_closes else '',
                        'stop_type': 'word' if FakeLlamaServer.thought_closes else 'limit',
                        'timings': {'predicted_n': len(thought)}}); return
        # Как у настоящего llama-server: tokens_evaluated — весь вопрос,
        # посчитано заново — timings.prompt_n, tokens_cached — кэш после ответа.
        self._json({'content': FakeLlamaServer.answer, 'tokens_evaluated': len(body['prompt']),
                    'tokens_predicted': 9, 'tokens_cached': len(body['prompt']) + 9,
                    'truncated': False, 'stop_type': 'eos', 'n_ctx': 12288,
                    'timings': {'prompt_n': 20, 'predicted_n': 9, 'predicted_per_second': 3.1,
                                'draft_n': 10, 'draft_n_accepted': 7}})

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)


@pytest.fixture()
def server(monkeypatch):
    srv = HTTPServer(('127.0.0.1', 0), FakeLlamaServer)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    monkeypatch.setattr(config, 'LLM_SERVER_URL', f'http://127.0.0.1:{srv.server_port}')
    monkeypatch.setattr(config, 'LLM_THINK_TAG', '')
    FakeLlamaServer.seen = []; FakeLlamaServer.status = 200; FakeLlamaServer.delay = 0.0
    FakeLlamaServer.thought = 'the leg is up, price in premium'; FakeLlamaServer.thought_closes = True
    yield srv
    srv.shutdown()


def test_the_question_goes_as_chatml_with_grammar(server):
    answer, stats = llm_server.ask('ВОПРОС', grammar='root ::= "x"', max_tokens=50)
    assert answer == '{"d":"skip"}'
    body = FakeLlamaServer.seen[-1]
    sent = ''.join(chr(t) for t in body['prompt'])        # поддельный /tokenize: символ = токен
    assert sent.startswith('<|im_start|>user' + chr(10) + 'ВОПРОС<|im_end|>') and sent.endswith('assistant' + chr(10))
    assert body['grammar'] == 'root ::= "x"' and body['n_predict'] == 50 and body['cache_prompt'] is True
    assert stats['answer_tokens'] == 9 and stats['finish'] == 'stop'
    assert stats['prompt_tokens'] == len(body['prompt']) and stats['cached_tokens'] == len(body['prompt']) - 20
    assert stats['draft_accepted'] == 7 and stats['tok_s'] == 3.1


def test_the_thought_is_its_own_phase_with_a_token_limit(server, monkeypatch):
    """
    21.09.2026: предел мысли в знаках стоял в грамматике, а llama-server с
    MTP-черновиком его не считал — ETH и LTC ушли в мысль на все 3000 токенов,
    JSON не случился. Теперь мысль — отдельная фаза без грамматики с пределом
    в токенах и стоп-словом «</think>»; ответ — вторая фаза по грамматике,
    подсказка продолжена мыслью. Журнал видит один текст, как раньше.
    """
    import llm_grammar
    import llm_prompt
    monkeypatch.setattr(config, 'LLM_THINK_TOKENS', 700)
    grammar = llm_grammar.with_thinking('root     ::= "{" "}"', 1800)
    answer, stats = llm_server.ask('ВОПРОС', grammar=grammar, max_tokens=900)
    phases = [b for b in FakeLlamaServer.seen if b.get('n_predict', 0) > 0]
    assert len(phases) == 2, 'мысль и ответ — два запроса'
    think, reply = phases
    sent = ''.join(chr(t) for t in think['prompt'])
    assert sent.endswith('assistant' + chr(10) + '<think>' + chr(10) + llm_prompt.THINK_SEED)
    assert 'grammar' not in think and think['n_predict'] == 700 and '</think>' in think['stop']
    sent2 = ''.join(chr(t) for t in reply['prompt'])
    assert sent2.endswith(llm_prompt.THINK_SEED + FakeLlamaServer.thought + '</think>' + chr(10) + chr(10)),         'ответ продолжает подсказку мыслью и закрытым тегом'
    assert reply['grammar'].startswith('root     ::= answer') and '"<think>"' not in reply['grammar']
    assert reply['n_predict'] == 900 - len(FakeLlamaServer.thought)
    assert answer.startswith('<think>' + chr(10) + llm_prompt.THINK_SEED + FakeLlamaServer.thought + '</think>')
    assert answer.endswith('{"d":"skip"}')
    assert stats['thought_tokens'] == len(FakeLlamaServer.thought)
    assert stats['answer_tokens'] == 9 + len(FakeLlamaServer.thought)


def test_a_thought_cut_by_the_limit_still_gets_an_answer(server, monkeypatch):
    """Мысль не закрылась сама — закрываем здесь; JSON обязан прийти."""
    import llm_grammar
    monkeypatch.setattr(config, 'LLM_THINK_TOKENS', 700)
    FakeLlamaServer.thought = 'x' * 700
    FakeLlamaServer.thought_closes = False
    grammar = llm_grammar.with_thinking('root     ::= "{" "}"', 1800)
    answer, stats = llm_server.ask('ВОПРОС', grammar=grammar, max_tokens=3000)
    assert answer.endswith('{"d":"skip"}') and '</think>' in answer
    assert stats['finish'] == 'stop'


def test_without_a_thinking_rule_the_prompt_is_untouched(server):
    llm_server.ask('ВОПРОС', grammar='root ::= "x"', max_tokens=5)
    sent = ''.join(chr(t) for t in FakeLlamaServer.seen[-1]['prompt'])
    assert sent.endswith('assistant' + chr(10))


def test_llm_local_routes_to_the_server_and_reports_it(server):
    assert llm_local.available() is True
    out = llm_local.ask('ВОПРОС', grammar=None, max_tokens=10)
    assert out == '{"d":"skip"}'
    last = llm_local.last_stats()
    assert last['model'] == 'Qwen3.6-MTP.gguf' and last['answer_tokens'] == 9


def test_a_server_error_is_named_a_crash(server):
    FakeLlamaServer.status = 500
    with pytest.raises(RuntimeError) as err:
        llm_server.ask('x')
    assert err.value.llm_gate == 'модель упала'


def test_no_server_is_named_unavailable(monkeypatch):
    monkeypatch.setattr(config, 'LLM_SERVER_URL', 'http://127.0.0.1:1')
    assert llm_local.available() is False
    with pytest.raises(RuntimeError) as err:
        llm_server.ask('x')
    assert err.value.llm_gate == 'модель недоступна'


def test_a_slow_server_is_named_hung(server):
    FakeLlamaServer.delay = 1.5
    with pytest.raises(RuntimeError) as err:
        llm_server.ask('x', timeout=1)
    assert err.value.llm_gate == 'модель зависла'


def test_the_prefix_is_warmed_before_the_question(server):
    import llm_prompt
    full = 'СИСТЕМА ' * 40 + llm_server.PREFIX_SEP + 'Пара: XRPUSDT'
    llm_server.ask(full, max_tokens=5)
    warm, question = FakeLlamaServer.seen[-2], FakeLlamaServer.seen[-1]
    assert warm['n_predict'] == 0
    assert question['prompt'][:len(warm['prompt'])] == warm['prompt'], 'прогрев — точное начало вопроса'
    assert ''.join(chr(t) for t in warm['prompt']).endswith(llm_server.PREFIX_SEP)
