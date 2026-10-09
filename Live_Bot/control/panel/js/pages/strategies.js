/* Стратегии: сравнение всех и страница каждой. Аналитика — с сервера
   (/api/strategy_compare, /api/strategy_report, accounts/stats.py): панель
   ничего не пересчитывает, только показывает. */

import { h, section, row, stat, delta, pill, empty, icon, strategyBadge, strategyColor,
  strategyTitle, segmented, button, openSheet, rich } from '../ui.js';
import { GUIDE } from '../guide.js';
import { money, moneyShort, signedMoney, signedPct, signedR, pct, num, tone, ago, dateTime,
  hours, plural, isNum } from '../format.js';
import { lineChart, multiLine } from '../charts.js';
import { strategyOrder, strategySeries, returnSeries, sliceSeries, closedTrades, honestStats } from '../model.js';
import { cachedJSON } from '../api.js';

export const title = 'Стратегии';

const PERIODS = [
  { value: 7, label: '7 дней' },
  { value: 30, label: 'Месяц' },
  { value: 0, label: 'Всё время' },
];
let period = 0;
let rerender = () => {};
export function bindRefresh(fn) { rerender = fn; }

const periodSeg = () => segmented(PERIODS, period, (v) => { period = v; rerender(); }, 'Период');
const periodWord = () => (period === 7 ? 'за 7 дней' : period === 30 ? 'за месяц' : 'за всё время');
const daysParam = () => (period ? String(period) : 'all');

export function render(store, args) {
  if (!store.data) return h('div', null, h('h1', { class: 'page-title' }, title));
  const code = (args[0] || '').toUpperCase();
  return code && store.data.strategies[code] ? detail(store.data, code) : list(store.data);
}

/* ══ Все стратегии ══════════════════════════════════════════════════ */
function list(data) {
  const codes = strategyOrder(data);
  const series = codes.map(c => {
    let pts = returnSeries(data, c);
    if (period) {
      const sl = sliceSeries(pts, period);
      const base = sl.length ? sl[0].v : 0;
      pts = sl.map(p => ({ t: p.t, v: p.v - base }));
    }
    return { label: strategyTitle(c), color: strategyColor(c), points: pts };
  });
  const compare = cachedJSON(`/api/strategy_compare?days=${daysParam()}`);
  return h('div', null,
    h('div', { class: 'page-head' },
      h('div', null, h('h1', { class: 'page-title' }, title),
        h('div', { class: 'page-sub' }, 'У каждой стратегии свой тестовый счёт; риск — 1% депозита на сделку у всех')),
      periodSeg()),
    h('div', { class: 'card fade-in' },
      h('div', { class: 'card-title' }, `Доходность ${periodWord()}, % от депозита стратегии`),
      multiLine(series, { height: 280, format: v => signedPct(v, 1), label: 'Доходность стратегий' }),
      h('div', { class: 'legend', style: { marginTop: '12px' } },
        series.map(s => h('span', null, h('span', { class: 'dot', style: { background: s.color } }), s.label)))),
    section(`Показатели ${periodWord()}`, null, h('div', { class: 'card flush' },
      h('div', { class: 'table-wrap', style: { padding: '18px 22px 8px' } }, compareTable(data, compare)))));
}

function compareTable(data, compare) {
  if (!compare.value) {
    return compare.error ? empty('Статистика не загрузилась', compare.error)
      : h('div', { class: 'skeleton', style: { height: '260px' } });
  }
  const rows = compare.value.strategies || [];
  const order = strategyOrder(data);
  rows.sort((a, b) => order.indexOf(a.strategy) - order.indexOf(b.strategy));
  return h('table', { class: 'table' },
    h('thead', null, h('tr', null,
      ['Стратегия', 'Сделок', 'Побед', 'Итог', 'На сделку', 'PF', 'Просадка', 'Доходность'].map(t => h('th', null, t)))),
    h('tbody', null, rows.map(r => {
      const q = r.quality || {};
      const go = () => { location.hash = `#/strategies/${r.strategy}`; };
      return h('tr', { class: 'link', onclick: go, tabindex: 0, onkeydown: (e) => { if (e.key === 'Enter') go(); } },
        h('td', null, h('div', { class: 'cell-name' }, strategyBadge(r.strategy),
          h('div', null, strategyTitle(r.strategy), h('small', null, r.enabled === false ? 'выключена' : `сетапов ${num((r.funnel || {}).setups)}`)))),
        h('td', null, num(q.trades)),
        h('td', null, isNum(q.win_rate) ? pct(q.win_rate * 100, 0) : '—'),
        h('td', { class: tone(q.total_r) }, signedR(q.total_r, 1)),
        h('td', { class: tone(q.avg_r) }, signedR(q.avg_r)),
        h('td', null, isNum(q.profit_factor) ? num(q.profit_factor, 2) : '—'),
        h('td', null, isNum(q.max_dd_r) ? `${num(q.max_dd_r, 1)}R` : '—'),
        h('td', { class: tone(r.return_pct) }, signedPct(r.return_pct)));
    })));
}

