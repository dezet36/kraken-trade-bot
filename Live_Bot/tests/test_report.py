"""
Отчёт для разбора неполадок: главное — что в нём нет ключей.

Файл делается для того, чтобы его ОТПРАВИТЬ. Секрет, попавший внутрь,
уезжает вместе с ним и остаётся в переписке навсегда. Поэтому проверок на
чистку здесь больше, чем на само содержимое.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report  # noqa: E402


def test_env_secret_is_removed(monkeypatch):
    monkeypatch.setenv('BYBIT_API_KEY', 'AbCdEf1234567890XyZ')
    monkeypatch.setenv('BYBIT_SECRET_KEY', 'zZzSecret99887766554433')
    text = ('подключение с ключом AbCdEf1234567890XyZ '
            'и секретом zZzSecret99887766554433')
    out = report.scrub(text)
    assert 'AbCdEf1234567890XyZ' not in out
    assert 'zZzSecret99887766554433' not in out
    assert report.MASK in out


def test_env_secret_removed_even_inside_url(monkeypatch):
    """Ключ, приклеенный к адресу, — самый частый способ его засветить."""
    monkeypatch.setenv('GITHUB_TOKEN', 'abcdefgh12345678ijklmnop')
    out = report.scrub('git clone https://abcdefgh12345678ijklmnop@github.com/x/y')
    assert 'abcdefgh12345678ijklmnop' not in out


def test_overlapping_secrets_do_not_leave_tail(monkeypatch):
    """
    Короткий секрет, оказавшийся куском длинного, не должен разрезать его.

    Если вырезать сначала короткий, от длинного остаётся хвост в открытом
    виде. Поэтому длинные вырезаются первыми — это и проверяется.
    """
    monkeypatch.setenv('A_TOKEN', 'ABCDEFGH')
    monkeypatch.setenv('B_TOKEN', 'ABCDEFGH_XYZ_9876543210')
    out = report.scrub('ключ ABCDEFGH_XYZ_9876543210 в тексте')
    assert 'XYZ_9876543210' not in out


def test_github_token_removed_without_env():
    """Токен из чужого текста вырезается по форме, а не по переменной."""
    for token in ('ghp_' + 'A' * 36,
                  'github_pat_11ABCDEFG0' + 'x' * 40,
                  'gho_' + 'Z' * 36):
        out = report.scrub(f'ошибка авторизации: {token} не подошёл')
        assert token not in out, token
        assert report.MASK in out


def test_telegram_token_removed():
    token = '123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw'
    out = report.scrub(f'sendMessage bot{token}/x')
    assert token not in out


def test_bearer_header_removed():
    out = report.scrub('Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9')
    assert 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9' not in out


def test_key_value_in_text_removed():
    out = report.scrub('запрос: api_key=Qw3rTy7uIoP0aSdFgHjK secret: "L0ngSecretValue123456"')
    assert 'Qw3rTy7uIoP0aSdFgHjK' not in out
    assert 'L0ngSecretValue123456' not in out


def test_long_unknown_token_removed():
    """Неизвестный формат тоже вырезается: длинная мешанина букв и цифр."""
    blob = 'k7Hs92Lm4Qp8Xr3Tv6Yw1Zb5Nc0Df'
    out = report.scrub(f'ответ биржи: {blob}')
    assert blob not in out


def test_useful_identifiers_survive():
    """
    Перестраховка не должна съедать то, ради чего отчёт и собирают.

    Хэш коммита, номер версии и название пары нужны для разбора и секретами
    не являются.
    """
    text = 'версия v1.2.3, коммит 758efb3, пара 1000PEPEUSDT, режим PAPER'
    out = report.scrub(text)
    for keep in ('v1.2.3', '758efb3', '1000PEPEUSDT', 'PAPER'):
        assert keep in out, keep


def test_plain_russian_text_untouched():
    text = ('Не удалось подключиться к бирже: превышено время ожидания. '
            'Повторю через тридцать секунд.')
    assert report.scrub(text) == text


def test_short_env_values_do_not_shred_the_report(monkeypatch):
    """
    Короткое значение секретной переменной не вырезается.

    PAPER_KEY=true встретится в тексте сто раз, и вырезание превратило бы
    отчёт в решето из масок, не добавив безопасности.
    """
    monkeypatch.setenv('SOME_KEY', 'true')
    out = report.scrub('режим true, значение true, ещё раз true')
    assert report.MASK not in out


def test_scrub_failure_never_leaks(monkeypatch):
    """Если чистка сломалась, текст наружу не выходит вообще."""
    def boom():
        raise RuntimeError('сломалось')

    monkeypatch.setattr(report, '_secret_values', boom)
    out = report.scrub('секретнейший ключ AbCdEf1234567890XyZ')
    assert 'AbCdEf1234567890XyZ' not in out
    assert report.MASK in out


def test_build_produces_report_and_is_scrubbed(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('BYBIT_API_KEY', 'SuperSecretKeyValue9911')
    text = report.build(log_lines=20)
    assert 'ОТЧЁТ ДЛЯ РАЗБОРА НЕПОЛАДОК' in text
    assert 'ОШИБКИ' in text and 'НАСТРОЙКИ' in text
    assert 'SuperSecretKeyValue9911' not in text
    # Просьба проверить глазами должна быть в файле, а не только в интерфейсе.
    assert 'глазами' in text


def test_filename_is_txt():
    assert report.filename().endswith('.txt')




def test_scrub_obj_keeps_structure_intact(monkeypatch):
    """
    Чистка структуры не должна её ломать.

    Прогон готового JSON-текста через scrub() ловил в нём пару "key": "abc…"
    и заменял её на key=⟨вырезано⟩ — вместе с кавычками. Разбор падал, и
    вместо списка ошибок приходила пятисотая. Поэтому чистим по строкам.
    """
    monkeypatch.setenv('BYBIT_SECRET_KEY', 'sV9wE3rT5yU7iO1pA2sD4fG6hJ8kL0zX')
    data = {
        'errors': [{
            'category': 'биржа',
            'count': 3,
            'samples': [{
                'text': 'отказ https://sV9wE3rT5yU7iO1pA2sD4fG6hJ8kL0zX@api',
                'context': {'key': 'sV9wE3rT5yU7iO1pA2sD4fG6hJ8kL0zX',
                            'pair': 'BTCUSDT'},
            }],
        }],
        'writable': True,
    }
    out = report.scrub_obj(data)

    # Структура на месте: ключи словаря не тронуты, типы сохранены.
    assert set(out) == {'errors', 'writable'}
    assert out['writable'] is True
    sample = out['errors'][0]['samples'][0]
    assert set(sample['context']) == {'key', 'pair'}
    assert out['errors'][0]['count'] == 3
    assert out['errors'][0]['category'] == 'биржа'
    # А секрет — вырезан, и в тексте, и в контексте.
    assert 'sV9wE3rT5yU7iO1pA2sD4fG6hJ8kL0zX' not in str(out)
    assert sample['context']['pair'] == 'BTCUSDT'

    import json
    json.dumps(out, ensure_ascii=False)          # должно сериализоваться


def test_scrub_obj_leaves_numbers_and_none():
    out = report.scrub_obj({'a': 1, 'b': None, 'c': 2.5, 'd': [1, 'x']})
    assert out == {'a': 1, 'b': None, 'c': 2.5, 'd': [1, 'x']}


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
