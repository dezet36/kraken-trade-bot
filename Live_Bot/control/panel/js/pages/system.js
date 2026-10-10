/* Система: торговля и пределы, настройки стратегий, уведомления,
   подключения, ошибки и выгрузки, журнал, обновление. Те же запросы, что у
   прежней панели (/api/settings, /api/deposit, /api/action, /api/errors,
   /api/log, /api/update, /api/sources, выгрузки).

   Набранное в полях живёт в черновике (drafts), а не в разметке: страница
   перерисовывается по опросу, и без черновика правка пропадала бы. */

import { h, empty, notice, pill, button, confirmSheet, toast, strategyBadge, strategyTitle, segmented } from '../ui.js';
import { money, signedMoney, signedPct, pct, num, tone, dateTime, ago, isNum, plural } from '../format.js';
import { cachedJSON, invalidate, postJSON, getJSON, refreshData } from '../api.js';

export const title = 'Система';

const TABS = [
  { value: 'trading', label: 'Торговля' },
  { value: 'strategies', label: 'Стратегии' },
  { value: 'notify', label: 'Уведомления' },
  { value: 'connect', label: 'Подключения' },
  { value: 'diag', label: 'Ошибки и выгрузки' },
  { value: 'log', label: 'Журнал' },
  { value: 'update', label: 'Обновление' },
];
let tab = 'trading';
let rerender = () => {};
export function bindRefresh(fn) { rerender = fn; }

const drafts = { strategies: {}, portfolio: null, notify: null };

export function render(store, args) {
  if (args[0] && TABS.some(t => t.value === args[0])) tab = args[0];
  const settings = cachedJSON('/api/settings', 30000);
  const s = settings.value;
  const writable = !!(s && s.writable);
  let body;
  if (!s && ['trading', 'strategies', 'notify', 'connect'].includes(tab)) {
    body = settings.error ? h('div', { class: 'card' }, empty('Настройки не загрузились', settings.error))
      : h('div', { class: 'card skeleton', style: { height: '320px' } });
  } else if (tab === 'trading') body = trading(store.data, s);
  else if (tab === 'strategies') body = strategies(store.data, s);
  else if (tab === 'notify') body = notifications(s);
  else if (tab === 'connect') body = connections(s);
  else if (tab === 'diag') body = diagnostics(store.data);
  else if (tab === 'log') body = logView();
  else body = updates();
  return h('div', null,
    h('div', { class: 'page-head' },
      h('div', null, h('h1', { class: 'page-title' }, title),
        h('div', { class: 'page-sub' }, 'Как бот торгует, о чём сообщает, откуда берёт данные и что с ним происходит'))),
    h('div', { class: 'toolbar', style: { marginBottom: '18px' } },
      segmented(TABS, tab, (v) => { location.hash = `#/system/${v}`; }, 'Раздел')),
    s && !writable && ['trading', 'strategies', 'notify', 'connect'].includes(tab)
      ? h('div', { style: { marginBottom: '14px' } }, notice('info', 'Изменение настроек выключено',
        'Панель слушает не localhost, а пароля у неё нет. Включить осознанно — DASHBOARD_ALLOW_CONTROL=true.')) : null,
    h('div', { class: 'fade-in' }, body));
}

/* ── Общие кусочки ──────────────────────────────────────────────────── */
function setting(name, control, note = null) {
  return h('div', { class: 'setting' }, h('div', { class: 'setting-name' }, name), control,
    note ? h('div', { class: 'setting-note' }, note) : null);
}

function numberInput(value, onInput, opts = {}) {
  const el = h('input', { class: 'input', type: 'number', value: value ?? '', step: opts.step, min: opts.min, max: opts.max,
    disabled: opts.disabled, 'aria-label': opts.label });
  el.addEventListener('input', () => onInput(el.value === '' ? null : Number(el.value)));
  return opts.unit != null ? h('span', { class: 'unit-wrap' }, el, h('span', null, opts.unit)) : el;
}

function toggleSwitch(checked, onChange, disabled = false, label = '') {
  const input = h('input', { type: 'checkbox', checked, disabled, 'aria-label': label });
  input.addEventListener('change', () => onChange(input.checked));
  return h('label', { class: 'switch' }, input, h('span'));
}

async function saveSettings(payload, okText = 'Сохранено') {
  try {
    const r = await postJSON('/api/settings', payload);
    const warn = (r && r.warnings) || [];
    if (warn.length) toast('Не всё применено. ' + warn.join(' · '), 'bad');
    else toast(okText, 'good');
    invalidate('/api/settings');
    refreshData();
    return r;
  } catch (e) {
    toast('Не сохранено: ' + e.message, 'bad');
    return null;
  }
}

