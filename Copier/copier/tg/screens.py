"""
Экраны Telegram участника — только отрисовка (текст и кнопки), без сети и базы.
Образец — control/telegram_panel.py торгового бота: одно сообщение-меню,
перерисовка на месте, коды кнопок короткие (≤ 64 байт).
"""

import json

from copier import stats

LEVEL_TITLES = {'observer': 'наблюдатель', 'demo': 'демо-счета', 'full': 'демо и реальные'}
STATUS_TITLES = {'new': 'регистрируется', 'pending': 'на проверке', 'active': 'активен',
                 'inactive': 'отключён'}
KIND_TITLES = {'demo': 'демо', 'real': 'реальный', 'tournament': 'турнир'}
PHASES = {'active_evaluation': 'этап 1', 'active_verification': 'этап 2', 'funded': 'funded',
          'funded_success': 'funded ✓'}


# ── путь участника: приветствие → регистрация на Upscale → «Готово» → ID → ключ → проверка ──

def welcome(first_name, signup_url):
    hello = f'Привет, {first_name}! 👋' if first_name else 'Привет! 👋'
    text = (f'{hello}\n\n'
            'Этот бот торгует на вашем счёте по сделкам наших стратегий.\n\n'
            'Мы работаем с проп-платформой Upscale. Чтобы начать, зарегистрируйтесь на ней '
            'по ссылке ниже. Когда закончите — нажмите «Готово».')
    return text, [[('Регистрация на Upscale', signup_url)], [('✅ Готово', 'rd')]]


ASK_ID = 'Отлично! Пришлите ваш ID на Upscale одним сообщением.'
BAD_ID = 'Не похоже на ID — пришлите его одним словом, без пробелов.'
ID_FIRST = 'Сначала пришлите ваш ID на Upscale, потом API-ключ. Сообщение с ключом я удалил.'


def ask_key(uid):
    return (f'ID {uid} записан.\n\n'
            'Теперь API-ключ — через него бот будет торговать на вашем счёте:\n'
            '1. В Upscale откройте «Настройки аккаунта» → «Настройки API».\n'
            '2. «Настроить счета» — включите API Trading на счёте, который подключаете '
            '(номинал от $10 000).\n'
            '3. Создайте ключ и пришлите его сюда одним сообщением — он начинается с usk_.\n\n'
            'Сообщение с ключом я сразу удалю, ключ хранится зашифрованным.')


KEY_HELP = (
    'Подключение счёта Upscale:\n\n'
    '1. В Upscale откройте «Настройки аккаунта» → «Настройки API».\n'
    '2. «Настроить счета» — включите API Trading на счёте, который подключаете '
    '(номинал от $10 000).\n'
    '3. Создайте ключ и пришлите его сюда одним сообщением — он начинается с usk_.\n\n'
    'Сообщение с ключом я сразу удалю.'
)
NO_API_ACCOUNTS = ('Ключ рабочий, но Upscale не показывает ни одного активного счёта с включённой '
                   'API Trading. Включите её на счёте («Настройки API» → «Настроить счета») и '
                   'пришлите ключ ещё раз.')
SUBMITTED = ('Спасибо! Данные отправлены на проверку. Как только доступ откроют, я напишу — и бот '
             'начнёт торговать на вашем счёте.')
UNDER_REVIEW = 'Ваши данные на проверке. Как только доступ откроют, я напишу.'
ACTIVATED = ('✅ Доступ открыт! Бот торгует на вашем счёте по сделкам наших стратегий. '
             'Уведомления о сделках будут приходить сюда.')
DEACTIVATED = ('Доступ отключён. Новые сделки на ваш счёт не открываются; уже открытые '
               'доводятся до закрытия по своим стопам и целям.')
DECLINED = 'Доступ не открыт. Если это ошибка — свяжитесь с нами.'


