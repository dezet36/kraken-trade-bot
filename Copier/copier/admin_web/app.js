// Админка копировщика: одна страница, данные из /api/* (cookie после /login?token=…).
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const main = $('#main');
let page = 'overview';
let cache = {};

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const num = (v, d = 2) => (v === null || v === undefined || v === '' || isNaN(+v)) ? '—' : (+v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
const usd = (v, sign = false) => (v === null || v === undefined) ? '—' : ((sign && +v > 0 ? '+' : '') + num(v) + ' $');
const pct = (v, sign = false) => (v === null || v === undefined) ? '—' : ((sign && +v > 0 ? '+' : '') + num(v) + '%');
const cls = (v) => (+v > 0 ? 'up' : (+v < 0 ? 'down' : ''));
const when = (s) => s ? String(s).slice(0, 16).replace('T', ' ') : '—';
// Коротко для таблиц: «26.10 18:16»; цена — значащими цифрами, без хвоста нулей.
const whenShort = (s) => s ? `${String(s).slice(8, 10)}.${String(s).slice(5, 7)} ${String(s).slice(11, 16)}` : '—';
const price = (v) => (v === null || v === undefined || v === '' || isNaN(+v)) ? '—'
  : (+v).toLocaleString('ru-RU', Math.abs(+v) >= 1 ? {maximumSignificantDigits: 7} : {maximumFractionDigits: 8});
const side = (d) => d === 'LONG' ? '<span class="up">ЛОНГ</span>' : '<span class="down">ШОРТ</span>';
const KIND = {demo: 'демо', real: 'реальный', tournament: 'турнир'};
const PHASE = {active_evaluation: 'этап 1', active_verification: 'этап 2', funded: 'funded', funded_success: 'funded ✓'};
const phase = (p) => PHASE[p] || esc(p || '');
const LEVEL = {observer: 'наблюдатель', demo: 'демо', full: 'полный'};
const STATUS = {new: 'регистрируется', pending: 'на проверке', active: 'активен', inactive: 'отключён'};
const STATUS_PILL = {new: '', pending: 'mid', active: 'ok', inactive: 'bad'};
const BOOK_STATUS = {active: 'торгует', target: 'цель этапа взята — стоит', failed: 'этап провален — стоит'};
const EXIT = {SL: 'стоп', BE: 'безубыток', TIME: 'срок', AI: 'ИИ', RULES: 'правила пропа', MANUAL: 'вручную'};
const exitWord = (c) => EXIT[c] || (String(c || '').startsWith('TP') ? 'цель ' + String(c).slice(2) : esc(c || '—'));
const STATE = {new: 'новая', pending: 'заявка', open: 'открыта', closed: 'закрыта', canceled: 'снята', error: 'ошибка'};

// Сборка страницы, с которой окно открыто. Сервер шлёт свою в каждом ответе;
// разошлись — копировщик обновили, пока окно было открыто: перезагружаемся, иначе
// прежний скрипт ходит по адресам, которых уже нет (26.10.2026: «Ошибка: Not Found»).
const BUILD = (document.querySelector('meta[name="copier-build"]') || {}).content || '';

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, {credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, ...opts});
  } catch (e) {
    throw new Error('нет связи с сервером — туннель переподключается, страница обновится сама');
  }
  const build = res.headers.get('X-Copier-Build');
  if (build && BUILD && build !== BUILD) { location.reload(); throw new Error('админка обновлена — перезагружаю'); }
  if (res.status === 401) { main.innerHTML = '<div class="alert">Нет доступа: откройте админку через CopierAdmin (вход по токену).</div>'; throw new Error('401'); }
  const data = await res.json().catch(() => ({}));
  if (res.status === 404 && !data.error) throw new Error(`на сервере нет ${path} — обновите окно (Ctrl+R)`);
  if (res.status === 502 || res.status === 503) throw new Error('копировщик перезапускается — через минуту страница оживёт');
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});

function toast(text) {
  const t = $('#toast'); t.textContent = text; t.classList.remove('hidden');
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add('hidden'), 2600);
}
function modal(html) { $('#sheet').innerHTML = '<button class="close" onclick="closeModal()">Закрыть</button>' + html; $('#modal').classList.remove('hidden'); }
window.closeModal = () => { $('#modal').classList.add('hidden'); render(); };
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });

