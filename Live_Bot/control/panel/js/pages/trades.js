/* Сделки: открытые позиции, заявки, история — и страница каждой сделки со
   свечным графиком (зона сетапа, вход, стоп, цели, отметки входа и выхода).

   Адреса: #/trades/open, #/trades/orders, #/trades/history[/СТРАТЕГИЯ],
   #/trades/h/<id сделки>, #/trades/p/<СТРАТЕГИЯ-ПАРА>, #/trades/o/<СТРАТЕГИЯ-ПАРА>. */

import { h, row, pill, empty, icon, strategyBadge, strategyColor, strategyTitle, segmented } from '../ui.js';
import { money, signedMoney, signedR, signedPct, pct, num, price, tone, ago, dateTime, day, hours,
  isNum, toDate, plural } from '../format.js';
import { candleChart, lineChart, bars, calendar } from '../charts.js';
import { strategyOrder, closedTrades, positionTrack, positionKey, totalSeries, strategySeries, honestStats } from '../model.js';
import { cachedJSON } from '../api.js';
import { tradeRow, sliceTable } from './strategies.js';

export const title = 'Сделки';

const PERIODS = [
  { value: 7, label: '7 дней' },
  { value: 30, label: 'Месяц' },
  { value: 0, label: 'Всё время' },
];
let period = 30;
let rerender = () => {};
export function bindRefresh(fn) { rerender = fn; }

export function render(store, args) {
  const data = store.data;
  if (!data) return h('div', null, h('h1', { class: 'page-title' }, title));
  const [tab, key] = args;
  if (tab === 'h') return closedDetail(data, decodeURIComponent(key || ''));
  if (tab === 'p') return openDetail(data, decodeURIComponent(key || ''));
  if (tab === 'o') return orderDetail(data, decodeURIComponent(key || ''));
  return lists(data, ['open', 'orders', 'history', 'analytics'].includes(tab) ? tab : 'open', (key || '').toUpperCase());
}

/* ══ Списки ═════════════════════════════════════════════════════════ */
function lists(data, tab, filter) {
  const open = (data.open_positions || []).filter(p => !filter || p.strategy === filter);
  const orders = (data.pending_orders || []).filter(p => !filter || p.strategy === filter);
  const closed = closedTrades(data, filter || null, period || null);
  const floating = open.reduce((a, p) => a + (isNum(p.unrealised) ? p.unrealised : 0), 0);
  const closedSum = closed.reduce((a, t) => a + (t.pnl || 0), 0);
  const go = (t) => { location.hash = `#/trades/${t}${filter ? '/' + filter : ''}`; };

  const select = h('select', { class: 'select', 'aria-label': 'Стратегия',
    onchange: (e) => { location.hash = `#/trades/${tab}${e.target.value ? '/' + e.target.value : ''}`; } },
    h('option', { value: '' }, 'Все стратегии'),
    strategyOrder(data).map(c => h('option', { value: c, selected: c === filter }, strategyTitle(c))));

  let body;
  if (tab === 'open') {
    body = open.length ? h('div', { class: 'card' }, h('div', { class: 'list' }, sortOpen(open).map(openRow)))
      : h('div', { class: 'card' }, empty('Открытых позиций нет', filter ? 'У этой стратегии сейчас нет позиций' : 'Стратегии ждут сетапы'));
  } else if (tab === 'orders') {
    body = orders.length ? h('div', { class: 'card' }, h('div', { class: 'list' }, orders.map(orderRow)))
      : h('div', { class: 'card' }, empty('Заявок нет', 'Лимитная заявка появляется, когда сетап найден, а цена ещё не дошла до входа'));
  } else if (tab === 'analytics') {
    body = analytics(data, filter, closed);
  } else {
    body = h('div', null,
      h('div', { class: 'toolbar', style: { marginBottom: '14px' } },
        segmented(PERIODS, period, (v) => { period = v; rerender(); }, 'Период')),
      honestKpis(closed, closedSum),
      h('div', { class: 'card' }, closed.length ? historyList(closed) : empty('Сделок за период нет', 'Выберите период длиннее')));
  }

  return h('div', null,
    h('div', { class: 'page-head' },
      h('div', null, h('h1', { class: 'page-title' }, title),
        h('div', { class: 'page-sub' }, `Открыто ${open.length} · плавающий результат `,
          h('span', { class: tone(floating) }, signedMoney(floating)))),
      select),
    h('div', { class: 'toolbar', style: { marginBottom: '18px' } },
      segmented([
        { value: 'open', label: `Открытые · ${open.length}` },
        { value: 'orders', label: `Заявки · ${orders.length}` },
        { value: 'history', label: 'История' },
        { value: 'analytics', label: 'Аналитика' },
      ], tab, go, 'Вид')),
    h('div', { class: 'fade-in' }, body));
}