def money(v, sign=False):
    if v is None:
        return '—'
    return f'{v:+,.2f} $'.replace(',', ' ') if sign else f'{v:,.2f} $'.replace(',', ' ')


def pct(v, sign=False):
    if v is None:
        return '—'
    return f'{v:+.2f}%' if sign else f'{v:.2f}%'


def _side(direction):
    return 'ЛОНГ' if str(direction).upper() == 'LONG' else 'ШОРТ'


def account_line(acc, status):
    c = stats.challenge(status)
    parts = [f'#{acc["id"]} {KIND_TITLES.get(acc.get("kind"), acc.get("kind") or "")}',
             PHASES.get(c.get('phase'), c.get('phase') or '')]
    if c.get('equity') is not None:
        parts.append(money(c['equity']))
    if c.get('profit_pct') is not None:
        parts.append(pct(c['profit_pct'], sign=True))
    flags = []
    if acc.get('paused'):
        flags.append('⏸')
    if not acc.get('enabled'):
        flags.append('выкл')
    if c.get('antifraud') not in (None, 'normal'):
        flags.append('⚠️ антифрод')
    return ' · '.join(p for p in parts if p) + (f' [{", ".join(flags)}]' if flags else '')


def main_menu(user, accounts, statuses):
    lines = [f'Копирование сделок — {LEVEL_TITLES.get(user.get("level"), user.get("level"))}']
    if user.get('paused'):
        lines.append('⏸ Новые входы на паузе (ваша пауза)')
    if not accounts:
        lines.append('\nСчетов пока нет. Подключите счёт Upscale.')
    else:
        lines.append('')
        for acc in accounts:
            lines.append(account_line(acc, statuses.get(acc['id'], {})))
    lines.append('\nКопируются все сделки счёта Upscale нашего бота.')
    risk = user.get('_risk')
    lines.append(f'Риск на сделку: {risk:g}% от баланса счёта' if risk else 'Риск на сделку: как у счёта бота')
    rows = [[('💼 Счета', 'ac'), ('📈 Статистика', 'st')],
            [('📂 Позиции', 'pl'), ('⏳ Заявки', 'ol'), ('🗂 История', 'hi:0')],
            [('⚙️ Настройки', 'se')],
            [('▶️ Продолжить' if user.get('paused') else '⏸ Пауза', 'pz')]]
    if not accounts:
        rows.insert(0, [('➕ Подключить счёт', 'add')])
    return '\n'.join(lines), rows


def accounts_view(accounts, statuses, max_accounts):
    lines = ['Ваши счета Upscale:']
    rows = []
    for acc in accounts:
        lines.append(account_line(acc, statuses.get(acc['id'], {})))
        rows.append([(f'#{acc["id"]} подробнее', f'a:{acc["id"]}')])
    if len([a for a in accounts if a.get('enabled')]) < (max_accounts or 1):
        rows.append([('➕ Подключить счёт', 'add')])
    rows.append([('« Меню', 'm')])
    return '\n'.join(lines), rows


