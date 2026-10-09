/* Графики панели — свои SVG, без библиотек: окно KrakenRemote не должно
   зависеть от доступа к CDN, а графиков нужно четыре вида.

   Правила читаемости:
   - деления оси — «круглые» числа (1, 2, 2.5, 5 × 10ⁿ), подписи справа, как в
     Акциях; сетка — волосяная, не спорит с линией;
   - капитал рисуется ступенькой: между сделками баланс не менялся, и плавная
     линия между двумя закрытиями выдумывала бы промежуточные значения;
   - цвет линии — по итогу периода (рост зелёный, падение красный) или цвет
     стратегии, если линия — её;
   - наведение показывает точное значение и время ближайшей точки. */

import { h, svg } from './ui.js';
import { day, dateTime, isNum } from './format.js';

let uid = 0;

/** Перерисовка по ширине контейнера. draw(width) возвращает содержимое. */
function responsive(el, draw) {
  let last = 0;
  const run = () => {
    const w = Math.round(el.clientWidth);
    if (!w || w === last) return;
    last = w;
    el.replaceChildren(...[].concat(draw(w)));
  };
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(run).observe(el);
  requestAnimationFrame(run);
  return el;
}

/** «Круглые» деления оси: шаг 1, 2, 2.5 или 5 × 10ⁿ. */
export function niceTicks(min, max, count = 4) {
  if (!isNum(min) || !isNum(max)) return [];
  if (min === max) { const pad = Math.abs(min) * 0.01 || 1; min -= pad; max += pad; }
  const raw = (max - min) / Math.max(1, count);
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * p).find(s => s >= raw) || 10 * p;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const out = [];
  for (let v = lo; v <= hi + step / 2; v += step) out.push(Math.round(v / step) * step);
  return out;
}

function nearest(points, t) {
  let lo = 0, hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].t < t) lo = mid + 1; else hi = mid;
  }
  if (lo > 0 && Math.abs(points[lo - 1].t - t) < Math.abs(points[lo].t - t)) lo -= 1;
  return points[lo];
}

/**
 * Линия во времени.
 * points — [{t: мс, v: число}] по возрастанию t.
 * opts: height, color ('auto' — по итогу), step (ступенька), baseline (число:
 * пунктир, например стартовый капитал), format(v) — подпись значения,
 * axisFormat(v) — подпись деления, label — имя ряда в подсказке.
 */