/* ══ Аналитика ══════════════════════════════════════════════════════ */
function groupBy(trades, keyOf) {
  const m = new Map();
  for (const t of trades) {
    const k = keyOf(t);
    const g = m.get(k) || { name: k, trades: 0, wins: 0, total_r: 0 };
    g.trades += 1;
    g.wins += (t.pnl || 0) > 0 ? 1 : 0;
    g.total_r += t.pnl_r || 0;
    m.set(k, g);
  }
  return [...m.values()].map(g => ({ ...g, win_rate: g.trades ? g.wins / g.trades : null, total_r: Math.round(g.total_r * 100) / 100 }));
}

function dailyOf(trades, days = 84) {
  const from = new Date();
  from.setHours(0, 0, 0, 0);
  from.setDate(from.getDate() - (days - 1));
  const buckets = new Map();
  for (let i = 0; i < days; i++) {
    const d = new Date(from);
    d.setDate(from.getDate() + i);
    buckets.set(d.toDateString(), { t: d.getTime(), v: 0 });
  }
  for (const t of trades) {
    const d = toDate(t.closed);
    const b = d && buckets.get(d.toDateString());
    if (b) b.v += t.pnl || 0;
  }
  return [...buckets.values()];
}

const R_BINS = [-Infinity, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3, Infinity];
function rLabel(lo, hi) {
  const f = v => num(v, 1);
  if (lo === -Infinity) return `< ${f(hi)}R`;
  if (hi === Infinity) return `≥ ${f(lo)}R`;
  return `${f(lo)}…${f(hi)}R`;
}

/** Просадка от пика в %: насколько капитал ниже своего максимума к моменту. */
function underwater(points) {
  let peak = -Infinity;
  return points.map(p => {
    peak = Math.max(peak, p.v);
    return { t: p.t, v: peak > 0 ? (p.v / peak - 1) * 100 : 0 };
  });
}

