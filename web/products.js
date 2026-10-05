/* Rings and charms: one icon set and one set of names, shared by the customer site and the Admin.
   The icons follow the site's line-icon style (24 × 24, round strokes, currentColor): a ring is a band with a
   small stone; a charm is a neutral pendant — a plain body hanging from a small loop. No emoji. */
(function () {
  const Paths = {
    ring: '<circle cx="12" cy="14.6" r="6.1"/><path d="M9.7 5.2h4.6l1.2 1.5-3.5 2.7-3.5-2.7z"/>',
    charm: '<circle cx="12" cy="4.3" r="1.8"/><path d="M12 6.1v1.7"/>' +
           '<path d="M12 7.8c3.3 0 5.6 2.4 5.6 5.7 0 4-3.1 6.9-5.6 7.6-2.5-.7-5.6-3.6-5.6-7.6 0-3.3 2.3-5.7 5.6-5.7z"/>',
  };
  const Labels = { ring: 'Ring', charm: 'Charm' };
  const Plurals = { ring: 'Rings', charm: 'Charms' };
  const Known = P => (P === 'charm' ? 'charm' : 'ring');
  window.P3Products = {
    all: ['ring', 'charm'],
    icon: (P, Cls) => `<svg class="${Cls || 'w-4 h-4'}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" `
      + `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${Paths[Known(P)]}</svg>`,
    label: P => Labels[Known(P)],
    plural: P => Plurals[Known(P)],
  };
})();
