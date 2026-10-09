/* Новая панель Kraken: маршруты, меню, тема, статус бота.
   Разделы — от денег: Главная → Стратегии → Сделки → Счета → ИИ → Система. */

import { h, icon } from './ui.js';
import { ago, num } from './format.js';
import { start, subscribe, getStore } from './api.js';
import * as home from './pages/home.js';
import * as strategies from './pages/strategies.js';
import * as trades from './pages/trades.js';
import * as accounts from './pages/accounts.js';
import * as ai from './pages/ai.js';
import * as system from './pages/system.js';

const ROUTES = [
  { id: 'home', title: 'Главная', icon: 'home', page: home },
  { id: 'strategies', title: 'Стратегии', icon: 'strategies', page: strategies },
  { id: 'trades', title: 'Сделки', icon: 'trades', page: trades },
  { id: 'accounts', title: 'Счета', icon: 'accounts', page: accounts },
  { id: 'ai', title: 'ИИ', icon: 'ai', page: ai },
  { id: 'system', title: 'Система', icon: 'system', page: system },
];

const main = document.getElementById('main');
let current = null;

function route() {
  const [id, ...rest] = (location.hash.replace(/^#\/?/, '') || 'home').split('/');
  const r = ROUTES.find(x => x.id === id) || ROUTES[0];
  return { route: r, args: rest };
}

function counts(store) {
  const d = store.data || {};
  return {
    trades: (d.open_positions || []).length + (d.pending_orders || []).length,
    accounts: ((store.accounts || {}).accounts || []).length,
  };
}

function navLink(r, active, count, compact) {
  return h('a', { href: `#/${r.id}`, 'aria-current': active ? 'page' : null },
    icon(r.icon), compact ? h('span', null, r.title) : r.title,
    !compact && count ? h('span', { class: 'count' }, num(count)) : null);
}

function renderNav(store) {
  const { route: r } = route();
  const c = counts(store);
  document.getElementById('nav').replaceChildren(...ROUTES.map(x => navLink(x, x === r, c[x.id], false)));
  document.getElementById('tabbar').replaceChildren(...ROUTES.map(x => navLink(x, x === r, 0, true)));
}

function renderStatus(store) {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  const d = store.data;
  if (store.error && !d) {
    dot.className = 'status-dot bad';
    text.replaceChildren('Нет связи с сервером', h('small', null, store.error));
    return;
  }
  if (!d) return;
  const st = d.status || {};
  const running = st.state === 'running';
  dot.className = 'status-dot ' + (store.error ? 'bad' : running ? 'live' : 'bad');
  const label = store.error ? 'Связь прервалась' : running ? 'Бот работает' : `Бот: ${st.state || 'неизвестно'}`;
  text.replaceChildren(label, h('small', null,
    store.error ? 'показаны последние данные' : `запущен ${ago(st.since || d.started_at)} · обновлено ${ago(store.updatedAt)}`));
  document.getElementById('brand-sub').textContent = d.paper ? 'тест стратегий' : 'торговля';
}

function renderPage() {
  const store = getStore();
  const { route: r, args } = route();
  if (current !== r) {
    current = r;
    document.title = `${r.title} — Kraken`;
    main.scrollTop = 0;
    window.scrollTo(0, 0);
  }
  if (r.page.bindRefresh) r.page.bindRefresh(renderPage);
  main.replaceChildren(r.page.render(store, args));
}

function setupTheme() {
  const seg = document.getElementById('theme');
  const currentTheme = () => document.documentElement.dataset.theme || '';
  const mark = () => { for (const b of seg.children) b.setAttribute('aria-pressed', String(b.dataset.theme === currentTheme())); };
  seg.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    const t = b.dataset.theme;
    if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
    try { if (t) localStorage.setItem('kraken.theme', t); else localStorage.removeItem('kraken.theme'); } catch (err) { /* без хранилища — тема на сеанс */ }
    mark();
    renderPage();                 // цвета графиков берутся из темы при отрисовке
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (!currentTheme()) renderPage(); });
  mark();
}

window.addEventListener('hashchange', () => { renderNav(getStore()); renderPage(); });
/* Пока человек печатает в поле раздела, перерисовка по опросу ждёт: иначе
   поле пересоздаётся и фокус с набранным пропадает. Догоняем по уходу фокуса. */
let deferred = false;
const typing = () => {
  const a = document.activeElement;
  return a && main.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName);
};
main.addEventListener('focusout', () => setTimeout(() => {
  if (deferred && !typing()) { deferred = false; renderPage(); }
}));
subscribe((store) => {
  renderNav(store);
  renderStatus(store);
  if (typing()) { deferred = true; return; }
  renderPage();
});
setupTheme();
renderNav(getStore());
renderPage();
start();
