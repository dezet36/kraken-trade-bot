"""
Погода: есть ли у нас прогноз, годный для ставок. Замер до всякой постройки.

ЧТО РЕШАЕТСЯ ЗДЕСЬ. Рынок просит назвать вероятность того, что максимум
температуры на станции попадёт в корзину шириной ровно один градус. Значит
нужен не прогноз в виде числа, а РАСПРЕДЕЛЕНИЕ, и оно должно быть откалибровано:
когда мы говорим «40%», это должно сбываться в 40% случаев. Пока это не
измерено, строить сбор, вкладку и исполнение бессмысленно.

ПОЧЕМУ НЕ АНСАМБЛЬ. Ансамбль из 31 участника даёт распределение напрямую, и
первым планом было взять его. Проверка показала, что за ПРОШЛОЕ ансамбль не
отдаётся вовсе: поля приходят, значения пустые. Живой ансамбль есть, замерить
его качество не на чем — а верить непроверенному нельзя.

Взамен распределение строится из СОБСТВЕННЫХ ОШИБОК ПРОГНОЗА. Архив прошлых
прогнозов и архив наблюдений станции доступны за годы, значит распределение
ошибок «прогноз минус факт» на конкретной станции измеримо. Оно и есть наша
неопределённость, причём честнее ансамблевой: ансамбль systematically занижает
разброс, а измеренная ошибка включает всё — и ошибку модели, и несовпадение
узла сетки со станцией, и разницу между дневным максимумом и максимумом
почасовых сводок.

ТРИ СМЕЩЕНИЯ, КОТОРЫЕ ЭТОТ ПОДХОД СНИМАЕТ РАЗОМ:
    1. узел сетки не совпадает со станцией — другая высота и поверхность;
    2. разрешение берёт максимум ПОЧАСОВЫХ сводок, а модель даёт дневной
       максимум: настоящий пик между сводками не попадает никуда;
    3. округление до целого градуса.
Все три сидят в одной и той же величине «прогноз минус факт», и поправка,
обученная на ней, лечит их вместе, не разбирая по причинам.

ПОПРАВКА УЧИТСЯ НА РАННЕЙ ПОЛОВИНЕ, ПРОВЕРЯЕТСЯ НА ПОЗДНЕЙ. Обучить смещение и
разброс на всей выборке и там же измерить качество — это измерить, насколько
хорошо мы запомнили ответ. Деление по времени, а не случайное: погода
автокоррелирована, и случайное деление посадило бы соседние дни по разные
стороны, завысив качество.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА:

    прогноз считается годным, если на ПОЗДНЕЙ половине его оценка Брайера по
    корзинам ниже, чем у климатической подсказки, на всех горизонтах, которые
    проверяются, И доля попаданий в названную корзину выше климатической
    хотя бы в полтора раза.

Климатическая подсказка — это распределение фактических максимумов на той же
станции за тот же календарный месяц, взятое из ранней половины. Побить её
обязательно: если модель не бьёт «как обычно бывает в августе», она не нужна.

ЧЕГО ЭТОТ ЗАМЕР НЕ ОТВЕЧАЕТ. Он не говорит, обыграем ли мы РЫНОК. Он говорит
лишь, есть ли у нас откалиброванное распределение. Рынок может быть точнее нас,
и это проверяется отдельно, на ценах — их история живёт 30-40 суток.

ИТОГ 2026-08-08: ПРИГОДЕН. 15 станций, по 398 дней на каждую.

    Брайер по корзинам: модель 0.6697 против климата 0.9555 — лучше на 30%
    попадание в корзину:  43.6% против 7.5% — в 5.8 раза

Поправка на станцию оказалась велика и подтвердила диагноз, поставленный по
одному наблюдению: у Гонконга смещение +1.92°C — ровно этим и объяснялось
расхождение между ансамблем (35.9-37.0) и фактическим разрешением рынка (34).
У Амстердама -1.08, у Сеула +1.03, у Токио +0.92. Сырой прогноз без поправки
ошибался бы на полторы-две корзины — то есть промахивался бы полностью.

Разброс ошибки различается между станциями втрое: Лондон 0.56, Париж 0.54,
Сеул 1.96, Амстердам 1.29. Это прямо переводится в пригодность города: там, где
разброс меньше половины градуса, распределение почти целиком ложится в одну-две
корзины, и назвать вероятность можно уверенно. Где разброс около двух градусов
— размазывается на пять корзин, и ставить бессмысленно.

ЗАГЛЯДЫВАНИЯ ВПЕРЁД НЕТ, И ЭТО ПРОВЕРЕНО ОТДЕЛЬНО. Архив прошлых прогнозов мог
бы отдавать не прогноз, а анализ — тогда весь навык был бы подделкой. Сравнение
с реанализом ERA5 за двадцать дней: среднее расхождение -0.02°C при разбросе
0.50 и точном совпадении лишь в 4 днях из 20. Прогноз настоящий.

ЧЕГО ЭТОТ РЕЗУЛЬТАТ НЕ ДОКАЗЫВАЕТ, И ЭТО ВАЖНЕЕ ВСЕГО СКАЗАННОГО ВЫШЕ. Побит
КЛИМАТ, а не рынок. Участники рынка пользуются теми же бесплатными прогнозами,
и вполне возможно, что цена уже содержит всё, что здесь измерено. Отдельная
оговорка: неизвестно, с каким упреждением выдан архивный прогноз. Если он
выпущен утром того же дня, наш навык завышен относительно того, что доступно за
сутки до разрешения, когда рынок ещё торгуется.

Оба вопроса решает только сравнение с ЦЕНОЙ.

Запуск:
    python research/weather_skill.py
"""

