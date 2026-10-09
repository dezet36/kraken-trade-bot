/* Кирпичи интерфейса. Данные вставляются только текстом (textContent), не
   разметкой: имя пары или причина отказа из журнала не должны стать HTML. */

import { signedMoney, signedPct, tone } from './format.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

/** h('div', {class: 'card', onclick}, 'текст', child, [детей]) */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'html') el.innerHTML = v;            // только для своих значков
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  return el;
}

/* ── Значки: штрихи 24×24 в духе SF Symbols ─────────────────────────── */
const PATHS = {
  home: '<path d="M3.5 10.5 12 3.8l8.5 6.7"/><path d="M5.8 9v10.2h4.4v-5.4h3.6v5.4h4.4V9"/>',
  strategies: '<rect x="3.5" y="12" width="4" height="8" rx="1.2"/><rect x="10" y="7" width="4" height="13" rx="1.2"/><rect x="16.5" y="3.5" width="4" height="16.5" rx="1.2"/>',
  trades: '<path d="M4 8h13.5"/><path d="m14 4.5 3.5 3.5-3.5 3.5"/><path d="M20 16H6.5"/><path d="M10 12.5 6.5 16l3.5 3.5"/>',
  accounts: '<rect x="3" y="6" width="18" height="13" rx="3"/><path d="M3 10.5h18"/><path d="M15.5 14.8h2.5"/>',
  ai: '<path d="M11 3.5 12.6 8a3 3 0 0 0 1.9 1.9l4.5 1.6-4.5 1.6a3 3 0 0 0-1.9 1.9L11 19.5l-1.6-4.5a3 3 0 0 0-1.9-1.9L3 11.5 7.5 9.9A3 3 0 0 0 9.4 8z"/><path d="M18.5 3v3.5M16.8 4.8h3.5"/>',
  system: '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M5.5 18.5l1.7-1.7M16.8 7.2l1.7-1.7"/>',
  warn: '<path d="M12 4 2.8 19.5h18.4z"/><path d="M12 10v4.5"/><circle cx="12" cy="17" r=".6" fill="currentColor"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><circle cx="12" cy="7.8" r=".6" fill="currentColor"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
};