/* ══ Одна стратегия ═════════════════════════════════════════════════ */
function detail(data, code) {
  const s = data.strategies[code] || {};
  const report = cachedJSON(`/api/strategy_report?strategy=${code}&days=${daysParam()}`);
  const rep = report.value;
  const acc = rep && rep.account;
  const settings = (acc && acc.settings) || {};
  const enabled = settings.enabled !== false;
  return h('div', null,
    h('a', { class: 'back', href: '#/strategies' }, icon('chevron'), 'Стратегии'),
    h('div', { class: 'page-head' },
      h('div', null,
        h('div', { class: 'title-row' }, strategyBadge(code), h('h1', { class: 'page-title' }, strategyTitle(code)),
          pill(enabled ? 'торгует' : 'выключена', enabled ? 'up' : ''),
          GUIDE[code] ? button('Как торгует', () => guideSheet(code), 'gray', true) : null),
        h('div', { class: 'page-sub' }, `Тестовый счёт ${money(s.start_balance, 0)} · риск ${pct(settings.risk_pct ?? 1, 1)} на сделку`
          + (settings.sides && settings.sides !== 'both' ? ` · только ${settings.sides === 'long' ? 'лонги' : 'шорты'}` : ''))),
      periodSeg()),
    equityCard(data, code, s),
    rep ? kpis(rep.quality || {}, honestStats(closedTrades(data, code, period || null))) : report.error ? h('div', { class: 'section' }, empty('Статистика не загрузилась', report.error))
      : h('div', { class: 'section skeleton', style: { height: '170px' } }),
    rep ? h('div', { class: 'grid grid-2 section' }, funnelCard(rep.funnel || {}), notTradedCard(rep.not_traded || {})) : null,
    rep ? breakdowns(rep) : null,
    h('div', { class: 'grid grid-2 section' }, scanCard(data, code), accountCard(acc, s)),
    recentTrades(data, code));
}

/** Лист «Как торгует»: таймфрейм, шаги, вывод замеров (js/guide.js). */
function guideSheet(code) {
  const g = GUIDE[code];
  openSheet(strategyTitle(code), () => h('div', null,
    h('div', { class: 'tags', style: { marginBottom: '12px' } }, pill(g.tf), g.trial ? pill('кандидат', 'warn') : null),
    h('div', { class: 'why', style: { fontSize: '15px' } }, rich(g.lead)),
    h('div', { class: 'guide-steps' }, (g.steps || []).map(([t, text], i) => h('div', { class: 'guide-step' },
      h('span', { class: 'guide-n' }, String(i + 1)),
      h('div', null, h('div', { class: 'guide-t' }, t), h('div', { class: 'why' }, rich(text)))))),
    g.fact ? h('div', { class: 'notice info', style: { marginTop: '16px' } }, h('div', { class: 'why' }, rich(g.fact))) : null));
}

function equityCard(data, code, s) {
  const pts = sliceSeries(strategySeries(data, code), period);
  const base = period ? (pts[0] && pts[0].v) : s.start_balance;
  const change = isNum(base) && isNum(s.equity) ? s.equity - base : null;
  return h('div', { class: 'card fade-in' },
    h('div', { class: 'card-title' }, 'Капитал счёта стратегии'),
    h('div', { class: 'big' }, money(s.equity)),
    h('div', { style: { margin: '8px 0 16px' } }, delta(change, isNum(base) && base ? (s.equity / base - 1) * 100 : null),
      h('span', { class: 'muted', style: { marginLeft: '8px', fontSize: '14px' } }, periodWord()),
      (s.open || s.pending) ? h('span', { class: 'muted', style: { marginLeft: '8px', fontSize: '14px' } },
        `· в рынке ${s.open || 0}${s.pending ? `, заявок ${s.pending}` : ''}`) : null),
    lineChart(pts, { height: 230, step: true, color: strategyColor(code), baseline: period ? undefined : s.start_balance,
      format: v => money(v), axisFormat: v => moneyShort(v), label: `Капитал ${strategyTitle(code)}`,
      emptyText: 'Сделок за период нет' }));
}

function kpi(label, value, cls = '', hint = null) {
  return h('div', { class: 'kpi' }, h('div', { class: 'stat-label' }, label),
    h('div', { class: 'kpi-v ' + cls }, value), hint ? h('small', null, hint) : null);
}