/* ══ Торговля: пауза и пределы портфеля ═════════════════════════════ */
function trading(data, s) {
  const writable = !!s.writable;
  const p = (data && data.portfolio) || {};
  const paused = data && data.status && data.status.state === 'paused';
  const pf = s.portfolio || {};
  const d = drafts.portfolio || (drafts.portfolio = {
    portfolio_risk_pct: pf.portfolio_risk_pct ?? p.limit_pct ?? 0,
    portfolio_max_positions: pf.portfolio_max_positions ?? p.limit_positions ?? 0,
    daily_loss_pct: pf.daily_loss_pct ?? p.day_limit ?? 0,
  });
  const bar = p.limit_pct > 0 ? Math.min(100, (p.risk_pct || 0) / p.limit_pct * 100) : 0;
  const pauseBtn = button(paused ? 'Продолжить торговлю' : 'Пауза', async () => {
    if (!paused && !(await confirmSheet('Поставить бота на паузу? Новые сделки открываться не будут; открытые позиции ведутся как обычно.', 'Пауза'))) return;
    try { await postJSON('/api/action', { action: paused ? 'resume' : 'pause' }); toast(paused ? 'Торговля продолжена' : 'Бот на паузе'); }
    catch (e) { toast(e.message, 'bad'); }
    refreshData();
  }, paused ? '' : 'gray');
  pauseBtn.disabled = !writable;
  return h('div', { class: 'grid', style: { gap: '18px' } },
    h('div', { class: 'card', style: { display: 'flex', alignItems: 'center', gap: '16px', flexWrap: 'wrap' } },
      h('span', { class: 'status-dot ' + (paused ? 'bad' : 'live'), style: { width: '12px', height: '12px' } }),
      h('div', { style: { flex: '1', minWidth: '220px' } },
        h('div', { class: 'section-title' }, paused ? 'Торговля на паузе' : 'Бот торгует'),
        h('div', { class: 'muted', style: { fontSize: '13px', marginTop: '4px' } },
          'Пауза останавливает только открытие новых сделок. Открытые позиции ведутся: стопы, безубыток, цели и выход по времени работают.')),
      pauseBtn),
    h('div', { class: 'card' },
      h('div', { class: 'card-title' }, 'Пределы на весь портфель теста'),
      h('div', { style: { display: 'flex', alignItems: 'baseline', gap: '10px', flexWrap: 'wrap' } },
        h('span', { class: 'big-sm ' + (p.over ? 'down' : '') }, money(p.risk_usd, 0)),
        h('span', { class: 'muted' }, `под риском · ${pct(p.risk_pct, 2)} от ${money(p.deposit, 0)} · позиций и заявок ${num(p.slots)}`)),
      p.limit_pct > 0 ? h('div', { class: 'meter-track', style: { marginTop: '10px' } },
        h('i', { style: { width: `${bar}%`, background: p.over ? 'var(--down)' : 'var(--accent)' } })) : null,
      h('div', { class: 'muted', style: { fontSize: '13px', margin: '10px 0 6px' } }, 'Сегодня ',
        h('b', { class: tone(p.day_pnl) }, signedMoney(p.day_pnl)), ` (${signedPct(p.day_pct)})`,
        p.day_stopped ? h('b', { class: 'down' }, ' · торговля остановлена до завтра') : null),
      (p.limits_off || []).length ? h('div', { style: { margin: '12px 0' } }, notice('warn', `Выключено: ${p.limits_off.join(', ')}`,
        (p.worst_by_strategy || []).length ? 'Заняв все слоты, каждая стратегия держит под риском своих денег: '
          + p.worst_by_strategy.map(w => (w.pct === null ? `${strategyTitle(w.name)} — без предела слотов` : `${strategyTitle(w.name)} до ${w.pct}%`)).join(', ') : null)) : null,
      h('div', { class: 'rows', style: { marginTop: '8px' } },
        setting('Максимум под риском', numberInput(d.portfolio_risk_pct, v => { d.portfolio_risk_pct = v; }, { step: 0.5, min: 0, max: 100, unit: '%', disabled: !writable }),
          'Сколько процентов счёта может стоять под риском одновременно. 0 — предел выключен.'),
        setting('Максимум позиций', numberInput(d.portfolio_max_positions, v => { d.portfolio_max_positions = v; }, { step: 1, min: 0, max: 60, unit: '', disabled: !writable }),
          'Открытых позиций и ожидающих заявок суммарно. 0 — предел выключен.'),
        setting('Дневной предел убытка', numberInput(d.daily_loss_pct, v => { d.daily_loss_pct = v; }, { step: 0.5, min: 0, max: 50, unit: '%', disabled: !writable }),
          'Потеряли за день больше — новые сделки до завтра не открываются. Считается по закрытым сделкам всех стратегий. 0 — выключен.')),
      h('div', { class: 'actions', style: { marginTop: '14px' } },
        Object.assign(button('Применить', async () => {
          if (await saveSettings({ PORTFOLIO: { portfolio_risk_pct: d.portfolio_risk_pct || 0, portfolio_max_positions: Math.round(d.portfolio_max_positions || 0), daily_loss_pct: d.daily_loss_pct || 0 } }, 'Пределы применены')) drafts.portfolio = null;
        }), { disabled: !writable }),
        Object.assign(button('Выключить все', async () => {
          if (!(await confirmSheet('Выключить все пределы портфеля?', 'Выключить'))) return;
          if (await saveSettings({ PORTFOLIO: { portfolio_risk_pct: 0, portfolio_max_positions: 0, daily_loss_pct: 0 } }, 'Пределы выключены')) drafts.portfolio = null;
        }, 'gray'), { disabled: !writable })),
      h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '12px' } },
        'Пределы не трогают открытые позиции: они решают только, открывать ли новую. Стратегия, упёршаяся в предел, пишет причину в журнал.')));
}

