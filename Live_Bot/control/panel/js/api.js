/* Данные панели: тот же API, что у прежней страницы (/api/data,
   /api/accounts). Опрос по таймеру; разделы подписываются на обновления.
   Если на сервере сменились файлы панели (выкатка), открытое окно само
   перезагружается — иначе оно днями показывало бы старую разметку. */

const store = { data: null, accounts: null, error: null, updatedAt: 0 };
const listeners = new Set();
let panelStamp = null;

export function getStore() { return store; }

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function emit() { for (const fn of listeners) { try { fn(store); } catch (e) { console.error(e); } } }

export async function getJSON(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) {
    let why = String(r.status);
    try { why = (await r.json()).error || why; } catch (e) { /* не JSON */ }
    throw new Error(why);
  }
  return r.json();
}

/* Кэш тяжёлых запросов (отчёты стратегий, свечи): не чаще раза в ttl, пока
   идёт обновление — показываются прежние данные. Готовность — через emit(),
   раздел перерисовывается и берёт значение из кэша. */
const cache = new Map();

export function cachedJSON(url, ttl = 60000) {
  const entry = cache.get(url) || { at: 0, value: undefined, error: null, loading: false };
  if (!entry.loading && Date.now() - entry.at > ttl) {
    entry.loading = true;
    entry.at = Date.now();
    getJSON(url)
      .then(v => { entry.value = v; entry.error = null; })
      .catch(e => { entry.error = e.message || String(e); })
      .finally(() => { entry.loading = false; entry.at = Date.now(); emit(); });
  }
  cache.set(url, entry);
  return entry;
}

export async function refreshData() {
  try {
    const data = await getJSON('/api/data');
    if (panelStamp === null) panelStamp = data.panel || '';
    else if (data.panel && data.panel !== panelStamp) { location.reload(); return; }
    store.data = data;
    store.error = null;
    store.updatedAt = Date.now();
  } catch (e) {
    store.error = e.message || String(e);
  }
  emit();
}

export async function refreshAccounts() {
  try {
    store.accounts = await getJSON('/api/accounts');
  } catch (e) {
    store.accounts = store.accounts || { accounts: [], books: {}, error: e.message };
  }
  emit();
}

export function start() {
  refreshData();
  refreshAccounts();
  setInterval(refreshData, 15000);
  setInterval(refreshAccounts, 30000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { refreshData(); refreshAccounts(); }
  });
}