function kpis(q, st) {
  return section(`Сделки ${periodWord()}`, null, h('div', { class: 'kpis' },
    kpi('Сделок', num(q.trades), '', q.trades ? `${num(q.wins)} в плюс · ${num(q.losses)} в минус` : null),
    kpi('Побед', isNum(q.win_rate) ? pct(q.win_rate * 100, 0) : '—', '', isNum(st.wrCI) ? `± ${num(st.wrCI, 0)} п.п.` : null),
    kpi('Итог', signedR(q.total_r, 1), tone(q.total_r), isNum(q.total_usd) ? signedMoney(q.total_usd, 0) : null),
    kpi('На сделку', signedR(q.avg_r), st.noise ? '' : tone(q.avg_r),
      isNum(st.expCI) ? `± ${num(st.expCI, 2)}R${st.noise ? ' · не отличить от 0' : ''}` : 'погрешность — от двух сделок'),
    kpi('Профит-фактор', isNum(q.profit_factor) ? num(q.profit_factor, 2) : '—', '', 'прибыль ÷ убытки'),
    kpi('Макс. просадка', isNum(q.max_dd_r) ? `${num(q.max_dd_r, 1)}R` : '—', '', isNum(q.max_loss_streak) ? `серия убытков: ${q.max_loss_streak}` : null),
    kpi('Лучшая / худшая', `${signedR(q.best_r, 1)} / ${signedR(q.worst_r, 1)}`),
    kpi('В сделке', isNum(q.median_minutes) ? hours(q.median_minutes / 60) : '—', '', 'медиана'),
    kpi('Ход в плюс', signedR(q.avg_mfe_r, 1), '', 'в среднем до выхода (MFE)'),
    kpi('Ход в минус', signedR(q.avg_mae_r, 1), '', 'в среднем (MAE)'),
    kpi('Издержки', isNum(st.costR) ? `${num(st.costR, 3)} R/сд` : '—', '',
      isNum(st.gross) ? `без них было бы ${signedR(st.gross)}` : 'комиссии и фандинг в долях риска')));
}

function funnelCard(f) {
  const top = Math.max(1, f.setups || 0);
  const line = (label, value, color, hint) => h('div', { class: 'funnel-row' },
    h('div', null, label, hint ? h('small', null, hint) : null),
    h('div', { class: 'funnel-bar' }, h('div', { class: 'funnel-fill', style: { width: `${Math.min(100, (value || 0) / top * 100)}%`, background: color } })),
    h('div', { class: 'v' }, num(value)));
  return h('div', { class: 'card' },
    h('div', { class: 'card-title' }, 'Путь сетапа'),
    h('div', { class: 'funnel' },
      line('Сетапов', f.setups, 'var(--accent)', 'нашла стратегия'),
      line('Отсеяно', f.refused, 'var(--text-4)', 'фильтры и пределы'),
      line('Заявок', f.orders, 'var(--accent)', 'поставлено'),
      line('Снято', f.dropped, 'var(--text-4)', 'не налились, отменены'),
      line('Налилось', f.filled, 'var(--accent)'),
      line('В плюс', f.wins, 'var(--up)'),
      line('В минус', f.losses, 'var(--down)'),
      (f.open || f.pending) ? line('Сейчас в работе', (f.open || 0) + (f.pending || 0), 'var(--warn)') : null));
}

function notTradedCard(nt) {
  const refused = nt.refused || [];
  const dropped = Object.entries(nt.dropped || {});
  const card = h('div', { class: 'card' }, h('div', { class: 'card-title' }, 'Что не взяли — и чем бы кончилось'));
  if (!refused.length && !dropped.length) { card.append(empty('Отказов нет', 'Все сетапы дошли до заявки')); return card; }
  card.append(h('div', { class: 'rows' },
    refused.map(g => row(h('span', null, g.gate, h('small', null,
      `${num(g.count)} ${plural(g.count, 'раз', 'раза', 'раз')} · ` + Object.entries(g.outcomes || {}).map(([k, v]) => `${k} ${v}`).join(', '))),
      isNum(g.would_r) ? signedR(g.would_r, 1) : '—', { tone: tone(g.would_r) })),
    dropped.map(([why, n]) => row(h('span', null, why, h('small', null, 'заявка снята')), num(n)))));
  card.append(h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '8px' } },
    'Справа — сколько R дали бы отсеянные сетапы по тени исполнения: минус — фильтр сберёг, плюс — стоил денег.'));
  return card;
}

