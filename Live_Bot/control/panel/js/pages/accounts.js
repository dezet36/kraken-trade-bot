/* Счета: торговые (биржа по ключам, проп по инструкциям) и тестовые счета
   стратегий. Правила денег — у счёта (accounts/live.py), исполнение — книга
   счёта (accounts/books.py). Все действия — через лист с подтверждением:
   страница перерисовывается по опросу, а действие на бирже не должно
   случиться от случайного щелчка. */

import { h, pill, empty, notice, button, openSheet, confirmSheet, toast, field, strategyColor,
  strategyTitle, strategyBadge, segmented } from '../ui.js';
import { money, signedMoney, signedPct, signedR, pct, num, price, tone, dateTime, ago, isNum } from '../format.js';
import { strategyOrder, testTotals } from '../model.js';
import { postJSON, refreshAccounts } from '../api.js';

export const title = 'Счета';
let tab = 'trading';
let rerender = () => {};
export function bindRefresh(fn) { rerender = fn; }

const SIDES = { both: 'обе стороны', long: 'только лонги', short: 'только шорты' };

export function render(store, args) {
  if (args[0] === 'test') tab = 'test';
  const acc = store.accounts;
  const list = (acc && acc.accounts) || [];
  return h('div', null,
    h('div', { class: 'page-head' },
      h('div', null, h('h1', { class: 'page-title' }, title),
        h('div', { class: 'page-sub' }, 'У каждого счёта свои стратегии и свои правила денег')),
      tab === 'trading' && acc && acc.writable ? button('Добавить счёт', () => accountForm(acc, null)) : null),
    h('div', { class: 'toolbar', style: { marginBottom: '18px' } },
      segmented([{ value: 'trading', label: `Торговые · ${list.length}` }, { value: 'test', label: 'Тестовые счета стратегий' }],
        tab, (v) => { tab = v; rerender(); }, 'Вид')),
    h('div', { class: 'fade-in' }, tab === 'trading' ? trading(acc) : testAccounts(store.data)));
}

/* ══ Торговые счета ═════════════════════════════════════════════════ */
function trading(acc) {
  if (!acc) return h('div', { class: 'card skeleton', style: { height: '240px' } });
  if (acc.error && !acc.accounts) return h('div', { class: 'card' }, empty('Счета не загрузились', acc.error));
  const list = acc.accounts || [];
  const out = [];
  if (!acc.writable) {
    out.push(notice('info', 'Управление выключено', 'Панель открыта в сеть: менять счета можно только с машины бота или через KrakenRemote.'));
  }
  if (!list.length) {
    out.push(h('div', { class: 'card' },
      empty('Торговых счетов пока нет', 'Биржевой счёт торгует по ключам API (сначала демо). Проп по инструкциям: бот присылает, что поставить, вы ставите сами и жмёте «Готово».'),
      acc.writable ? h('div', { style: { textAlign: 'center', marginTop: '6px' } }, button('Добавить счёт', () => accountForm(acc, null))) : null));
    return h('div', null, out);
  }
  out.push(...list.map(a => accountCard(acc, a)));
  return h('div', { class: 'grid', style: { gap: '18px' } }, out);
}

