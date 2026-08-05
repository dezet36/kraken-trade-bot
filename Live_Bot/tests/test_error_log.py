"""
Проверка журнала ошибок.

Главное здесь — группировка и невозможность уронить бота. Журнал ошибок,
который сам бросает исключение, превращает мелкий сбой в остановку
торговли; поэтому все его операции обёрнуты, и это проверяется отдельно.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def errors(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    for module in ('error_log', 'logger'):
        sys.modules.pop(module, None)
    import error_log
    error_log.ERRORS_FILE = str(tmp_path / 'errors.json')
    error_log._groups = {}
    return error_log


class TestГруппировка:
    def test_odinakovye_s_raznymi_chislami_odna_zapis(self, errors):
        """
        Недоступная на десять минут биржа даёт сотни одинаковых строк. Без
        схлопывания журнал ошибок станет таким же нечитаемым, как обычный лог.
        """
        errors.record('Сетевая ошибка (BTCUSDT 1h): timeout 30s')
        errors.record('Сетевая ошибка (ETHUSDT 4h): timeout 5s')
        errors.record('Сетевая ошибка (SOLUSDT 1h): timeout 12s')
        groups = errors.snapshot()
        assert len(groups) == 1
        assert groups[0]['count'] == 3

    def test_raznye_oshibki_ne_sklеivayutsya(self, errors):
        errors.record('Сетевая ошибка: timeout')
        errors.record('Не удалось сохранить настройки')
        assert len(errors.snapshot()) == 2

    def test_schetchik_i_vremya_obnovlyayutsya(self, errors):
        errors.record('Ошибка X')
        first = errors.snapshot()[0]['first']
        errors.record('Ошибка X')
        item = errors.snapshot()[0]
        assert item['count'] == 2
        assert item['first'] == first        # первое появление не меняется
        assert item['last'] >= first


class TestКатегории:
    @pytest.mark.parametrize('message,expected', [
        ('Сетевая ошибка (BTCUSDT): timeout 30s', 'сеть'),
        ('bybit retCode 10003 API key is invalid', 'биржа'),
        ('Не удалось сохранить настройки: [Errno 13] Permission denied', 'данные'),
        ('BTCUSDT: ошибка SMC-сканирования', 'стратегия'),
        ('Совершенно непонятное сообщение', 'прочее'),
    ])
    def test_klassifikaciya(self, errors, message, expected):
        assert errors._classify(message) == expected

    def test_otkaz_v_pravah_na_fayl_ne_birzhevaya_oshibka(self, errors):
        """
        Слово permission встречается и в правах на файл, и в правах ключа
        биржи. Лечатся они по-разному, поэтому порядок проверки категорий
        важен и закреплён тестом.
        """
        errors.record('Не удалось сохранить: [Errno 13] Permission denied')
        assert errors.snapshot()[0]['category'] == 'данные'


class TestНадёжность:
    def test_zapis_bez_fayla_ne_padaet(self, errors, monkeypatch):
        """
        Каталог данных может стать недоступным на запись. Журнал ошибок
        обязан это пережить молча: иначе мелкий сбой останавливает торговлю.
        """
        monkeypatch.setattr(errors, 'ERRORS_FILE', '/несуществующий/путь/errors.json')
        errors.record('Что-то сломалось')        # не должно бросить

    def test_pustoe_soobshchenie_ignoriruetsya(self, errors):
        errors.record('')
        errors.record('   ')
        assert errors.snapshot() == []

    def test_ne_rastyot_beskonechno(self, errors):
        monkey = errors.MAX_GROUPS
        for i in range(monkey + 20):
            errors.record(f'Уникальная ошибка номер {i} типа {chr(65 + i % 26)}!')
        assert len(errors.snapshot(10_000)) <= monkey

    def test_ochistka(self, errors):
        errors.record('Ошибка')
        assert errors.snapshot()
        assert errors.clear() is True
        assert errors.snapshot() == []


class TestСборИзЛога:
    def test_znachki_popadayut_v_zhurnal(self, errors):
        import logger
        errors.install()
        logger.log('⚠️ Сетевая ошибка: timeout')
        logger.log('❌ Ошибка загрузки свечей')
        assert len(errors.snapshot()) == 2

    def test_obychnye_stroki_ne_popadayut(self, errors):
        import logger
        errors.install()
        logger.log('Цикл завершён, открыто 2 позиции')
        logger.log('SMC: сетапов найдено 0')
        assert errors.snapshot() == []

    def test_uroven_error_popadaet_bez_znachka(self, errors):
        import logger
        errors.install()
        logger.log('Что-то пошло не так', level='ERROR')
        assert len(errors.snapshot()) == 1

    def test_sboy_sbora_ne_ronyaet_log(self, errors, monkeypatch):
        """
        Обработчик — сторонний код в горячем пути логирования. Его падение
        не имеет права ронять ни лог, ни бота.
        """
        import logger

        def broken(message, level):
            raise RuntimeError('обработчик сломан')

        logger.set_error_hook(broken)
        logger.log('⚠️ Обычная ошибка')      # не должно бросить
        logger.set_error_hook(None)


class TestТрассировка:
    def test_sohranyaetsya_pri_peredache_isklyucheniya(self, errors):
        try:
            raise ValueError('деление зоны на ноль')
        except ValueError as exc:
            errors.record('Ошибка построения структуры', exc=exc, pair='BTCUSDT')
        sample = errors.snapshot()[0]['samples'][0]
        assert 'ValueError' in sample['traceback']
        assert sample['context']['pair'] == 'BTCUSDT'

    def test_hranyatsya_neskolko_poslednih_sluchaev(self, errors):
        for i in range(10):
            errors.record(f'Ошибка с деталью {i}')
        samples = errors.snapshot()[0]['samples']
        assert 1 <= len(samples) <= errors.MAX_SAMPLES