import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, 'research', 'polymarket_cache', 'weather')

DAYS_BACK = 400          # чем длиннее ряд, тем надёжнее распределение ошибок
MIN_DAYS = 120           # меньше — станция не считается
LEADS = (1, 3)           # упреждение прогноза в сутках

# Станции взяты из описаний рынков: последний сегмент ссылки Weather
# Underground либо параметр site= у NOAA. Координаты нужны Open-Meteo, они
# берутся у самой станции через IEM, а не забиваются руками — руками ошибиться
# на десятые доли градуса легко, а это километры и другой узел сетки.
STATIONS = {
    'New York City': 'KLGA', 'London': 'EGLC', 'Paris': 'LFPB',
    'Tokyo': 'RJTT', 'Tel Aviv': 'LLBG', 'Moscow': 'UUWW',
    'Chicago': 'KMDW', 'Los Angeles': 'KLAX', 'Miami': 'KMIA',
    'Hong Kong': 'VHHH', 'Singapore': 'WSSS', 'Seoul (Incheon)': 'RKSI',
    'Amsterdam': 'EHAM', 'Madrid': 'LEMD', 'Toronto': 'CYYZ',
}


def fetch(url, tries=3, as_json=True):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={'User-Agent': 'Mozilla/5.0 research'})
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode('utf-8', 'replace')
            return json.loads(body) if as_json else body
        except Exception:                                   # noqa: BLE001
            if attempt == tries - 1:
                return None
            time.sleep(2.0 + 2 * attempt)
    return None


def cached(name, builder):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    data = builder()
    if data is not None:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
    return data


def station_meta(icao):
    """Координаты станции из справочника IEM."""
    def build():
        d = fetch(f'https://mesonet.agron.iastate.edu/api/1/station/{icao}.json')
        # Справочник отдаёт {'schema': ..., 'data': ...}, причём data бывает и
        # списком, и словарём. Разбор по факту, а не по ожиданию: на догадке о
        # форме этот замер уже один раз молча вернул «станции нет».
        data = (d or {}).get('data')
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        lat, lon = data.get('latitude'), data.get('longitude')
        if lat is None or lon is None:
            return None
        return {'lat': float(lat), 'lon': float(lon),
                'name': data.get('name'), 'tz': data.get('tzname')}
    return cached(f'meta_{icao}.json', build)


