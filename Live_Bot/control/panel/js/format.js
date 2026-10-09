/* Числа и даты для человека: по-русски, со знаком, настоящим минусом «−» и
   неразрывными пробелами — сумма не рвётся на две строки. */

const NBSP = ' ';
const MINUS = '−';

const nf = (digits) => new Intl.NumberFormat('ru-RU', {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
});
const cache = {};
function fmt(value, digits) {
  const f = cache[digits] || (cache[digits] = nf(digits));
  return f.format(value).replace(/\s/g, NBSP).replace('-', MINUS);
}

export function isNum(v) { return typeof v === 'number' && Number.isFinite(v); }

/** $12 345.67 (знак — только у отрицательных). */
export function money(v, digits = 2) {
  if (!isNum(v)) return '—';
  const s = fmt(Math.abs(v), digits);
  return (v < 0 ? MINUS : '') + '$' + s;
}

/** Крупные суммы без копеек: $12 346. */
export function moneyShort(v) {
  if (!isNum(v)) return '—';
  if (Math.abs(v) >= 1e6) return (v < 0 ? MINUS : '') + '$' + fmt(Math.abs(v) / 1e6, 2) + NBSP + 'млн';
  return money(v, Math.abs(v) >= 1000 ? 0 : 2);
}

/** +$12.30 / −$5.00 / $0.00 */
export function signedMoney(v, digits = 2) {
  if (!isNum(v)) return '—';
  if (Math.abs(v) < 0.005) return '$' + fmt(0, digits);
  return (v > 0 ? '+' : MINUS) + '$' + fmt(Math.abs(v), digits);
}

/** +1.31% / −0.61% */
export function signedPct(v, digits = 2) {
  if (!isNum(v)) return '—';
  if (Math.abs(v) < 0.5 * 10 ** -digits) return fmt(0, digits) + '%';
  return (v > 0 ? '+' : MINUS) + fmt(Math.abs(v), digits) + '%';
}

export function pct(v, digits = 1) { return isNum(v) ? fmt(v, digits) + '%' : '—'; }

/** +0.66R */
export function signedR(v, digits = 2) {
  if (!isNum(v)) return '—';
  return (v > 0 ? '+' : v < 0 ? MINUS : '') + fmt(Math.abs(v), digits) + 'R';
}

export function num(v, digits = 0) { return isNum(v) ? fmt(v, digits) : '—'; }

/** Цена инструмента: значащих цифр столько, сколько нужно её масштабу. */
export function price(v) {
  if (!isNum(v)) return '—';
  const a = Math.abs(v);
  const d = a >= 1000 ? 1 : a >= 100 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 5 : 7;
  return fmt(v, d);
}

/** Класс цвета для результата: прибыль, убыток или ноль. */
export function tone(v) { return !isNum(v) || Math.abs(v) < 1e-9 ? '' : v > 0 ? 'up' : 'down'; }

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

export function toDate(v) {
  if (v instanceof Date) return v;
  if (isNum(v)) return new Date(v);
  if (!v) return null;
  // «2026-10-09T11:22:47» без пояса — время сервера в UTC
  const s = /[zZ]|[+-]\d\d:?\d\d$/.test(v) ? v : v + 'Z';
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 9 окт, 14:05 */
export function dateTime(v) {
  const d = toDate(v);
  if (!d) return '—';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${d.getDate()}${NBSP}${MONTHS[d.getMonth()]}, ${hh}:${mm}`;
}

/** 9 окт */
export function day(v) {
  const d = toDate(v);
  return d ? `${d.getDate()}${NBSP}${MONTHS[d.getMonth()]}` : '—';
}

/** «5 мин назад», «3 ч назад», «2 дн назад» */
export function ago(v, now = Date.now()) {
  const d = toDate(v);
  if (!d) return '—';
  const s = Math.max(0, (now - d.getTime()) / 1000);
  if (s < 60) return 'только что';
  if (s < 3600) return `${Math.round(s / 60)}${NBSP}мин назад`;
  if (s < 86400) return `${Math.round(s / 3600)}${NBSP}ч назад`;
  return `${Math.round(s / 86400)}${NBSP}дн назад`;
}

/** Длительность в часах: «5 ч», «2 дн 4 ч». */
export function hours(h) {
  if (!isNum(h)) return '—';
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}${NBSP}мин`;
  if (h < 48) return `${Math.round(h)}${NBSP}ч`;
  const d = Math.floor(h / 24);
  const r = Math.round(h - d * 24);
  return r ? `${d}${NBSP}дн ${r}${NBSP}ч` : `${d}${NBSP}дн`;
}

/** Склонение: plural(5, 'сделка', 'сделки', 'сделок') */
export function plural(n, one, few, many) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b === 1) return one;
  if (b >= 2 && b <= 4) return few;
  return many;
}
