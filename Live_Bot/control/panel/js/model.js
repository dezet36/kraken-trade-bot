/* Расчёты из ответа /api/data, общие для разделов. Числа берутся как есть —
   панель ничего не пересчитывает по-своему там, где сервер уже посчитал
   (баланс, доходность, просадка); здесь только сборка рядов и выборки. */

import { toDate, isNum } from './format.js';
import { strategyCodes } from './ui.js';

const DAY = 86400000;

/** Стратегии в порядке реестра, затем незнакомые. */
export function strategyOrder(data) {
  const known = strategyCodes();
  const present = Object.keys((data && data.strategies) || {});
  return known.filter(c => present.includes(c)).concat(present.filter(c => !known.includes(c)));
}

/** Сводка теста: сумма по направлениям (вложено, капитал, результат). */
export function testTotals(data) {
  const dirs = (data && data.directions) || [];
  const invested = dirs.reduce((a, d) => a + (d.invested || 0), 0);
  const equity = dirs.reduce((a, d) => a + (d.equity || 0), 0);
  const byStrategy = {};
  dirs.forEach(d => (d.strategies || []).forEach(s => { byStrategy[s.name] = s; }));
  return { invested, equity, pnl: equity - invested, pct: invested ? (equity / invested - 1) * 100 : null, byStrategy };
}

/** Ряд капитала одной стратегии: [{t, v}] от старта до «сейчас» (с открытыми). */
export function strategySeries(data, code) {
  const s = (data.strategies || {})[code] || {};
  const raw = ((data.equity || {})[code] || [])
    .map(([at, bal]) => ({ t: toDate(at)?.getTime(), v: bal }))
    .filter(p => isNum(p.t) && isNum(p.v));
  const start = s.start_balance;
  const out = [];
  const t0 = toDate(s.reset_at || data.forward_since)?.getTime();
  if (isNum(start)) {
    const first = raw.length ? raw[0].t : Date.now();
    out.push({ t: isNum(t0) && t0 < first ? t0 : first - DAY, v: start });
  }
  out.push(...raw);
  if (isNum(s.equity)) out.push({ t: Date.now(), v: s.equity, now: true });
  return out;
}

/** Общий капитал теста во времени: сумма стратегий ступенькой. */
export function totalSeries(data) {
  const codes = strategyOrder(data);
  const series = Object.fromEntries(codes.map(c => [c, strategySeries(data, c)]));
  const current = {};
  codes.forEach(c => { current[c] = series[c].length ? series[c][0].v : 0; });
  const events = [];
  codes.forEach(c => series[c].forEach((p, i) => { if (i > 0) events.push({ t: p.t, c, v: p.v, now: p.now }); }));
  events.sort((a, b) => a.t - b.t);
  const starts = codes.map(c => series[c][0]?.t).filter(isNum);
  const out = [];
  const sum = () => codes.reduce((a, c) => a + (current[c] || 0), 0);
  if (starts.length) out.push({ t: Math.min(...starts), v: sum() });
  let nowPoint = null;
  for (const e of events) {
    current[e.c] = e.v;
    if (e.now) { nowPoint = e.t; continue; }
    const last = out[out.length - 1];
    if (last && last.t === e.t) last.v = sum(); else out.push({ t: e.t, v: sum() });
  }
  if (nowPoint) out.push({ t: nowPoint, v: sum(), now: true });
  return out;
}

/** Срез ряда за последние days дней: с точкой-началом периода. days=null — весь. */
export function sliceSeries(points, days) {
  if (!points.length || !days) return points.slice();
  const from = Date.now() - days * DAY;
  let base = points[0].v;
  for (const p of points) { if (p.t <= from) base = p.v; else break; }
  const rest = points.filter(p => p.t > from);
  return [{ t: Math.max(from, points[0].t), v: base }, ...rest];
}

/** Где цена позиции между стопом (0) и первой целью (1); вход — тоже. */
export function positionTrack(p) {
  const target = (p.targets && p.targets[0]) ?? p.tp1;
  const { stop, entry, price } = p;
  if (![target, stop, entry, price].every(isNum) || target === stop) return null;
  const at = v => Math.min(1, Math.max(0, (v - stop) / (target - stop)));
  return { entry: at(entry), price: at(price) };
}

/** Результат закрытых сделок по дням (местное время): [{t, v, label}] за days дней. */
export function dailyPnl(data, days = 30) {
  const from = new Date(); from.setHours(0, 0, 0, 0); from.setDate(from.getDate() - (days - 1));
  const buckets = new Map();
  for (let i = 0; i < days; i++) {
    const d = new Date(from); d.setDate(from.getDate() + i);
    buckets.set(d.toDateString(), { t: d.getTime(), v: 0 });
  }
  for (const tr of (data.closed || [])) {
    const d = toDate(tr.closed);
    if (!d || !isNum(tr.pnl)) continue;
    const b = buckets.get(d.toDateString());
    if (b) b.v += tr.pnl;
  }
  return [...buckets.values()].map(b => ({ ...b, v: Math.round(b.v * 100) / 100 }));
}

/** Доходность стратегии в % от стартового депозита во времени. */
export function returnSeries(data, code) {
  const start = ((data.strategies || {})[code] || {}).start_balance;
  if (!isNum(start) || !start) return [];
  return strategySeries(data, code).map(p => ({ t: p.t, v: (p.v / start - 1) * 100 }));
}

/** Закрытые сделки (новые сверху), по стратегии и за days дней. */
export function closedTrades(data, code = null, days = null) {
  const from = days ? Date.now() - days * DAY : null;
  return (data.closed || [])
    .filter(t => (!code || t.strategy === code) && (!from || (toDate(t.closed)?.getTime() || 0) >= from))
    .sort((a, b) => (toDate(b.closed)?.getTime() || 0) - (toDate(a.closed)?.getTime() || 0));
}

/** Ключ открытой позиции или заявки в адресе: СТРАТЕГИЯ-ПАРА. */
export function positionKey(p) { return `${p.strategy}-${p.pair}`; }

export function openFloating(data) {
  return (data.open_positions || []).reduce((a, p) => a + (isNum(p.unrealised) ? p.unrealised : 0), 0);
}