export function lineChart(points, opts = {}) {
  const height = opts.height || 220;
  const el = h('div', { class: 'chart', style: { height: height + 'px' } });
  const pts = (points || []).filter(p => isNum(p.t) && isNum(p.v));
  if (pts.length < 2) {
    el.classList.add('chart-empty');
    el.textContent = opts.emptyText || 'Пока мало точек для графика';
    return el;
  }
  const format = opts.format || (v => String(v));
  const axisFormat = opts.axisFormat || format;
  const first = pts[0].v, lastV = pts[pts.length - 1].v;
  const ref = isNum(opts.baseline) ? opts.baseline : first;
  const color = opts.color && opts.color !== 'auto' ? opts.color
    : (lastV >= ref ? 'var(--up)' : 'var(--down)');

  return responsive(el, (W) => {
    const padR = opts.axis === false ? 4 : 64, padL = 4, padT = 10, padB = opts.axis === false ? 4 : 26;
    const iw = W - padL - padR, ih = height - padT - padB;
    let lo = Math.min(...pts.map(p => p.v)), hi = Math.max(...pts.map(p => p.v));
    if (isNum(opts.baseline)) { lo = Math.min(lo, opts.baseline); hi = Math.max(hi, opts.baseline); }
    const ticks = niceTicks(lo, hi, 4);
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    const X = t => padL + (t1 === t0 ? iw : (t - t0) / (t1 - t0) * iw);
    const Y = v => padT + (y1 === y0 ? ih / 2 : (1 - (v - y0) / (y1 - y0)) * ih);

    const s = svg('svg', { viewBox: `0 0 ${W} ${height}`, height, role: 'img',
      'aria-label': opts.label || 'график' });
    const gid = 'g' + (++uid);
    const defs = svg('defs');
    const grad = svg('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.append(svg('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': .22 }),
      svg('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': 0 }));
    defs.append(grad);
    s.append(defs);

    if (opts.axis !== false) {
      const axis = svg('g', { class: 'axis' });
      for (const v of ticks) {
        const y = Y(v);
        s.append(svg('line', { class: 'gridline', x1: padL, x2: padL + iw, y1: y, y2: y }));
        const tx = svg('text', { x: W - 2, y: y + 4, 'text-anchor': 'end' });
        tx.textContent = axisFormat(v);
        axis.append(tx);
      }
      const n = Math.max(2, Math.min(6, Math.floor(iw / 110)));
      for (let i = 0; i < n; i++) {
        const t = t0 + (t1 - t0) * i / (n - 1);
        const tx = svg('text', { x: X(t), y: height - 6, 'text-anchor': i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle' });
        tx.textContent = day(t);
        axis.append(tx);
      }
      s.append(axis);
    }
    if (isNum(opts.baseline)) {
      s.append(svg('line', { class: 'baseline', x1: padL, x2: padL + iw, y1: Y(opts.baseline), y2: Y(opts.baseline) }));
    }

    let d = '';
    pts.forEach((p, i) => {
      const x = X(p.t), y = Y(p.v);
      if (i === 0) d = `M${x},${y}`;
      else if (opts.step) d += `H${x}V${y}`;
      else d += `L${x},${y}`;
    });
    const bottom = padT + ih;
    s.append(svg('path', { d: `${d}V${bottom}H${X(t0)}Z`, fill: `url(#${gid})`, stroke: 'none' }));
    s.append(svg('path', { d, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    const endDot = svg('circle', { cx: X(t1), cy: Y(lastV), r: 3.5, fill: color });
    s.append(endDot);

    // Наведение: перекрестье и подсказка
    const cross = svg('line', { class: 'cross', y1: padT, y2: bottom, visibility: 'hidden' });
    const dot = svg('circle', { r: 5, fill: color, stroke: 'var(--bg-elev)', 'stroke-width': 2, visibility: 'hidden' });
    s.append(cross, dot);
    const tip = h('div', { class: 'chart-tip', hidden: true });
    const hit = svg('rect', { x: padL, y: 0, width: iw, height, fill: 'transparent' });
    s.append(hit);
    const show = (ev) => {
      const box = s.getBoundingClientRect();
      const x = (ev.clientX - box.left) * (W / box.width);
      const t = t0 + (Math.min(Math.max(x, padL), padL + iw) - padL) / iw * (t1 - t0);
      const p = nearest(pts, t);
      const px = X(p.t), py = Y(p.v);
      cross.setAttribute('x1', px); cross.setAttribute('x2', px);
      cross.setAttribute('visibility', 'visible');
      dot.setAttribute('cx', px); dot.setAttribute('cy', py); dot.setAttribute('visibility', 'visible');
      tip.replaceChildren(h('b', null, format(p.v)),
        opts.tipExtra ? h('div', null, opts.tipExtra(p)) : null,
        h('div', { class: 'muted' }, dateTime(p.t)));
      tip.hidden = false;
      tip.style.left = Math.min(Math.max(px / W * box.width, 70), box.width - 70) + 'px';
      tip.style.top = Math.max(py / height * box.height - 10, 46) + 'px';
    };
    const hide = () => { cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); tip.hidden = true; };
    hit.addEventListener('pointermove', show);
    hit.addEventListener('pointerdown', show);
    hit.addEventListener('pointerleave', hide);
    return [s, tip];
  });
}

/** Мини-график без осей — для карточек. */
export function sparkline(points, color, height = 46) {
  const el = h('div', { class: 'chart strat-spark', style: { height: height + 'px' } });
  const pts = (points || []).filter(p => isNum(p.t) && isNum(p.v));
  if (pts.length < 2) return el;
  return responsive(el, (W) => {
    const lo = Math.min(...pts.map(p => p.v)), hi = Math.max(...pts.map(p => p.v));
    const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    const X = t => 2 + (t1 === t0 ? 0 : (t - t0) / (t1 - t0) * (W - 4));
    const Y = v => 3 + (hi === lo ? (height - 6) / 2 : (1 - (v - lo) / (hi - lo)) * (height - 6));
    let d = '';
    pts.forEach((p, i) => { d += i === 0 ? `M${X(p.t)},${Y(p.v)}` : `H${X(p.t)}V${Y(p.v)}`; });
    const gid = 's' + (++uid);
    const s = svg('svg', { viewBox: `0 0 ${W} ${height}`, height, 'aria-hidden': 'true' });
    const defs = svg('defs');
    const grad = svg('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.append(svg('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': .18 }),
      svg('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': 0 }));
    defs.append(grad);
    s.append(defs,
      svg('path', { d: `${d}V${height}H${X(t0)}Z`, fill: `url(#${gid})` }),
      svg('path', { d, fill: 'none', stroke: color, 'stroke-width': 1.8, 'stroke-linejoin': 'round' }));
    return s;
  });
}

/**
 * Кольцо долей. items — [{label, value, color}]; center — {value, label}.
 */
export function donut(items, center, size = 132) {
  const total = items.reduce((a, i) => a + Math.max(0, i.value || 0), 0);
  const r = size / 2 - 9, c = 2 * Math.PI * r, cx = size / 2;
  const s = svg('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: 'img', 'aria-label': 'распределение' });
  s.append(svg('circle', { cx, cy: cx, r, fill: 'none', stroke: 'var(--fill)', 'stroke-width': 14 }));
  let off = 0;
  const gap = items.length > 1 ? 2 : 0;
  for (const it of items) {
    const share = total ? Math.max(0, it.value || 0) / total : 0;
    const len = Math.max(0, share * c - gap);
    if (len > 0) {
      s.append(svg('circle', {
        cx, cy: cx, r, fill: 'none', stroke: it.color, 'stroke-width': 14,
        'stroke-dasharray': `${len} ${c - len}`, 'stroke-dashoffset': -off,
        transform: `rotate(-90 ${cx} ${cx})`,
      }));
    }
    off += share * c;
  }
  if (center) {
    const v = svg('text', { x: cx, y: cx + 2, 'text-anchor': 'middle', style: 'font: 600 17px var(--font-display); fill: var(--text); font-variant-numeric: tabular-nums' });
    v.textContent = center.value;
    const l = svg('text', { x: cx, y: cx + 19, 'text-anchor': 'middle', style: 'font-size: 11px; fill: var(--text-3)' });
    l.textContent = center.label;
    s.append(v, l);
  }
  return s;
}

/**
 * Столбцы по дням: [{t, v}] — выше нуля зелёные, ниже красные.
 */
export function bars(items, opts = {}) {
  const height = opts.height || 160;
  const el = h('div', { class: 'chart', style: { height: height + 'px' } });
  const data = (items || []).filter(i => isNum(i.v));
  if (!data.length) {
    el.classList.add('chart-empty');
    el.textContent = opts.emptyText || 'Нет данных за период';
    return el;
  }
  const format = opts.format || String;
  return responsive(el, (W) => {
    const padR = 64, padT = 8, padB = 24;
    const iw = W - padR, ih = height - padT - padB;
    const ticks = niceTicks(Math.min(0, ...data.map(d => d.v)), Math.max(0, ...data.map(d => d.v)), 3);
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const Y = v => padT + (y1 === y0 ? ih / 2 : (1 - (v - y0) / (y1 - y0)) * ih);
    const s = svg('svg', { viewBox: `0 0 ${W} ${height}`, height, role: 'img', 'aria-label': opts.label || 'столбцы' });
    const axis = svg('g', { class: 'axis' });
    for (const v of ticks) {
      s.append(svg('line', { class: 'gridline', x1: 0, x2: iw, y1: Y(v), y2: Y(v) }));
      const tx = svg('text', { x: W - 2, y: Y(v) + 4, 'text-anchor': 'end' });
      tx.textContent = format(v);
      axis.append(tx);
    }
    const slot = iw / data.length;
    const bw = Math.max(2, Math.min(28, slot * 0.62));
    const tip = h('div', { class: 'chart-tip', hidden: true });
    data.forEach((d, i) => {
      if (!d.v) return;                       // день без сделок — без столбика
      const x = i * slot + (slot - bw) / 2;
      const top = Y(Math.max(0, d.v)), bot = Y(Math.min(0, d.v));
      const rect = svg('rect', { x, y: top, width: bw, height: Math.max(1, bot - top), rx: Math.min(4, bw / 3),
        fill: d.v >= 0 ? 'var(--up)' : 'var(--down)', opacity: .85 });
      rect.addEventListener('pointerenter', () => {
        rect.setAttribute('opacity', 1);
        tip.replaceChildren(h('b', null, format(d.v)), h('div', { class: 'muted' }, d.label || day(d.t)));
        tip.hidden = false;
        const box = s.getBoundingClientRect();
        tip.style.left = Math.min(Math.max((x + bw / 2) / W * box.width, 60), box.width - 60) + 'px';
        tip.style.top = Math.max(top / height * box.height - 8, 44) + 'px';
      });
      rect.addEventListener('pointerleave', () => { rect.setAttribute('opacity', .85); tip.hidden = true; });
      s.append(rect);
    });
    const labelsEvery = Math.ceil(data.length / Math.max(2, Math.floor(iw / 70)));
    data.forEach((d, i) => {
      if (i % labelsEvery) return;
      const tx = svg('text', { x: i * slot + slot / 2, y: height - 6, 'text-anchor': 'middle' });
      tx.textContent = d.label || day(d.t);
      axis.append(tx);
    });
    s.append(axis);
    return [s, tip];
  });
}