export function sliceTable(titleText, rows) {
  if (!rows || !rows.length) return null;
  const maxAbs = Math.max(0.01, ...rows.map(r => Math.abs(r.total_r || 0)));
  return h('div', { class: 'card' },
    h('div', { class: 'card-title' }, titleText),
    h('table', { class: 'table' },
      h('thead', null, h('tr', null, ['', 'Сделок', 'Побед', 'Итог', ''].map(t => h('th', null, t)))),
      h('tbody', null, rows.map(r => {
        const w = Math.abs(r.total_r || 0) / maxAbs * 50;
        return h('tr', null,
          h('td', null, String(r.name).replace(/USDT$/, '')),
          h('td', null, num(r.trades)),
          h('td', null, isNum(r.win_rate) ? pct(r.win_rate * 100, 0) : '—'),
          h('td', { class: tone(r.total_r) }, signedR(r.total_r, 1)),
          h('td', { style: { width: '30%' } }, h('div', { class: 'rbar' }, h('i', { style: {
            left: (r.total_r || 0) >= 0 ? '50%' : `${50 - w}%`, width: `${w}%`,
            background: (r.total_r || 0) >= 0 ? 'var(--up)' : 'var(--down)' } }))));
      }))));
}

function breakdowns(rep) {
  const cards = [
    sliceTable('По стороне', rep.sides),
    sliceTable('По выходу', rep.exits),
    sliceTable('По месяцам', rep.months),
    sliceTable('Лучшие пары', rep.pairs_best),
    sliceTable('Худшие пары', rep.pairs_worst),
  ].filter(Boolean);
  if (!cards.length) return null;
  return section('Разрезы', null, h('div', { class: 'grid grid-2' }, cards));
}

function scanCard(data, code) {
  const f = (data.funnel || {})[code];
  const card = h('div', { class: 'card' }, h('div', { class: 'card-title' }, 'Последний проход сканера'));
  if (!f) { card.append(empty('Нет данных', 'Стратегия сканирует рынок своим расписанием')); return card; }
  card.append(h('div', { class: 'stats', style: { marginBottom: '12px' } },
    stat('Когда', ago(f.at)), stat('Пар', num(f.pairs)), stat('Найдено', num(f.found), f.found ? 'up' : '')));
  card.append(h('div', { class: 'rows' }, (f.reasons || []).slice(0, 6).map(r =>
    row(h('span', null, r.reason, r.example ? h('small', null, r.example) : null), num(r.count)))));
  return card;
}

function accountCard(acc, s) {
  const st = (acc && acc.settings) || {};
  const sides = { both: 'обе', long: 'только лонги', short: 'только шорты' }[st.sides] || st.sides || '—';
  return h('div', { class: 'card' },
    h('div', { class: 'card-title' }, 'Тестовый счёт стратегии'),
    h('div', { class: 'rows' },
      row('Депозит', money(st.deposit ?? s.start_balance, 0)),
      row('Риск на сделку', pct(st.risk_pct ?? 1, 1), { hint: 'одинаковый у всех стратегий на тесте' }),
      row('Стороны', sides),
      row('Позиций одновременно', st.max_slots ? num(st.max_slots) : 'без предела'),
      row('В одну сторону', st.max_same_direction ? num(st.max_same_direction) : 'без предела'),
      acc ? row('Риск в рынке', money(acc.open_risk, 0), { hint: isNum(acc.open_risk_pct) ? `${pct(acc.open_risk_pct, 2)} депозита` : null }) : null,
      row('Макс. просадка счёта', isNum(s.max_dd_pct) ? pct(s.max_dd_pct, 1) : '—')),
    h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '8px' } }, 'Изменить правила счёта — в разделе «Система» (пока в прежней панели).'));
}

function recentTrades(data, code) {
  const trades = closedTrades(data, code, period || null).slice(0, 12);
  return section('Последние сделки', { href: `#/trades/history/${code}`, text: 'Все сделки' },
    h('div', { class: 'card' }, trades.length ? h('div', { class: 'list' }, trades.map(tradeRow))
      : empty('Сделок за период нет', 'Стратегия ждёт своих сетапов')));
}

export function tradeRow(t) {
  const long = t.direction === 'LONG';
  return h('a', { class: 'list-item', href: `#/trades/h/${encodeURIComponent(t.id)}` },
    h('span', { class: 'side ' + (long ? 'long' : 'short') }, long ? 'ЛОНГ' : 'ШОРТ'),
    h('div', { style: { minWidth: 0 } },
      h('div', { class: 'title' }, t.pair.replace(/USDT$/, ''), h('span', { class: 'meta', style: { fontWeight: 400, marginLeft: '8px' } }, t.reason_ru || t.reason || ''),
        t.suspect ? h('span', { class: 'pill warn', style: { marginLeft: '8px' }, title: 'В свечах этой сделки была дыра — в статистику она не идёт' }, 'дыра в данных') : null),
      h('div', { class: 'meta' }, `${strategyTitle(t.strategy)} · ${dateTime(t.closed)} · ${hours((t.duration_min || 0) / 60)}`)),
    h('div', { class: 'val ' + tone(t.pnl) }, signedMoney(t.pnl), h('small', null, signedR(t.pnl_r))));
}
