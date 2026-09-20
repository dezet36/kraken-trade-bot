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
        self._json({'content': FakeLlamaServer.answer, 'tokens_evaluated': 20, 'tokens_predicted': 9,
                    'tokens_cached': 3000, 'truncated': False, 'stop_type': 'eos', 'n_ctx': 12288,
                    'timings': {'predicted_per_second': 3.1, 'draft_n': 10, 'draft_n_accepted': 7}})

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