/* ══ Стратегии: деньги и правила тестовых счетов ════════════════════ */
const SIDES = [{ value: 'both', label: 'Обе' }, { value: 'long', label: 'Лонг' }, { value: 'short', label: 'Шорт' }];
const FIELDS = ['enabled', 'risk_pct', 'min_stop_pct', 'max_slots', 'deposit', 'sides', 'critic'];

function strategies(data, s) {
  const writable = !!s.writable;
  const paper = data ? !!data.paper : true;
  const keys = Object.keys(s.settings || {});
  const value = (k, f) => (drafts.strategies[k] && f in drafts.strategies[k] ? drafts.strategies[k][f] : (s.settings[k] || {})[f]);
  const set = (k, f, v) => { (drafts.strategies[k] = drafts.strategies[k] || {})[f] = v; changed(); };
  const dirty = () => Object.values(drafts.strategies).some(o => Object.keys(o).length);
  const lim = f => (s.limits || {})[f] || [];
  const msg = h('span', { class: 'muted', style: { fontSize: '13px' } });
  const changed = () => { msg.textContent = dirty() ? 'Есть несохранённые изменения' : 'Изменений нет'; };
  const started = k => {
    const live = ((data || {}).strategies || {})[k] || {};
    return (live.trades || 0) > 0 || (live.open || 0) > 0 || (live.pending || 0) > 0;
  };

  const cards = keys.map(k => {
    const own = (s.own || {})[k] || {};
    const live = ((data || {}).strategies || {})[k] || {};
    const begun = started(k);
    const knobOff = own.min_stop_knob === false;
    const ownStop = isNum(own.min_stop_pct) ? pct(own.min_stop_pct, 2) : '—';
    const slots = value(k, 'max_slots');
    const held = (live.open || 0) + (live.pending || 0);
    const depBtn = button(begun ? 'Начать заново' : 'Задать', async () => {
      const dep = Number(value(k, 'deposit'));
      if (!isNum(dep) || dep <= 0) { toast('Укажите депозит', 'bad'); return; }
      if (begun && !(await confirmSheet(`Начать отсчёт «${strategyTitle(k)}» заново с ${money(dep, 0)}? Прежние сделки останутся в журнале и выгрузке, но в статистику стратегии входить не будут.${held ? ` Открытые позиции и заявки (${num(held)}) будут сняты со счёта без закрытия по рынку.` : ''}`, 'Начать заново', true))) return;
      try {
        const r = await postJSON('/api/deposit', { strategy: k, deposit: dep, restart: begun });
        toast(r.message || 'Депозит задан', 'good');
        if (drafts.strategies[k]) delete drafts.strategies[k].deposit;
        invalidate('/api/settings'); refreshData();
      } catch (e) { toast('Не применено: ' + e.message, 'bad'); }
    }, 'gray', true);
    depBtn.disabled = !writable;
    return h('div', { class: 'card' },
      h('div', { class: 'strat-head', style: { marginBottom: '6px' } }, strategyBadge(k),
        h('div', { style: { minWidth: 0 } }, h('div', { class: 'strat-name' }, strategyTitle(k)),
          h('div', { class: 'strat-sub' }, value(k, 'enabled') ? 'торгует' : 'выключена')),
        h('div', { style: { marginLeft: 'auto' } }, toggleSwitch(!!value(k, 'enabled'), v => { set(k, 'enabled', v); rerender(); }, !writable, `Включить ${strategyTitle(k)}`))),
      h('div', { class: 'subhead', style: { marginTop: '12px' } }, 'Тестовый счёт'),
      h('div', { class: 'rows' },
        paper ? setting('Стартовый депозит', h('span', { class: 'actions' },
          numberInput(value(k, 'deposit'), v => set(k, 'deposit', v), { step: 100, min: lim('deposit')[0], unit: '$', disabled: !writable }), depBtn),
          begun ? `Стратегия уже торгует (${num(live.trades || 0)} сделок): новый депозит — только кнопкой «Начать заново».`
            + (isNum(live.start_balance) && Math.abs(Number(value(k, 'deposit')) - live.start_balance) >= 0.01
              ? ` Сейчас счёт ведётся от ${money(live.start_balance, 0)}.` : '')
            : 'Стратегия ещё не торговала — депозит меняется сразу.') : null,
        setting('Риск на сделку', numberInput(value(k, 'risk_pct'), v => set(k, 'risk_pct', v), { step: 0.05, min: lim('risk_pct')[0], max: lim('risk_pct')[1], unit: '%', disabled: !writable }),
          'Доля депозита, теряемая при срабатывании стопа. На тесте у всех стратегий одна — 1%.'),
        setting('Позиций одновременно', numberInput(slots, v => set(k, 'max_slots', v), { step: 1, min: 0, max: 60, unit: '', disabled: !writable }),
          Number(slots) > 0 ? `Больше ${slots} позиций стратегия не откроет: следующий сетап будет отброшен.` : '0 — без предела: стратегия берёт столько сетапов, сколько нашла.'),
        setting('Стороны', segmented(SIDES, value(k, 'sides') || 'both', v => set(k, 'sides', v), 'Стороны'),
          'Выключенная сторона — это сделки, которых не будет.')),
      h('div', { class: 'subhead' }, 'Правила стратегии'),
      h('div', { class: 'rows' },
        setting('Минимальный стоп', numberInput(knobOff ? own.min_stop_pct : value(k, 'min_stop_pct'), v => set(k, 'min_stop_pct', v),
          { step: 0.1, min: lim('min_stop_pct')[0], max: lim('min_stop_pct')[1], unit: '%', disabled: !writable || knobOff }),
          knobOff ? `Не применяется: стратегия считает минимальный стоп по-своему (сейчас ${ownStop}), это поле ей не читается.`
            : 'Ближе этого стоп не ставится — сетап с более тесным стопом не берётся.'),
        k === 'LLM' ? setting('Критик', toggleSwitch(value(k, 'critic') !== false, v => set(k, 'critic', v), !writable, 'Критик ИИ'),
          'Второе мнение о каждом плане: та же модель ищет, почему план не сработает. Выключенный — планы идут в сделку сразу после проверок кода.') : null));
  });

  const apply = button('Применить', async () => {
    const payload = {};
    for (const k of keys) {
      const p = {};
      for (const f of FIELDS) {
        if (f === 'critic' && k !== 'LLM') continue;
        if (f === 'deposit' && !paper) continue;
        // Депозит торговавшей стратегии меняется только кнопкой «Начать
        // заново»: «Применить» записал бы его в настройки, а счёт его не
        // примет (10.10.2026 — так у ФИБО разошлись 10 000 и 20 000).
        if (f === 'deposit' && started(k)) continue;
        const v = value(k, f);
        if (v !== undefined) p[f] = v;
      }
      payload[k] = p;
    }
    if (await saveSettings(payload, 'Настройки применены')) { drafts.strategies = {}; rerender(); }
  });
  apply.disabled = !writable;
  const reset = button('Отменить', () => { drafts.strategies = {}; rerender(); }, 'gray');
  changed();
  return h('div', null,
    h('div', { class: 'grid grid-2' }, cards),
    h('div', { class: 'sticky-actions' }, msg, h('span', { style: { marginLeft: 'auto' } }), reset, apply),
    h('div', { class: 'muted', style: { fontSize: '12px', margin: '10px 4px' } },
      'Изменения вступают в силу со следующего цикла. Открытые позиции не трогаются: их размер и стоп посчитаны при входе.'));
}

