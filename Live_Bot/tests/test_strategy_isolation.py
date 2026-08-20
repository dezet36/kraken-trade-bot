"""
Стратегии и их анализ не должны зависеть друг от друга.

ЗАЧЕМ ЭТО ПРОВЕРЯТЬ МАШИНОЙ. Правило «каждая стратегия работает отдельно по
своим параметрам» держалось в проекте только на дисциплине: код ему следовал,
но ничто этого не требовало. Такое правило нарушается не злым умыслом, а одной
строчкой «возьму пока отсюда, потом вынесу» — и обнаруживается через месяцы,
когда правка ради одной стратегии молча сдвинула результаты трёх других.

ЧТО ИМЕННО ЗАПРЕЩЕНО И ПОЧЕМУ

1. Пакет стратегии не импортирует чужой пакет. Иначе пороги перетекают между
   стратегиями, и «независимая проверка на двух периодах» перестаёт быть
   независимой.

2. Пакет стратегии не импортирует config. Там живут параметры Фибоначчи, и
   любое обращение к ним делает чужую стратегию заложником её настроек.

3. Адаптер стратегии импортирует только СВОЙ пакет. Адаптер — единственное
   место, где стратегия встречается с биржей и настройками; смешение здесь
   означает, что в бою торгуется не то, что измерено.

4. У каждой стратегии свой префикс переменных окружения. Общий префикс
   означал бы, что одна переменная меняет поведение двух стратегий сразу — и
   заметить это можно только по разошедшимся результатам.

5. Замеры берут статистику и периоды из research/common.py, а не из файла
   аудита конкретной стратегии. Двадцать один замер брал их из fibo_audit —
   работало исправно, но означало, что правка ради Фибоначчи меняет замеры
   волн, уровней, сетки и Боллинджера.
"""

import ast
import os
import re

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(BOT)
RESEARCH = os.path.join(ROOT, 'research')

# Пакеты стратегий: чистая логика, без биржи и без настроек.
PACKAGES = ('smc', 'levels', 'grid', 'scalp', 'revert', 'wave', 'rsibb')

# Адаптер -> пакет, который ему единственно разрешён.
ADAPTERS = {
    'strategy_smc.py': 'smc',
    'strategy_levels.py': 'levels',
    'strategy_rsibb.py': 'rsibb',
}



def modules_of(path):
    """Имена модулей верхнего уровня, которые импортирует файл."""
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:               # относительный импорт внутри пакета
                continue
            if node.module:
                found.add(node.module.split('.')[0])
    return found


def files_of(package):
    folder = os.path.join(BOT, package)
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, name) for name in sorted(os.listdir(folder))
            if name.endswith('.py')]


class TestPackagesAreSelfContained:
    def test_no_package_imports_another(self):
        others = set(PACKAGES)
        for package in PACKAGES:
            for path in files_of(package):
                foreign = modules_of(path) & (others - {package})
                assert not foreign, (
                    f'{package}/{os.path.basename(path)} импортирует чужую '
                    f'стратегию: {sorted(foreign)}. Пороги не должны '
                    f'перетекать между стратегиями.')

    def test_no_package_reads_config(self):
        """
        В config живут параметры Фибоначчи. Обращение к ним из чужого пакета
        делает эту стратегию заложником её настроек — и замер перестаёт
        отвечать за то, что торгуется в бою.
        """
        for package in PACKAGES:
            for path in files_of(package):
                assert 'config' not in modules_of(path), (
                    f'{package}/{os.path.basename(path)} импортирует config')

    def test_packages_do_not_touch_the_exchange(self):
        """Пакет — чистая логика. Биржа живёт в адаптере, и только там."""
        banned = {'exchange', 'ccxt', 'trade_manager', 'paper_broker'}
        for package in PACKAGES:
            for path in files_of(package):
                touched = modules_of(path) & banned
                assert not touched, (
                    f'{package}/{os.path.basename(path)}: {sorted(touched)}')


class TestAdaptersStayInTheirLane:
    def test_adapter_imports_only_its_own_package(self):
        for name, own in ADAPTERS.items():
            path = os.path.join(BOT, name)
            if not os.path.exists(path):
                continue
            foreign = modules_of(path) & (set(PACKAGES) - {own})
            assert not foreign, (
                f'{name} импортирует {sorted(foreign)} вместо одного лишь '
                f'{own}. Адаптер — место встречи стратегии с биржей; смешение '
                f'здесь означает, что торгуется не то, что измерено.')


