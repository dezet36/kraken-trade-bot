/* Главная: сначала деньги — сколько, как изменилось, что сегодня, что
   требует внимания; дальше стратегии, позиции, торговые счета. */

import { h, section, row, stat, delta, pill, empty, notice, strategyBadge, strategyColor,
  strategyTitle, segmented } from '../ui.js';
import { money, moneyShort, signedMoney, signedPct, signedR, pct, num, price, tone, ago,
  plural, isNum } from '../format.js';
import { lineChart, sparkline, donut, bars } from '../charts.js';
import { strategyOrder, testTotals, totalSeries, sliceSeries, strategySeries, positionTrack,
  positionKey, dailyPnl, openFloating } from '../model.js';

const PERIODS = [
  { value: 7, label: '7 дней' },
  { value: 30, label: 'Месяц' },
  { value: 0, label: 'Всё время' },
];
let period = 0;

export const title = 'Главная';

export function render(store) {
  const data = store.data;
  if (!data) return loading();
  const totals = testTotals(data);
  return h('div', null,
    head(data),
    h('div', { class: 'grid grid-hero fade-in' }, capitalCard(data, totals), todayCard(data)),
    attention(data, store),
    h('div', { class: 'grid grid-2 section fade-in' }, allocationCard(data, totals), dailyCard(data)),
    strategiesSection(data, totals),
    h('div', { class: 'grid grid-2 section' }, positionsCard(data), accountsCard(store.accounts)),
  );
}

function loading() {
  return h('div', null,
    h('div', { class: 'page-head' }, h('h1', { class: 'page-title' }, title)),
    h('div', { class: 'grid grid-hero' },
      h('div', { class: 'card skeleton', style: { height: '340px' } }),
      h('div', { class: 'card skeleton', style: { height: '340px' } })));
}

function head(data) {
  const mode = data.paper ? 'Тест на бумаге' : 'Торговля';
  const exch = (data.exchange || '').toUpperCase();
  return h('div', { class: 'page-head' },
    h('div', null,
      h('h1', { class: 'page-title' }, title),
      h('div', { class: 'page-sub' }, `${mode} · ${exch} · данные ${ago(data.generated)}`)),
    data.regime ? pill(`Рынок: ${data.regime.name}`, data.regime.reduced ? 'warn' : '') : null);
}

/* ── Капитал теста ─────────────────────────────────────────────────── */
function capitalCard(data, totals) {
  const all = totalSeries(data);
  const pts = sliceSeries(all, period);
  const base = period ? (pts[0] && pts[0].v) : totals.invested;
  const end = totals.equity;
  const change = isNum(base) ? end - base : null;
  const changePct = isNum(base) && base ? (end / base - 1) * 100 : null;
  const chartBox = h('div');
  const draw = () => chartBox.replaceChildren(lineChart(sliceSeries(all, period), {
    height: 240, step: true, color: 'auto', baseline: period ? undefined : totals.invested,
    format: v => money(v), axisFormat: v => moneyShort(v), label: 'Капитал теста',
    emptyText: 'Сделок ещё нет — график появится после первых закрытий',
  }));
  draw();
  const changeBox = h('div', { style: { marginTop: '8px' } }, delta(change, changePct),
    h('span', { class: 'muted', style: { marginLeft: '8px', fontSize: '14px' } }, periodText()));
  return h('div', { class: 'card' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '12px', flexWrap: 'wrap' } },
      h('div', null,
        h('div', { class: 'card-title' }, 'Капитал теста стратегий'),
        h('div', { class: 'big' }, money(end)),
        changeBox),
      segmented(PERIODS, period, (v) => { period = v; refreshSelf(); }, 'Период')),
    h('div', { style: { marginTop: '18px' } }, chartBox),
    h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '6px' } },
      `Вложено ${money(totals.invested, 0)} · ${(data.directions || []).map(d => d.subtitle).join(' · ')}`));
}

function periodText() {
  return period === 7 ? 'за 7 дней' : period === 30 ? 'за месяц' : 'с начала теста';
}

let refreshSelf = () => {};
export function bindRefresh(fn) { refreshSelf = fn; }