function card(label, value, sub = '', klass = '') {
  return `<div class="card"><div class="label">${esc(label)}</div><div class="value ${klass}">${value}</div><div class="sub">${sub}</div></div>`;
}
function bar(value, limit, risk = false) {
  if (value === null || value === undefined || !limit) return '—';
  const w = Math.max(0, Math.min(100, value / limit * 100));
  return `<div class="bar ${risk ? 'risk' : ''}" title="${num(value)} из ${num(limit)}"><i style="width:${w}%"></i></div>`;
}
// Сделки одной ячейкой: таблицы должны помещаться в 1280 точек (экран владельца).
function tradesCell(s) {
  if (!s.trades) return `<td class="num muted">нет сделок${s.open ? ` · открыто ${s.open}` : ''}</td>`;
  return `<td class="num">${s.trades} сд · ${s.win_rate ?? '—'}%${s.open ? ` · откр. ${s.open}` : ''}
    <div class="${cls(s.pnl_usd)}">${usd(s.pnl_usd, true)} · ${num(s.sum_r)}R</div></td>`;
}
function feedPill(f) {
  if (f.frozen) return `<span class="pill bad">заморожена: ${esc(f.frozen)}</span>`;
  if (f.schema) return `<span class="pill bad">${esc(f.schema)}</span>`;
  if (f.stale) return '<span class="pill mid">книга не отвечает</span>';
  if (f.master_status && f.master_status !== 'active') return '<span class="pill mid">счёт стоит</span>';
  return '<span class="pill ok">в порядке</span>';
}
function accountState(a) {
  if (!a.enabled) return '<span class="pill">выкл</span>';
  if (a.user_status !== 'active') return `<span class="pill mid">${STATUS[a.user_status] || 'не активен'}</span>`;
  return a.paused ? '<span class="pill mid">пауза</span>' : '<span class="pill ok">копирует</span>';
}
function summaryCells(s) {
  return `<td class="num">${s.trades}</td><td class="num">${s.win_rate ?? '—'}%</td><td class="num ${cls(s.pnl_usd)}">${usd(s.pnl_usd, true)}</td><td class="num ${cls(s.sum_r)}">${num(s.sum_r)}R</td>`;
}
function idCheck(u) {
  // ID, присланный участником, против userId владельца ключа по ответу Upscale.
  if (!u.key_users || !u.key_users.length) return '';
  const same = u.upscale_uid && u.key_users.includes(u.upscale_uid);
  return `<div class="muted ${same ? '' : 'warn'}" title="userId владельца API-ключа по ответу Upscale">ключ: ${esc(u.key_users.join(', '))}${same ? ' ✓' : ''}</div>`;
}
function chart(points) {
  if (!points || points.length < 2) return '<div class="muted">Кривая эквити появится после нескольких снимков.</div>';
  const W = 1000, H = 180, P = 30;
  const ys = points.map((p) => +p.equity); const lo = Math.min(...ys), hi = Math.max(...ys);
  const span = hi - lo || 1;
  const xy = points.map((p, i) => `${P + i / (points.length - 1) * (W - 2 * P)},${H - P - (p.equity - lo) / span * (H - 2 * P)}`).join(' ');
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}"/>
    <polyline points="${xy}"/><text x="${P}" y="14">${num(hi)} $</text><text x="${P}" y="${H - 8}">${num(lo)} $ · ${when(points[0].at)} — ${when(points[points.length - 1].at)}</text></svg>`;
}

// ── страницы ────────────────────────────────────────────────────────────
const pages = {
  async overview() {
    const d = await api('/api/overview');
    const f = d.feed;
    const feedState = feedPill(f);
    return `<h1>Обзор</h1>
      ${d.alerts.length ? `<div class="alerts">${d.alerts.map((a) => `<div class="alert">${esc(a)}</div>`).join('')}</div>` : ''}
      <div class="grid">
        ${card('Участники', d.users.active, `на проверке ${d.users.pending} · регистрируются ${d.users.new} · отключено ${d.users.inactive}`)}
        ${card('На проверке', d.users.pending, d.users.pending ? 'раздел «Участники»' : '', d.users.pending ? 'warn' : '')}
        ${card('Счета в работе', d.accounts.enabled, `демо ${d.accounts.demo} · реальных ${d.accounts.real}`)}
        ${card('Эквити счетов', usd(d.equity_total))}
        ${card('Сегодня', usd(d.periods.day.pnl_usd, true), `${d.periods.day.trades} сд · ${num(d.periods.day.sum_r)}R`, cls(d.periods.day.pnl_usd))}
        ${card('Неделя', usd(d.periods.week.pnl_usd, true), `${d.periods.week.trades} сд · ${num(d.periods.week.sum_r)}R`, cls(d.periods.week.pnl_usd))}
        ${card('Всё время', usd(d.periods.all.pnl_usd, true), `${d.periods.all.trades} сд · прибыльных ${d.periods.all.win_rate ?? '—'}%`, cls(d.periods.all.pnl_usd))}
        ${card('Открыто / заявок', `${d.open} / ${d.pending}`)}
        ${card('Входы', d.entries_paused ? 'на паузе' : 'разрешены', '', d.entries_paused ? 'warn' : 'up')}
      </div>
      <h2>Источник сделок</h2>
      <div class="kv card"><div>Счёт бота</div><div><b>${esc(d.source)}</b> · ${BOOK_STATUS[f.master_status] || esc(f.master_status || '—')} <span class="muted">(«Источник»)</span></div>
        <div>Лента</div><div>${feedState} <span class="muted">${f.age_s === null ? '' : 'ответ ' + f.age_s + ' с назад'}</span></div>
        <div>Заявок / позиций у мастера</div><div>${f.pending} / ${f.positions}</div>
        <div>Риск на сделку участника</div><div>${d.risk_pct ?? '—'}% <span class="muted">— из правил счёта в боте; своих пределов у копировщика нет</span></div></div>
      <h2>Общая позиция</h2>
      ${d.exposure.length ? `<div class="scroll"><table><tr><th>Монета</th><th>Сторона</th><th class="num">Открыто</th><th class="num">Заявок</th>
        <th class="num">Счетов</th><th class="num">Риск, $</th><th>Стратегии</th></tr>
        ${d.exposure.map((g) => `<tr><td><b>${esc(g.pair)}</b></td><td>${side(g.direction)}</td><td class="num">${g.open}</td>
          <td class="num">${g.pending}</td><td class="num">${g.accounts}</td><td class="num">${num(g.risk_usd)}</td>
          <td>${esc(g.strategies.join(', '))}</td></tr>`).join('')}</table></div>`
        : '<div class="muted">Открытых сделок и заявок у участников нет.</div>'}`;
  },

  async users() {
    const d = await api('/api/users');
    cache.users = d;
    const action = (u) => `<label class="tick" onclick="event.stopPropagation()"><input type="checkbox"
      ${u.status === 'active' ? 'checked' : ''} onchange="toggleUser(${u.tg_id}, this)"> ${u.status === 'active' ? 'активен' : 'активировать'}</label>`;
    const accs = (u) => (u.account_list || []).map((a) =>
      `#${a.id} ${KIND[a.kind] || esc(a.kind || '')} · ${phase(a.phase)}<div class="muted">${a.nominal ? num(a.nominal, 0) + ' $' : ''}</div>`).join('') || '<span class="muted">—</span>';
    const rows = d.users.map((u) => `<tr class="click" onclick="editUser(${u.tg_id})">
      <td>${esc(u.name)}<div class="muted">${u.tg_id} · с ${when(u.created_at).slice(0, 10)}</div></td>
      <td>${u.upscale_uid ? `<b>${esc(u.upscale_uid)}</b>` : '<span class="muted">—</span>'}${idCheck(u)}</td>
      <td>${accs(u)}</td>
      <td><span class="pill ${STATUS_PILL[u.status] || ''}">${STATUS[u.status] || esc(u.status)}</span>${u.paused ? ' <span class="pill mid">пауза</span>' : ''}</td>
      <td>${action(u)}</td>
      <td>${LEVEL[u.level] || esc(u.level)}</td>
      ${tradesCell(u.summary)}</tr>`).join('');
    return `<h1>Участники</h1>
      <div class="card row" style="margin-bottom:12px">Бот для участников: ${d.bot_link ? `<b>${esc(d.bot_link)}</b>
        <button onclick="navigator.clipboard.writeText('${esc(d.bot_link)}');toast('Скопировано')">Копировать</button>` : '<span class="muted">появится после запуска</span>'}
        <span class="muted">· регистрация на Upscale: ${esc(d.signup_url)}</span></div>
      <div class="scroll"><table><tr><th>Участник</th><th>ID на Upscale</th><th>Счёт</th><th>Статус</th><th>Доступ</th><th>Уровень</th>
        <th class="num">Сделки</th></tr>
        ${rows || '<tr><td colspan="7" class="muted">Пока никого. Пользователь появляется здесь, как только нажмёт /start в боте.</td></tr>'}</table></div>`;
  },

  async accounts() {
    const d = await api('/api/accounts');
    const rows = d.accounts.map((a) => {
      const c = a.challenge;
      return `<tr class="click" onclick="openAccount(${a.id})">
        <td><b>#${a.id}</b><div class="muted">${KIND[a.kind] || esc(a.kind)} · ${phase(c.phase || a.phase)}</div></td>
        <td>${esc(a.user)}</td><td>${accountState(a)}</td>
        <td class="num">${usd(c.equity)}<div class="${cls(c.profit_pct)}">${pct(c.profit_pct, true)}</div>
          ${a.risk_per_trade ? `<div class="muted">риск/сделка ${usd(a.risk_per_trade)}</div>` : ''}</td>
        <td>${c.target_pct ? bar(Math.max(c.profit_pct || 0, 0), c.target_pct) + `<div class="muted">цель ${c.target_pct}%</div>` : '—'}</td>
        <td>${bar(c.daily_dd_used_pct, c.daily_dd_limit_pct, true)}<div class="muted">${pct(c.daily_dd_used_pct)} / ${c.daily_dd_limit_pct ?? '—'}%</div></td>
        <td>${bar(c.total_dd_used_pct, c.total_dd_limit_pct, true)}<div class="muted">${pct(c.total_dd_used_pct)} / ${c.total_dd_limit_pct ?? '—'}%</div></td>
        <td class="num">${c.profitable_days ?? '—'} / ${c.min_days ?? '—'}
          <div class="${c.consistency_met === false ? 'down' : 'muted'}">лучший день ${c.consistency_ratio === null ? '—' : pct(c.consistency_ratio)}</div></td>
        <td>${c.antifraud && c.antifraud !== 'normal' ? `<span class="pill bad">${esc(c.antifraud)}</span>` : '<span class="pill ok">норма</span>'}</td>
        ${tradesCell(a.summary)}</tr>`;
    }).join('');
    return `<h1>Счета</h1>
      <div class="scroll"><table><tr><th>Счёт</th><th>Участник</th><th>Состояние</th><th class="num">Эквити</th><th>Цель</th>
        <th>Дн. просадка</th><th>Общ. просадка</th><th class="num" title="профитные дни фазы и доля лучшего дня в прибыли (правило 30%)">Профит. дни</th><th>Антифрод</th>
        <th class="num">Сделки</th></tr>
        ${rows || '<tr><td colspan="10" class="muted">Счетов нет. Счёт появляется здесь, когда участник присылает API-ключ.</td></tr>'}</table></div>`;
  },

  async trades() {
    const q = new URLSearchParams(cache.tradeFilter || {days: '30'});
    const d = await api('/api/trades?' + q);
    const s = d.summary;
    const rows = d.trades.map((t) => `<tr><td>${whenShort(t.created_at)}</td><td>${esc(t.user)}<div class="muted">счёт #${t.account_id}</div></td>
      <td>${esc(t.strategy)} · ${esc(t.pair)}<div>${side(t.direction)}</div></td><td>${STATE[t.state] || esc(t.state)}</td>
      <td class="num">${price(t.entry_price || t.limit_price)}<div class="muted">${t.entry_price ? 'вход' : 'лимит'}</div></td>
      <td class="num">${price(t.stop)}</td><td class="num">${price(t.exit_price)}</td><td class="num">${usd(t.risk_usd)}</td>
      <td class="num ${cls(t.pnl_usd)}">${usd(t.pnl_usd, true)}<div>${t.pnl_r === null || t.pnl_r === undefined ? '' : num(t.pnl_r) + 'R'}</div></td>
      <td class="wrap">${esc(t.exit_reason || t.note || '')}</td>
      <td>${['new', 'pending', 'open'].includes(t.state) ? `<button onclick="closeTrade(${t.id})">Закрыть</button>` : ''}</td></tr>`).join('');
    const f = cache.tradeFilter || {days: '30'};
    return `<h1>Сделки</h1>
      <div class="row">За <select id="fDays"><option value="1">сутки</option><option value="7">неделю</option><option value="30">месяц</option><option value="">всё время</option></select>
        Стратегия <input id="fStrategy" value="${esc(f.strategy || '')}" style="width:90px">
        Состояние <select id="fState"><option value="">все</option><option value="open">открытые</option><option value="new,pending">заявки</option>
          <option value="closed">закрытые</option><option value="canceled">снятые</option><option value="error">ошибки</option></select>
        <button onclick="applyTradeFilter()">Показать</button>
        <a class="btn" href="/api/trades.csv?${q}">Выгрузить CSV</a></div>
      <div class="grid">${card('Закрыто', s.trades, `открыто ${s.open} · заявок ${s.pending}`)}${card('P&L', usd(s.pnl_usd, true), '', cls(s.pnl_usd))}
        ${card('Сумма R', num(s.sum_r), `средний ${num(s.avg_r, 3)}R`, cls(s.sum_r))}${card('Прибыльных', (s.win_rate ?? '—') + '%', `PF ${s.profit_factor ?? '—'}`)}
        ${card('Просадка', num(s.max_dd_r) + 'R', `серия минусов ${s.loss_streak}`)}</div><h2></h2>
      <div class="scroll"><table><tr><th>Время</th><th>Участник</th><th>Сделка</th><th>Состояние</th><th class="num">Вход</th>
        <th class="num">Стоп</th><th class="num">Выход</th><th class="num">Риск</th><th class="num">P&L</th><th>Причина</th><th></th></tr>
        ${rows || '<tr><td colspan="11" class="muted">Нет сделок.</td></tr>'}</table></div>`;
  },

  async source() {
    const d = await api('/api/source');
    const f = d.feed, b = d.book;
    const ts = (ms) => ms ? whenShort(new Date(+ms).toISOString()) : '—';
    const pos = b ? b.positions.map((p) => `<tr><td>${esc(p.strategy || '')} · <b>${esc(p.pair)}</b><div>${side(p.direction)}</div></td>
      <td class="num">${price(p.entry_price)}</td><td class="num">${price(p.stop_loss)}${+p.stop_loss !== +p.initial_stop ? '<div class="muted">был ' + price(p.initial_stop) + '</div>' : ''}</td>
      <td class="num">${(p.targets || []).map(price).join(' · ')}</td><td class="num">${num((p.remaining ?? 1) * 100, 0)}%</td>
      <td class="num">${price(p.last_price)}</td><td>${ts(p.opened_ts)}</td></tr>`).join('') : '';
    const pend = b ? b.pending.map((p) => `<tr><td>${esc(p.strategy || '')} · <b>${esc(p.pair)}</b><div>${side(p.direction)}</div></td>
      <td class="num">${price(p.limit_price)}<div class="muted">${p.stop_entry ? 'стоп-заявка' : 'лимит'}</div></td><td class="num">${price(p.stop_loss)}</td>
      <td class="num">${(p.targets || []).map(price).join(' · ')}</td><td>${p.expires_ts ? 'до ' + ts(p.expires_ts) : '—'}</td></tr>`).join('') : '';
    const ins = b ? b.instructions.map((i) => `<tr><td>${whenShort(i.at)}</td><td>${i.kind === 'ai' ? '🤖 ' : ''}${esc(i.title)}</td>
      <td class="wrap muted">${esc((i.lines || []).join(' · '))}</td></tr>`).join('') : '';
    const closed = b ? b.closed.map((c) => `<tr><td>${whenShort(c.closed_at)}</td><td>${esc(c.strategy || '')} · ${esc(c.pair)}</td>
      <td>${exitWord(c.exit_reason)}</td><td class="num ${cls(c.pnl_r)}">${c.pnl_r === null || c.pnl_r === undefined ? '—' : num(c.pnl_r) + 'R'}</td></tr>`).join('') : '';
    const strat = Object.entries(d.by_strategy).map(([k, s]) => `<tr><td>${esc(k || '—')}</td>${tradesCell(s)}<td class="num">${num(s.avg_r, 3)}</td></tr>`).join('');
    const r = (b && b.rules) || {};
    return `<h1>Источник сделок</h1>
      <p class="muted">Участники повторяют все сделки торгового счёта бота: заявки, входы по решению ИИ, перенос стопа, частичные и полные закрытия.
        Счёт торгует на живом коде бота — правки его стратегий доходят до участников.</p>
      ${d.error ? `<div class="alert">Книга счёта не получена: ${esc(d.error)}</div>` : ''}
      <div class="kv card"><div>Счёт бота</div><div><b>${esc(d.account)}</b> ${b ? '«' + esc(r.name || '') + '»' : ''}
          <input id="srcAcc" value="${esc(d.account)}" style="width:120px;margin-left:12px"> <button onclick="setSource()">Сменить</button></div>
        <div>Состояние</div><div>${b ? (BOOK_STATUS[b.status] || esc(b.status)) + (b.status_why ? ' — ' + esc(b.status_why) : '') + ` · этап ${esc(b.phase)} с ${when(b.phase_started_at)}` : '—'}</div>
        <div>Деньги книги</div><div>${b ? `баланс ${usd(b.balance)} · эквити ${usd(b.equity)} · начало этапа ${usd(b.start_balance)}${b.room ? ` · запас ${usd(b.room.amount)} (${esc(b.room.rule)})` : ''}` : '—'}</div>
        <div>Правила счёта</div><div>${b ? `${esc((r.strategies || []).join(', '))} · риск ${r.risk_pct}% · позиций ${r.max_slots || '∞'}, в одну сторону ${r.max_same_direction || '∞'} · дневной убыток ${r.daily_loss_pct}% · просадка ${r.max_drawdown_pct}% · ИИ ведёт счёт: ${r.ai_control ? 'да' : 'нет'}` : '—'}</div>
        <div>Лента</div><div>${feedPill(f)} <span class="muted">${f.age_s === null ? '' : 'ответ ' + f.age_s + ' с назад'}${f.last_error ? ' · ' + esc(f.last_error) : ''}</span>
          ${f.frozen || f.schema ? '<button onclick="unfreeze()">Снять заморозку</button>' : ''}</div></div>
      <h2>Позиции мастера</h2>
      ${pos ? `<div class="scroll"><table><tr><th>Позиция</th><th class="num">Вход</th><th class="num">Стоп</th><th class="num">Цели</th><th class="num">Остаток</th><th class="num">Цена</th><th>Открыта</th></tr>${pos}</table></div>` : '<div class="muted">Нет.</div>'}
      <h2>Заявки мастера</h2>
      ${pend ? `<div class="scroll"><table><tr><th>Заявка</th><th class="num">Вход</th><th class="num">Стоп</th><th class="num">Цели</th><th>Живёт</th></tr>${pend}</table></div>` : '<div class="muted">Нет.</div>'}
      <h2>Последние события счёта (в том числе решения ИИ)</h2>
      ${ins ? `<div class="scroll"><table><tr><th>Время</th><th>Событие</th><th>Подробно</th></tr>${ins}</table></div>` : '<div class="muted">Нет.</div>'}
      <h2>Закрытые у мастера</h2>
      ${closed ? `<div class="scroll"><table><tr><th>Время</th><th>Сделка</th><th>Выход</th><th class="num">Итог</th></tr>${closed}</table></div>` : '<div class="muted">С начала копирования закрытых не было.</div>'}
      <h2>Копии у участников</h2>
      <div class="grid">${card('Сделки участников', d.followers.trades, `открыто ${d.followers.open} · заявок ${d.followers.pending}`)}
        ${card('P&L участников', usd(d.followers.pnl_usd, true), `${num(d.followers.sum_r)}R`, cls(d.followers.pnl_usd))}
        ${card('Не налилось / сироты / ошибки', `${d.divergence.missed} / ${d.divergence.orphans} / ${d.divergence.errors}`, 'расхождения с мастером')}</div>
      ${strat ? `<h2></h2><div class="scroll"><table><tr><th>Стратегия мастера</th><th class="num">Сделки участников</th><th class="num">ср. R</th></tr>${strat}</table></div>` : ''}`;
  },

  async journal() {
    const d = await api('/api/journal');
    const tab = cache.journalTab || 'audit';
    const tabs = `<div class="row">${[['audit', 'Действия'], ['refusals', 'Отказы'], ['events', 'События мастера']].map(([k, t]) =>
      `<button class="${tab === k ? 'primary' : ''}" onclick="cache.journalTab='${k}';render()">${t}</button>`).join('')}</div>`;
    let body = '';
    if (tab === 'audit') body = `<div class="scroll"><table><tr><th>Время</th><th>Кто</th><th>Действие</th><th>Подробно</th></tr>${d.audit.map((a) =>
      `<tr><td>${when(a.at)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td>${esc(a.details)}</td></tr>`).join('')}</table></div>`;
    if (tab === 'refusals') body = `<div class="scroll"><table><tr><th>Время</th><th>Счёт</th><th>Стратегия</th><th>Пара</th><th>Причина</th></tr>${d.refusals.map((r) =>
      `<tr><td>${when(r.at)}</td><td>#${r.account_id}</td><td>${esc(r.strategy)}</td><td>${esc(r.pair)}</td><td>${esc(r.reason)}</td></tr>`).join('')}</table></div>`;
    if (tab === 'events') body = `<div class="scroll"><table><tr><th>Время</th><th>Событие</th><th>Стратегия</th><th>Пара</th><th>Ключ</th></tr>${d.events.map((e) =>
      `<tr><td>${when(e.at)}</td><td>${esc(e.kind)}</td><td>${esc(e.strategy || '')}</td><td>${esc(e.pair || '')}</td><td class="muted">${esc(e.master_key || '')}</td></tr>`).join('')}</table></div>`;
    return `<h1>Журнал</h1>${tabs}${body}`;
  },

  async system() {
    const d = await api('/api/overview');
    return `<h1>Система</h1>
      <div class="card"><div class="row"><b>Новые входы:</b> ${d.entries_paused ? '<span class="pill mid">на паузе</span>' : '<span class="pill ok">разрешены</span>'}
        <button onclick="sys('${d.entries_paused ? 'resume_entries' : 'pause_entries'}')">${d.entries_paused ? 'Разрешить входы' : 'Пауза для всех'}</button></div>
        <div class="muted">Пауза не трогает открытые позиции: их ведут стопы и тейки Upscale и сверка.</div></div>
      <h2>Аварийно</h2>
      <div class="card"><button class="danger" onclick="closeEveryone()">Закрыть всё у всех участников</button>
        <div class="muted">Закроет позиции и снимет заявки на всех счетах, поставит входы на паузу.</div></div>
      <h2>Рассылка участникам</h2>
      <div class="card"><textarea id="bcast" placeholder="Текст сообщения всем активным участникам"></textarea>
        <div class="row" style="margin-top:8px"><button class="primary" onclick="broadcast()">Отправить</button></div></div>`;
  },
};

