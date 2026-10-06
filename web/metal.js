// Metal colour filters for the website and the showcase prototype: one SVG <filter id="p3-metal-<id>"> per
// material, built from the catalog (swatch, 9-stop ramp, or a CSS tint chain for recolor "tint"). Used as
// `filter: url(#p3-metal-<id>)` on the Customize image and 360° movie, the Materials page and the gallery preview.
function P3MetalFilterDefs(materials) {
  const hex = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255);
  const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
  const f = n => Math.min(1, Math.max(0, n)).toFixed(3);
  // A material with recolor "tint" uses its CSS filter (e.g. "sepia(0.72) hue-rotate(-3deg) …"),
  // converted to the equivalent SVG primitives so it can be applied to the ring only.
  const cssChain = tint => {
    const out = []; let i = 0, prev = 'SourceGraphic';
    const step = (body) => { const r = 't' + (i++); out.push(body.replace('IN', prev).replace('OUT', r)); prev = r; };
    for (const [, fn, arg] of tint.matchAll(/([a-z-]+)\(\s*(-?[\d.]+)/g)) {
      const v = parseFloat(arg), k = 1 - Math.min(1, Math.max(0, v));
      if (fn === 'sepia') step(`<feColorMatrix in="IN" result="OUT" type="matrix" values="${[0.393 + 0.607 * k, 0.769 - 0.769 * k, 0.189 - 0.189 * k, 0, 0, 0.349 - 0.349 * k, 0.686 + 0.314 * k, 0.168 - 0.168 * k, 0, 0, 0.272 - 0.272 * k, 0.534 - 0.534 * k, 0.131 + 0.869 * k, 0, 0, 0, 0, 0, 1, 0].map(f).join(' ')}"/>`);
      else if (fn === 'hue-rotate') step(`<feColorMatrix in="IN" result="OUT" type="hueRotate" values="${v}"/>`);
      else if (fn === 'saturate') step(`<feColorMatrix in="IN" result="OUT" type="saturate" values="${v}"/>`);
      else if (fn === 'grayscale') step(`<feColorMatrix in="IN" result="OUT" type="saturate" values="${f(1 - v)}"/>`);
      else if (fn === 'brightness') step(`<feComponentTransfer in="IN" result="OUT"><feFuncR type="linear" slope="${v}"/><feFuncG type="linear" slope="${v}"/><feFuncB type="linear" slope="${v}"/></feComponentTransfer>`);
      else if (fn === 'contrast') step(`<feComponentTransfer in="IN" result="OUT"><feFuncR type="linear" slope="${v}" intercept="${-(0.5 * v) + 0.5}"/><feFuncG type="linear" slope="${v}" intercept="${-(0.5 * v) + 0.5}"/><feFuncB type="linear" slope="${v}" intercept="${-(0.5 * v) + 0.5}"/></feComponentTransfer>`);
    }
    return { body: out.join(''), result: prev };
  };
  // Where the metal colour goes: the piece, not the backdrop. One mask, by brightness alone, the same at every
  // point of the frame: light pixels are left as generated (the white background of the stills, the light grey
  // studio backdrop of a 360° movie, the brightest sparkle) — fully from an average of 0.94, fading in to the full
  // colour at 0.87 — and everything darker takes the metal. A darker backdrop (the grey wall some movies have)
  // takes the colour too, evenly, like a tinted room. There is no spatial window: an earlier soft centred window
  // left the frame edges uncoloured, which showed as grey bands at the top and bottom of a movie and left a
  // charm's loop or a ring's rim near the edge in the original metal.
  const masks = `<feColorMatrix in="SourceGraphic" type="matrix" result="lum" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 -4.545 -4.545 -4.545 0 12.82"/>
          <feComposite in="metal" in2="lum" operator="in" result="ring"/>
          <feMerge><feMergeNode in="SourceGraphic"/><feMergeNode in="ring"/></feMerge>`;
  const open = id => `<filter id="p3-metal-${id}" x="0" y="0" width="1" height="1" color-interpolation-filters="sRGB">`;
  return materials.map(m => {
    const s = hex(m.swatch || '#C8C8C5');
    if (m.recolor === 'tint' && m.tint) {
      const c = cssChain(m.tint);
      return `${open(m.id)}${c.body}<feColorMatrix in="${c.result}" type="identity" result="metal"/>${masks}</filter>`;
    }
    // ramp stops at luminance 0, .25, .5, .75, 1 — most of the ring lands on the swatch itself
    // An explicit 5-colour ramp (config/materials.json "ramp") gives real metal hue shifts:
    // warm brown shadows, the alloy colour in the mid-tones, pale highlights. Otherwise derive one.
    // A ramp may have any number of evenly spaced stops (5 or 9 in config/materials.json); the derived
    // one keeps the metal's own colour in the mid-tones and lets the brightest highlights reach white.
    const ramp = (m.ramp && m.ramp.length >= 3) ? m.ramp.map(hex)
      : [s.map(v => v * 0.12), s.map(v => v * 0.45), s.map(v => v * 0.8), s, mix(s, [1, 1, 1], 0.55), [1, 1, 1]];
    const table = c => ramp.map(p => f(p[c])).join(' ');
    return `${open(m.id)}
          <feColorMatrix in="SourceGraphic" type="matrix" result="gray" values="0.2126 0.7152 0.0722 0 0 0.2126 0.7152 0.0722 0 0 0.2126 0.7152 0.0722 0 0 0 0 0 1 0"/>
          <feComponentTransfer in="gray" result="metal"><feFuncR type="table" tableValues="${table(0)}"/><feFuncG type="table" tableValues="${table(1)}"/><feFuncB type="table" tableValues="${table(2)}"/></feComponentTransfer>
          ${masks}
        </filter>`;
  }).join('');
}

window.P3MetalFilterDefs = P3MetalFilterDefs;