class TestEnvPrefixesDoNotCollide:
    """
    Требование здесь — УНИКАЛЬНОСТЬ префикса, а не совпадение его с именем
    пакета. Первая версия этого теста требовала второго и поймала `scalp`,
    который пользуется префиксом BRK_ (от «breakout»). Неправ был тест: имя
    произвольно, важно лишь чтобы одна переменная окружения не управляла
    двумя стратегиями сразу.
    """

    @staticmethod
    def prefix_of(path):
        """Префикс, которым пакет склеивает имена переменных окружения."""
        text = open(path, encoding='utf-8').read()
        # Основная форма: общий читатель, связанный с префиксом пакета.
        # params_env.reader('RSIBB') — одна реализация на все стратегии, см.
        # test_params_env о том, почему своих копий больше нет.
        shared = re.findall(r"params_env\.reader\(['\"]([A-Z][A-Z0-9]*)['\"]\)", text)
        if shared:
            return sorted(set(f'{name}_' for name in shared))
        # Собственный читатель: os.getenv(f'RSIBB_{name}', default)
        glued = re.findall(r"getenv\(f['\"]([A-Z][A-Z0-9]*_)\{", text)
        if glued:
            return sorted(set(glued))
        # Запасная форма: имена перечислены целиком.
        plain = re.findall(r"getenv\(['\"]([A-Z][A-Z0-9]*)_", text)
        return sorted(set(f'{name}_' for name in plain))

    def test_every_package_has_a_prefix(self):
        for package in PACKAGES:
            path = os.path.join(BOT, package, 'params.py')
            if not os.path.exists(path):
                continue
            assert self.prefix_of(path), (
                f'{package}/params.py читает переменные окружения без общего '
                f'префикса — значит их имена могут совпасть с чужими')

    def test_prefixes_are_unique_across_packages(self):
        owner = {}
        for package in PACKAGES:
            path = os.path.join(BOT, package, 'params.py')
            if not os.path.exists(path):
                continue
            for prefix in self.prefix_of(path):
                assert prefix not in owner, (
                    f'{package} и {owner[prefix]} делят префикс {prefix}: '
                    f'одна переменная окружения меняла бы обе стратегии')
                owner[prefix] = package


class TestAnalysisIsIndependent:
    """
    Замеры не должны зависеть от файла аудита ЧУЖОЙ стратегии.

    До этой проверки двадцать один замер брал бутстрап, списки пар и пути к
    кэшу из fibo_audit. Работало исправно — и означало, что правка ради
    Фибоначчи молча меняет замеры всех остальных.
    """

    def test_measurements_take_statistics_from_common(self):
        if not os.path.isdir(RESEARCH):
            return
        offenders = []
        for name in sorted(os.listdir(RESEARCH)):
            if not name.endswith('.py') or name == 'fibo_audit.py':
                continue
            path = os.path.join(RESEARCH, name)
            if 'fibo_audit' in modules_of(path):
                offenders.append(name)
        assert not offenders, (
            f'берут оснастку из аудита Фибоначчи: {offenders}. '
            f'Общее место — research/common.py.')

    def test_common_holds_no_strategy_settings(self):
        """
        В общий модуль нельзя складывать пороги: он немедленно превратился бы
        в то, от чего уходим.
        """
        path = os.path.join(RESEARCH, 'common.py')
        if not os.path.exists(path):
            return
        text = open(path, encoding='utf-8').read()
        banned = ('MIN_RR', 'RISK_PCT', 'STOP_', 'TARGET_', 'MAX_POSITIONS',
                  'ENTRY_', 'MIN_IMPULSE')
        found = [word for word in banned if word in text]
        assert not found, f'в common.py попали настройки стратегий: {found}'

    def test_common_does_not_import_any_strategy(self):
        path = os.path.join(RESEARCH, 'common.py')
        if not os.path.exists(path):
            return
        touched = modules_of(path) & set(PACKAGES)
        assert not touched, f'common.py тянет стратегии: {sorted(touched)}'