def account_view(acc, status, summary, risk_pct=None):
    c = stats.challenge(status)
    lines = [account_line(acc, status), '']
    if c.get('equity') is not None:
        lines.append(f'Эквити {money(c["equity"])} · баланс {money(c.get("balance"))} · '
                     f'номинал {money(c.get("initial"))}')
    risk_usd = stats.risk_per_trade(status, risk_pct)
    if risk_usd:
        lines.append(f'Риск на сделку: {risk_pct:g}% от баланса = {money(risk_usd)} — размер позиции '
                     f'считается от вашего счёта')
    if c.get('target_pct'):
        lines.append(f'Цель этапа {c["target_pct"]:g}% — сейчас {pct(c.get("profit_pct"), True)} '
                     f'({c.get("target_progress", 0):g}% пути)')
    if c.get('daily_dd_limit_pct'):
        lines.append(f'Дневная просадка {pct(c.get("daily_dd_used_pct"))} из '
                     f'{c["daily_dd_limit_pct"]:g}%')
    if c.get('total_dd_limit_pct'):
        lines.append(f'Общая просадка {pct(c.get("total_dd_used_pct"))} из '
                     f'{c["total_dd_limit_pct"]:g}%')
    if c.get('profitable_days') is not None:
        lines.append(f'Профитных дней {c["profitable_days"]} из {c.get("min_days") or "—"}')
    if c.get('consistency_ratio') is not None:
        met = '✓' if c.get('consistency_met') else '✗'
        lines.append(f'Правило 30%: лучший день {c["consistency_ratio"]:.1f}% прибыли {met}')
    if c.get('lock'):
        lines.append(f'⚠️ Блокировка: {c["lock"]}')
    if c.get('antifraud') not in (None, 'normal'):
        lines.append(f'⚠️ Антифрод Upscale: {c["antifraud"]}')
    lines.append('')
    lines.append(f'Сделок {summary["trades"]} · прибыльных {summary["win_rate"] or 0:g}% · '
                 f'{money(summary["pnl_usd"], True)} · {summary["sum_r"]:+g}R')
    rows = [[('▶️ Продолжить' if acc.get('paused') else '⏸ Пауза счёта', f'ap:{acc["id"]}')],
            [('🛑 Закрыть всё', f'ax:{acc["id"]}'), ('🔌 Отключить', f'ad:{acc["id"]}')],
            [('« Счета', 'ac')]]
    return '\n'.join(lines), rows


def trades_view(title, trades, accounts_by_id, empty, with_close=False):
    lines = [title]
    rows = []
    if not trades:
        lines.append(empty)
    for t in trades[:15]:
        acc = accounts_by_id.get(t['account_id']) or {}
        price = t.get('entry_price') or t.get('limit_price')
        lines.append(f'#{acc.get("id", "?")} {t["strategy"]} {t["pair"]} {_side(t["direction"])} '
                     f'@ {_num(price)} · стоп {_num(t.get("stop"))}')
        if with_close:
            rows.append([(f'✖ {t["pair"]} {_side(t["direction"])}', f'tc:{t["id"]}')])
    rows.append([('« Меню', 'm')])
    return '\n'.join(lines), rows


def history_view(trades, page, per_page=10):
    closed = [t for t in trades if t['state'] in ('closed', 'canceled', 'error')]
    chunk = closed[page * per_page:(page + 1) * per_page]
    lines = ['История сделок:']
    if not chunk:
        lines.append('пока пусто')
    for t in chunk:
        if t['state'] == 'closed':
            pnl = _f(t.get('pnl_usd'))
            r = _f(t.get('pnl_r'))
            res = money(pnl, True) + (f' ({r:+.2f}R)' if r is not None else '')
        elif t['state'] == 'canceled':
            res = 'снята без налива'
        else:
            res = 'ошибка: ' + (t.get('note') or '')[:60]
        lines.append(f'{(t.get("closed_at") or t.get("created_at") or "")[:16].replace("T", " ")} '
                     f'{t["strategy"]} {t["pair"]} {_side(t["direction"])} — {res}')
    nav = []
    if page > 0:
        nav.append(('‹ новее', f'hi:{page - 1}'))
    if len(closed) > (page + 1) * per_page:
        nav.append(('старее ›', f'hi:{page + 1}'))
    return '\n'.join(lines), [nav, [('« Меню', 'm')]]