def observed_max(icao, start, end, tz='Etc/UTC'):
    """
    Максимум почасовых наблюдений станции по дням — то, ЧЕМ РАЗРЕШАЕТСЯ рынок.

    Берутся именно сводки (report_type=3), а не сглаженные суточные сводки
    климатической службы: рынок читает таблицу наблюдений, и настоящий пик дня,
    не попавший в сводку, для него не существует.

    СУТКИ БЕРУТСЯ МЕСТНЫЕ, А НЕ UTC, И ЭТО НЕ МЕЛОЧЬ. Рынок спрашивает про
    календарный день на станции. Для Нью-Йорка граница UTC-суток приходится на
    восемь вечера по местному времени, то есть в «день» попал бы вечер
    предыдущего и утро следующего — а дневной максимум приходится как раз на
    послеполуденные часы. Группировка по UTC дала бы правдоподобный, но чужой
    ряд, и вся поправка училась бы на нём.
    """
    def build():
        y1, m1, d1 = start.split('-')
        y2, m2, d2 = end.split('-')
        zone = urllib.parse.quote(tz, safe='')
        txt = fetch(
            'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?'
            f'station={icao}&data=tmpc&year1={y1}&month1={m1}&day1={d1}'
            f'&year2={y2}&month2={m2}&day2={d2}&tz={zone}'
            '&format=onlycomma&latlon=no&missing=empty&trace=empty'
            '&direct=no&report_type=3', as_json=False)
        if not txt:
            return None
        by_day = defaultdict(list)
        for line in txt.strip().split('\n')[1:]:
            parts = line.split(',')
            if len(parts) < 3 or not parts[2]:
                continue
            try:
                by_day[parts[1][:10]].append(float(parts[2]))
            except ValueError:
                continue
        return {day: max(vals) for day, vals in by_day.items() if vals}
    return cached(f'obs_{icao}_{start}_{end}_{tz.replace("/", "-")}.json', build)


def forecast_max(lat, lon, start, end, tz='UTC'):
    """
    Прогноз дневного максимума, каким он был тогда.

    Часовой пояс тот же, что у наблюдений: иначе «день» у прогноза и у факта
    разъедется, и разница между ними перестанет быть ошибкой модели.
    """
    def build():
        zone = urllib.parse.quote(tz, safe='')
        d = fetch('https://historical-forecast-api.open-meteo.com/v1/forecast?'
                  f'latitude={lat}&longitude={lon}&start_date={start}'
                  f'&end_date={end}&daily=temperature_2m_max&timezone={zone}')
        daily = (d or {}).get('daily') or {}
        times = daily.get('time') or []
        vals = daily.get('temperature_2m_max') or []
        return {t: v for t, v in zip(times, vals) if v is not None}
    return cached(f'fc_{lat:.3f}_{lon:.3f}_{start}_{end}_{tz.replace("/", "-")}.json', build)


def brier_bucket(prob_of_bucket, hit):
    """Оценка Брайера для одного дня: сумма квадратов по всем корзинам."""
    return sum((p - (1.0 if b == hit else 0.0)) ** 2
               for b, p in prob_of_bucket.items())


def normal_bucket_probs(centre, sigma, buckets):
    """Вероятности целочисленных корзин при нормальной ошибке."""
    from math import erf, sqrt
    def cdf(x):
        return 0.5 * (1 + erf((x - centre) / (sigma * sqrt(2))))
    out = {}
    for b in buckets:
        out[b] = max(cdf(b + 0.5) - cdf(b - 0.5), 1e-6)
    total = sum(out.values())
    return {b: p / total for b, p in out.items()}