function analytics(data, filter, closed) {
  const all = closedTrades(data, filter || null, null);
  const bins = R_BINS.slice(0, -1).map((lo, i) => {
    const hi = R_BINS[i + 1];
    const n = closed.filter(t => isNum(t.pnl_r) && t.pnl_r >= lo && t.pnl_r < hi).length;
    return { label: rLabel(lo, hi), v: n, tip: `${n} ${plural(n, 'сделка', 'сделки', 'сделок')}`, color: hi <= 0 ? 'var(--down)' : 'var(--up)' };
  });
  const dd = underwater(filter ? strategySeries(data, filter) : totalSeries(data));
  const maxDd = Math.min(0, ...dd.map(p => p.v));
  const pairs = groupBy(closed, t => t.pair).sort((a, b) => b.total_r - a.total_r);
  const shadow = (data.shadow && data.shadow.closed) || {};
  const shadowRows = Object.entries(shadow).filter(([s]) => !filter || s === filter)
    .flatMap(([s, gates]) => Object.entries(gates).map(([gate, g]) => ({ s, gate, ...g })));
  const days = dailyOf(all, 140);
  const exits = groupBy(closed, t => t.reason_ru || t.reason || '—').sort((a, b) => b.trades - a.trades);
  return h('div', { class: 'grid', style: { gap: '18px' } },
    h('div', { class: 'toolbar' }, segmented(PERIODS, period, (v) => { period = v; rerender(); }, 'Период'),
      h('span', { class: 'muted', style: { fontSize: '13px' } },
        `${closed.length} ${plural(closed.length, 'сделка', 'сделки', 'сделок')} за период${filter ? ' · ' + strategyTitle(filter) : ''}`)),
    h('div', { class: 'grid grid-2' },
      h('div', { class: 'card' },
        h('div', { class: 'card-title' }, 'Результат по дням, 20 недель'),
        calendar(days, v => signedMoney(v, 0)),
        h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '10px' } },
          `Зелёный — день в плюсе, красный — в минусе; чем ярче, тем больше. Итого ${signedMoney(days.reduce((a, d) => a + d.v, 0), 0)}.`)),
      h('div', { class: 'card' },
        h('div', { class: 'card-title' }, 'Распределение результата, R'),
        bars(bins, { height: 200, format: v => String(Math.round(v)), label: 'Распределение результата' }),
        h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '8px' } }, 'Сколько сделок закрылось с каким результатом в долях риска.'))),
    h('div', { class: 'card' },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' } },
        h('div', { class: 'card-title' }, 'Под водой: насколько капитал ниже своего пика'),
        h('div', { class: 'row-value down' }, `макс. ${signedPct(maxDd, 1)}`)),
      lineChart(dd, { height: 180, step: true, color: 'var(--down)', format: v => signedPct(v, 2), axisFormat: v => signedPct(v, 0), label: 'Просадка от пика' })),
    h('div', { class: 'grid grid-2' },
      sliceTable('Чем закончились', exits) || h('div', { class: 'card' }, empty('Сделок нет', '')),
      sliceTable('Лучшие и худшие пары', pairs.length > 12 ? pairs.slice(0, 6).concat(pairs.slice(-6)) : pairs)
        || h('div', { class: 'card' }, empty('Сделок нет', ''))),
    h('div', { class: 'card' },
      h('div', { class: 'card-title' }, 'Не открыто из-за пределов — и чем бы кончилось'),
      shadowRows.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
        h('thead', null, h('tr', null, ['Стратегия', 'Причина', 'Отказов', 'Вошли бы', 'В плюс', 'В минус', 'Итог', 'Не дошли'].map(t => h('th', null, t)))),
        h('tbody', null, shadowRows.map(r => h('tr', null,
          h('td', null, strategyTitle(r.s)), h('td', null, r.gate), h('td', null, num(r.n)), h('td', null, num(r.entered)),
          h('td', { class: 'up' }, num(r.wins)), h('td', { class: 'down' }, num(r.losses)),
          h('td', { class: tone(r.sum_r) }, signedR(r.sum_r, 2)), h('td', null, num(r.missed)))))))
        : empty('Отказов из-за пределов нет', ''),
      h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '10px' } },
        `Тень исполнения: сетап, отсеянный пределом, ведётся «как если бы» — минус в итоге значит, что предел сберёг деньги. Сейчас в тени ${num(((data.shadow || {}).active || []).length)}.`)));
}

/** Показатели с погрешностью (model.honestStats): интервал, накрывающий
    ноль, подписан «не отличить от 0» и не красится — прибыли не доказано. */
export function honestKpis(trades, pnlSum) {
  const st = honestStats(trades);
  return h('div', { class: 'kpis', style: { marginBottom: '18px' } },
    kpi('Сделок', num(trades.length), st.skipped
      ? `${st.wins} в плюс · ${st.losses} в минус · без ${st.skipped} с дырой в данных`
      : `${st.wins} в плюс · ${st.losses} в минус`),
    kpi('Итог', signedMoney(pnlSum, 0), signedR(st.sumR, 1), tone(pnlSum)),
    kpi('Побед', isNum(st.winrate) ? pct(st.winrate, 0) : '—', isNum(st.wrCI) ? `± ${num(st.wrCI, 0)} п.п.` : null),
    kpi('На сделку', isNum(st.expectancy) ? signedR(st.expectancy) : '—',
      isNum(st.expCI) ? `± ${num(st.expCI, 2)}R${st.noise ? ' · не отличить от 0' : ''}` : 'погрешность — от двух сделок',
      st.noise ? '' : tone(st.expectancy)),
    kpi('Издержки', isNum(st.costR) ? `${num(st.costR, 3)} R/сд` : '—',
      isNum(st.gross) ? `без них было бы ${signedR(st.gross)} на сделку` : 'комиссии и фандинг в долях риска'));
}

function kpi(label, value, hint, cls = '') {
  return h('div', { class: 'kpi' }, h('div', { class: 'stat-label' }, label),
    h('div', { class: 'kpi-v ' + cls }, value), hint ? h('small', null, hint) : null);
}

function sortOpen(list) {
  return list.slice().sort((a, b) => (toDate(b.opened)?.getTime() || 0) - (toDate(a.opened)?.getTime() || 0));
}

