/* Раздел, который переезжает в новую панель на следующих этапах: до тех пор —
   честная заглушка со ссылкой на тот же раздел прежней панели. */

import { h, icon } from '../ui.js';

export function make(title, text, oldPage) {
  return {
    title,
    render() {
      return h('div', null,
        h('div', { class: 'page-head' }, h('h1', { class: 'page-title' }, title)),
        h('div', { class: 'card fade-in', style: { maxWidth: '640px' } },
          h('div', { class: 'empty', style: { textAlign: 'left', padding: '6px 0' } },
            h('b', null, 'Раздел переезжает в новую панель'),
            h('div', null, text)),
          h('a', { href: `/?page=${oldPage}`, style: { display: 'inline-flex', alignItems: 'center', gap: '4px', marginTop: '10px' } },
            'Открыть в прежней панели', h('span', { style: { width: '14px', display: 'inline-flex' } }, icon('chevron')))));
    },
  };
}
