/* ИИ: тетрадь модели (LLM_MODE=notebook) — обзоры рынка, образцы и их
   состояние, решения по сигналам; рынок в целом; исходы планов; сервер
   модели. Данные — /api/llm (раз в минуту). Анализ дня по активу —
   /api/daily (раз в минуту), настройки — /api/settings (раздел DAILY). */

import { h, section, row, pill, empty, notice, button, toast, openSheet } from '../ui.js';
import { signedPct, pct, num, tone, dateTime, ago, hours, isNum, plural, price } from '../format.js';
import { cachedJSON, invalidate, postJSON } from '../api.js';

export const title = 'ИИ';
let rerender = () => {};
export function bindRefresh(fn) { rerender = fn; }

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
    section('Анализ дня по активу', null, dailyBlock(cachedJSON('/api/daily', 60000))),
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

/* ── Анализ дня по активу (strategies/llm/llm_daily, с 15.10.2026) ──────
   Раз в сутки к часу из настроек модель разбирает выбранные активы по
   методике владельца (SMC/ICT, Вайкофф, Герчик; 16 разделов). На карточке —
   оценки, сетапы с R:R, пересчитанным кодом, и вывод; полный разбор — в листе:
   страница перерисовывается по опросу, и раскрытый блок свернулся бы. */
const dailyDraft = { v: null };
const coin = p => String(p || '').replace(/USDT$/, '');

function toggle(checked, onChange, disabled, label) {
  const input = h('input', { type: 'checkbox', checked, disabled, 'aria-label': label });
  input.addEventListener('change', () => onChange(input.checked));
  return h('label', { class: 'switch' }, input, h('span'));
}

function dailyBlock(res) {
  const d = res.value;
  if (!d) {
    return h('div', { class: 'card' }, res.error ? empty('Анализ дня не загрузился', res.error)
      : h('div', { class: 'skeleton', style: { height: '160px' } }));
  }
  const reports = d.reports || [];
  const days = [...new Set(reports.map(r => r.day))];
  const latest = reports.filter(r => r.day === days[0]);
  const hour = (d.settings || {}).hour_utc;
  return h('div', { class: 'grid grid-cards' },
    dailySettings(d),
    latest.length ? latest.map(reportCard)
      : h('div', { class: 'card' }, empty('Разборов ещё нет', isNum(hour) ? `Первые будут готовы к ${hour}:00 UTC` : '')),
    days.length > 1 ? olderCard(reports, days.slice(1)) : null);
}

function dailySettings(d) {
  const s = d.settings || {};
  const dr = dailyDraft.v || (dailyDraft.v = {
    enabled: s.enabled !== false, pairs: [...(s.pairs || [])], hour_utc: isNum(s.hour_utc) ? s.hour_utc : 6 });
  const writable = !!d.writable;
  const max = d.max_pairs || 6;
  const chips = h('div', { class: 'actions' }, (d.allowed || []).map(p => {
    const on = dr.pairs.includes(p);
    const b = button(coin(p), () => {
      if (on) dr.pairs = dr.pairs.filter(x => x !== p);
      else if (dr.pairs.length >= max) { toast(`Не больше ${max}: модель разбирает по одному активу в час`, 'bad'); return; }
      else dr.pairs = [...dr.pairs, p];
      rerender();
    }, on ? '' : 'gray', true);
    b.disabled = !writable;
    b.setAttribute('aria-pressed', String(on));
    return b;
  }));
  const hourInput = h('input', { class: 'input', type: 'number', min: 0, max: 23, step: 1, value: dr.hour_utc,
    disabled: !writable, 'aria-label': 'Готов к, час UTC' });
  hourInput.addEventListener('input', () => { dr.hour_utc = hourInput.value === '' ? null : Number(hourInput.value); });
  const apply = button('Применить', async () => {
    if (dr.enabled && !dr.pairs.length) { toast('Выберите хотя бы один актив', 'bad'); return; }
    if (!Number.isInteger(dr.hour_utc) || dr.hour_utc < 0 || dr.hour_utc > 23) { toast('Час — целое от 0 до 23', 'bad'); return; }
    try {
      await postJSON('/api/settings', { DAILY: { enabled: dr.enabled, pairs: dr.pairs, hour_utc: dr.hour_utc } });
      toast('Анализ дня сохранён', 'good');
      dailyDraft.v = null;
      invalidate('/api/settings');
      invalidate('/api/daily');
    } catch (e) { toast('Не сохранено: ' + e.message, 'bad'); }
  });
  apply.disabled = !writable;
  const n = dr.pairs.length;
  const from = isNum(dr.hour_utc) ? Math.max(0, dr.hour_utc - n) : null;
  return h('div', { class: 'card' },
    h('div', { class: 'card-title' }, 'Что и когда'),
    h('div', { class: 'setting' }, h('div', { class: 'setting-name' }, 'Анализ дня'),
      toggle(dr.enabled, v => { dr.enabled = v; }, !writable, 'Анализ дня')),
    h('div', { class: 'setting' }, h('div', { class: 'setting-name' }, 'Готов к, час UTC'),
      h('span', { class: 'unit-wrap' }, hourInput, h('span', null, ':00')),
      h('div', { class: 'setting-note' }, n && from !== null
        ? `Модель разбирает по одному активу в час: с ${from}:00 до ${dr.hour_utc}:00 UTC.` : 'Активы не выбраны.')),
    h('div', { class: 'subhead' }, `Активы · ${n} из ${max}`),
    chips,
    h('div', { class: 'actions', style: { marginTop: '14px' } }, apply),
    h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '10px' } },
      !d.model ? 'Сервер модели не настроен — разборов не будет.'
        : d.busy ? `Сейчас разбирается ${coin(d.busy)}.`
          : 'Выжимка уходит в Telegram (уведомление «Анализ дня по активу»), полный разбор — здесь.'));
}