/* ══ Уведомления ════════════════════════════════════════════════════ */
const EVENTS = [
  ['trade_opened', 'Вход в сделку'], ['trade_closed', 'Закрытие сделки'], ['tp_hit', 'Взята частичная цель'],
  ['breakeven', 'Стоп в безубыток'], ['plan_dropped', 'Заявка или план сняты без сделки'],
  ['llm_setup', 'Сетап ИИ (план с графиком)'], ['llm_rejected', 'Отказ плана ИИ'], ['error', 'Ошибки бота'],
  ['daily', 'Дневная сводка'], ['service', 'Запуск бота'], ['account_orders', 'Инструкции по счетам без API (проп)'],
];

function notifications(s) {
  const writable = !!s.writable;
  const cur = s.notify || {};
  const d = drafts.notify || (drafts.notify = {});
  const on = key => (key in d ? d[key] : cur[key] !== false);
  const apply = button('Применить', async () => {
    const payload = {};
    for (const [e] of EVENTS) payload[`${e}_telegram`] = on(`${e}_telegram`);
    if (await saveSettings({ NOTIFY: payload }, 'Уведомления сохранены')) drafts.notify = null;
  });
  apply.disabled = !writable;
  return h('div', { class: 'card', style: { maxWidth: '760px' } },
    h('div', { class: 'card-title' }, 'Сообщения в Telegram'),
    h('div', { class: 'rows' }, EVENTS.map(([e, label]) =>
      setting(label, toggleSwitch(on(`${e}_telegram`), v => { d[`${e}_telegram`] = v; }, !writable, label)))),
    h('div', { class: 'actions', style: { marginTop: '14px' } }, apply),
    h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '12px' } },
      'Выключенное уведомление не влияет на торговлю: событие всё равно попадёт в журнал и на панель. Те же настройки — в Telegram-панели бота.'));
}