function sideBadge(direction) {
  const long = direction === 'LONG';
  return h('span', { class: 'side ' + (long ? 'long' : 'short') }, long ? 'ЛОНГ' : 'ШОРТ');
}

function track(p) {
  const tr = positionTrack(p);
  if (!tr) return null;
  const a = Math.min(tr.entry, tr.price), b = Math.max(tr.entry, tr.price);
  return [
    h('div', { class: 'track' },
      h('div', { class: 'track-fill', style: { left: `${a * 100}%`, width: `${Math.max(0.5, (b - a) * 100)}%`,
        background: tr.price >= tr.entry ? 'var(--up)' : 'var(--down)' } }),
      h('div', { class: 'track-entry', style: { left: `calc(${tr.entry * 100}% - 1px)` } })),
    h('div', { class: 'track-ends' }, h('span', null, `стоп ${price(p.stop)}`), h('span', null, `цель ${price((p.targets || [])[0] ?? p.tp1)}`)),
  ];
}

function openRow(p) {
  return h('a', { class: 'list-item', href: `#/trades/p/${encodeURIComponent(positionKey(p))}` },
    sideBadge(p.direction),
    h('div', { style: { minWidth: 0 } },
      h('div', { class: 'title' }, p.pair.replace(/USDT$/, ''),
        p.breakeven ? h('span', { class: 'pill', style: { marginLeft: '8px' } }, 'стоп в безубытке') : null,
        p.tp_hit ? h('span', { class: 'pill up', style: { marginLeft: '6px' } }, `цель ${p.tp_hit} взята`) : null),
      h('div', { class: 'meta' }, `${strategyTitle(p.strategy)} · открыта ${ago(p.opened)} · вход ${price(p.entry)} → ${price(p.price)}`)),
    h('div', { class: 'val ' + tone(p.unrealised) }, signedMoney(p.unrealised), h('small', null, signedR(p.unrealised_r))),
    track(p));
}

function orderRow(o) {
  return h('a', { class: 'list-item', href: `#/trades/o/${encodeURIComponent(positionKey(o))}` },
    sideBadge(o.direction),
    h('div', { style: { minWidth: 0 } },
      h('div', { class: 'title' }, o.pair.replace(/USDT$/, '')),
      h('div', { class: 'meta' }, `${strategyTitle(o.strategy)} · вход ${price(o.entry)} · сейчас ${price(o.price)}`
        + (isNum(o.expires_in_min) ? ` · снимется через ${hours(o.expires_in_min / 60)}` : ''))),
    h('div', { class: 'val' }, isNum(o.distance_pct) ? pct(o.distance_pct, 2) : '—', h('small', { class: 'muted' }, 'до входа')));
}

function historyList(trades) {
  const out = [];
  let lastDay = '';
  for (const t of trades) {
    const d = day(t.closed);
    if (d !== lastDay) {
      const sum = trades.filter(x => day(x.closed) === d).reduce((a, x) => a + (x.pnl || 0), 0);
      out.push(h('div', { class: 'day-head', style: { display: 'flex', justifyContent: 'space-between' } },
        h('span', null, d), h('span', { class: tone(sum) }, signedMoney(sum))));
      lastDay = d;
    }
    out.push(tradeRow(t));
  }
  return h('div', { class: 'list' }, out);
}

/* ══ Страница сделки ════════════════════════════════════════════════ */
function back(href, text) { return h('a', { class: 'back', href }, icon('chevron'), text); }

function header(t, sub, right) {
  return h('div', { class: 'page-head' },
    h('div', null,
      h('div', { class: 'title-row' }, strategyBadge(t.strategy),
        h('h1', { class: 'page-title' }, t.pair.replace(/USDT$/, '')), sideBadge(t.direction)),
      h('div', { class: 'page-sub' }, sub)),
    right);
}