/* ── Сегодня ───────────────────────────────────────────────────────── */
function todayCard(data) {
  const pf = data.portfolio || {};
  const open = (data.open_positions || []).length;
  const pending = (data.pending_orders || []).length;
  const floating = openFloating(data);
  return h('div', { class: 'card' },
    h('div', { class: 'card-title' }, 'Сегодня'),
    h('div', { class: 'big-sm ' + tone(pf.day_pnl) }, signedMoney(pf.day_pnl)),
    h('div', { class: 'muted', style: { fontSize: '13px', margin: '2px 0 14px' } },
      `${signedPct(pf.day_pct)} к депозиту`,
      pf.day_stopped ? ' · дневной предел достигнут' : ''),
    h('div', { class: 'rows' },
      row('Открытые позиции', num(open), { hint: pending ? `и ${pending} ${plural(pending, 'заявка', 'заявки', 'заявок')} ждут входа` : null }),
      row('Плавающий результат', signedMoney(floating), { tone: tone(floating) }),
      row('Риск в рынке', `${money(pf.risk_usd, 0)}`, { hint: isNum(pf.risk_pct) ? `${pct(pf.risk_pct, 2)} депозита` : null }),
      row('Дневной предел убытка', pf.day_limit ? pct(pf.day_limit, 1) : 'не задан', { hint: 'по портфелю теста' }),
    ));
}

/* ── Внимание ──────────────────────────────────────────────────────── */
function attention(data, store) {
  const items = (data.attention || []).slice();
  if (store.error) items.unshift({ level: 'bad', text: 'Нет связи с сервером', detail: store.error });
  if (data.status && data.status.state && data.status.state !== 'running') {
    items.unshift({ level: 'bad', text: `Бот: ${data.status.state}`, detail: data.status.detail });
  }
  if (!items.length) return null;
  return section('Требует внимания', null, h('div', null, items.map(i => notice(i.level, i.text, i.detail))));
}

/* ── Распределение капитала ────────────────────────────────────────── */
function allocationCard(data, totals) {
  const codes = strategyOrder(data);
  const items = codes.map(c => ({
    code: c, label: strategyTitle(c), color: strategyColor(c),
    value: (totals.byStrategy[c] && totals.byStrategy[c].equity) ?? (data.strategies[c] || {}).equity ?? 0,
  }));
  return h('div', { class: 'card' },
    h('div', { class: 'card-title' }, 'Капитал по стратегиям'),
    h('div', { class: 'donut-wrap' },
      donut(items, { value: moneyShort(totals.equity), label: `${codes.length} ${plural(codes.length, 'стратегия', 'стратегии', 'стратегий')}` }),
      h('div', { class: 'donut-legend' }, items.map(i => h('div', null,
        h('span', { class: 'dot', style: { background: i.color } }), i.label,
        h('span', { class: 'v' }, money(i.value, 0)))))));
}

/* ── Результат по дням ─────────────────────────────────────────────── */
function dailyCard(data) {
  const days = dailyPnl(data, 30);
  const sum = days.reduce((a, d) => a + d.v, 0);
  const plus = days.filter(d => d.v > 0).length, minus = days.filter(d => d.v < 0).length;
  return h('div', { class: 'card' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: '8px' } },
      h('div', { class: 'card-title' }, 'Закрытые сделки по дням, 30 дней'),
      h('div', { class: 'row-value ' + tone(sum) }, signedMoney(sum))),
    bars(days, { height: 168, format: v => signedMoney(v, 0), label: 'Результат по дням' }),
    h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '6px' } },
      `${plus} ${plural(plus, 'день', 'дня', 'дней')} в плюсе · ${minus} в минусе`));
}

/* ── Стратегии ─────────────────────────────────────────────────────── */
function strategiesSection(data, totals) {
  const codes = strategyOrder(data);
  return section('Стратегии', { href: '#/strategies', text: 'Все подробности' },
    h('div', { class: 'grid grid-cards' }, codes.map(c => strategyCard(data, totals, c))));
}

function strategyCard(data, totals, code) {
  const s = data.strategies[code] || {};
  const d = totals.byStrategy[code] || {};
  const color = strategyColor(code);
  const series = strategySeries(data, code);
  const openN = (s.open || 0) + (s.pending || 0);
  const off = d.enabled === false;
  return h('a', { class: 'card strat-card' + (off ? ' off' : ''), href: `#/strategies/${code}` },
    h('div', { class: 'strat-head' },
      strategyBadge(code),
      h('div', { style: { minWidth: 0 } },
        h('div', { class: 'strat-name' }, strategyTitle(code)),
        h('div', { class: 'strat-sub' }, off ? 'выключена' : openN ? `в рынке: ${openN}` : 'ждёт сетап')),
      h('div', { style: { marginLeft: 'auto' } }, pill(signedPct(s.return_pct), tone(s.return_pct)))),
    h('div', { class: 'strat-money' },
      h('div', { class: 'big-sm' }, money(s.equity ?? d.equity, 0)),
      h('div', { class: 'row-value ' + tone(s.pnl) }, signedMoney(s.pnl, 0))),
    sparkline(series, color),
    h('div', { class: 'stats four' },
      stat('Сделок', num(s.trades)),
      stat('Побед', isNum(s.winrate) ? pct(s.winrate, 0) : '—'),
      stat('На сделку', signedR(s.expectancy_r), tone(s.expectancy_r)),
      stat('Просадка', isNum(s.max_dd_pct) ? pct(s.max_dd_pct, 1) : '—')));
}

