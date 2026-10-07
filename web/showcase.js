// XJet Atelier showcase engine (styles: showcase.css) — the animated story of one Inspiration Gallery design:
// your idea → four possibilities → make it yours → every angle → your finish → the finished ring.
// Mounted by the homepage hero and by /showcase (the standalone reference page with review tools).
// Every position, fade and wipe is a function of one clock (render(t)), so the beats never overlap, pausing freezes
// everything and seeking shows an exact moment. Read-only: the story comes from GET /api/showcase (p3/showcase.py);
// nothing is generated or charged. The clock stops while the stage is off screen or the tab is hidden.
(() => {
    'use strict';
    const RATE = 34;                    // typed characters per second
    const CAPTIONS = ['Your idea', 'Four possibilities', 'Make it yours', 'Make it yours', 'Every angle', 'Your finish', ''];
    const LABELS = ['Prompt', '4 designs', 'Select', 'Refine', '360°', 'Metal', 'Final'];
    const VCOVER = 1.855;               // seconds of movie the turn beat plays: 1.48 s at normal speed, then 0.6 s easing to ¼
    // Silver for a warm (gold) render: a contrast curve that keeps depth and the bright highlights; near-white pixels
    // (the background, the brightest sparkle) stay untouched.
    const SILVER = `<filter id="sc-silver" x="0" y="0" width="1" height="1" color-interpolation-filters="sRGB">
        <feColorMatrix in="SourceGraphic" type="matrix" result="gray" values="0.2126 0.7152 0.0722 0 0 0.2126 0.7152 0.0722 0 0 0.2126 0.7152 0.0722 0 0 0 0 0 1 0"/>
        <feComponentTransfer in="gray" result="metal">
            <feFuncR type="table" tableValues="0 0.025 0.07 0.15 0.28 0.46 0.66 0.83 0.94 1"/>
            <feFuncG type="table" tableValues="0 0.027 0.073 0.154 0.285 0.465 0.664 0.833 0.942 1"/>
            <feFuncB type="table" tableValues="0 0.035 0.085 0.17 0.30 0.48 0.68 0.845 0.95 1"/>
        </feComponentTransfer>
        <feColorMatrix in="SourceGraphic" type="matrix" result="lum" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 -4 -4 -4 0 11.7"/>
        <feComposite in="metal" in2="lum" operator="in" result="ring"/>
        <feMerge><feMergeNode in="SourceGraphic"/><feMergeNode in="ring"/></feMerge>
    </filter>`;
    const TEMPLATE = `
        <p class="sc-el sc-eyebrow" aria-hidden="true"><span class="sc-dot"></span><span class="sc-cap"></span></p>
        <div class="sc-el sc-composer"><span class="sc-spark" aria-hidden="true">✦</span><span class="sc-typed"><span class="sc-typed-text"></span><span class="sc-caret"></span></span><span class="sc-go" aria-hidden="true">Design</span></div>
        <div class="sc-el sc-card sc-opt" data-k="0"><img alt="" decoding="async"></div>
        <div class="sc-el sc-card sc-opt" data-k="1"><img alt="" decoding="async"></div>
        <div class="sc-el sc-card sc-opt" data-k="2"><img alt="" decoding="async"></div>
        <div class="sc-el sc-card sc-opt" data-k="3"><img alt="" decoding="async"></div>
        <div class="sc-el sc-card sc-focus">
            <img class="sc-f-orig" alt="" decoding="async"><img class="sc-f-ref" alt="" decoding="async"><div class="sc-sweep"></div>
            <video muted playsinline preload="auto" disablepictureinpicture></video>
            <img class="sc-f-silver" alt="" decoding="async"><img class="sc-f-gold" alt="" decoding="async">
            <span class="sc-tag"></span>
        </div>
        <div class="sc-el sc-chip"><span class="sc-spark" aria-hidden="true">✦</span><span><span class="sc-r-text"></span><span class="sc-caret sc-r-caret"></span></span></div>
        <div class="sc-el sc-metals"></div>
        <div class="sc-el sc-name"><span class="sc-title"></span><span class="sc-by">Designed with XJet Atelier</span></div>
        <svg class="sc-defs" width="0" height="0" aria-hidden="true" focusable="false" style="position:absolute;width:0;height:0;overflow:hidden"></svg>
        <div class="sc-loading"></div>`;

    const clamp = v => Math.max(0, Math.min(1, v));
    const ramp = (t, a, b) => (b <= a ? (t >= b ? 1 : 0) : clamp((t - a) / (b - a)));
    const ease = x => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
    const E = (t, a, b) => ease(ramp(t, a, b));
    const lerp = (a, b, k) => a + (b - a) * k;
    const LR = (p, q, k) => ({ x: lerp(p.x, q.x, k), y: lerp(p.y, q.y, k), w: lerp(p.w, q.w, k), h: lerp(p.h, q.h, k) });
    const grow = (r, s, dy = 0) => ({ x: r.x - (r.w * (s - 1)) / 2, y: r.y - (r.h * (s - 1)) / 2 + dy, w: r.w * s, h: r.h * s });
    const span = (t, a, b) => ramp(t, a, a + 200) * (1 - ramp(t, b - 200, b));          // a label: 200 ms in at a, out by b
    const thumb = (u, w) => (u && u.includes('/assets/') ? u.replace('/assets/', '/thumb/') + '?w=' + w : u);
    const base = () => (document.querySelector('meta[name="p3-base"]')?.content || '').replace(/\/+$/, '');
    const stories = new Map();          // design → the API answer (one request per page)
    function fetchStory(design) {
        const key = design || '';
        if (!stories.has(key)) {
            stories.set(key, fetch(base() + '/api/showcase' + (design ? '?design=' + encodeURIComponent(design) : ''))
                .then(r => r.json()).catch(e => { stories.delete(key); throw e; }));
        }
        return stories.get(key);
    }
    // the beat lengths: the typing keeps one pace; the reading holds and the final frame are fixed
    function planFor(S) {
        const P = S.prompt.length, I = S.refine ? S.refine.text.length : 0;
        const typeP = 200 + (P / RATE) * 1000, pressAt = typeP + 300;
        const typeI = 1000 + (I / RATE) * 1000, wipeAt = Math.max(2300, typeI + 350);
        const dur = [Math.max(1700, pressAt + 300), 1150, 1100, S.refine ? wipeAt + 800 + 200 : 1900, 2300, 2300, 3900];
        const start = dur.map((_, i) => dur.slice(0, i).reduce((a, b) => a + b, 0));
        return { dur, start, total: start[6] + dur[6], pressAt, typeI, wipeAt, goldAt: 1150 };
    }
    // the filters the metal beat needs, under the showcase's own ids
    function filterDefs(S) {
        const gold = (S.metals || []).find(m => m.id === 'gold_18k_yellow');
        const goldDefs = gold && window.P3MetalFilterDefs ? window.P3MetalFilterDefs([gold]).replace('id="p3-metal-gold_18k_yellow"', 'id="sc-gold"') : '';
        return SILVER + goldDefs;
    }

    function mount(stage, opts = {}) {
        if (stage._sc) stage._sc.destroy();
        stage.classList.add('sc-stage');
        stage.innerHTML = TEMPLATE;
        const q = s => stage.querySelector(s);
        const X = {
            eyebrow: q('.sc-eyebrow'), cap: q('.sc-cap'), composer: q('.sc-composer'), typed: q('.sc-typed-text'), caret: q('.sc-composer .sc-caret'),
            opts: [...stage.querySelectorAll('.sc-opt')], focus: q('.sc-focus'), orig: q('.sc-f-orig'), ref: q('.sc-f-ref'), sweep: q('.sc-sweep'),
            video: q('.sc-focus video'), silver: q('.sc-f-silver'), gold: q('.sc-f-gold'), tag: q('.sc-tag'), chip: q('.sc-chip'),
            rtext: q('.sc-r-text'), rcaret: q('.sc-r-caret'), metals: q('.sc-metals'), name: q('.sc-name'), title: q('.sc-title'),
            defs: q('.sc-defs'), loading: q('.sc-loading'),
        };
        const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches && opts.motion !== 'always';
        let S = null, D = null, T = 0, last = null, raf = 0, visible = true, playing = opts.autoplay !== false && !reduced, destroyed = false;
        let chipKey = '', chips = [], VOFF = 0, small = null, fellBack = false;
        X.loading.textContent = opts.loadingText || '';

        // ── layout: stage pixels for a square stage (computer) and a 4:5 stage (phone) ──
        function layout() {
            const W = stage.clientWidth, H = stage.clientHeight, sm = W < 480;
            if (sm !== small) { small = sm; stage.classList.toggle('sc-small', sm); chipKey = ''; }
            const L = { W, H, sm };
            L.eyebrow = sm ? { x: 16, y: 14 } : { x: 24, y: 22 };
            L.composerW = sm ? 0.9 * W : Math.min(0.84 * W, 600);
            L.composerMidY = 0.44 * H;
            L.composerTop = { y: (sm ? 0.075 : 0.09) * H, s: sm ? 0.95 : 0.92 };
            L.grid = { cols: 2, s: (sm ? 0.39 : 0.31) * W, g: (sm ? 0.04 : 0.03) * W, y0: 0.22 * H, gap: 0.03 * H, bottom: 0.05 * H };
            // the ring with the line under it (the request, the metals) and the final ring with its name are centred
            // as groups, so a square stage has no empty band at the bottom
            const hs = Math.min((sm ? 0.76 : 0.66) * W, (sm ? 0.66 : 0.68) * H);
            L.hero = { x: (W - hs) / 2, y: Math.max((sm ? 0.08 : 0.09) * H, (H - (hs + 0.035 * H + (sm ? 44 : 48))) / 2), w: hs, h: hs };
            const fs = Math.min((sm ? 0.78 : 0.72) * W, 0.7 * H);
            L.final = { x: (W - fs) / 2, y: Math.max(0.04 * H, (H - (fs + 0.04 * H + (sm ? 52 : 62))) / 2), w: fs, h: fs };
            L.chipMax = (sm ? 0.9 : 0.8) * W;
            return L;
        }
        // the four options sit below the composer as it really is (a long prompt can take more lines) and the cards
        // shrink rather than run off the stage
        function placeSlots(L, composerBottom) {
            const G = L.grid, rows = 4 / G.cols, y0 = Math.max(G.y0, composerBottom + G.gap);
            const s = Math.min(G.s, (L.H - G.bottom - y0 - (rows - 1) * G.g) / rows);
            const x0 = (L.W - (G.cols * s + (G.cols - 1) * G.g)) / 2;
            L.slots = [0, 1, 2, 3].map(k => ({ x: x0 + (k % G.cols) * (s + G.g), y: y0 + Math.floor(k / G.cols) * (s + G.g), w: s, h: s }));
        }
        function put(el, r, o, extra = '') {
            el.style.transform = `translate(${r.x.toFixed(1)}px, ${r.y.toFixed(1)}px)${extra}`;
            if (r.w != null) el.style.width = r.w.toFixed(1) + 'px';
            if (r.h != null) el.style.height = r.h.toFixed(1) + 'px';
            el.style.opacity = o.toFixed(3);
            el.style.visibility = o > 0.001 ? 'visible' : 'hidden';
        }
        const beatAt = t => { for (let i = 6; i >= 0; i--) if (t >= D.start[i]) return i; return 0; };
        const runStart = b => { let i = b; while (i > 0 && CAPTIONS[i - 1] === CAPTIONS[b]) i--; return i; };
        const runEnd = b => { let i = b; while (i < 6 && CAPTIONS[i + 1] === CAPTIONS[b]) i++; return i; };

        function render(t) {
            const L = layout(), st = D.start, s1 = st[1], s2 = st[2], s3 = st[3], s4 = st[4], s5 = st[5], s6 = st[6], END = D.total;
            const b = beatAt(t);

            // the caption: one at a time; "Make it yours" runs on through the selection and the refinement
            const rs = st[runStart(b)], re = st[runEnd(b)] + D.dur[runEnd(b)];
            if (X.cap.textContent !== CAPTIONS[b]) X.cap.textContent = CAPTIONS[b];
            put(X.eyebrow, L.eyebrow, CAPTIONS[b] ? ramp(t, rs, rs + 250) * (1 - ramp(t, re - 180, re)) : 0);

            // the composer types the prompt, then rises above the four options; it leaves before the chosen ring moves
            X.composer.style.width = L.composerW + 'px';
            const n = t < s1 ? Math.max(0, Math.floor(((t - 200) / 1000) * RATE)) : S.prompt.length;
            const typed = S.prompt.slice(0, Math.min(n, S.prompt.length));
            if (X.typed.textContent !== typed) X.typed.textContent = typed;
            X.caret.classList.toggle('sc-off', !(t < D.pressAt));
            X.composer.classList.toggle('sc-press', t >= D.pressAt && t < D.pressAt + 260);
            const ch = X.composer.offsetHeight, rise = E(t, s1, s1 + 500);
            placeSlots(L, L.composerTop.y + ch * L.composerTop.s);
            put(X.composer, { x: (L.W - L.composerW) / 2, y: lerp(L.composerMidY - ch / 2, L.composerTop.y, rise) },
                t < s1 ? ramp(t, 0, 300) : t < s3 ? 1 : 1 - ramp(t, s3, s3 + 300), ` scale(${lerp(1, L.composerTop.s, rise).toFixed(3)})`);

            // the four options appear one by one; the three not chosen dim, then leave before the chosen one moves
            const appear = k => E(t, s1 + 300 + k * 120, s1 + 300 + k * 120 + 340);
            const dim = E(t, s2, s2 + 450);
            X.opts.forEach(el => {
                const k = +el.dataset.k;
                if (k === S.picked) { put(el, L.slots[k], 0); return; }
                const a = appear(k);
                put(el, grow(L.slots[k], lerp(0.94, 1, a), (1 - a) * 16), t < s1 ? 0 : t < s3 ? a * lerp(1, 0.34, dim) : 0.34 * (1 - ramp(t, s3, s3 + 300)));
                el.style.filter = `saturate(${lerp(1, 0.5, t >= s2 ? dim : 0).toFixed(2)})`;
            });

            // the focus card: the chosen option → lifted → centre stage → refined → turning → metals → the final ring
            const p = S.picked, a = appear(p), lifted = grow(L.slots[p], 1 + 0.045 * dim, -6 * dim);
            let fr, fo = 1;
            if (t < s1) { fr = L.slots[p]; fo = 0; }
            else if (t < s2) { fr = grow(L.slots[p], lerp(0.94, 1, a), (1 - a) * 16); fo = a; }
            else if (t < s3) fr = lifted;
            else if (t < s4) fr = LR(lifted, L.hero, E(t, s3 + 300, s3 + 950));
            else if (t < s6) fr = L.hero;
            else { fr = LR(L.hero, L.final, E(t, s6, s6 + 750)); fo = 1 - ramp(t, END - 450, END); }
            put(X.focus, fr, fo);
            X.focus.style.borderRadius = Math.max(14, fr.w * 0.055).toFixed(1) + 'px';
            const ring = t < s2 ? 0 : t < s3 ? dim : t < s4 ? 1 - E(t, s3 + 300, s3 + 950) : 0;
            X.focus.style.boxShadow = `0 0 0 2px rgba(201,169,110,${ring.toFixed(3)}), 0 ${lerp(18, 26, ring).toFixed(0)}px ${lerp(40, 50, ring).toFixed(0)}px -22px rgba(60,40,10,.38), 0 0 0 1px rgba(0,0,0,.04)`;

            // inside the card: the refined ring is revealed by a soft wipe (never a ghosted blend of two rings)
            const videoOk = X.video.readyState >= 2;
            const wipe = S.refine ? ramp(t, s3 + D.wipeAt, s3 + D.wipeAt + 800) : 0;
            const revealed = !!S.refine && t >= s3 + D.wipeAt + 800;
            const out = videoOk ? ramp(t, s4, s4 + 220) : 0;                 // the turn starts with a short fade through white
            X.orig.style.opacity = (S.refine ? (revealed ? 0 : 1) : 1 - out).toFixed(3);
            if (S.refine) {
                const W = lerp(-20, 120, ease(wipe));
                const m = t < s3 + D.wipeAt ? 'linear-gradient(105deg, transparent 0%, transparent 100%)'
                        : revealed ? 'none' : `linear-gradient(105deg, #000 ${(W - 14).toFixed(1)}%, transparent ${(W + 14).toFixed(1)}%)`;
                X.ref.style.webkitMaskImage = m; X.ref.style.maskImage = m;
                X.ref.style.opacity = (t < s3 + D.wipeAt ? 0 : 1 - out).toFixed(3);
                X.sweep.style.opacity = (wipe > 0 && wipe < 1 ? Math.sin(Math.PI * wipe) * 0.35 : 0).toFixed(3);
                X.sweep.style.background = `linear-gradient(105deg, transparent ${(W - 7).toFixed(1)}%, rgba(255,247,228,.85) ${W.toFixed(1)}%, transparent ${(W + 7).toFixed(1)}%)`;
            } else X.ref.style.opacity = '0';
            X.video.style.opacity = (videoOk ? (t < s4 ? 0 : t < s5 ? ramp(t, s4 + 220, s4 + 520) : t < s5 + 450 ? 1 : 0) : 0).toFixed(3);
            X.silver.style.opacity = (t < s5 ? 0 : ramp(t, s5, s5 + 450)).toFixed(3);
            X.gold.style.opacity = (t < s5 ? 0 : ramp(t, s5 + D.goldAt, s5 + D.goldAt + 550)).toFixed(3);

            // one small label on the card at a time
            const labels = [[s2 + 150, s3 + 220, 'Selected']];
            if (S.refine) labels.push([s3 + 950, s3 + D.wipeAt + 300, 'Original'], [s3 + D.wipeAt + 500, s4 + 220, 'Refined']);
            else labels.push([s3 + 950, s4 + 220, 'Selected']);
            labels.push([s4 + 350, s5 + 220, '360°']);
            const lab = labels.find(([x, y]) => t >= x && t < y);
            if (lab && X.tag.textContent !== lab[2]) X.tag.textContent = lab[2];
            X.tag.style.opacity = lab ? span(t, lab[0], lab[1]).toFixed(3) : '0';

            // the refinement request, typed under the ring at the same pace and held long enough to read
            if (S.refine) {
                const key = S.refine.text + '|' + L.chipMax;
                if (chipKey !== key) {        // measure the finished request once: the chip never grows while it is typed
                    X.chip.style.width = 'auto'; X.chip.style.minHeight = '0px'; X.chip.style.maxWidth = L.chipMax + 'px'; X.rtext.textContent = S.refine.text;
                    X.chip.dataset.w = Math.ceil(X.chip.getBoundingClientRect().width) + 1;
                    X.chip.style.width = Math.min(+X.chip.dataset.w, L.chipMax) + 'px';
                    X.chip.style.minHeight = Math.ceil(X.chip.getBoundingClientRect().height) + 'px';
                    chipKey = key;
                }
                const k = Math.max(0, Math.floor(((t - (s3 + 1000)) / 1000) * RATE));
                const txt = t < s3 ? '' : S.refine.text.slice(0, Math.min(k, S.refine.text.length));
                if (X.rtext.textContent !== txt) X.rtext.textContent = txt;
                X.rcaret.classList.toggle('sc-off', t >= s3 + D.typeI + 250);
                const cw = Math.min(+X.chip.dataset.w, L.chipMax);
                put(X.chip, { x: (L.W - cw) / 2, y: L.hero.y + L.hero.h + 0.035 * L.H, w: cw }, ramp(t, s3 + 900, s3 + 1100) * (1 - ramp(t, s4, s4 + 220)));
            } else put(X.chip, { x: 0, y: 0 }, 0);

            // the metals: Silver, then Yellow Gold — one still, the same angle and scale
            const goldOn = t >= s5 + D.goldAt + 275;
            chips.forEach((c, i) => c.classList.toggle('sc-on', i === (goldOn ? 1 : 0)));
            put(X.metals, { x: (L.W - X.metals.offsetWidth) / 2, y: L.hero.y + L.hero.h + 0.035 * L.H },
                ramp(t, s5 + 100, s5 + 400) * (1 - ramp(t, s6, s6 + 250)));

            // the final frame: the gold ring, large, with its name and one line — nothing else
            const fin = E(t, s6 + 350, s6 + 950), outro = 1 - ramp(t, END - 450, END);
            put(X.name, { x: 0, y: L.final.y + L.final.h + 0.04 * L.H + (1 - fin) * 10, w: L.W }, t >= s6 ? fin * outro : 0);

            syncVideo(t);
            if (ctl.onFrame) ctl.onFrame(ctl.state());
        }

        // ── the movie (a short clip of its last seconds): it settles on the front view — the same view as the still ──
        function covered(x) {
            if (x <= 0) return 0;
            if (x <= 1480) return x / 1000;
            const d = Math.min(x, 2080) - 1480;
            return 1.48 + d / 1000 - (0.75 * d * d) / (2 * 600 * 1000);
        }
        function syncVideo(t) {
            const v = X.video, s4 = D.start[4], s5 = D.start[5], x = t - (s4 + 220);
            const want = VOFF + covered(Math.min(x, 2080));
            const running = playing && visible && !document.hidden;
            if (t >= s4 + 220 && t < s5 && running) {
                const rate = x < 1480 ? 1 : lerp(1, 0.25, clamp((x - 1480) / 600));
                if (Math.abs(v.playbackRate - rate) > 0.02) v.playbackRate = rate;
                if (v.paused) { if (v.readyState >= 1) { try { v.currentTime = want; } catch (_) {} } v.play().catch(() => {}); }
                else if (Math.abs(v.currentTime - want) > 0.25) { try { v.currentTime = want; } catch (_) {} }
            } else {
                if (!v.paused) v.pause();
                const hold = t < s4 ? VOFF : t < s5 ? want : VOFF + VCOVER;
                if (v.readyState >= 1 && !v.seeking && Math.abs(v.currentTime - hold) > 0.04) { try { v.currentTime = hold; } catch (_) {} }
            }
        }

        // ── build, preload, run ──
        function build() {
            X.defs.innerHTML = filterDefs(S);
            const sm = stage.clientWidth < 480;
            X.opts.forEach(el => {
                const u = S.options[+el.dataset.k], img = el.querySelector('img');
                img.srcset = `${thumb(u, 320)} 320w, ${thumb(u, 800)} 800w`; img.sizes = sm ? '40vw' : '240px'; img.src = thumb(u, sm ? 320 : 800);
            });
            const set = (img, u) => { img.srcset = `${thumb(u, 800)} 800w, ${thumb(u, 1024)} 1024w`; img.sizes = sm ? '80vw' : '520px'; img.src = thumb(u, 800); };
            set(X.orig, S.options[S.picked]); set(X.ref, S.refine ? S.refine.image_url : S.options[S.picked]);
            set(X.silver, S.still_url); set(X.gold, S.still_url);
            // the render's own metal is shown untouched; only the other metal is a filter
            const goldFilter = X.defs.querySelector('#sc-gold') ? 'url(#sc-gold)' : '';
            X.silver.style.filter = S.tone === 'gold' ? 'url(#sc-silver)' : '';
            X.gold.style.filter = S.tone === 'gold' ? '' : goldFilter;
            const v = X.video;
            v.muted = true; v.defaultMuted = true; v.loop = false; v.playsInline = true;
            v.onloadedmetadata = () => { VOFF = Math.max(0, (v.duration || VCOVER) - VCOVER - 0.03); try { v.currentTime = VOFF; } catch (_) {} };
            v.onerror = () => { if (!fellBack && S.movie_url) { fellBack = true; v.src = S.movie_url; v.load(); } };     // no clip: the whole movie
            v.src = S.clip_url ? S.clip_url.replace('w=720', sm ? 'w=480' : 'w=720') : S.movie_url;
            const shown = [['Silver', (S.metals || []).find(m => m.id === 'silver')], ['18K Yellow Gold', (S.metals || []).find(m => m.id === 'gold_18k_yellow')]].filter(x => x[1]);
            X.metals.innerHTML = '';
            chips = shown.map(([label, m]) => { const c = document.createElement('span'); c.className = 'sc-metal'; c.innerHTML = `<i style="background:${m.swatch}"></i>`; c.append(label); X.metals.appendChild(c); return c; });
            X.title.textContent = S.title;
            stage.setAttribute('aria-label', `${S.title}, designed with XJet Atelier: an idea, four possibilities, one chosen and refined, seen from every angle and in Silver and Yellow Gold`);
        }
        function preload() {
            const imgs = [...X.opts.map(o => o.querySelector('img')), X.orig, X.ref];
            const ready = el => (el.complete && el.naturalWidth ? Promise.resolve() : (el.decode ? el.decode() : Promise.resolve())).catch(() => {});
            return Promise.race([Promise.all(imgs.map(ready)), new Promise(r => setTimeout(r, 2500))]);
        }
        function frame(now) {
            if (destroyed) return;
            if (!stage.isConnected) { destroy(); return; }
            raf = requestAnimationFrame(frame);
            if (last === null) last = now;
            const dt = Math.min(64, now - last); last = now;
            if (!(playing && visible && !document.hidden)) return;           // off screen, hidden or paused: nothing to draw
            T = (T + dt) % D.total;
            render(T);
        }

        const io = 'IntersectionObserver' in window
            ? new IntersectionObserver(es => { visible = es[es.length - 1].isIntersecting; if (!visible) X.video.pause(); }, { threshold: 0.15 }) : null;
        if (io) io.observe(stage);
        const onHidden = () => { if (document.hidden) X.video.pause(); };
        const onResize = () => { if (S && D) render(T); };
        document.addEventListener('visibilitychange', onHidden);
        window.addEventListener('resize', onResize);
        if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { chipKey = ''; if (S && D && !destroyed) render(T); });

        function destroy() {
            if (destroyed) return;
            destroyed = true; cancelAnimationFrame(raf);
            try { X.video.pause(); X.video.removeAttribute('src'); X.video.load(); } catch (_) {}
            if (io) io.disconnect();
            document.removeEventListener('visibilitychange', onHidden);
            window.removeEventListener('resize', onResize);
            if (stage._sc === ctl) stage._sc = null;
        }

        // No story may be told (no XJet design of a product on offer with its movie on): a gallery design's still and name
        function showStill(St) {
            const img = document.createElement('img');
            img.className = 'sc-still'; img.alt = St.title + ' — designed with XJet Atelier'; img.decoding = 'async';
            img.src = thumb(St.image_url, stage.clientWidth < 480 ? 480 : 960);
            const cap = document.createElement('div');
            cap.className = 'sc-still-name';
            cap.innerHTML = '<span class="sc-title"></span><span class="sc-by">Designed with XJet Atelier</span>';
            cap.querySelector('.sc-title').textContent = St.title;
            stage.append(img, cap);
            X.loading.classList.add('sc-gone');
        }

        const ctl = {
            captions: CAPTIONS, labels: LABELS, onFrame: opts.onFrame || null, choices: [],
            get story() { return S; }, get plan() { return D; },
            state() { const b = D ? beatAt(T) : 0; return { beat: b, label: LABELS[b], t: Math.round(T), playing, total: D ? D.total : 0, starts: D ? D.start : [], story: S ? S.title : null }; },
            seek(ms) { if (!D) return null; T = Math.max(0, Math.min(D.total - 1, ms)); render(T); return ctl.state(); },
            play() { playing = true; last = null; },
            pause() { playing = false; if (D) render(T); },
            restart() { T = 0; playing = true; last = null; },
            filterDefs: () => (S ? filterDefs(S) : ''),
            destroy,
        };
        stage._sc = ctl;

        fetchStory(opts.design).then(async A => {
            if (destroyed) return;
            ctl.choices = A.choices || [];
            if (!A.story) {
                if (A.still && A.still.image_url) showStill(A.still);         // no story may be told: the design's still
                else X.loading.textContent = opts.emptyText || '';
                return;
            }
            S = A.story; D = planFor(S);
            build(); await preload();
            if (destroyed) return;
            X.loading.classList.add('sc-gone');
            if (reduced) T = D.start[6] + 1500;          // reduced motion: the finished ring and its name, still
            render(T);
            if (opts.onReady) opts.onReady(ctl);
            raf = requestAnimationFrame(frame);
        }).catch(e => { if (!destroyed) X.loading.textContent = opts.errorText || ''; console.error('showcase:', e); });
        return ctl;
    }

    window.P3Showcase = { mount, captions: CAPTIONS, labels: LABELS, planFor };
})();