function chartCard(t, url, extra = {}) {
  const res = cachedJSON(url, extra.ttl || 300000);
  const lines = [];
  const geo = t.geometry || {};
  (geo.lines || []).forEach(l => lines.push({ price: l.price, label: l.label, color: 'var(--text-3)', dash: true }));
  lines.push({ price: t.entry, label: 'вход', color: 'var(--accent)', core: true });
  if (isNum(t.stop)) lines.push({ price: t.stop, label: 'стоп', color: 'var(--down)', dash: true, core: true });
  const targets = (t.targets && t.targets.length ? t.targets : [t.tp1, t.tp2]).filter(v => isNum(v) && v > 0);
  targets.forEach((v, i) => lines.push({ price: v, label: targets.length > 1 ? `цель ${i + 1}` : 'цель', color: 'var(--up)', dash: true }));
  if (extra.exit) lines.push({ price: extra.exit, label: extra.exitLabel || 'выход', color: extra.exitColor || 'var(--warn)', core: true });
  const bands = (geo.bands || []).map(b => ({ ...b, color: strategyColor(t.strategy) }));
  const marks = [];
  if (toDate(t.opened) && extra.markEntry !== false) marks.push({ t: toDate(t.opened).getTime(), price: t.entry, color: 'var(--accent)', label: 'вход', up: t.direction === 'SHORT' });
  if (extra.exit && toDate(t.closed) && !extra.exitLabel) marks.push({ t: toDate(t.closed).getTime(), price: extra.exit, color: extra.exitColor, label: 'выход', up: t.direction === 'LONG' });
  let body;
  if (res.value) {
    body = candleChart(res.value.candles, { height: 430, lines, bands, marks, priceFormat: price, label: `График ${t.pair}` });
  } else if (res.error) {
    body = empty('Свечи не загрузились', res.error);
  } else {
    body = h('div', { class: 'skeleton', style: { height: '430px' } });
  }
  return h('div', { class: 'card fade-in' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '10px' } },
      h('div', { class: 'card-title', style: { margin: 0 } }, 'График'),
      res.value ? h('span', { class: 'muted', style: { fontSize: '12px' } }, `свечи ${res.value.timeframe}`) : null),
    body);
}

function candlesUrl(t, to) {
  return '/api/candles?pair=' + encodeURIComponent(t.pair) + '&from=' + encodeURIComponent(t.opened || '')
    + '&to=' + encodeURIComponent(to || '') + '&setup=' + encodeURIComponent((t.geometry && t.geometry.from) || '');
}

function whyCard(t) {
  const confirmed = t.confirmed || [], missing = t.missing || [];
  if (!t.why && !confirmed.length && !missing.length) return null;
  return h('div', { class: 'card' },
    h('div', { class: 'card-title' }, 'Почему вошли'),
    t.why ? h('div', { class: 'why' }, t.why) : null,
    confirmed.length || missing.length ? h('div', { class: 'tags', style: { marginTop: '12px' } },
      confirmed.map(c => pill('✓ ' + c, 'up')), missing.map(c => pill('— ' + c))) : null);
}

function closedDetail(data, id) {
  const t = (data.closed || []).find(x => String(x.id) === id);
  if (!t) return h('div', null, back('#/trades/history', 'Сделки'), empty('Сделка не найдена', 'Возможно, она старше списка, который отдаёт сервер'));
  const exitColor = (t.pnl || 0) >= 0 ? 'var(--up)' : 'var(--down)';
  const risk = t.risk;
  return h('div', null,
    back('#/trades/history', 'История сделок'),
    header(t, `${strategyTitle(t.strategy)} · ${dateTime(t.opened)} → ${dateTime(t.closed)} · ${t.reason_ru || t.reason || ''}`,
      pill(t.result === 'WIN' || (t.pnl || 0) > 0 ? 'прибыль' : 'убыток', tone(t.pnl))),
    h('div', { class: 'grid grid-hero' },
      chartCard(t, candlesUrl(t, t.closed), { exit: t.exit, exitColor, ttl: 3600000 }),
      h('div', { class: 'card' },
        h('div', { class: 'card-title' }, 'Результат'),
        h('div', { class: 'big ' + tone(t.pnl) }, signedMoney(t.pnl)),
        h('div', { class: 'muted', style: { margin: '4px 0 14px' } }, `${signedR(t.pnl_r)} · ${signedPct(t.pnl_pct)} к депозиту`),
        h('div', { class: 'rows' },
          row('Вход', price(t.entry)), row('Выход', price(t.exit)),
          row('Стоп', price(t.stop)), row('Цель', price(t.tp1)),
          row('План R:R', isNum(t.rr) ? `1 : ${num(t.rr, 2)}` : '—'),
          row('Риск', money(risk)),
          row('Комиссии и фандинг', signedMoney(-((t.fees || 0) + (t.funding || 0))), { hint: isNum(t.cost_share) ? `${pct(t.cost_share, 1)} от риска` : null }),
          row('В сделке', hours((t.duration_min || 0) / 60)),
          row('Лучший ход / худший', `${signedR(t.mfe_r, 2)} / ${signedR(t.mae_r, 2)}`)))),
    h('div', { class: 'section' }, whyCard(t)));
}

