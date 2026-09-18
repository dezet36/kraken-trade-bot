"""
Прогон разбора по живому рынку: что модель видит и что решает.

ЗАЧЕМ ОТДЕЛЬНЫЙ ИНСТРУМЕНТ. Между «конвейер собран» и «конвейер работает»
лежит расстояние, которое проверками не закрывается. Проверки гоняют
синтетические свечи и поддельную модель — они ловят ошибки в формулах, но
молчат о том, осмысленно ли модель судит настоящий рынок.

Этот файл запускается руками на сервере, где лежит модель, и печатает ВСЮ
цепочку: разметку, которую собрал код, ответ модели дословно, и вердикт
проверок. Каждое звено видно отдельно, поэтому поломку не приходится угадывать.

ПОЧЕМУ ПЕЧАТАЕТСЯ СЫРОЙ ОТВЕТ. Разбор может пройти успешно и дать вердикт
«отказ», а причин у этого две совершенно разные: модель действительно
отказалась или её ответ не прошёл проверку. По одному вердикту их не
различить, а по сырому ответу — сразу.

ЗАПУСК

    LLM_MODEL_PATH=/путь/model.gguf python llm_probe.py
    LLM_MODEL_PATH=... python llm_probe.py BTCUSDT ETHUSDT

Без аргументов берутся три пары из пула. Каждая занимает около минуты.
"""

import sys

DEFAULT_PAIRS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')


def show(pair, verdict, context):
    """Печатает одну пару: разметку, сырой ответ, вердикт."""
    print('=' * 72)
    print(context['text'] or '(разметки нет)')
    print('-' * 72)

    raw = verdict.get('raw')
    print('ответ модели:', raw if raw else '(не вызывалась)')

    if verdict['ok']:
        print(f"ВЕРДИКТ: вход {verdict['side']} от {verdict['entry']:.6g}")
        print(f"  стоп {verdict['stop']:.6g} ({verdict['stop_pct']}%), "
              f"цели {[round(t, 6) for t in verdict['targets']]}")
        print(f"  R:R {verdict['rr']}   издержки {verdict['cost_r']}R   "
              f"EV {verdict['ev']}   вероятность {verdict['p']}")
        print(f"  конфлюенс {verdict['votes']}/5: "
              f"{', '.join(k for k, v in verdict['confluence'].items() if v) or '—'}")
    else:
        print(f"ВЕРДИКТ: отказ — {verdict['gate']}")
        if verdict.get('detail'):
            print(f"  {verdict['detail']}")
        if 'votes' in verdict:
            print(f"  конфлюенс {verdict['votes']}/5")

    why = verdict.get('why')
    if why:
        print(f"  почему: {why}")
    print()


def main(pairs):
    import config
    import exchange
    import llm_context
    import llm_decide
    import llm_local

    if not llm_local.available():
        print('Модель недоступна. Задайте LLM_MODEL_PATH на существующий файл.')
        print(f'  сейчас: {llm_local.model_path()!r}')
        return 1

    # KEYLESS КЛИЕНТ, А НЕ ОБЩИЙ. Первая версия звала fetch_ohlcv без клиента,
    # и тот уходил в get_exchange(), который требует ключей биржи. Прогон
    # падал на сервере с «BYBIT_API_KEY не загружены» — при том что свечи
    # публичные и ключей не требуют вовсе. Разбору незачем иметь доступ к
    # счёту: он ничего не отправляет.
    market = exchange.make_market_client(getattr(config, 'EXCHANGE_NAME', 'bybit'))

    for pair in pairs:
        df = exchange.fetch_ohlcv('1h', limit=500, symbol=pair, client=market)
        if df is None or len(df) < 100:
            print(f'{pair}: свечей не хватило\n')
            continue
        context = llm_context.build(pair, df)
        verdict = llm_decide.decide(pair, df, llm_local.ask)
        show(pair, verdict, context)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:] or list(DEFAULT_PAIRS)))