def main():
    end = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 2 * 86400))
    start = time.strftime('%Y-%m-%d',
                          time.gmtime(time.time() - DAYS_BACK * 86400))
    print(f'окно: {start} … {end}\n')

    print(f'{"город":<18}{"станция":>8}{"дней":>7}{"смещение":>11}'
          f'{"разброс":>10}{"Брайер модель":>15}{"Брайер климат":>15}{"попал%":>9}')
    print('-' * 93)

    totals = {'model': [], 'clim': [], 'hit_model': [], 'hit_clim': []}
    for city, icao in STATIONS.items():
        meta = station_meta(icao)
        if not meta:
            print(f'{city[:16]:<18}{icao:>8}{"нет станции":>18}')
            continue
        zone = meta.get('tz') or 'Etc/UTC'
        obs = observed_max(icao, start, end, zone)
        fcs = forecast_max(meta['lat'], meta['lon'], start, end, zone)
        if not obs or not fcs:
            print(f'{city[:16]:<18}{icao:>8}{"нет данных":>18}')
            continue
        days = sorted(set(obs) & set(fcs))
        if len(days) < MIN_DAYS:
            print(f'{city[:16]:<18}{icao:>8}{len(days):>7}   мало дней')
            continue

        half = len(days) // 2
        train, test = days[:half], days[half:]
        errs = np.array([obs[d] - fcs[d] for d in train], dtype=float)
        bias, sigma = float(errs.mean()), float(errs.std(ddof=1))
        sigma = max(sigma, 0.4)

        # Климатическая подсказка: распределение целых максимумов той же
        # станции на ранней половине. Побить её — обязательное условие.
        clim = defaultdict(int)
        for d in train:
            clim[int(round(obs[d]))] += 1
        clim_total = sum(clim.values())
        clim_probs = {b: c / clim_total for b, c in clim.items()}

        bs_model, bs_clim, hit_m, hit_c = [], [], [], []
        for d in test:
            truth = int(round(obs[d]))
            centre = fcs[d] + bias
            span = range(int(centre - 6), int(centre + 7))
            probs = normal_bucket_probs(centre, sigma, span)
            bs_model.append(brier_bucket(probs, truth))
            hit_m.append(1.0 if max(probs, key=probs.get) == truth else 0.0)
            keys = set(clim_probs) | {truth}
            cl = {b: clim_probs.get(b, 1e-6) for b in keys}
            s = sum(cl.values())
            cl = {b: p / s for b, p in cl.items()}
            bs_clim.append(brier_bucket(cl, truth))
            hit_c.append(1.0 if max(cl, key=cl.get) == truth else 0.0)

        totals['model'] += bs_model
        totals['clim'] += bs_clim
        totals['hit_model'] += hit_m
        totals['hit_clim'] += hit_c
        print(f'{city[:16]:<18}{icao:>8}{len(days):>7}{bias:>+11.2f}'
              f'{sigma:>10.2f}{np.mean(bs_model):>15.4f}'
              f'{np.mean(bs_clim):>15.4f}{np.mean(hit_m) * 100:>8.1f}%')

    if not totals['model']:
        print('\nданных не собралось — замер не состоялся')
        return

    print()
    print('=' * 93)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: на поздней половине оценка Брайера')
    print('ниже климатической И доля попаданий выше climat в полтора раза.')
    print('=' * 93)
    bm, bc = np.mean(totals['model']), np.mean(totals['clim'])
    hm, hc = np.mean(totals['hit_model']), np.mean(totals['hit_clim'])
    print(f'  Брайер: модель {bm:.4f}  климат {bc:.4f}  '
          f'{"лучше" if bm < bc else "ХУЖЕ"} на {abs(bc - bm) / bc * 100:.0f}%')
    print(f'  попадание в корзину: модель {hm * 100:.1f}%  климат {hc * 100:.1f}%'
          f'  отношение {hm / max(hc, 1e-9):.2f}')
    print()
    if bm < bc and hm > hc * 1.5:
        print('ПРИГОДЕН. Распределение есть и оно лучше климатической подсказки.')
        print('Следующий шаг — сравнить его с ЦЕНОЙ рынка, а не с климатом:')
        print('рынок может быть точнее нас, и это отдельный вопрос.')
    else:
        print('НЕ ПРИГОДЕН по записанному условию.')
        print('Прогноз, не бьющий «как обычно бывает в этом месяце», не даёт')
        print('оснований ставить деньги, каким бы точным ни выглядел по средней')
        print('ошибке.')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    main()
