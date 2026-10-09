/* ИИ: тетрадь модели (LLM_MODE=notebook) — обзоры рынка, образцы и их
   состояние, решения по сигналам; рынок в целом; исходы планов; сервер
   модели. Данные — /api/llm (раз в минуту). */

import { h, section, row, pill, empty } from '../ui.js';
import { signedPct, pct, num, tone, dateTime, ago, hours, isNum, plural } from '../format.js';
import { cachedJSON } from '../api.js';

export const title = 'ИИ';

export function render(store) {
  const res = cachedJSON('/api/llm', 60000);
  const d = res.value;
  const head = h('div', { class: 'page-head' },
    h('div', null, h('h1', { class: 'page-title' }, title),
      h('div', { class: 'page-sub' }, 'Модель ведёт тетрадь: следит за образцами рынка и решает по их сигналам')),
    h('a', { href: '#/strategies/LLM', class: 'btn gray sm', style: { textDecoration: 'none' } }, 'Счёт ИИ и сделки'));
  if (!d) {
    return h('div', null, head, res.error ? h('div', { class: 'card' }, empty('Данные ИИ не загрузились', res.error))
      : h('div', { class: 'grid grid-hero' }, h('div', { class: 'card skeleton', style: { height: '300px' } }),
        h('div', { class: 'card skeleton', style: { height: '300px' } })));
  }
  const nb = d.notebook || {};
  return h('div', null, head,
    h('div', { class: 'grid grid-hero fade-in' }, reviewCard(nb), stateCard(d)),
    section('Образцы тетради', null, patternsCard(nb)),
    section('Рынок в целом', null, marketKpis(d.macro || {}, d.mood || {})),
    h('div', { class: 'grid grid-2 section' }, decisionsCard(nb), outcomesCard(d.outcomes || {}, d.gates || {}, d.code_gates || [], d.broken_gates || [])),
  );
}

function reviewCard(nb) {
  const reviews = nb.reviews || [];
  const last = reviews[0];
  const card = h('div', { class: 'card' }, h('div', { class: 'card-title' }, 'Обзор рынка от модели'));
  if (!last) { card.append(empty('Обзоров ещё нет', 'Модель пишет обзор раз в четыре часа')); return card; }
  card.append(h('div', { class: 'muted', style: { fontSize: '13px', marginBottom: '8px' } }, `${dateTime(last.hour)} · ${ago(last.hour)}`),
    h('div', { class: 'why', style: { fontSize: '15px' } }, last.text));
  if (reviews.length > 1) {
    card.append(h('details', { class: 'log' }, h('summary', null, `Прежние обзоры · ${reviews.length - 1}`),
      reviews.slice(1).map(r => h('div', { class: 'instr' },
        h('div', { class: 'instr-h' }, h('b', null, dateTime(r.hour))), h('div', { class: 'instr-b' }, r.text)))));
  }
  return card;
}

function stateCard(d) {
  const streams = d.streams || {};
  const st = (s) => (s && s.connected ? pill('на связи', 'up') : pill('нет связи', 'down'));
  return h('div', { class: 'card' },
    h('div', { class: 'card-title' }, 'Состояние'),
    h('div', { class: 'rows' },
      row('Сервер модели', d.server_alive ? pill('работает', 'up') : pill('недоступен', 'down'), { hint: d.server || null }),
      row('Модель', h('span', { style: { fontWeight: 500, whiteSpace: 'normal' } }, (d.model || '—').replace(/\.gguf$/, ''))),
      row('Окно контекста', `${num(d.ctx)} токенов`),
      row('Средний ответ', isNum(d.avg_seconds) ? hours(d.avg_seconds / 3600) : '—', { hint: d.avg_over ? `по ${d.avg_over} последним` : null }),
      row('Поток сделок', st(streams.trades), { hint: streams.trades ? `${num(streams.trades.events)} событий` : null }),
      row('Поток ликвидаций', st(streams.liquidations), { hint: streams.liquidations ? `${num(streams.liquidations.events)} событий` : null })));
}

function patternsCard(nb) {
  const patterns = nb.patterns || [];
  const lines = ((nb.status || {}).lines) || [];
  if (!patterns.length) return h('div', { class: 'card' }, empty('Образцов нет', ''));
  const lineFor = (p) => {
    const name = p.split(' (')[0];
    return lines.find(l => l.includes(`«${name}»`)) || '';
  };
  return h('div', { class: 'grid grid-cards' }, patterns.map(p => {
    const line = lineFor(p);
    const ready = line && !/не может|ни у одной/.test(line);
    const [name, rest] = [p.split(' (')[0], (p.match(/\(([^)]*)\)/) || [])[1] || ''];
    return h('div', { class: 'card' },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: '8px', alignItems: 'flex-start' } },
        h('div', null, h('div', { class: 'strat-name' }, name), h('div', { class: 'strat-sub' }, rest)),
        pill(ready ? 'есть сигнал' : 'ждёт', ready ? 'up' : '')),
      h('div', { class: 'why', style: { fontSize: '13px', marginTop: '12px' } },
        line.replace(/^«[^»]*»\s*\([^)]*\)\s*—\s*/, '') || 'Состояние появится после следующего часа'));
  }).concat(h('div', { class: 'muted', style: { fontSize: '12px', gridColumn: '1 / -1' } },
    (nb.status && nb.status.at) ? `Проверено ${ago(nb.status.at)}` : '')));
}