/* ══ Подключения ════════════════════════════════════════════════════ */
const EX_NAMES = { bybit: 'Bybit', bingx: 'BingX' };
const CAPS = {
  fetchOpenInterestHistory: 'открытый интерес', fetchLongShortRatioHistory: 'соотношение лонг/шорт',
  fetchPremiumIndexOHLCV: 'премия над индексом', fetchFundingRateHistory: 'ставки фандинга',
};

function connections(s) {
  const ex = s.exchange || {};
  const writable = !!s.writable;
  const sources = cachedJSON('/api/sources', 30000);
  const exSeg = segmented((ex.supported || []).map(n => ({ value: n, label: `${EX_NAMES[n] || n}${(ex.configured || {})[n] ? '' : ' · нет ключей'}` })),
    ex.active, async (v) => {
      if (!writable || !(ex.configured || {})[v] || v === ex.active) { rerender(); return; }
      if (!(await confirmSheet(`Переключить рынок на ${EX_NAMES[v] || v}? Изменение вступит в силу после перезапуска бота: менять подключение на ходу нельзя, пока есть открытые ордера.`, 'Переключить'))) { rerender(); return; }
      await saveSettings({ EXCHANGE: { name: v } }, 'Биржа выбрана — применится после перезапуска');
    }, 'Биржа');
  const caps = ex.capabilities || {};
  const list = (sources.value && sources.value.sources) || [];
  return h('div', { class: 'grid', style: { gap: '18px' } },
    h('div', { class: 'grid grid-2' },
      h('div', { class: 'card' },
        h('div', { class: 'card-title' }, 'Биржа данных и тестовой торговли'),
        exSeg,
        h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '10px' } },
          `Сейчас ${EX_NAMES[ex.active] || ex.active || '—'}${ex.chosen && ex.chosen !== ex.active ? ` · выбрана ${EX_NAMES[ex.chosen] || ex.chosen}, применится после перезапуска` : ''}.`),
        h('div', { class: 'subhead' }, 'Что отдаёт эта биржа'),
        caps.error ? h('div', { class: 'muted' }, `Подключение не проверено: ${caps.error}`)
          : h('div', { class: 'rows' }, Object.entries(CAPS).map(([k, label]) =>
            setting(label, pill(caps[k] ? 'есть' : 'нет', caps[k] ? 'up' : 'down'))))),
      h('div', { class: 'card' },
        h('div', { class: 'card-title' }, 'Ключи бирж'),
        h('div', { class: 'why' }, 'Ключи задаются для каждого торгового счёта: выберите биржу и режим (демо или реальный), введите ключи — они проверяются на бирже и хранятся только на сервере.'),
        h('div', { style: { marginTop: '14px' } }, h('a', { class: 'btn gray sm', href: '#/accounts', style: { textDecoration: 'none' } }, 'Открыть «Счета»')))),
    h('div', { class: 'card' },
      h('div', { class: 'card-title' }, 'Источники данных'),
      sources.value ? h('div', null, list.map(source)) : sources.error ? empty('Не загрузились', sources.error)
        : h('div', { class: 'skeleton', style: { height: '200px' } }),
      sources.value ? h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '12px' } },
        h('span', null, 'Адрес, запасные адреса и прокси меняются файлом '), h('span', { class: 'mono' }, sources.value.file || 'data_sources.json'),
        h('span', null, ' на сервере, а не здесь: у панели нет пароля, а подменённый адрес — это подменённые котировки. Отказ источника торговлю не останавливает.')) : null));
}