function accountCard(acc, a) {
  const b = (acc.books || {})[a.id];
  const ex = a.kind === 'exchange';
  const live = ex && a.mode === 'live';
  const rules = [
    `риск ${pct(a.risk_pct, 2)} на сделку`, SIDES[a.sides] || a.sides,
    `позиций ${a.max_slots || 'без предела'}`, `в одну сторону ${a.max_same_direction || 'без предела'}`,
    a.profit_target_pct ? `цель +${a.profit_target_pct}%` : null,
    a.max_drawdown_pct ? `макс. просадка ${a.max_drawdown_pct}%` : null,
    a.daily_loss_pct ? `дневной убыток ${a.daily_loss_pct}%` : null,
  ].filter(Boolean).join(' · ');
  const strats = (a.strategies || []).length
    ? h('div', { class: 'legend', style: { marginTop: '10px' } }, a.strategies.map(c =>
      h('span', null, h('span', { class: 'dot', style: { background: strategyColor(c) } }), strategyTitle(c))))
    : h('div', { class: 'muted', style: { marginTop: '10px', fontSize: '13px' } }, 'Стратегии не выбраны — счёт не получит сделок');
  return h('div', { class: 'card' },
    h('div', { style: { display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap' } },
      h('div', null,
        h('div', { class: 'section-title' }, a.name || a.id),
        h('div', { class: 'tags', style: { marginTop: '8px' } },
          pill(a.kind_title),
          ex ? pill(`${(a.exchange || '').toUpperCase()} · ${live ? 'реальные деньги' : 'демо'}`, live ? 'down' : 'accent') : pill(`счёт ${money(a.deposit, 0)}`),
          pill(a.enabled ? 'торгует' : 'выключен', a.enabled ? 'up' : ''),
          ex ? pill(a.has_keys ? 'ключи заданы' : 'ключей нет', a.has_keys ? '' : 'warn') : null)),
      acc.writable ? h('div', { class: 'actions' },
        button('Изменить', () => accountForm(acc, a), 'gray', true),
        ex ? button('Ключи', () => keysForm(a), 'gray', true) : null,
        button(a.enabled ? 'Выключить' : 'Включить', () => toggle(a), 'gray', true),
        button('Удалить', () => remove(a), 'danger soft', true)) : null),
    a.draft ? h('div', { style: { marginTop: '12px' } }, notice('warn', 'Правила — черновик', 'Типичные правила пропа: проверьте и сохраните свои.')) : null,
    live && !acc.live_enabled ? h('div', { style: { marginTop: '12px' } }, notice('warn', 'Реальные деньги пока выключены',
      'Торговля на этом счёте начнётся после проверки на демо (флаг EXCHANGE_LIVE_ENABLED на сервере).')) : null,
    h('div', { class: 'muted', style: { marginTop: '12px', fontSize: '13px' } }, rules),
    strats,
    b ? book(acc, a, b) : null);
}

function meter(label, value, frac, color) {
  const w = Math.round(Math.min(Math.max(Number(frac) || 0, 0), 1) * 100);
  return h('div', { class: 'meter' },
    h('div', { class: 'meter-l' }, h('span', null, label), h('b', null, value)),
    h('div', { class: 'meter-track' }, h('i', { style: { width: `${w}%`, background: color } })));
}

function kpi(label, value, hint, cls = '') {
  return h('div', { class: 'kpi', style: { background: 'var(--fill-2)', boxShadow: 'none' } },
    h('div', { class: 'stat-label' }, label), h('div', { class: 'kpi-v ' + cls }, value), hint ? h('small', null, hint) : null);
}

function book(acc, a, b) {
  const ex = a.kind === 'exchange';
  const w = acc.writable;
  const q = b.quality || {};
  const out = [];
  if (ex && !b.started) {
    return a.enabled && b.tradable ? h('div', { class: 'muted', style: { marginTop: '16px', fontSize: '13px' } },
      'Счёт ещё не торговал: капитал придёт с биржи с первой заявкой. Заявки ставятся сразу со стопом; события — здесь и в Telegram.') : null;
  }
  if (ex && (b.foreign || []).length) {
    out.push(notice('warn', 'На бирже есть позиции вне учёта бота',
      b.foreign.map(f => `${f.pair} ${f.long ? 'лонг' : 'шорт'} ${f.size}`).join(', ') + ' — бот их не ведёт и не трогает.'));
  }
  if (b.status && b.status !== 'active') {
    out.push(h('div', { class: 'notice ' + (b.status === 'target' ? 'info' : 'bad') },
      h('div', { style: { flex: 1 } }, h('b', null, b.status_text), h('small', null, b.status_why)),
      w ? button('Новый этап', () => act(a, 'new_phase'), '', true) : null));
  }
  out.push(h('div', { class: 'kpis', style: { marginTop: '16px' } },
    kpi(ex ? 'Капитал на бирже' : 'Капитал', money(b.equity), `${signedPct(b.return_pct)} за этап ${b.phase}`, ''),
    kpi('Сегодня', signedMoney(b.day_pnl), 'с полуночи UTC', tone(b.day_pnl)),
    kpi('Открытый риск', money(b.open_risk), b.room ? `до предела ${money(Math.max(0, b.room.amount))}` : null),
    kpi('Сделок за этап', num(q.trades || 0), q.trades ? `в плюс ${pct((q.win_rate || 0) * 100, 0)} · ${signedR(q.total_r, 1)}` : 'закрытых ещё нет')));
  if (b.target_pct) out.push(meter(`Цель +${b.target_pct}% · ${money(b.target_level, 0)}`, pct((b.target_progress || 0) * 100, 0), b.target_progress, 'var(--up)'));
  if (b.drawdown_limit_pct) out.push(meter(`Просадка · предел ${b.drawdown_limit_pct}% (${money(b.floor, 0)})`, pct(b.drawdown_pct, 2), b.drawdown_pct / b.drawdown_limit_pct, 'var(--down)'));
  if (b.daily_limit) out.push(meter(`Убыток за сутки · предел ${money(b.daily_limit, 0)}`, pct(b.daily_used_pct || 0, 0), (b.daily_used_pct || 0) / 100, 'var(--warn)'));
  if (!ex && b.started && Math.abs((b.deposit_rules || 0) - b.start) >= 0.01) {
    out.push(h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '10px' } },
      `В правилах размер счёта ${money(b.deposit_rules, 0)} — применится с нового этапа (этап ${b.phase} начат с ${money(b.start, 0)}).`));
  }
  if ((b.waiting || []).length) {
    out.push(h('div', { class: 'subhead' }, `Ждут исполнения · ${b.waiting.length}`), b.waiting.map(it => instruction(a, it, w)));
  } else if (!b.started && !ex) {
    out.push(h('div', { class: 'muted', style: { fontSize: '13px', marginTop: '14px' } },
      'Заявок ещё не было. Когда стратегии счёта найдут сетап, инструкция придёт в Telegram и появится здесь: пара, сторона, вход, стоп, тейки, объём.'));
  }
  if ((b.positions || []).length) {
    out.push(h('div', { class: 'subhead' }, `Позиции · ${b.positions.length}`), table(
      ['', 'Стратегия', 'Вход', 'Стоп', 'Цели', 'Объём', 'Итог', ''],
      b.positions.map(p => [pairCell(p), strategyTitle(p.strategy), price(p.entry), price(p.stop) + (p.breakeven ? ' · б/у' : ''),
        targets(p), qty(p.size), h('span', { class: tone(p.result) }, `${signedMoney(p.result)} · ${signedR(p.result_r)}`),
        w ? button(ex ? 'Закрыть на бирже' : 'Закрыл сам', () => act(a, 'close', null, p.pair), 'gray', true) : ''])));
  }
  if ((b.pending || []).length) {
    out.push(h('div', { class: 'subhead' }, `Заявки · ${b.pending.length}`), table(
      ['', 'Стратегия', 'Вход', 'Стоп', 'Цели', 'Объём', 'До цены', 'Снять до', ''],
      b.pending.map(o => [pairCell(o), strategyTitle(o.strategy), price(o.entry) + (o.stop_entry ? ' (стоп-заявка)' : ''),
        price(o.stop), targets(o), qty(o.size), isNum(o.distance_pct) ? pct(o.distance_pct, 2) : '—', dateTime(o.expires),
        w ? button(ex ? 'Снять на бирже' : 'Снял сам', () => act(a, 'cancel', null, o.pair), 'gray', true) : ''])));
  }
  if ((b.recent || []).length) {
    out.push(h('div', { class: 'subhead' }, 'Последние сделки этапа'), table(
      ['', 'Стратегия', 'Вход → выход', 'Выход', 'Итог', 'Закрыта'],
      b.recent.map(r => [pairCell(r), strategyTitle(r.strategy), `${price(r.entry_price)} → ${price(r.exit_price)}`, r.exit_reason,
        h('span', { class: tone(r.pnl_usd) }, `${signedMoney(r.pnl_usd, 0)} · ${signedR(r.pnl_r)}`), dateTime(r.closed_at)])));
  }
  if (ex && w && a.enabled && ((b.positions || []).length || (b.pending || []).length)) {
    out.push(h('div', { class: 'actions', style: { marginTop: '16px' } },
      button('Закрыть всё и выключить', () => act(a, 'panic'), 'danger', true),
      h('span', { class: 'muted', style: { fontSize: '12px' } }, 'аварийно: позиции — по рынку, заявки — снять, счёт — выключить')));
  }
  if ((b.instructions || []).length) {
    out.push(h('details', { class: 'log' }, h('summary', null, `${ex ? 'События счёта' : 'Все инструкции'} · последние ${b.instructions.length}`),
      b.instructions.map(it => instruction(a, it, false))));
  }
  return h('div', null, out);
}