function kpi(label, value, hint, cls = '') {
  return h('div', { class: 'kpi' }, h('div', { class: 'stat-label' }, label),
    h('div', { class: 'kpi-v ' + cls }, value), hint ? h('small', null, hint) : null);
}

function marketKpis(m, mood) {
  const bp = v => (isNum(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}${num(Math.abs(v), 1)} б.п.` : '—');
  return h('div', null, h('div', { class: 'kpis' },
    kpi('BTC за сутки', signedPct(m.btc_24h), `за неделю ${signedPct(m.btc_7d)}`, tone(m.btc_24h)),
    kpi('Доминация BTC', isNum(m.btc_d) ? pct(m.btc_d, 1) : '—', `за сутки ${signedPct(m.btc_d_24h, 2)}`),
    kpi('Доминация USDT', isNum(m.usdt_d) ? pct(m.usdt_d, 2) : '—', `за сутки ${signedPct(m.usdt_d_24h, 2)}`),
    kpi('Альткоины (TOTAL2)', signedPct(m.total2_24h), `за неделю ${signedPct(m.total2_7d)}`, tone(m.total2_24h)),
    kpi('Монет в плюсе', isNum(m.up_24h) ? `${m.up_24h} из ${m.counted_24h}` : '—', 'за сутки'),
    kpi('Страх в опционах (DVOL)', isNum(mood.dvol) ? num(mood.dvol, 1) : '—', isNum(mood.dvol_chg_24h) ? `за сутки ${signedPct(mood.dvol_chg_24h, 1)}` : null),
    kpi('Премия Coinbase', bp(mood.btc_cb_prem_bp), 'американский спрос на BTC', tone(mood.btc_cb_prem_bp)),
    kpi('Доля спота', isNum(mood.btc_spot_share_24h) ? pct(mood.btc_spot_share_24h * 100, 0) : '—', 'в обороте BTC за сутки')),
    h('div', { class: 'muted', style: { fontSize: '12px', margin: '10px 4px 0' } },
      m.stale ? 'Данные рынка устарели' : `Обновлено ${!isNum(m.age_min) ? '—' : m.age_min < 1 ? 'только что' : `${num(m.age_min, 0)} мин назад`}`));
}

function decisionsCard(nb) {
  const list = nb.decisions || [];
  const card = h('div', { class: 'card' }, h('div', { class: 'card-title' }, 'Решения по сигналам'));
  if (!list.length) { card.append(empty('Сигналов не было', 'Модель решает, только когда образец сработал и есть место')); return card; }
  card.append(h('div', { class: 'list' }, list.slice(0, 8).map(dc => h('div', { class: 'list-item', style: { gridTemplateColumns: 'minmax(0, 1fr)' } },
    h('div', null,
      h('div', { class: 'title' }, `${dateTime(dc.hour)} · сигналы: ${(dc.alerts || []).map(a => a.pair).join(', ') || '—'}`),
      h('div', { class: 'meta', style: { marginTop: '2px' } }, `модель берёт: ${(dc.picks || []).join(', ') || 'ничего'}`),
      dc.reason ? h('div', { class: 'why', style: { fontSize: '13px', marginTop: '6px' } }, dc.reason) : null)))));
  return card;
}

function outcomesCard(o, gates, codeGates, brokenGates) {
  const entries = Object.entries(gates).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(e => e[1]));
  const color = g => (g === 'сделка' ? 'var(--up)' : brokenGates.includes(g) ? 'var(--down)' : codeGates.includes(g) ? 'var(--text-4)' : 'var(--accent)');
  return h('div', { class: 'card' },
    h('div', { class: 'card-title' }, `Исходы планов за ${o.days || 7} ${plural(o.days || 7, 'день', 'дня', 'дней')}`),
    h('div', { class: 'stats', style: { marginBottom: '16px' } },
      h('div', null, h('div', { class: 'stat-label' }, 'Принято'), h('div', { class: 'stat-value' }, num(o.accepted))),
      h('div', null, h('div', { class: 'stat-label' }, 'Вошли'), h('div', { class: 'stat-value' }, num(o.entered))),
      h('div', null, h('div', { class: 'stat-label' }, 'Цель первой'), h('div', { class: 'stat-value up' }, num(o.tp_first))),
      h('div', null, h('div', { class: 'stat-label' }, 'Стоп первым'), h('div', { class: 'stat-value down' }, num(o.sl_first)))),
    entries.length ? [h('div', { class: 'subhead', style: { marginTop: 0 } }, 'Чем кончались разборы'),
      h('div', { class: 'funnel' }, entries.map(([g, n]) => h('div', { class: 'funnel-row' },
        h('div', null, g), h('div', { class: 'funnel-bar' }, h('div', { class: 'funnel-fill', style: { width: `${n / max * 100}%`, background: color(g) } })),
        h('div', { class: 'v' }, num(n)))))] : null,
    h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '10px' } }, 'Серые — отказы кода без модели, красные — поломки разбора.'));
}