function source(s) {
  const tonePill = s.status === 'отвечает' ? 'up' : s.status === 'сбой' ? 'down' : '';
  const okAgo = s.last_ok ? ago(s.last_ok * 1000) : 'ещё ни разу';
  const health = s.status === 'нет запросов' ? 'запросов с запуска не было'
    : `отвечал ${okAgo}${isNum(s.ms) ? ` · ${Math.round(s.ms)} мс` : ''} · ответов ${num(s.ok)}${s.fail ? `, отказов ${num(s.fail)}` : ''}`;
  return h('div', { class: 'src' },
    h('div', { class: 'src-head' }, h('b', null, s.title), pill(s.status, tonePill)),
    h('div', { class: 'src-line' }, h('span', { class: 'mono' }, s.url),
      (s.fallbacks || []).length ? ` · запасные: ${s.fallbacks.join(', ')}` : '', s.proxy ? ` · прокси: ${s.proxy}` : '',
      s.overridden ? ' · задано оператором' : ''),
    h('div', { class: 'src-line' }, 'даёт: ' + (s.provides || []).join(', ')),
    h('div', { class: 'src-line' }, 'читают: ' + (s.readers || []).join(', ') + (s.limit ? ` · ${s.limit}` : '')),
    h('div', { class: 'src-line' }, health),
    s.status === 'сбой' && s.error ? h('div', { class: 'src-line down' }, s.error) : null);
}

/* ══ Ошибки и выгрузки ══════════════════════════════════════════════ */
function diagnostics(data) {
  const res = cachedJSON('/api/errors', 30000);
  const d = res.value;
  const errs = (d && d.errors) || [];
  const cats = {};
  errs.forEach(g => { cats[g.category] = (cats[g.category] || 0) + g.count; });
  const clear = button('Очистить', async () => {
    if (!(await confirmSheet('Очистить список ошибок? Имеет смысл после того, как разобрались: счётчик покажет, вернулась ли проблема.', 'Очистить'))) return;
    try { const r = await postJSON('/api/errors/clear', {}); toast(r.message || 'Очищено'); }
    catch (e) { toast(e.message, 'bad'); }
    invalidate('/api/errors'); refreshData();
  }, 'gray', true);
  clear.disabled = !(d && d.writable) || !errs.length;
  const codes = Object.keys((data && data.strategies) || {});
  return h('div', { class: 'grid', style: { gap: '18px' } },
    h('div', { class: 'card' },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '10px', flexWrap: 'wrap', marginBottom: '10px' } },
        h('div', { class: 'card-title', style: { margin: 0 } }, 'Ошибки', errs.length ? ` · ${errs.length} ${plural(errs.length, 'вид', 'вида', 'видов')}` : ''),
        h('div', { class: 'actions' }, button('Обновить', () => invalidate('/api/errors'), 'gray', true), clear)),
      !d ? (res.error ? empty('Не загрузились', res.error) : h('div', { class: 'skeleton', style: { height: '160px' } }))
        : !errs.length ? empty('Ошибок не было', 'Здесь появится всё, на чём бот споткнулся: недоступная биржа, отказ в правах на файл, сбой стратегии.')
          : h('div', null,
            h('div', { class: 'tags', style: { marginBottom: '8px' } }, Object.entries(cats).sort((a, b) => b[1] - a[1]).map(([c, n]) => pill(`${c} · ${n}`))),
            errs.map(errorRow))),
    h('div', { class: 'card' },
      h('div', { class: 'card-title' }, 'Выгрузки'),
      h('div', { class: 'rows' },
        setting('Отчёт для разбора', exportButton('report', 'Выгрузить отчёт'),
          'Версия, режим, настройки, ошибки с трассировками и хвост журнала — одним файлом. Ключи и токены вырезаются, но пробегите файл глазами перед отправкой.'),
        setting('Таблица сделок', exportButton('csv', 'CSV'), 'Все закрытые сделки — для Excel.'),
        setting('Полный дамп', exportButton('jsonl', 'JSONL'), 'Сделки со всеми полями — для разбора программой.'),
        setting('Журналы сетапов по стратегиям', h('div', { class: 'actions' }, codes.map(c => exportButton(`journal-${c}`, strategyTitle(c)))),
          'Строка на каждый сетап: что нашла стратегия, что с ним стало.'))));
}