/* ── Открытые позиции ──────────────────────────────────────────────── */
function positionsCard(data) {
  const list = (data.open_positions || []).slice()
    .sort((a, b) => Math.abs(b.unrealised || 0) - Math.abs(a.unrealised || 0)).slice(0, 6);
  const total = (data.open_positions || []).length;
  return h('div', { class: 'card' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' } },
      h('div', { class: 'card-title' }, `Открытые позиции · ${total}`),
      total > list.length ? h('a', { href: '#/trades/open', style: { fontSize: '13px' } }, 'Все') : null),
    list.length ? h('div', { class: 'plist' }, list.map(positionItem))
      : empty('Позиций нет', 'Стратегии ждут своих сетапов'));
}

function positionItem(p) {
  const long = p.direction === 'LONG';
  const tr = positionTrack(p);
  const color = strategyColor(p.strategy);
  let track = null;
  if (tr) {
    const a = Math.min(tr.entry, tr.price), b = Math.max(tr.entry, tr.price);
    track = h('div', { class: 'track', title: 'стоп ← вход → цель' },
      h('div', { class: 'track-fill', style: { left: (a * 100) + '%', width: Math.max(0.5, (b - a) * 100) + '%',
        background: tr.price >= tr.entry ? 'var(--up)' : 'var(--down)' } }),
      h('div', { class: 'track-entry', style: { left: `calc(${tr.entry * 100}% - 1px)` } }));
  }
  const ends = tr ? h('div', { class: 'track-ends' }, h('span', null, `стоп ${price(p.stop)}`), h('span', null, `цель ${price((p.targets || [])[0] ?? p.tp1)}`)) : null;
  return h('a', { class: 'pitem', href: `#/trades/p/${encodeURIComponent(positionKey(p))}` },
    h('span', { class: 'dot', style: { background: color, width: '10px', height: '10px' } }),
    h('div', { style: { minWidth: 0 } },
      h('div', null, h('span', { class: 'pitem-pair' }, p.pair.replace(/USDT$/, '')), ' ',
        h('span', { class: 'side ' + (long ? 'long' : 'short') }, long ? 'ЛОНГ' : 'ШОРТ')),
      h('div', { class: 'pitem-meta' }, `${strategyTitle(p.strategy)} · вход ${price(p.entry)} · сейчас ${price(p.price)}`)),
    h('div', { class: 'pitem-val ' + tone(p.unrealised) }, signedMoney(p.unrealised),
      h('small', null, signedR(p.unrealised_r))),
    track, ends);
}

/* ── Торговые счета ────────────────────────────────────────────────── */
function accountsCard(acc) {
  const list = (acc && acc.accounts) || [];
  const books = (acc && acc.books) || {};
  const card = h('div', { class: 'card' }, h('div', { class: 'card-title' }, 'Торговые счета'));
  if (!acc) { card.append(h('div', { class: 'skeleton', style: { height: '120px' } })); return card; }
  if (!list.length) {
    card.append(empty('Счетов пока нет',
      'Биржевой счёт или проп заводится в разделе «Счета»: правила, стратегии и ключи.'));
    card.append(h('div', { style: { textAlign: 'center' } }, h('a', { href: '#/accounts' }, 'Открыть «Счета»')));
    return card;
  }
  card.append(h('div', { class: 'rows' }, list.map(a => {
    const b = books[a.id] || {};
    const kind = a.kind === 'exchange' ? `${(a.exchange || '').toUpperCase()} · ${a.mode === 'live' ? 'реальные деньги' : 'демо'}` : a.kind_title;
    return row(h('span', null, a.name || a.id, h('small', null, `${kind} · ${b.status_text || (a.enabled ? 'включён' : 'выключен')}`)),
      h('span', null, money(b.equity, 0), h('small', { class: tone(b.return_pct), style: { display: 'block', fontWeight: 500, fontSize: '12px' } }, signedPct(b.return_pct))));
  })));
  return card;
}