const pairCell = p => h('span', { style: { fontWeight: 600 } }, p.pair.replace(/USDT$/, ''), ' ',
  h('span', { class: 'side ' + (p.direction === 'LONG' ? 'long' : 'short') }, p.direction === 'LONG' ? 'ЛОНГ' : 'ШОРТ'));

function qty(v) {
  v = Number(v || 0);
  return v >= 1000 ? num(v, 0) : v >= 10 ? num(v, 1) : v >= 1 ? num(v, 3) : String(Number(v.toPrecision(4)));
}

function targets(x) {
  const t = x.targets || [], f = x.fractions || [];
  return t.map((v, i) => price(v) + (t.length > 1 && f[i] != null ? ` (${Math.round(f[i] * 100)}%)` : '')).join(' · ') || '—';
}

function table(head, rows) {
  return h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
    h('thead', null, h('tr', null, head.map(t => h('th', null, t)))),
    h('tbody', null, rows.map(r => h('tr', null, r.map(c => h('td', null, c)))))));
}

function instruction(a, it, buttons) {
  return h('div', { class: 'instr' + (it.action && !it.done ? ' todo' : '') },
    h('div', { class: 'instr-h' },
      h('span', null, (it.icon || '📋') + ' '), h('b', null, it.title), h('span', { class: 'muted' }, dateTime(it.at)),
      it.done ? pill(`сделано ${ago(it.done)}`, 'up') : null,
      buttons && it.action && !it.done ? button('Готово', () => act(a, 'ack', it.id), '', true) : null),
    h('div', { class: 'instr-b' }, (it.lines || []).map((l, i) => [i ? h('br') : null, l])));
}