function openDetail(data, key) {
  const p = (data.open_positions || []).find(x => positionKey(x) === key);
  if (!p) return h('div', null, back('#/trades/open', 'Открытые позиции'), empty('Позиция закрыта или не найдена', 'Итог — в истории сделок'));
  return h('div', null,
    back('#/trades/open', 'Открытые позиции'),
    header(p, `${strategyTitle(p.strategy)} · открыта ${dateTime(p.opened)} (${ago(p.opened)})`,
      p.breakeven ? pill('стоп в безубытке', 'accent') : null),
    h('div', { class: 'grid grid-hero' },
      chartCard({ ...p, closed: null }, candlesUrl(p, ''), { exit: p.price, exitLabel: 'сейчас', exitColor: 'var(--warn)', ttl: 60000 }),
      h('div', { class: 'card' },
        h('div', { class: 'card-title' }, 'Сейчас'),
        h('div', { class: 'big ' + tone(p.unrealised) }, signedMoney(p.unrealised)),
        h('div', { class: 'muted', style: { margin: '4px 0 14px' } }, `${signedR(p.unrealised_r)} · цена ${price(p.price)} на ${dateTime(p.price_at)}`),
        h('div', { class: 'rows' },
          row('Вход', price(p.entry)), row('Стоп', price(p.stop), { hint: p.initial_stop !== p.stop ? `изначально ${price(p.initial_stop)}` : null }),
          row('Цели', (p.targets || []).map(price).join(' · ') || price(p.tp1), { hint: p.tp_hit ? `взято: ${p.tp_hit}` : null }),
          row('План R:R', isNum(p.rr) ? `1 : ${num(p.rr, 2)}` : '—'),
          row('Риск', money(p.risk)), row('Осталось позиции', pct(p.size_left_pct, 0)),
          row('Зафиксировано', signedMoney(p.realized)), row('Издержки', signedMoney(-(p.costs || 0))),
          row('Лучший ход', signedR(p.mfe_r, 2)),
          row('Выход по времени', isNum(p.time_left_h) ? `через ${hours(p.time_left_h)}` : 'нет')))),
    h('div', { class: 'section' }, whyCard(p)));
}

function orderDetail(data, key) {
  const o = (data.pending_orders || []).find(x => positionKey(x) === key);
  if (!o) return h('div', null, back('#/trades/orders', 'Заявки'), empty('Заявки уже нет', 'Она налилась (см. открытые позиции) или снята'));
  return h('div', null,
    back('#/trades/orders', 'Заявки'),
    header(o, `${strategyTitle(o.strategy)} · ждёт ${hours((o.waiting_min || 0) / 60)}`
      + (isNum(o.expires_in_min) ? ` · снимется через ${hours(o.expires_in_min / 60)}` : ''), null),
    h('div', { class: 'grid grid-hero' },
      chartCard(o, candlesUrl(o, ''), { exit: o.price, exitLabel: 'сейчас', exitColor: 'var(--warn)', markEntry: false, ttl: 60000 }),
      h('div', { class: 'card' },
        h('div', { class: 'card-title' }, 'До входа'),
        h('div', { class: 'big' }, isNum(o.distance_pct) ? pct(o.distance_pct, 2) : '—'),
        h('div', { class: 'rows', style: { marginTop: '14px' } },
          row('Вход', price(o.entry)), row('Сейчас', price(o.price)), row('Стоп', price(o.stop)),
          row('Цели', (o.targets || []).map(price).join(' · ') || price(o.tp1)),
          row('План R:R', isNum(o.rr) ? `1 : ${num(o.rr, 2)}` : '—'), row('Риск', money(o.risk)),
          isNum(o.invalidation) ? row('Отмена сетапа', price(o.invalidation), { hint: 'цена за этим уровнем снимает заявку' }) : null))),
    h('div', { class: 'section' }, whyCard(o)));
}