function errorRow(g) {
  const sample = (g.samples || [])[0] || {};
  const ctx = sample.context ? Object.entries(sample.context).map(([k, v]) => `${k}: ${v}`).join(' · ') : '';
  return h('details', { class: 'err' },
    h('summary', null, pill(g.category, g.level === 'WARN' ? 'warn' : 'down'), h('span', { class: 'err-sig' }, g.signature),
      h('span', { class: 'err-count' }, `${num(g.count)}×`), h('span', { class: 'err-when' }, dateTime(g.last))),
    h('div', { class: 'err-body' },
      h('div', null, `впервые ${dateTime(g.first)} · последний раз ${dateTime(g.last)} · всего ${num(g.count)}`),
      ctx ? h('div', { class: 'muted', style: { marginTop: '4px' } }, ctx) : null,
      (g.samples || []).map(sm => h('div', { style: { marginTop: '10px' } },
        h('div', { class: 'muted', style: { fontSize: '12px' } }, dateTime(sm.at)), h('div', null, sm.text),
        sm.traceback ? h('pre', null, sm.traceback) : null))));
}

/* Выгрузка: в окне приложения (pywebview) файл сохраняет сервер и называет
   путь; в браузере — обычное скачивание. Так было и в прежней панели. */
const EXPORTS = { csv: '/api/export.csv', jsonl: '/api/export.jsonl', report: '/api/report.txt' };

function exportButton(kind, text) {
  return button(text, () => doExport(kind), 'gray', true);
}

async function doExport(kind) {
  if (typeof window.pywebview !== 'undefined') {
    try {
      const r = await getJSON('/api/export/save?kind=' + encodeURIComponent(kind));
      if (!r.ok) throw new Error(r.error || 'не сохранилось');
      toast(`Сохранено (${Math.max(1, Math.round(r.bytes / 1024))} КБ): ${r.path}`, 'good');
    } catch (e) { toast('Не сохранилось: ' + e.message, 'bad'); }
    return;
  }
  const url = kind.startsWith('journal-') ? '/api/journal.csv?strategy=' + encodeURIComponent(kind.slice(8)) : EXPORTS[kind];
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error('сервер ответил ' + r.status);
    const blob = await r.blob();
    const disp = r.headers.get('Content-Disposition') || '';
    const star = /filename\*=UTF-8''([^;]+)/i.exec(disp), plain = /filename="([^"]+)"/i.exec(disp);
    const name = star ? decodeURIComponent(star[1]) : plain ? plain[1] : `${kind}.txt`;
    const a = h('a', { href: URL.createObjectURL(blob), download: name });
    document.body.append(a); a.click(); a.remove();
    toast('Скачано: ' + name, 'good');
  } catch (e) { toast('Не скачалось: ' + e.message, 'bad'); }
}

/* ══ Журнал ═════════════════════════════════════════════════════════ */
function logClass(line) {
  if (/❌|⚠️|ошибк|Error|Traceback/i.test(line)) return 'l-err';
  if (/🟢|✅|TP\d|ВХОД/.test(line)) return 'l-win';
  if (/👻|ЦИКЛ|===/.test(line)) return 'l-act';
  return '';
}

let logScrollAtEnd = true;
function logView() {
  const res = cachedJSON('/api/log', 10000);
  const lines = (res.value && res.value.lines) || [];
  const pre = h('pre', { class: 'logview' }, lines.map((l, i) => [i ? '\n' : '', h('span', { class: logClass(l) }, l)]));
  pre.addEventListener('scroll', () => { logScrollAtEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40; });
  requestAnimationFrame(() => { if (logScrollAtEnd) pre.scrollTop = pre.scrollHeight; });
  return h('div', { class: 'card' },
    h('div', { class: 'card-title' }, `Журнал бота · последние ${lines.length} строк`, h('span', { class: 'muted', style: { fontWeight: 400 } }, ' · обновляется сам')),
    res.value ? pre : res.error ? empty('Журнал не загрузился', res.error) : h('div', { class: 'skeleton', style: { height: '400px' } }));
}

/* ══ Обновление и история настроек ══════════════════════════════════ */
const FIELD_NAMES = {
  enabled: 'торгует', risk_pct: 'риск на сделку', min_stop_pct: 'минимальный стоп', deposit: 'стартовый депозит',
  max_slots: 'одновременных позиций', max_same_direction: 'позиций в одну сторону', sides: 'стороны',
  notify: 'сообщения в Telegram', 'перенос': 'перенос денег в счета', critic: 'критик ИИ',
  portfolio_risk_pct: 'предел портфеля, %', portfolio_max_positions: 'предел позиций', daily_loss_pct: 'дневной предел убытка, %',
};
const CHANNELS = { telegram: 'Telegram', desktop: 'на рабочем столе' };
const EVENT_NAMES = Object.fromEntries(EVENTS);
const fieldLabel = (path) => {
  const [section, field = ''] = String(path).split('.');
  if (section === 'NOTIFY') {
    const m = /^(.*)_(telegram|desktop)$/.exec(field);
    return m ? `Уведомления · ${EVENT_NAMES[m[1]] || m[1]} (${CHANNELS[m[2]]})` : `Уведомления · ${field}`;
  }
  const where = section === 'PORTFOLIO' ? 'Портфель' : section === 'EXCHANGE' ? 'Биржа' : strategyTitle(section);
  return `${where} · ${FIELD_NAMES[field] || field}`;
};
const SIDE_WORDS = { both: 'обе', long: 'только лонги', short: 'только шорты' };
const fieldValue = v => (typeof v === 'boolean' ? (v ? 'да' : 'нет') : v == null ? '—' : SIDE_WORDS[v] || String(v));