/* ── Действия ───────────────────────────────────────────────────────── */
const ASK = {
  close: (p, ex) => ex ? `Закрыть позицию ${p} по рынку на бирже?` : `Позиция ${p} закрыта у вас на счёте? В записи она закроется по последней цене.`,
  cancel: (p, ex) => ex ? `Снять заявку ${p} на бирже?` : `Заявка ${p} снята у вас на счёте? Из записи она уберётся.`,
  panic: () => 'Аварийная остановка: закрыть все позиции счёта по рынку, снять его заявки и выключить счёт?',
  new_phase: (p, ex) => ex ? 'Начать новый этап от текущего капитала на бирже? Итоги прошлого этапа останутся в журнале.'
    : 'Начать новый этап? Размер счёта — из правил, итоги прошлого этапа останутся в журнале.',
};
const DANGER = new Set(['close', 'cancel', 'panic']);

async function act(a, action, item = null, pair = null) {
  const ex = a.kind === 'exchange';
  if (ASK[action] && !(await confirmSheet(ASK[action](pair || '', ex), action === 'panic' ? 'Остановить' : 'Да', DANGER.has(action) && ex))) return;
  try {
    await postJSON('/api/accounts/act', { id: a.id, action, item, pair });
    toast('Готово', 'good');
  } catch (e) { toast(e.message, 'bad'); }
  refreshAccounts();
}

async function toggle(a) {
  if (!a.enabled && a.kind === 'exchange' && a.mode === 'live'
      && !(await confirmSheet('Включить торговлю реальными деньгами на этом счёте?', 'Включить', true))) return;
  try { await postJSON('/api/accounts/save', { id: a.id, enabled: !a.enabled }); toast(a.enabled ? 'Счёт выключен' : 'Счёт включён'); }
  catch (e) { toast(e.message, 'bad'); }
  refreshAccounts();
}

async function remove(a) {
  if (!(await confirmSheet(`Удалить счёт «${a.name || a.id}» и его ключи?`, 'Удалить', true))) return;
  try { await postJSON('/api/accounts/delete', { id: a.id }); toast('Счёт удалён'); }
  catch (e) { toast(e.message, 'bad'); }
  refreshAccounts();
}