export function icon(name, cls = '') {
  return h('span', {
    class: 'ic ' + cls, 'aria-hidden': 'true', style: { display: 'contents' },
    html: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${PATHS[name] || ''}</svg>`,
  });
}

/* ── Реестр стратегий (вставляет сервер: window.STRATEGY_REGISTRY) ───── */
const REG = (window.STRATEGY_REGISTRY || []);
const BY_CODE = Object.fromEntries(REG.map(s => [s.code, s]));

export function strategyTitle(code) { return (BY_CODE[code] && BY_CODE[code].title) || code; }
export function strategyShort(code) { return (BY_CODE[code] && BY_CODE[code].short) || code; }
export function strategyCodes() { return REG.map(s => s.code); }

/** Цвет стратегии под текущую тему (переменная --код из реестра). */
export function strategyColor(code) {
  const v = getComputedStyle(document.documentElement).getPropertyValue('--' + String(code).toLowerCase()).trim();
  return v || '#8e8e93';
}

/** Сокращение для значка: «Фибо 12ч» → Ф12, «Smart Money» → SM, «Уровни» → У. */
export function strategyInitials(code) {
  const title = strategyTitle(code);
  const words = title.split(/[\s-]+/).filter(Boolean);
  const digits = (title.match(/\d+/) || [''])[0];
  if (digits) return (words[0][0] + digits).toUpperCase();
  if (words.length === 1 && words[0].length <= 3) return words[0].toUpperCase();
  return words.slice(0, 2).map(w => w[0]).join('').toUpperCase();
}

/** Квадрат с буквами стратегии её цвета. */
export function strategyBadge(code) {
  const label = strategyInitials(code);
  return h('span', { class: 'strat-badge', style: { background: strategyColor(code), fontSize: label.length > 2 ? '11px' : '13px' } }, label);
}

/* ── Составные элементы ─────────────────────────────────────────────── */

/** Изменение: «+$12.30 · +1.31%» цветом результата. */
export function delta(amount, percent) {
  const t = tone(amount ?? percent);
  return h('span', { class: 'delta ' + t },
    amount != null ? signedMoney(amount) : null,
    percent != null ? h('span', { class: 'chip' }, signedPct(percent)) : null);
}

export function row(label, value, opts = {}) {
  return h('div', { class: 'row' },
    h('div', { class: 'row-label' }, label, opts.hint ? h('small', null, opts.hint) : null),
    h('div', { class: 'row-value ' + (opts.tone || '') }, value));
}

export function stat(label, value, cls = '') {
  return h('div', null, h('div', { class: 'stat-label' }, label), h('div', { class: 'stat-value ' + cls }, value));
}

export function pill(text, cls = '') { return h('span', { class: 'pill ' + cls }, text); }

export function empty(title, text) {
  return h('div', { class: 'empty' }, h('b', null, title), text);
}

export function notice(level, title, detail) {
  const cls = level === 'bad' || level === 'error' ? 'bad' : level === 'info' ? 'info' : '';
  return h('div', { class: 'notice ' + cls },
    icon(level === 'info' ? 'info' : 'warn'),
    h('div', null, h('b', null, title), detail ? h('small', null, detail) : null));
}

export function section(title, link, ...content) {
  return h('section', { class: 'section fade-in' },
    h('div', { class: 'section-head' },
      h('h2', { class: 'section-title' }, title),
      link ? h('a', { class: 'section-link', href: link.href }, link.text) : null),
    content);
}

/* ── Кнопки, листы, подтверждения ───────────────────────────────────── */

/** kind: '' (основная), 'gray', 'danger', 'plain'; small — компактная. */
export function button(text, onClick, kind = '', small = false) {
  return h('button', { type: 'button', class: `btn ${kind} ${small ? 'sm' : ''}`.trim(), onclick: onClick }, text);
}

/**
 * Лист поверх страницы (как sheet в macOS). Живёт вне раздела: страница
 * перерисовывается по опросу, а введённое в лист не должно пропадать.
 * build(close) возвращает содержимое. -> { close }
 */
export function openSheet(title, build, onClose = null) {
  const dlg = h('dialog', { class: 'sheet' });
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    dlg.close();
    dlg.remove();
    if (onClose) onClose();
  };
  dlg.append(h('div', { class: 'sheet-head' }, h('h2', null, title),
    h('button', { type: 'button', class: 'sheet-x', 'aria-label': 'Закрыть', onclick: close }, '×')),
    h('div', { class: 'sheet-body' }, build(close)));
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); });
  document.body.append(dlg);
  dlg.showModal();
  return { close };
}

/** Подтверждение действия. Закрытие крестиком или Esc — «нет». -> Promise<boolean> */
export function confirmSheet(text, okText = 'Подтвердить', danger = false) {
  return new Promise((resolve) => {
    let answer = false;
    openSheet('Подтвердите', (close) => {
      const cancel = button('Отмена', close, 'gray');
      cancel.autofocus = true;                    // по умолчанию — безопасный ответ
      return h('div', null, h('p', { class: 'sheet-text' }, text),
        h('div', { class: 'sheet-actions' }, cancel, button(okText, () => { answer = true; close(); }, danger ? 'danger' : '')));
    }, () => resolve(answer));
  });
}

/** Короткое сообщение снизу экрана. tone: '', 'bad', 'good'. */
export function toast(text, tone = '') {
  const el = h('div', { class: `toast ${tone}`, role: 'status' }, text);
  document.body.append(el);
  setTimeout(() => el.classList.add('out'), 3600);
  setTimeout(() => el.remove(), 4000);
}

/** Поле формы: подпись, элемент, подсказка. */
export function field(label, control, hint = null) {
  return h('label', { class: 'field' }, h('span', { class: 'field-label' }, label), control,
    hint ? h('small', null, hint) : null);
}

/** Сегментный переключатель. onChange(значение). */
export function segmented(options, value, onChange, label = '') {
  const el = h('div', { class: 'seg', role: 'group', 'aria-label': label });
  for (const o of options) {
    const button = h('button', { type: 'button', 'aria-pressed': String(o.value === value) }, o.label);
    button.addEventListener('click', () => {
      for (const b of el.children) b.setAttribute('aria-pressed', String(b === button));
      onChange(o.value);
    });
    el.append(button);
  }
  return el;
}