function updates() {
  const res = cachedJSON('/api/update', 60000);
  const hist = cachedJSON('/api/settings/history', 60000);
  const u = res.value && res.value.update;
  const writable = !!(res.value && res.value.writable);
  let card;
  if (!u) {
    card = h('div', { class: 'card' }, res.error ? empty('Не загрузилось', res.error) : h('div', { class: 'skeleton', style: { height: '160px' } }));
  } else {
    const c = u.current || {};
    const run = async (url, ask, ok) => {
      if (!(await confirmSheet(ask, ok))) return;
      try { const r = await postJSON(url, {}); toast(r.message || 'Готово', 'good'); }
      catch (e) { toast('Не выполнено: ' + e.message, 'bad'); }
      invalidate('/api/update');
    };
    const apply = button('Обновить', () => run('/api/update', 'Обновить код бота? После обновления прогоняются тесты: не прошли — код вернётся на прежнюю версию сам.', 'Обновить'));
    apply.disabled = !(writable && u.can_update);
    card = h('div', { class: 'card' },
      h('div', { class: 'card-title' }, 'Версия'),
      h('div', { style: { display: 'flex', alignItems: 'baseline', gap: '10px', flexWrap: 'wrap' } },
        h('span', { class: 'big-sm mono', style: { fontSize: '22px' } }, c.commit || '—'),
        h('span', { class: 'muted' }, [c.date, u.branch ? `ветка ${u.branch}` : null].filter(Boolean).join(' · '))),
      h('div', { class: 'why', style: { marginTop: '6px' } }, c.subject || ''),
      !u.available ? h('div', { style: { marginTop: '12px' } }, notice('warn', 'Обновление недоступно', u.reason || '')) : null,
      u.available ? h('div', { style: { marginTop: '14px' } }, u.behind
        ? pill(`${u.behind} ${plural(u.behind, 'обновление', 'обновления', 'обновлений')} доступно`, 'accent')
        : pill('установлена последняя версия', 'up')) : null,
      (u.pending || []).length ? h('div', { class: 'list', style: { marginTop: '10px' } }, u.pending.map(p => h('div', { class: 'list-item', style: { gridTemplateColumns: 'auto minmax(0, 1fr)' } },
        h('span', { class: 'mono' }, p.commit), h('div', null, h('div', null, p.subject), h('div', { class: 'meta' }, p.date, p.notes ? ` · ${p.notes}` : ''))))) : null,
      u.restart_required ? h('div', { style: { marginTop: '12px' } }, notice('warn', 'Нужен перезапуск', 'Код обновлён, но работает ещё старый: Python держит загруженные модули в памяти.')) : null,
      u.available ? h('div', { class: 'actions', style: { marginTop: '16px' } },
        button('Проверить', async () => { try { await getJSON('/api/update?check=1'); } catch (e) { toast(e.message, 'bad'); } invalidate('/api/update'); }, 'gray'),
        apply,
        u.previous ? Object.assign(button('Откатить', () => run('/api/update/rollback', 'Вернуть прежнюю версию кода?', 'Откатить'), 'gray'), { disabled: !writable }) : null) : null,
      h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '12px' } },
        'Обновление переносит только код. Журнал сделок, открытые позиции, настройки и ключи лежат вне репозитория.'));
  }
  const items = (hist.value && hist.value.history) || [];
  return h('div', { class: 'grid', style: { gap: '18px' } }, card,
    h('div', { class: 'card' },
      h('div', { class: 'card-title' }, 'Что меняли в настройках'),
      !hist.value ? h('div', { class: 'skeleton', style: { height: '120px' } })
        : !items.length ? empty('Настройки ещё не менялись', '')
          : h('div', { class: 'list' }, items.map(rec => h('div', { class: 'list-item', style: { gridTemplateColumns: '130px minmax(0, 1fr)', alignItems: 'start' } },
            h('div', { class: 'meta' }, dateTime(rec.at)),
            h('div', null, (rec.changes || []).map(ch => h('div', { style: { fontSize: '14px' } },
              h('span', { class: 'muted' }, fieldLabel(ch.field) + ': '),
              h('s', { class: 'muted' }, fieldValue(ch.from)), ' → ', h('b', null, fieldValue(ch.to))))))))));
}