/* ── Лист: счёт (новый или правка) ──────────────────────────────────── */
function accountForm(acc, a) {
  const base = a || (acc.defaults || {}).manual || { kind: 'manual', exchange: 'bybit', mode: 'demo', sides: 'both', strategies: [] };
  openSheet(a ? `Счёт «${a.name || a.id}»` : 'Новый счёт', (close) => {
    const input = (id, value, type = 'number', step = null, placeholder = null) =>
      h('input', { class: 'input', id: `f-${id}`, type, step, value: value ?? (type === 'number' ? 0 : ''), placeholder, autocomplete: 'off' });
    const select = (id, options, value, disabled = false) => h('select', { id: `f-${id}`, disabled },
      options.map(([v, t]) => h('option', { value: v, selected: v === value }, t)));
    const only = {};
    const kindSel = select('kind', [['manual', 'Проп по инструкциям (без API)'], ['exchange', 'Биржа (по ключам)']], base.kind, !!a);
    const modeSel = select('mode', [['demo', 'Демо'], ['live', 'Реальные деньги']], base.mode);
    const warn = notice('bad', 'Реальные деньги', 'Сделки пойдут на настоящий счёт биржи.');
    const fields = [
      h('div', { class: 'wide' }, field('Название', Object.assign(input('name', base.name, 'text', null, 'например, HashHedge 10k'), { autofocus: true }))),
      field('Вид', kindSel),
      only.exchange1 = field('Биржа', select('exchange', (acc.exchanges || ['bybit']).map(x => [x, x.toUpperCase()]), base.exchange)),
      only.exchange2 = field('Режим', modeSel),
      only.manual1 = field('Размер счёта, $', input('deposit', base.deposit, 'number', 100)),
      field('Риск на сделку, %', input('risk_pct', base.risk_pct, 'number', 0.05)),
      field('Стороны', select('sides', [['both', 'Обе'], ['long', 'Только лонги'], ['short', 'Только шорты']], base.sides)),
      field('Позиций одновременно', input('max_slots', base.max_slots, 'number', 1), '0 — без предела'),
      field('В одну сторону', input('max_same_direction', base.max_same_direction, 'number', 1), '0 — без предела'),
      only.manual2 = field('Цель по доходу, %', input('profit_target_pct', base.profit_target_pct, 'number', 0.5), '0 — нет'),
      field('Макс. просадка, %', input('max_drawdown_pct', base.max_drawdown_pct, 'number', 0.5), '0 — нет'),
      field('Дневной убыток, %', input('daily_loss_pct', base.daily_loss_pct, 'number', 0.5), '0 — нет'),
    ];
    const strategies = h('div', { class: 'checks' }, (acc.strategies || []).map(s => h('label', { class: 'check' },
      h('input', { type: 'checkbox', 'data-strat': s.code, checked: (base.strategies || []).includes(s.code) }),
      h('span', { class: 'dot', style: { background: strategyColor(s.code) } }), strategyTitle(s.code))));
    const msg = h('span', { class: 'muted', style: { fontSize: '13px' } });
    const body = h('div', null, h('div', { class: 'form' }, fields),
      h('div', { class: 'field-label', style: { margin: '18px 0 8px', fontSize: '13px' } }, 'Стратегии счёта'),
      strategies,
      h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '8px' } }, 'ИИ торгуется только на тесте.'),
      h('div', { style: { marginTop: '14px' } }, warn));
    const fit = () => {
      const k = kindSel.value;
      only.exchange1.hidden = only.exchange2.hidden = k !== 'exchange';
      only.manual1.hidden = only.manual2.hidden = k !== 'manual';
      warn.hidden = !(k === 'exchange' && modeSel.value === 'live');
    };
    kindSel.addEventListener('change', () => {
      const d = !a && (acc.defaults || {})[kindSel.value];
      if (d) for (const f of ['profit_target_pct', 'max_drawdown_pct', 'daily_loss_pct']) body.querySelector(`#f-${f}`).value = d[f];
      fit();
    });
    modeSel.addEventListener('change', fit);
    fit();
    const save = button('Сохранить', async () => {
      const val = id => body.querySelector(`#f-${id}`).value;
      const shown = id => !body.querySelector(`#f-${id}`).closest('label').hidden;
      const req = { name: val('name'), kind: val('kind'), sides: val('sides'), draft: false,
        strategies: [...body.querySelectorAll('[data-strat]')].filter(x => x.checked).map(x => x.dataset.strat) };
      for (const f of ['exchange', 'mode']) if (shown(f)) req[f] = val(f);
      for (const f of ['deposit', 'risk_pct', 'max_slots', 'max_same_direction', 'profit_target_pct', 'max_drawdown_pct', 'daily_loss_pct']) {
        if (shown(f)) req[f] = Number(val(f));
      }
      if (a) req.id = a.id;
      if (req.kind === 'exchange' && req.mode === 'live' && !(await confirmSheet('Счёт с реальными деньгами. Сохранить?', 'Сохранить', true))) return;
      try {
        save.disabled = true; msg.textContent = 'Сохраняю…';
        await postJSON('/api/accounts/save', req);
        close(); toast('Счёт сохранён', 'good'); refreshAccounts();
      } catch (e) { save.disabled = false; msg.textContent = e.message; msg.className = 'down'; }
    });
    return h('div', null, body, h('div', { class: 'sheet-actions' }, msg, button('Отмена', close, 'gray'), save));
  });
}