function reportCard(r) {
  const p = r.parsed || {};
  const lean = isNum(p.p_bull) && isNum(p.p_bear)
    ? pill(p.p_bull >= p.p_bear ? `вверх ${p.p_bull}%` : `вниз ${p.p_bear}%`, p.p_bull >= p.p_bear ? 'up' : 'down') : null;
  const card = h('div', { class: 'card' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: '8px', alignItems: 'flex-start' } },
      h('div', null, h('div', { class: 'strat-name' }, coin(r.pair)),
        h('div', { class: 'strat-sub' }, r.error ? r.day : `цена ${price(r.price)} · ${dateTime(r.built_at)}`)),
      r.error ? pill('сбой', 'down') : lean));
  if (r.error) {
    card.append(h('div', { class: 'why', style: { fontSize: '13px', marginTop: '10px' } }, r.error));
    return card;
  }
  if (isNum(p.bull) || isNum(p.bear)) {
    card.append(h('div', { class: 'actions', style: { margin: '10px 0 4px' } },
      pill(`bull ${p.bull ?? '—'}/10`, 'up'), pill(`bear ${p.bear ?? '—'}/10`, 'down'), pill(`ловушка ${p.trap ?? '—'}/10`, 'warn')));
  }
  if ((r.rr || []).length) card.append(h('div', { class: 'list' }, r.rr.map(setupRow)));
  const text = String(r.conclusion || '');
  card.append(
    h('div', { class: 'why', style: { fontSize: '13px', marginTop: '10px', whiteSpace: 'pre-wrap' } },
      text.length > 700 ? text.slice(0, 700) + '…' : text),
    h('div', { class: 'actions', style: { marginTop: '12px' } }, button('Все 16 разделов', () => openReport(r), 'gray', true)));
  return card;
}

function setupRow(s) {
  return h('div', { class: 'list-item', style: { gridTemplateColumns: 'minmax(0, 1fr) auto' } },
    h('div', null,
      h('div', { class: 'title', style: { fontSize: '14px' } }, `${s.side === 'long' ? '🟢' : '🔴'} ${s.name}`),
      h('div', { class: 'meta' }, `вход ${price(s.entry)} · стоп ${price(s.stop)} · цели ${(s.targets || []).map(price).join(' / ') || '—'}`
        + (s.why ? ` · ${s.why}` : ''))),
    h('div', { class: 'val' + (s.weak ? '' : ' up') }, (s.rr || []).length ? `R:R ${s.rr.map(x => num(x, 1)).join(' / ')}` : '—',
      h('small', { class: 'muted' }, s.weak ? 'слабый' : 'от 1.5')));
}

function olderCard(reports, days) {
  return h('div', { class: 'card' }, h('div', { class: 'card-title' }, 'Прежние дни'),
    h('div', { class: 'rows' }, days.map(dd => row(dd, h('div', { class: 'actions', style: { justifyContent: 'flex-end' } },
      reports.filter(r => r.day === dd).map(r => button(coin(r.pair) + (r.error ? ' · сбой' : ''), () => openReport(r), 'gray', true)))))));
}

function openReport(r) {
  openSheet(`Анализ дня · ${coin(r.pair)} · ${r.day}`, () => h('div', null,
    h('div', { class: 'muted', style: { fontSize: '13px', marginBottom: '12px' } }, r.error ? 'Разбор не получен'
      : `цена ${price(r.price)} · готов ${dateTime(r.built_at)} · ответ модели ${isNum(r.seconds) ? `${Math.round(r.seconds / 60)} мин` : '—'}`),
    r.error ? notice('bad', 'Сбой', r.error) : reportText(r.answer),
    h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '16px' } },
      'Разбор модели по данным бота — не проверенный сигнал. R:R сетапов на карточке пересчитан кодом.')));
}

/* Ответ модели — узлами, без HTML: заголовки разделов «N) NAME» и «#»,
   **жирный**, пункты списка. Прочая разметка остаётся текстом. */
function reportText(text) {
  const out = h('div', { style: { fontSize: '14px', lineHeight: '1.55' } });
  for (const raw of String(text || '').split('\n')) {
    const line = raw.replace(/\s+$/, '');
    if (!line.trim()) { out.append(h('div', { style: { height: '8px' } })); continue; }
    const md = /^\s*#{1,4}\s*(.*)$/.exec(line);
    const sec = /^\s*\**\s*(\d{1,2}\)\s*[A-Z][A-Z0-9 /&.-]*[A-Z])\**\s*(.*)$/.exec(line);
    if (md) out.append(h('div', { style: { fontWeight: 600, marginTop: '14px' } }, inline(md[1].replace(/\*\*/g, ''))));
    else if (sec) {
      out.append(h('div', { style: { fontWeight: 600, marginTop: '14px' } }, sec[1],
        sec[2] ? h('span', { style: { fontWeight: 400 } }, ' ', inline(sec[2].replace(/^\**\s*/, ''))) : null));
    } else out.append(h('div', null, inline(line.replace(/^\s*[-*•]\s+/, '• '))));
  }
  return out;
}

function inline(text) {
  const span = h('span');
  for (const part of String(text).split(/(\*\*[^*]+\*\*)/)) {
    if (/^\*\*[^*]+\*\*$/.test(part)) span.append(h('b', null, part.slice(2, -2)));
    else if (part) span.append(document.createTextNode(part));
  }
  return span;
}