// ── действия ────────────────────────────────────────────────────────────
window.editUser = (tgId) => {
  const u = cache.users.users.find((x) => x.tg_id === tgId);
  modal(`<h1>${esc(u.name)}</h1>
    <div class="kv"><div>Telegram id</div><div>${u.tg_id}</div>
      <div>ID на Upscale</div><div>${esc(u.upscale_uid || '—')} <span class="muted">${when(u.uid_at)}</span>${idCheck(u)}</div>
      <div>Статус</div><div><span class="pill ${STATUS_PILL[u.status] || ''}">${STATUS[u.status] || esc(u.status)}</span>
        ${u.status === 'active' ? `<button onclick="deactivateUser(${u.tg_id})">Отключить</button>`
          : `<button class="primary" onclick="activateUser(${u.tg_id}, $('#uLevel').value)">Активировать</button>`}</div>
      <div>Уровень доступа</div><div><select id="uLevel">${cache.users.levels.map((s) => `<option value="${s}" ${u.level === s ? 'selected' : ''}>${LEVEL[s]}</option>`).join('')}</select></div>
      <div>Счетов не больше</div><div><input id="uAcc" type="number" min="0" value="${u.max_accounts}" style="width:80px"></div>
      <div>Пауза входов</div><div><input type="checkbox" id="uPaused" ${u.paused ? 'checked' : ''}></div>
      <div>Заметки</div><div><textarea id="uNotes">${esc(u.notes || '')}</textarea></div></div>
    <div class="row" style="margin-top:12px"><button class="primary" onclick="saveUser(${u.tg_id})">Сохранить</button></div>`);
};
window.saveUser = async (tgId) => {
  await post(`/api/users/${tgId}`, {level: $('#uLevel').value, max_accounts: $('#uAcc').value, paused: $('#uPaused').checked, notes: $('#uNotes').value});
  toast('Сохранено'); closeModal();
};
window.activateUser = async (tgId, level) => {
  // Уровень не выбран — сервер берёт его по проверенному счёту (реальный — полный, демо — демо).
  const u = cache.users.users.find((x) => x.tg_id === tgId);
  if (!confirm(`Активировать ${u ? u.name : tgId}? Бот начнёт торговать на его счёте, участник получит сообщение в Telegram.`)) return false;
  try { await post(`/api/users/${tgId}/activate`, level ? {level} : {}); toast('Активирован'); } catch (e) { toast(e.message); return false; }
  if (!$('#modal').classList.contains('hidden')) closeModal(); else render();
  return true;
};
window.deactivateUser = async (tgId) => {
  const u = cache.users.users.find((x) => x.tg_id === tgId);
  if (!confirm(`Отключить ${u ? u.name : tgId}? Новые сделки на его счёт перестанут открываться; уже открытые доведутся до закрытия по стопам и целям.`)) return false;
  try { await post(`/api/users/${tgId}/deactivate`); toast('Отключён'); } catch (e) { toast(e.message); return false; }
  if (!$('#modal').classList.contains('hidden')) closeModal(); else render();
  return true;
};
window.toggleUser = async (tgId, box) => {
  const done = box.checked ? await activateUser(tgId) : await deactivateUser(tgId);
  if (!done) box.checked = !box.checked;            // отменили — галочка возвращается
};