def stats_view(trades):
    p = stats.periods(trades)
    lines = ['Статистика ваших сделок:']
    for key, title in (('day', 'Сутки'), ('week', 'Неделя'), ('month', 'Месяц'), ('all', 'Всё время')):
        s = p[key]
        lines.append(f'{title}: {s["trades"]} сд · прибыльных {s["win_rate"] or 0:g}% · '
                     f'{money(s["pnl_usd"], True)} · {s["sum_r"]:+g}R')
    by_strategy = stats.by([t for t in trades if t['state'] == 'closed'], 'strategy')
    if by_strategy:
        lines.append('\nПо стратегиям:')
        for code, s in sorted(by_strategy.items()):
            lines.append(f'{code}: {s["trades"]} сд · {s["win_rate"] or 0:g}% · '
                         f'{money(s["pnl_usd"], True)} · ср. {s["avg_r"] or 0:+.2f}R')
    a = p['all']
    if a['trades']:
        lines.append(f'\nЛучшая {money(a["best_usd"], True)} · худшая {money(a["worst_usd"], True)}'
                     f' · просадка {a["max_dd_r"]:g}R · серия минусов {a["loss_streak"]}')
    return '\n'.join(lines), [[('« Меню', 'm')]]


def settings_view(user, risk=None):
    lines = ['Настройки', '',
             (f'Риск на сделку: {risk:g}% от баланса — как у счёта бота' if risk
              else 'Риск на сделку: как у счёта бота'),
             f'Ежедневная сводка: {"вкл" if user.get("daily_summary") else "выкл"}']
    rows = []
    rows.append([('🗞 Сводка: выкл' if user.get('daily_summary') else '🗞 Сводка: вкл', 'ds')])
    rows.append([('🔌 Отключиться полностью', 'dx')])
    rows.append([('« Меню', 'm')])
    return '\n'.join(lines), rows


def pick_accounts(items, connected_ids):
    lines = ['Счета, видимые этому ключу (API Trading включён):']
    rows = []
    for item in items:
        aid = str(item.get('accountId'))
        kind = KIND_TITLES.get(item.get('type'), item.get('type'))
        phase = PHASES.get(item.get('currentPhase'), item.get('currentPhase') or '')
        mark = ' — уже подключён' if aid in connected_ids else ''
        lines.append(f'• {kind} {phase} {aid[:8]}…{mark}')
        if not mark and str(item.get('status')) == 'active':
            rows.append([(f'Подключить {kind} {aid[:8]}…', f'pk:{aid[:36]}')])
    if not rows:
        lines.append('\nПодключить нечего: нет активных счетов с API Trading.')
    rows.append([('« Меню', 'm')])
    return '\n'.join(lines), rows


def confirm(text, yes_data, no_data='m'):
    return text, [[('Да', yes_data), ('Нет', no_data)]]


def admin_request(user, accounts, key_users):
    """Карточка проверки для владельца: ID, владелец ключа по Upscale, счёт."""
    name = user.get('username') and f'@{user["username"]}' or user.get('first_name') or ''
    uid = user.get('upscale_uid') or '—'
    if key_users:
        match = '✓ совпадает с ID' if uid in key_users else '≠ не совпадает с ID — сверьте'
        owner = f'{", ".join(key_users)} ({match})'
    else:
        owner = '—'
    lines = [f'🆕 На проверку: {name} (Telegram {user["tg_id"]})',
             f'ID на Upscale: {uid}',
             f'Владелец ключа по Upscale: {owner}']
    for acc in accounts:
        lines.append(f'Счёт #{acc["id"]}: {KIND_TITLES.get(acc.get("kind"), acc.get("kind") or "")}'
                     f' {PHASES.get(acc.get("phase"), acc.get("phase") or "")}'
                     f' ${acc.get("nominal") or 0:,.0f}'.replace(',', ' '))
    if not accounts:
        lines.append('Счёт: ещё не подключён')
    lines.append('\nАктивировать — галочкой в админке или кнопкой ниже.')
    return '\n'.join(lines), [[('✅ Активировать', f'ua:{user["tg_id"]}:auto'),
                               ('⛔ Отказать', f'ub:{user["tg_id"]}')]]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num(v):
    f = _f(v)
    if f is None:
        return '—'
    return f'{f:.6g}'


def parse_list(text):
    try:
        v = json.loads(text) if text else None
        return list(v) if isinstance(v, list) else None
    except ValueError:
        return None