/* ── Лист: ключи биржи ──────────────────────────────────────────────── */
function keysForm(a) {
  openSheet(`Ключи · ${a.name || a.id}`, (close) => {
    const key = h('input', { class: 'input', type: 'password', autocomplete: 'off', autofocus: true });
    const secret = h('input', { class: 'input', type: 'password', autocomplete: 'off' });
    const msg = h('span', { class: 'muted', style: { fontSize: '13px' } });
    const save = button('Проверить и сохранить', async () => {
      try {
        save.disabled = true; msg.className = 'muted'; msg.textContent = 'Проверяю на бирже…';
        const r = await postJSON('/api/accounts/keys', { id: a.id, key: key.value, secret: secret.value });
        close(); toast(r.message || 'Ключи сохранены', 'good'); refreshAccounts();
      } catch (e) { save.disabled = false; msg.className = 'down'; msg.textContent = e.message; }
    });
    return h('div', null,
      h('div', { class: 'form' }, h('div', { class: 'wide' }, field('API key', key)), h('div', { class: 'wide' }, field('Secret key', secret))),
      h('div', { class: 'muted', style: { fontSize: '13px', marginTop: '12px' } },
        `Ключи проверяются на бирже (${a.mode === 'live' ? 'реальный' : 'демо'} адрес) и хранятся только на сервере — ни в панель, ни в журналы они не попадают. Права ключа — только торговля, без вывода средств.`),
      h('div', { class: 'sheet-actions' }, msg, button('Отмена', close, 'gray'), save));
  });
}

/* ══ Тестовые счета стратегий ═══════════════════════════════════════ */
function testAccounts(data) {
  if (!data) return h('div', { class: 'card skeleton', style: { height: '240px' } });
  const totals = testTotals(data);
  const rows = strategyOrder(data).map(c => {
    const s = data.strategies[c] || {};
    const d = totals.byStrategy[c] || {};
    const go = () => { location.hash = `#/strategies/${c}`; };
    return h('tr', { class: 'link', onclick: go },
      h('td', null, h('div', { class: 'cell-name' }, strategyBadge(c),
        h('div', null, strategyTitle(c), h('small', null, d.enabled === false ? 'выключена' : 'торгует')))),
      h('td', null, money(s.start_balance, 0)),
      h('td', null, money(s.equity, 0)),
      h('td', { class: tone(s.return_pct) }, signedPct(s.return_pct)),
      h('td', null, isNum(s.max_dd_pct) ? pct(s.max_dd_pct, 1) : '—'),
      h('td', null, num((s.open || 0) + (s.pending || 0))));
  });
  return h('div', null,
    h('div', { class: 'card' },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', flexWrap: 'wrap', gap: '8px', marginBottom: '14px' } },
        h('div', null, h('div', { class: 'card-title', style: { margin: 0 } }, 'Всего на тесте'),
          h('div', { class: 'big-sm' }, money(totals.equity, 0))),
        h('div', { class: tone(totals.pnl), style: { fontWeight: 600 } }, `${signedMoney(totals.pnl, 0)} · ${signedPct(totals.pct)}`)),
      h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
        h('thead', null, h('tr', null, ['Стратегия', 'Депозит', 'Капитал', 'Доходность', 'Просадка', 'В рынке'].map(t => h('th', null, t)))),
        h('tbody', null, rows)))),
    h('div', { class: 'muted', style: { fontSize: '13px', margin: '12px 4px' } },
      'У каждой стратегии свой тестовый счёт; риск — 1% депозита на сделку у всех, чтобы стратегии сравнивались при равном риске.'));
}