window.openAccount = async (id) => {
  const a = await api('/api/accounts/' + id);
  const c = a.challenge;
  const trades = a.trades.slice(0, 100).map((t) => `<tr><td>${whenShort(t.created_at)}</td><td>${esc(t.strategy)}</td><td>${esc(t.pair)}</td><td>${side(t.direction)}</td>
    <td>${STATE[t.state] || esc(t.state)}</td><td class="num">${price(t.entry_price || t.limit_price)}</td><td class="num">${price(t.stop)}</td>
    <td class="num ${cls(t.pnl_usd)}">${usd(t.pnl_usd, true)}</td><td class="num">${num(t.pnl_r)}</td><td>${esc(t.exit_reason || t.note || '')}</td></tr>`).join('');
  const strat = Object.entries(a.by_strategy).map(([k, s]) => `<tr><td>${esc(k)}</td>${summaryCells(s)}<td class="num">${num(s.avg_r, 3)}</td></tr>`).join('');
  const refusals = a.refusals.slice(0, 30).map((r) => `<tr><td>${when(r.at)}</td><td>${esc(r.strategy)}</td><td>${esc(r.pair)}</td><td>${esc(r.reason)}</td></tr>`).join('');
  modal(`<h1>Счёт #${a.id} — ${esc(a.user)}</h1>
    <div class="row"><button onclick="acct(${a.id},'${a.paused ? 'resume' : 'pause'}')">${a.paused ? 'Снять паузу' : 'Пауза'}</button>
      <button onclick="acct(${a.id},'sync')">Сверить сейчас</button>
      <button onclick="acct(${a.id},'${a.enabled ? 'disable' : 'enable'}')">${a.enabled ? 'Выключить копирование' : 'Включить копирование'}</button>
      <button class="danger" onclick="if(confirm('Закрыть все позиции и снять заявки на счёте #${a.id}?')) acct(${a.id},'close_all')">Закрыть всё</button></div>
    <div class="grid">${card('Эквити', usd(c.equity), `номинал ${usd(c.initial)}`)}${card('Итог', pct(c.profit_pct, true), c.target_pct ? `цель ${c.target_pct}% · пройдено ${c.target_progress ?? 0}%` : '', cls(c.profit_pct))}
      ${card('Дневная просадка', pct(c.daily_dd_used_pct), `лимит ${c.daily_dd_limit_pct ?? '—'}%`)}${card('Общая просадка', pct(c.total_dd_used_pct), `лимит ${c.total_dd_limit_pct ?? '—'}%`)}
      ${card('Профитные дни', `${c.profitable_days ?? '—'} / ${c.min_days ?? '—'}`)}${card('Правило 30%', c.consistency_ratio === null ? '—' : pct(c.consistency_ratio), c.consistency_met === false ? 'нарушено' : '', c.consistency_met === false ? 'down' : '')}
      ${card('Антифрод', esc(c.antifraud || '—'), c.lock ? 'блокировка: ' + esc(c.lock) : '', c.antifraud && c.antifraud !== 'normal' ? 'down' : '')}
      ${card('Сделки', a.summary.trades, `прибыльных ${a.summary.win_rate ?? '—'}% · ${num(a.summary.sum_r)}R`)}</div>
    <h2>Кривая эквити</h2>${chart(a.equity)}
    <h2>По стратегиям</h2><div class="scroll"><table><tr><th>Стратегия</th><th class="num">Сделок</th><th class="num">Прибыльных</th><th class="num">P&L</th><th class="num">R</th><th class="num">ср. R</th></tr>${strat || '<tr><td colspan="6" class="muted">—</td></tr>'}</table></div>
    <h2>Сделки</h2><div class="scroll"><table><tr><th>Время</th><th>Стратегия</th><th>Пара</th><th>Сторона</th><th>Состояние</th><th class="num">Вход</th><th class="num">Стоп</th><th class="num">P&L</th><th class="num">R</th><th>Причина</th></tr>${trades || '<tr><td colspan="10" class="muted">—</td></tr>'}</table></div>
    <h2>Отказы</h2><div class="scroll"><table><tr><th>Время</th><th>Стратегия</th><th>Пара</th><th>Причина</th></tr>${refusals || '<tr><td colspan="4" class="muted">—</td></tr>'}</table></div>
    <div class="muted" style="margin-top:10px">Upscale: ${esc(a.upscale_id)} · сверка ${when(a.status_at)}${a.last_error ? ' · ошибка ' + esc(a.last_error) : ''}</div>`);
};
window.acct = async (id, action) => { try { await post(`/api/accounts/${id}/action`, {action}); toast('Готово'); openAccount(id); } catch (e) { toast(e.message); } };
window.closeTrade = async (id) => { if (!confirm('Закрыть сделку по рынку (или снять заявку)?')) return; try { await post(`/api/trades/${id}/close`); toast('Готово'); render(); } catch (e) { toast(e.message); } };
window.applyTradeFilter = () => {
  cache.tradeFilter = {days: $('#fDays').value, strategy: $('#fStrategy').value.trim().toUpperCase(), state: $('#fState').value};
  Object.keys(cache.tradeFilter).forEach((k) => { if (!cache.tradeFilter[k]) delete cache.tradeFilter[k]; });
  render();
};
window.unfreeze = async () => { try { await post('/api/source/unfreeze'); toast('Лента снова идёт'); } catch (e) { toast(e.message); } render(); };
window.setSource = async () => {
  const code = $('#srcAcc').value.trim();
  if (!confirm(`Копировать сделки счёта бота «${code}»? Уже открытые позиции этого счёта участникам не открываются — только новые сделки.`)) return;
  try { await post('/api/source', {account: code}); toast('Источник: ' + code); } catch (e) { toast(e.message); }
  render();
};
window.sys = async (action) => { await post('/api/system', {action}); toast('Готово'); render(); };
window.closeEveryone = async () => {
  const word = prompt('Закрыть ВСЕ позиции у ВСЕХ участников. Напишите ЗАКРЫТЬ:');
  if (word !== 'ЗАКРЫТЬ') return;
  const r = await post('/api/system', {action: 'close_everyone', confirm: word});
  toast(r.ok ? 'Всё закрыто' : 'Не везде: ' + r.failed.join('; ')); render();
};
window.broadcast = async () => {
  const text = $('#bcast').value.trim(); if (!text) return;
  if (!confirm('Отправить всем активным участникам?')) return;
  const r = await post('/api/system', {action: 'broadcast', text}); toast(`Отправлено: ${r.sent}`); $('#bcast').value = '';
};

// ── навигация ───────────────────────────────────────────────────────────
async function render() {
  try {
    main.innerHTML = await pages[page]();
    if (page === 'trades') {
      const f = cache.tradeFilter || {days: '30'};
      $('#fDays').value = f.days || ''; $('#fState').value = f.state || '';
    }
    $('#foot').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU');
  } catch (e) {
    if (e.message !== '401') main.innerHTML = `<div class="alert">Ошибка: ${esc(e.message)}</div>`;
  }
}
function go(name) {
  if (!pages[name]) name = 'overview';
  page = name;
  document.querySelectorAll('#nav a').forEach((x) => x.classList.toggle('active', x.dataset.page === name));
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  render();
}
document.querySelectorAll('#nav a').forEach((a) => a.addEventListener('click', () => go(a.dataset.page)));
page = (location.hash || '').slice(1) || 'overview';
if (!pages[page]) page = 'overview';
document.querySelectorAll('#nav a').forEach((x) => x.classList.toggle('active', x.dataset.page === page));
setInterval(() => { if ($('#modal').classList.contains('hidden') && page !== 'system') render(); }, 30000);
render();
