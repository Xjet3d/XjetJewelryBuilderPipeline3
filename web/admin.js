// P3 Admin — Dashboard | Sessions | Users. Talks only to {base}/api/admin/*. The admin key is entered once
// per browser: POST /api/admin/login turns it into a server-side session cookie (HttpOnly) that outlives
// closed tabs, a closed browser and a restart, until Sign out. The key itself is never stored here.
const BASE = document.querySelector('meta[name="p3-base"]')?.content || '';

const STATUS = {
  active: ['Active', 'bg-emerald-100 text-emerald-800'],
  unused: ['Unused', 'bg-amber-100 text-amber-800'],
  pending_verification: ['Pending verification', 'bg-sky-100 text-sky-800'],
  exhausted: ['Exhausted', 'bg-red-100 text-red-700'],
  inactive: ['Inactive', 'bg-zinc-200 text-zinc-600'],
  removed: ['Removed', 'bg-zinc-800 text-white'],
};
const STEPS = {
  started: ['Started', 'bg-zinc-900 text-white'],
  generate_requested: ['Prompt', 'bg-zinc-100 text-zinc-600'],
  generated: ['Generated', 'bg-blue-100 text-blue-700'],
  refine_requested: ['Refine request', 'bg-zinc-100 text-zinc-600'],
  refined: ['Refined', 'bg-violet-100 text-violet-700'],
  option_selected: ['Option selected', 'bg-zinc-100 text-zinc-600'],
  customize_opened: ['Customize', 'bg-amber-100 text-amber-800'],
  customization_changed: ['Changed choice', 'bg-amber-50 text-amber-800'],
  movie: ['360° movie', 'bg-purple-100 text-purple-700'],
  bag_added: ['Added to Bag', 'bg-emerald-100 text-emerald-800'],
  bag_removed: ['Removed from Bag', 'bg-zinc-200 text-zinc-600'],
  bag_viewed: ['Viewed Bag', 'bg-zinc-100 text-zinc-600'],
  checkout_clicked: ['Checkout Clicked', 'bg-emerald-600 text-white'],
  design_opened: ['Reopened', 'bg-zinc-100 text-zinc-600'],
  admin_3d_requested: ['Admin: 3D requested', 'bg-green-100 text-green-700'],
  admin_3d_measured: ['Admin: 3D measured', 'bg-green-100 text-green-700'],
  admin_3d_new_model_override: ['Admin: NEW Hi3D model (override)', 'bg-red-100 text-red-700'],
};
const THREE_D = {
  requested: ['Requested', 'bg-zinc-100 text-zinc-600'], generating: ['Hi3D running', 'bg-sky-100 text-sky-800'],
  queued: ['Queued', 'bg-sky-100 text-sky-800'],
  measuring: ['Measuring', 'bg-sky-100 text-sky-800'], measured: ['Measured', 'bg-emerald-100 text-emerald-800'],
  needs_review: ['Review required', 'bg-amber-100 text-amber-800'], failed: ['Failed', 'bg-red-100 text-red-700'],
  cancelled: ['Cancelled', 'bg-zinc-100 text-zinc-600'],
};
// Production readiness, separate from processing: "complete" only when nothing was flagged.
const PROD_STATE = {
  processing: ['Processing', 'bg-sky-100 text-sky-800'],
  complete: ['Ready for production', 'bg-emerald-100 text-emerald-800'],
  review_required: ['Processing complete — production review required', 'bg-amber-100 text-amber-800'],
  failed: ['Failed', 'bg-red-100 text-red-700'],
  cancelled: ['Cancelled', 'bg-zinc-100 text-zinc-600'],
};
const GL = { renderer: null, failed: false, scene: null, camera: null, light: null, mesh: null, controls: null, io: null, raf: 0, soft: null };
// WebGL-free 3D view (browsers with graphics acceleration off): the light preview (~25k faces) drawn with
// the plain 2D canvas — depth-sorted, shaded triangles; drag to rotate, slow auto-rotate while visible.
function SoftViewer(el, geo, colorHex) {
  const pos = geo.attributes.position.array, idx = geo.index ? geo.index.array : null;
  const nv = pos.length / 3, nf = idx ? idx.length / 3 : nv / 3;
  geo.computeBoundingSphere();
  const R = geo.boundingSphere.radius || 1, C = geo.boundingSphere.center;
  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'width:100%;height:100%;display:block;touch-action:none;cursor:grab';
  el.innerHTML = ''; el.appendChild(canvas);
  const ctx = canvas.getContext('2d');
  const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
  const hex = (colorHex || '#c0c0c0').replace('#', '').padEnd(6, '0');
  const base = [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16));
  const X = new Float32Array(nv), Y = new Float32Array(nv), Z = new Float32Array(nv);
  const depth = new Float32Array(nf), order = new Uint32Array(nf), shade = new Float32Array(nf);
  const L = [0.35, 0.55, 0.76];
  let yaw = 0.5, pitch = -0.35, drag = null, raf = 0, alive = true, onScreen = true, last = 0;
  const v = (f, k) => (idx ? idx[f * 3 + k] : f * 3 + k);
  function draw() {
    const W = canvas.clientWidth || 300, H = canvas.clientHeight || 300;
    if (canvas.width !== Math.round(W * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
    const s = 0.42 * Math.min(canvas.width, canvas.height) / R, cx = canvas.width / 2, cy = canvas.height / 2;
    const cyw = Math.cos(yaw), syw = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
    for (let i = 0; i < nv; i++) {
      const x = pos[i * 3] - C.x, y = pos[i * 3 + 1] - C.y, z = pos[i * 3 + 2] - C.z;
      const x1 = x * cyw + z * syw, z1 = -x * syw + z * cyw;
      X[i] = x1; Y[i] = y * cp - z1 * sp; Z[i] = y * sp + z1 * cp;
    }
    for (let f = 0; f < nf; f++) {
      const a = v(f, 0), b = v(f, 1), c = v(f, 2);
      const ux = X[b] - X[a], uy = Y[b] - Y[a], uz = Z[b] - Z[a], wx = X[c] - X[a], wy = Y[c] - Y[a], wz = Z[c] - Z[a];
      let nx = uy * wz - uz * wy, ny = uz * wx - ux * wz, nz = ux * wy - uy * wx;
      const n = Math.hypot(nx, ny, nz) || 1; nx /= n; ny /= n; nz /= n;
      const d = Math.abs(nx * L[0] + ny * L[1] + nz * L[2]);         // two-sided: no reliance on winding
      shade[f] = 0.32 + 0.6 * d + 0.35 * Math.pow(d, 24);           // ambient + diffuse + a metal highlight
      depth[f] = Z[a] + Z[b] + Z[c];
      order[f] = f;
    }
    order.sort((p, q) => depth[p] - depth[q]);                       // far → near (painter's algorithm)
    ctx.fillStyle = '#fafafa'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.lineWidth = 0.6; ctx.lineJoin = 'round';
    for (let k = 0; k < nf; k++) {
      const f = order[k], a = v(f, 0), b = v(f, 1), c = v(f, 2), t = shade[f];
      const col = 'rgb(' + (Math.min(255, base[0] * t) | 0) + ',' + (Math.min(255, base[1] * t) | 0) + ',' + (Math.min(255, base[2] * t) | 0) + ')';
      ctx.fillStyle = col; ctx.strokeStyle = col;
      ctx.beginPath();
      ctx.moveTo(cx + X[a] * s, cy - Y[a] * s); ctx.lineTo(cx + X[b] * s, cy - Y[b] * s); ctx.lineTo(cx + X[c] * s, cy - Y[c] * s);
      ctx.closePath(); ctx.fill(); ctx.stroke();
    }
  }
  function tick(ts) {
    if (!alive) return;
    if (!el.isConnected) { alive = false; return; }
    if (!drag && onScreen && !document.hidden && ts - last > 80) { yaw += 0.03; last = ts; draw(); }   // ~12 fps
    raf = requestAnimationFrame(tick);
  }
  const down = (e) => { drag = { x: e.clientX, y: e.clientY, yaw, pitch }; canvas.setPointerCapture(e.pointerId); canvas.style.cursor = 'grabbing'; };
  const move = (e) => { if (!drag) return; yaw = drag.yaw + (e.clientX - drag.x) * 0.01; pitch = Math.max(-1.5, Math.min(1.5, drag.pitch + (e.clientY - drag.y) * 0.01)); draw(); };
  const up = () => { drag = null; canvas.style.cursor = 'grab'; };
  canvas.addEventListener('pointerdown', down); canvas.addEventListener('pointermove', move);
  canvas.addEventListener('pointerup', up); canvas.addEventListener('pointercancel', up);
  const io = new IntersectionObserver(([e]) => { onScreen = e.isIntersecting; }); io.observe(el);
  draw(); raf = requestAnimationFrame(tick);
  return { dispose() { alive = false; cancelAnimationFrame(raf); io.disconnect(); canvas.remove(); geo.dispose(); }, draw };
}

const NEW_MODEL_PHRASE = 'GENERATE NEW 3D';   // the phrase the admin must type for a second paid Hi3D model
const STAGE_SHORT = { waiting_hi3d: 'Waiting for Hi3D', generating_3d: 'Generating 3D', downloading_stl: 'Download',
  queued: 'Queued', calculating_geometry: 'Geometry', correcting_bore: 'Rounding the bore', exporting: 'Scaled STL' };
const EVENTS = {
  design_created: ['Design', 'bg-blue-100 text-blue-700'],
  gallery_started: ['Started from gallery', 'bg-amber-100 text-amber-800'],
  gallery_reopened: ['Reopened from gallery', 'bg-amber-100 text-amber-800'],
  gallery_refined: ['Refined from gallery', 'bg-violet-100 text-violet-700'],
  design_forked: ['Refined into a new design', 'bg-violet-100 text-violet-700'],
  design_restored: ['Back in My Designs', 'bg-amber-100 text-amber-800'],
  admin_refinement_split: ['Refinement moved to its own design', 'bg-amber-100 text-amber-800'],
  legacy_copy_merged: ['Legacy copy merged', 'bg-amber-100 text-amber-800'],
  design_removed: ['Removed from My Designs', 'bg-zinc-200 text-zinc-600'],
  refinement: ['Refinement', 'bg-violet-100 text-violet-700'],
  movie: ['360° movie', 'bg-purple-100 text-purple-700'],
  mesh: ['3D', 'bg-green-100 text-green-700'],
  bag_add: ['Added to bag', 'bg-zinc-900 text-white'],
  sign_in: ['Sign-in', 'bg-zinc-100 text-zinc-600'],
  admin_created: ['Created by admin', 'bg-zinc-100 text-zinc-600'],
  admin_edited: ['Edited by admin', 'bg-zinc-100 text-zinc-600'],
  activated: ['Activated', 'bg-emerald-100 text-emerald-800'],
  deactivated: ['Deactivated', 'bg-zinc-200 text-zinc-600'],
  removed: ['Removed', 'bg-zinc-800 text-white'],
  restored: ['Restored', 'bg-emerald-100 text-emerald-800'],
};

// Copy text to the clipboard. The Clipboard API exists only on https (and localhost); on a plain-http site such
// as proto the older selection copy still works inside a click. Returns whether the text was copied.
async function copyText(text) {
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; } } catch (_) {}
  const active = document.activeElement;
  try {
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', ''); ta.setAttribute('aria-hidden', 'true');
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none;';
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch (_) { return false; }
  finally { try { active && active.focus && active.focus({ preventScroll: true }); } catch (_) {} }
}

function adminApp() {
  return {
    BASE,
    key: '', keyInput: '', ok: false, busy: false, error: '', mode: '',
    users: [], search: '', showRemoved: false,
    form: { Name: '', Email: '', MaxGenerations: 10 },
    created: null, createError: '', duplicateOf: null, copied: null,
    editing: null, edit: {}, editError: '',
    userId: '', d: null, detailError: '', openDesign: null,
    tab: 'sessions', materials: {}, charmMaterials: {}, swatches: {}, showChoices: false,
    viewer3d: { id: null, label: '', loading: false, error: '' }, downloadNote: '', zoom: null,
    live3d: {}, exports3d: {}, clock: Date.now(), skew: 0, storage: null,
    dash: null, dashDays: 0, sessions: [], idleMinutes: 30, sq: '', sStage: '', sBag: '', s3d: '', sGallery: '', sMock: false, mockSessions: 0,
    sProduct: '', oProduct: '', gProduct: '',         // product filters: '' = All · 'ring' · 'charm'
    productFilters: ['', 'ring', 'charm'],
    sAttention: false, sSort: 'started', attention: null, _listScroll: 0,
    sessionId: '', sd: null, sdError: '', g3: { size: 10, material: '', busy: false, error: '' },
    sect: { gallery: true, pipeline: false, choice: true, designs: false, journey: false },   // session sections (collapsed by default: secondary)
    rename: { open: false, title: '', busy: false, error: '', force: false, suggestions: [], lineage: null },
    retired: [],                                   // Ring IDs of merged legacy copies (never reused): a search for one says where it went
    refBox: null,                                  // full-size view of a customer's reference image (Journey)
    // In-app dialogs and toasts instead of the browser's alert() / confirm() / prompt()
    dialog: null, toasts: [],
    newModel: { open: false, text: '', candidate: '', error: '', busy: false },
    get EVENTS() { return EVENTS; },
    models: [], runtimePlaceholders: {}, mid: '', mc: null, draft: {}, dirty: false, note: '',
    mProduct: 'ring',                              // AI models & prompts: which product's configuration is shown (Ring | Charm)
    products: null, productsMsg: '', productsErr: false, productsBusy: false,
    mProblems: [], mMessage: '', mBusy: false, preview: null,
    prices: null, pricesEdit: null, pricesBusy: false, pricesMsg: '', pricesErr: false,
    mprices: null, mpEdit: null, mpNote: '', mpBusy: false, mpMsg: '', mpErr: false,
    // Pricing & Materials: which product's prices are shown (Ring | Charm); the charm table is its own
    pProduct: 'ring', cprices: null, cpEdit: null, cpNote: '', cpBusy: false, cpMsg: '', cpErr: false,
    sizesEdit: null, sizeAdd: '', sizeAddName: '', sizeNames: {}, sizeDefault: null, sizesNote: '', sizesBusy: false, sizesMsg: '', sizesErr: false,
    galleryItems: [], galleryMsg: '', gallerySort: 'position', galleryOpen: null, galleryUsage: {}, galleryLinkCopied: '',
    // Orders (operational) · quote requests (gold) · promo codes · settings sub-tabs
    orders: [], ordersMeta: { statuses: [], payment_statuses: [] }, quoteRequests: [], ordersMsg: '', ordersLoading: false,
    oq: '', oStatus: '', oPayment: '', orderId: '', od: null, odError: '', odBusy: false,
    oStatusForm: { status: '', note: '' }, oPay: { open: false, status: 'paid', note: '', ref: '' }, oNote: '',
    promos: [], promoMsg: '', promoErr: false, promoEdit: null,
    sub: 'pricing', health: null,
    stageOptions: [['started', 'Started'], ['generated', 'Generated'], ['selected', 'Selected'], ['customize', 'Customize'], ['bag', 'Bag'], ['checkout_clicked', 'Checkout'], ['order', 'Order']],
    chartKinds: [
      { key: 'images', label: 'Images', color: '#3b82f6' },
      { key: 'refinement_images', label: 'Refinement images', color: '#8b5cf6' },
      { key: 'movies', label: '360° movies', color: '#9A7230' },
      { key: 'meshes', label: '3D', color: '#16a34a' },
    ],

    async init() {
      window.addEventListener('hashchange', () => this.route());
      this.startSessionsRefresh();
      this.installZoom();
      try {   // the Sessions filters survive a reload
        const f = JSON.parse(sessionStorage.getItem('p3_admin_filters') || 'null');
        if (f) Object.assign(this, { sq: f.sq || '', sStage: f.sStage || '', sBag: f.sBag || '', s3d: f.s3d || '', sGallery: f.sGallery || '', sMock: !!f.sMock, sAttention: !!f.sAttention, sSort: f.sSort || 'started',
                                     sProduct: ['ring', 'charm'].includes(f.sProduct) ? f.sProduct : '' });
      } catch (_) {}
      // A remembered browser: the session cookie signs in without asking for the key again.
      try { await this._enter(await this.api('GET', '/api/admin/session')); } catch (_) { this.ok = false; this.error = ''; }
    },

    // ── dialogs & toasts (no native alert/confirm/prompt) ───────────────
    ask(opts) {
      return new Promise(resolve => {
        this.dialog = { title: opts.title || 'Are you sure?', text: opts.text || '', confirmLabel: opts.confirmLabel || 'Confirm',
                        cancelLabel: opts.cancelLabel || 'Cancel', danger: !!opts.danger, copy: opts.copy || '', resolve };
        setTimeout(() => document.getElementById('admin-dialog')?.querySelector('button, input')?.focus(), 30);
      });
    },
    answer(ok) { const d = this.dialog; this.dialog = null; d?.resolve?.(ok); },
    notify(text, kind = 'ok') {
      const id = Date.now() + Math.random();
      this.toasts.push({ id, text, kind });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, kind === 'error' ? 8000 : 4500);
    },
    fail(e) { this.notify(e?.message || String(e), 'error'); },

    authHeaders() { return this.key ? { Authorization: 'Bearer ' + this.key } : {}; },   // cookie otherwise
    async api(method, path, body) {
      const r = await fetch(BASE + path, {
        method, headers: { ...this.authHeaders(), ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await r.json().catch(() => ({}));
      if ((r.status === 401 || r.status === 403) && !path.endsWith('/login')) {
        this.ok = false; this.error = 'Your admin sign-in has expired or was revoked — please sign in again.';
      }
      if (!r.ok) { const e = new Error(data?.error?.message || ('Request failed (' + r.status + ')')); e.code = data?.error?.code; throw e; }
      return data;
    },

    async _enter(s) {
      this.mode = s.mode; this.ok = true; this.keyInput = ''; this.error = '';
      try {
        const cat = await (await fetch(BASE + '/api/catalog')).json();
        for (const g of cat.groups || []) for (const m of g.materials || []) { this.materials[m.id] = m.label; this.swatches[m.id] = m.swatch; }
        for (const m of cat.products?.charm?.materials || []) this.charmMaterials[m.id] = m.label;    // "Sterling Silver" …
      } catch (_) {}
      this.route();
    },
    // The key is sent once, to /api/admin/login, and never kept in the browser.
    async connect() {
      const key = this.keyInput.trim();
      if (!key) return;
      this.busy = true; this.error = '';
      try { await this._enter(await this.api('POST', '/api/admin/login', { key })); }
      catch (e) { this.ok = false; this.error = e.message; }
      finally { this.busy = false; }
    },
    async signOut(everywhere = false) {
      try { await this.api('POST', everywhere ? '/api/admin/logout-everywhere' : '/api/admin/logout'); } catch (_) {}
      this.key = ''; this.ok = false; this.users = []; this.d = null; this.sd = null; this.dash = null;
      this.error = everywhere ? 'Signed out of every browser. Sign in again here when needed.' : '';
    },

    // ── routing: #/dashboard · #/sessions[/<id>] · #/users[/<account id>] · #/orders[/<id>] · #/gallery ·
    //    #/settings/(pricing|promos|models[/<model>]|system)   (#/models/<id> still works) ──
    go(hash) { if (location.hash === hash) this.route(); else location.hash = hash; },
    async route() {
      let hash = location.hash;
      if (hash.startsWith('#/models')) { hash = '#/settings' + hash.slice(1); history.replaceState(null, '', hash); }   // old links
      if (this.dirty && this.tab === 'settings' && this.sub === 'models' && !hash.startsWith('#/settings/models/' + this.mid)) {
        history.replaceState(null, '', '#/settings/models/' + this.mid);
        if (!await this.ask({ title: 'Discard unsaved changes?', text: 'The changes to ' + (this.mc?.model.label || 'this model') + ' are not saved.', confirmLabel: 'Discard', danger: true })) return;
        this.dirty = false; location.hash = hash; return;
      }
      const m = hash.match(/^#\/(dashboard|sessions|gallery|users|orders|settings)(?:\/(.+))?$/);
      this.tab = m ? m[1] : 'sessions';
      let id = m && m[2] ? decodeURIComponent(m[2]) : '';
      if (this.tab === 'settings') {
        const parts = id.split('/');
        this.sub = ['pricing', 'promos', 'models', 'products', 'system'].includes(parts[0]) ? parts[0] : 'pricing';
        id = parts.slice(1).join('/');
      }
      const wasList = this.tab === 'sessions' && !this.sessionId;
      this.userId = this.tab === 'users' ? id : '';
      this.sessionId = this.tab === 'sessions' ? id : '';
      this.orderId = this.tab === 'orders' ? id : '';
      this.openDesign = null; this.zoom = null; this.rename.open = false;
      const backToList = this.tab === 'sessions' && !id && !wasList;
      if (!backToList) window.scrollTo({ top: 0 });
      if (!this.ok) return;
      if (this.tab === 'dashboard') await this.loadDashboard();
      if (this.tab === 'sessions' && !id) {
        await this.loadSessions();
        if (backToList) this.$nextTick(() => window.scrollTo({ top: this._listScroll }));   // back where you were
      }
      if (this.tab === 'sessions' && id) await this.loadSession();
      if (this.tab === 'users' && !id) await this.load();
      if (this.tab === 'users' && id) await this.loadDetail(); else this.d = null;
      if (this.tab === 'orders' && !id) await this.loadOrders();
      if (this.tab === 'orders' && id) await this.loadOrder(); else this.od = null;
      if (this.tab === 'gallery') await this.loadGallery();
      if (this.tab === 'settings') {
        if (this.sub === 'models' || this.sub === 'pricing') await this.loadModels(id);
        if (this.sub === 'pricing') await this.loadCharmPrices();
        if (this.sub === 'promos') await this.loadPromos();
        if (this.sub === 'products') await this.loadProducts();
        if (this.sub === 'system') {
          this.storage = await this.api('GET', '/api/admin/storage').catch(() => null);
          this.health = await this.api('GET', '/api/admin/health').catch(() => null);   // the full picture is admin-only
        }
      }
    },

    // ── orders ──────────────────────────────────────────────────────────
    async loadOrders() {
      this.ordersLoading = true; this.ordersMsg = '';
      try {
        const q = new URLSearchParams();
        if (this.oStatus) q.set('status', this.oStatus);
        if (this.oPayment) q.set('payment', this.oPayment);
        if (this.oq.trim()) q.set('q', this.oq.trim());
        if (this.oProduct) q.set('product', this.oProduct);
        const r = await this.api('GET', '/api/admin/orders' + (q.toString() ? '?' + q : ''));
        this.orders = r.orders; this.quoteRequests = r.quote_requests;
        this.ordersMeta = { statuses: r.statuses, payment_statuses: r.payment_statuses };
      } catch (e) { this.ordersMsg = e.message; }
      finally { this.ordersLoading = false; }
    },
    orderStatusLabel(s) { return (this.ordersMeta.statuses.find(x => x.id === s) || { label: s })?.label || s; },
    orderStatusClass(s) {
      return { new: 'bg-sky-100 text-sky-800', payment_confirmed: 'bg-emerald-100 text-emerald-800', three_d_ready: 'bg-green-100 text-green-700',
               production: 'bg-violet-100 text-violet-700', qc: 'bg-amber-100 text-amber-800', shipped: 'bg-zinc-900 text-white',
               completed: 'bg-zinc-200 text-zinc-700', cancelled: 'bg-red-100 text-red-700' }[s] || 'bg-zinc-100 text-zinc-600';
    },
    payLabel(s) { return (this.ordersMeta.payment_statuses.find(x => x.id === s) || { label: s })?.label || s; },
    payClass(s) { return { pending: 'bg-amber-100 text-amber-800', paid: 'bg-emerald-100 text-emerald-800', failed: 'bg-red-100 text-red-700',
                           refunded: 'bg-zinc-200 text-zinc-700', cancelled: 'bg-zinc-100 text-zinc-500' }[s] || 'bg-zinc-100 text-zinc-600'; },
    addrClass(s) { return { verified: 'bg-emerald-100 text-emerald-800', corrected: 'bg-sky-100 text-sky-800', failed: 'bg-red-100 text-red-700',
                            unverified: 'bg-zinc-100 text-zinc-600' }[s] || 'bg-zinc-100 text-zinc-600'; },
    // ── Settings → Products: customer availability of charms ──
    async loadProducts() {
      this.productsMsg = ''; this.productsErr = false;
      try { this.products = await this.api('GET', '/api/admin/products'); } catch (e) { this.productsErr = true; this.productsMsg = e.message; }
    },
    // Charms available to customers: one switch, each change confirmed in a normal dialog
    async toggleCharms() {
      if (!this.products || this.productsBusy) return;
      const on = !this.products.charms_available;
      const ok = await this.ask(on
        ? { title: 'Enable Charms for customers?', text: 'Customers will be able to choose a ring or a charm when they start a design, and charms will appear in the Inspiration Gallery.', confirmLabel: 'Enable Charms', cancelLabel: 'Cancel' }
        : { title: 'Hide Charms from customers?', text: 'Customers will see the ring-only site again: no product choice, no charm text, no charm tiles. Charm designs, orders and settings are kept, and a browser signed in to the Admin still previews them.', confirmLabel: 'Hide Charms', cancelLabel: 'Cancel' });
      if (!ok) return;
      this.productsBusy = true; this.productsMsg = ''; this.productsErr = false;
      try {
        this.products = await this.api('PUT', '/api/admin/products/availability', { charms_available: on });
        this.productsMsg = on ? 'Charms are now available to customers.' : 'Charms are hidden from customers.';
        this.notify(this.productsMsg);
      } catch (e) { this.productsErr = true; this.productsMsg = e.message; } finally { this.productsBusy = false; }
    },

    // ── rings and charms: one icon set (web/products.js), the size as people read it ──
    pIcon(p, cls) { return window.P3Products ? window.P3Products.icon(p || 'ring', cls) : ''; },
    pLabel(p) { return window.P3Products ? window.P3Products.label(p) : 'Ring'; },
    pPlural(p) { return window.P3Products ? window.P3Products.plural(p) : 'Rings'; },
    isCharm(x) { return !!x && (x.product_type || 'ring') === 'charm'; },
    // An order line / quote request: "US 7" for a ring, "20 mm" for a charm (the server's label; rings read as before)
    sizeOf(l) { return l?.size_label || (l?.ring_size != null ? 'US ' + l.ring_size : (this.isCharm(l) ? 'size TBC' : 'size TBC')); },
    // A session's chosen size: rings exactly as before ("US 7", "US 10 (default)"); a charm in millimetres
    sessionSize(x) {
      if (this.isCharm(x)) return x.charm_size_chosen ? x.charm_size + ' mm' : (x.stage_times.customize ? 'Size not chosen' : '—');
      return x.ring_size_chosen ? 'US ' + x.ring_size : (x.stage_times.customize ? 'US 10 (default)' : '—');
    },
    sessionSizeChosen(x) { return this.isCharm(x) ? !!x.charm_size_chosen : !!x.ring_size_chosen; },
    orderProducts(o) { return (o && o.product_types && o.product_types.length) ? o.product_types : ['ring']; },
    productMix(o) { const p = this.orderProducts(o); return p.length > 1 ? 'Ring + Charm' : this.pLabel(p[0]); },
    orderLineText(o) {
      const l = o.lines[0]; if (!l) return '—';
      return l.title + (o.lines.length > 1 ? ' +' + (o.lines.length - 1) : '');
    },
    async loadOrder() {
      this.odError = ''; this.oPay.open = false; this.oNote = '';
      try {
        this.od = await this.api('GET', '/api/admin/orders/' + encodeURIComponent(this.orderId));
        if (!this.ordersMeta.statuses.length) { const r = await this.api('GET', '/api/admin/orders'); this.ordersMeta = { statuses: r.statuses, payment_statuses: r.payment_statuses }; }
        this.oStatusForm = { status: this.od.status, note: '' };
      } catch (e) { this.od = null; this.odError = e.message; }
    },
    async setOrderStatus() {
      if (!this.od || this.oStatusForm.status === this.od.status) return;
      this.odBusy = true; this.odError = '';
      try { this.od = await this.api('POST', `/api/admin/orders/${encodeURIComponent(this.od.id)}/status`, this.oStatusForm); this.oStatusForm.note = ''; }
      catch (e) { this.odError = e.message; } finally { this.odBusy = false; }
    },
    async recordPayment() {
      if (!this.od || !this.oPay.note.trim()) { this.odError = 'Please say how the payment was received (or why it failed).'; return; }
      this.odBusy = true; this.odError = '';
      try {
        this.od = await this.api('POST', `/api/admin/orders/${encodeURIComponent(this.od.id)}/payment`, { status: this.oPay.status, note: this.oPay.note, ref: this.oPay.ref });
        this.oPay = { open: false, status: 'paid', note: '', ref: '' };
      } catch (e) { this.odError = e.message; } finally { this.odBusy = false; }
    },
    async addOrderNote() {
      if (!this.od || !this.oNote.trim()) return;
      this.odBusy = true; this.odError = '';
      try { this.od = await this.api('POST', `/api/admin/orders/${encodeURIComponent(this.od.id)}/note`, { note: this.oNote }); this.oNote = ''; }
      catch (e) { this.odError = e.message; } finally { this.odBusy = false; }
    },
    async setQuoteStatus(q, status) {
      try { const r = await this.api('POST', `/api/admin/quote-requests/${encodeURIComponent(q.id)}/status`, { status }); Object.assign(q, r); }
      catch (e) { this.ordersMsg = e.message; }
    },
    // Scaled STL for an ordered ring: ONLY the result for the ordered size and material, with the Order ID in the name.
    linePrep: {},
    async downloadOrderStl(line) {
      if (!line.three_d_id || !line.three_d_match) return;
      await this.exportStl({ id: line.three_d_id, scaled_stl: 'on_demand' }, this.od?.ref);
    },
    // No result for this size/material yet: scale the design's existing model to it (arithmetic, no Hi3D
    // call), wait for the numbers, then download. A design without a model is handled on its session page.
    async prepareOrderStl(line) {
      this.linePrep = { ...this.linePrep, [line.id]: true };
      try {
        const r = await this.api('POST', `/api/admin/orders/${encodeURIComponent(this.od.id)}/lines/${encodeURIComponent(line.id)}/3d`);
        let s = await this.api('GET', `/api/admin/3d/${encodeURIComponent(r.three_d_id)}/status`);
        const until = Date.now() + 120000;
        while (!s.done && Date.now() < until) { await new Promise(x => setTimeout(x, 1500)); s = await this.api('GET', `/api/admin/3d/${encodeURIComponent(r.three_d_id)}/status`); }
        this.od = await this.api('GET', '/api/admin/orders/' + encodeURIComponent(this.od.id));
        if (!s.done) { this.notify('The geometry is still being calculated — try again in a moment.', 'error'); return; }
        if (s.status === 'failed') { this.notify('Geometry failed: ' + (s.error || ''), 'error'); return; }
        const fresh = this.od.lines.find(x => x.id === line.id);
        if (fresh?.three_d_id && fresh.three_d_match) await this.exportStl({ id: fresh.three_d_id, scaled_stl: 'on_demand' }, this.od.ref);
      } catch (e) { this.fail(e); }
      finally { const p = { ...this.linePrep }; delete p[line.id]; this.linePrep = p; }
    },

    // ── promo codes ─────────────────────────────────────────────────────
    async loadPromos() {
      this.promoMsg = '';
      try { this.promos = (await this.api('GET', '/api/admin/promo-codes')).promo_codes; } catch (e) { this.promoMsg = e.message; this.promoErr = true; }
    },
    newPromo() { this.promoEdit = { id: null, code: '', kind: 'percent', value: 10, starts_at: '', ends_at: '', usage_limit: '', materials: [], min_subtotal: '', note: '', active: true }; this.promoMsg = ''; },
    editPromo(p) {
      this.promoEdit = { id: p.id, code: p.code, kind: p.kind, value: p.value, starts_at: (p.starts_at || '').slice(0, 10), ends_at: (p.ends_at || '').slice(0, 10),
                         usage_limit: p.usage_limit ?? '', materials: p.materials || [], min_subtotal: p.min_subtotal ?? '', note: p.note || '', active: p.active };
      this.promoMsg = '';
    },
    togglePromoMaterial(id) { const m = this.promoEdit.materials; const i = m.indexOf(id); if (i >= 0) m.splice(i, 1); else m.push(id); },
    async savePromo() {
      const p = this.promoEdit; if (!p) return;
      this.promoMsg = ''; this.promoErr = false;
      const body = { ...p, starts_at: p.starts_at ? p.starts_at + 'T00:00:00Z' : null, ends_at: p.ends_at ? p.ends_at + 'T23:59:59Z' : null,
                     usage_limit: p.usage_limit === '' ? null : p.usage_limit, min_subtotal: p.min_subtotal === '' ? null : p.min_subtotal };
      try {
        if (p.id) await this.api('PATCH', '/api/admin/promo-codes/' + encodeURIComponent(p.id), body);
        else await this.api('POST', '/api/admin/promo-codes', body);
        this.promoEdit = null; await this.loadPromos(); this.promoMsg = 'Saved.';
      } catch (e) { this.promoErr = true; this.promoMsg = e.message; }
    },
    async setPromoActive(p, active) {
      try { await this.api('PATCH', '/api/admin/promo-codes/' + encodeURIComponent(p.id), { active }); await this.loadPromos(); }
      catch (e) { this.promoErr = true; this.promoMsg = e.message; }
    },
    promoRule(p) {
      const parts = [p.kind === 'percent' ? p.value + '% off' : '$' + Number(p.value).toFixed(2) + ' off'];
      if (p.materials?.length) parts.push(p.materials.map(m => this.materialLabel(m)).join(', ') + ' only');
      if (p.min_subtotal) parts.push('min $' + p.min_subtotal);
      if (p.starts_at || p.ends_at) parts.push((p.starts_at ? this.date(p.starts_at) : '…') + ' → ' + (p.ends_at ? this.date(p.ends_at) : '…'));
      return parts.join(' · ');
    },

    // ── list ───────────────────────────────────────────────────────────
    async load() {
      const r = await this.api('GET', '/api/admin/users' + (this.showRemoved ? '?include_removed=true' : ''));
      this.users = r.users;
    },
    get filtered() {
      const q = this.search.trim().toLowerCase();
      return q ? this.users.filter(u => [u.name, u.email, u.token].some(v => (v || '').toLowerCase().includes(q))) : this.users;
    },
    async create() {
      this.busy = true; this.createError = ''; this.duplicateOf = null; this.created = null;
      try {
        const u = await this.api('POST', '/api/admin/users', this.form);
        this.created = u;
        this.form = { Name: '', Email: '', MaxGenerations: 10 };
        await this.load();
      } catch (e) {
        this.createError = e.message;
        if (e.code === 'duplicate_email') this.duplicateOf = (e.message.match(/\((p3local:[^)]+)\)/) || [])[1] || null;
      } finally { this.busy = false; }
    },
    startEdit(u) {
      this.editing = u.account_id; this.editError = '';
      this.edit = { Name: u.name, Email: u.email, MaxGenerations: u.max, ResetUsage: false };
    },
    async saveEdit(u) {
      this.busy = true; this.editError = '';
      try {
        await this.api('PATCH', '/api/admin/users/' + encodeURIComponent(u.account_id), this.edit);
        this.editing = null; await this.load();
      } catch (e) { this.editError = e.message; } finally { this.busy = false; }
    },
    async act(u, action) {
      try {
        await this.api('POST', '/api/admin/users/' + encodeURIComponent(u.account_id) + '/' + action);
        await this.load();
        if (this.userId) await this.loadDetail();
      } catch (e) { this.fail(e); }
    },
    async remove(u) {
      if (!await this.ask({ title: `Remove ${u.name || u.email || u.token}?`, text: 'Their sign-in code stops working immediately. Designs and usage history are kept, and the user can be restored from "Show removed".', confirmLabel: 'Remove', danger: true })) return;
      await this.act(u, 'remove');
    },
    async copy(text) {
      if (!text) return;
      try { await navigator.clipboard.writeText(text); } catch (_) {}
      this.copied = text; setTimeout(() => { if (this.copied === text) this.copied = null; }, 2000);
    },

    // ── dashboard ──────────────────────────────────────────────────────
    async loadDashboard() {
      this.dash = await this.api('GET', '/api/admin/dashboard' + (this.dashDays ? '?days=' + this.dashDays : '')).catch(() => null);
      this.attention = this.dash?.needs_attention || null;
      if (!this.galleryItems.length) this.loadGallery().catch(() => {});       // top designs
    },
    galleryTopList(k, n) { return [...this.galleryItems].filter(g => g[k]).sort((a, b) => (b[k] || 0) - (a[k] || 0)).slice(0, n); },
    latestResult() { return (this.sd?.three_d || []).find(t => t.geometry?.production) || null; },
    async setDashDays(d) { this.dashDays = d; await this.loadDashboard(); },

    // ── sessions ───────────────────────────────────────────────────────
    // Three states for every list: loading (skeleton) · loaded (rows, or a real empty message) · failed (retry)
    sessionsState: 'idle', galleryState: 'idle',
    async loadSessions(opts = {}) {
      if (!opts.quiet) this.sessionsState = 'loading';
      try {
        const r = await this.api('GET', '/api/admin/sessions' + (this.sMock ? '?include_mock=true' : ''));
        this.sessions = r.sessions; this.idleMinutes = r.idle_minutes; this.mockSessions = r.mock_sessions; this.retired = r.retired || [];
        this.sessionsState = 'loaded';
      } catch (e) { this.sessionsState = 'failed'; this.fail(e); }
      if (this.sAttention || !this.attention) this.attention = await this.api('GET', '/api/admin/attention').catch(() => null);
      this.saveFilters();
    },
    saveFilters() {
      try { sessionStorage.setItem('p3_admin_filters', JSON.stringify({ sq: this.sq, sStage: this.sStage, sBag: this.sBag, s3d: this.s3d, sGallery: this.sGallery, sMock: this.sMock, sAttention: this.sAttention, sSort: this.sSort, sProduct: this.sProduct })); } catch (_) {}
    },
    resetFilters() { this.sq = ''; this.sStage = ''; this.sBag = ''; this.s3d = ''; this.sGallery = ''; this.sAttention = false; this.sProduct = ''; this.saveFilters(); },
    // Gallery filter: a master shown in the gallery · a journey started from a gallery design · neither
    galleryMatches(x) {
      if (!this.sGallery) return true;
      if (this.sGallery === 'master') return !!x.in_gallery;
      if (this.sGallery === 'from') return x.origin === 'gallery';
      return !x.in_gallery && x.origin !== 'gallery';
    },
    activeFilterText() {
      const parts = [];
      if (this.sq.trim()) parts.push('search “' + this.sq.trim() + '”');
      if (this.sAttention) parts.push('Needs attention only');
      if (this.sStage) parts.push('stopped at ' + (this.stageOptions.find(o => o[0] === this.sStage)?.[1] || this.sStage));
      if (this.sBag) parts.push(this.sBag === 'yes' ? 'reached Bag' : 'no Bag');
      if (this.s3d) parts.push(this.s3d === 'any' ? 'has 3D' : 'no 3D');
      if (this.sGallery) parts.push({ master: 'in the gallery', from: 'started from the gallery', none: 'not from the gallery' }[this.sGallery]);
      if (this.sProduct) parts.push(this.pPlural(this.sProduct) + ' only');
      return parts.length ? 'filters on: ' + parts.join(', ') : 'filters on';
    },
    // The list refreshes itself while it is open (new sessions appear without a reload), and when the tab comes back
    _sessionsTimer: null,
    startSessionsRefresh() {
      if (this._sessionsTimer) return;
      this._sessionsTimer = setInterval(() => {
        if (this.ok && this.tab === 'sessions' && !this.sessionId && !document.hidden && this.sessionsState === 'loaded') this.loadSessions({ quiet: true }).catch(() => {});
      }, 30000);
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden && this.ok && this.tab === 'sessions' && !this.sessionId) this.loadSessions({ quiet: true }).catch(() => {});
      });
    },
    get filtersActive() { return !!(this.sq.trim() || this.sStage || this.sBag || this.s3d || this.sGallery || this.sAttention || this.sProduct); },
    // Search by what staff actually use: Ring ID (R-1013), option ID (R-1013-B), design name, Order ID, customer, email
    sessionMatches(x, q) {
      if (!q) return true;
      const hay = [x.ring_id, x.selected_ring_id, ...(x.option_ring_ids || []), ...(x.order_refs || []), x.source_ring_id,
                   x.title, x.prompt, x.customer_name, x.customer_email].map(v => (v || '').toLowerCase());
      return hay.some(v => v.includes(q));
    },
    retiredMatches() {
      const q = this.sq.trim().toLowerCase();
      return q ? this.retired.filter(r => (r.ring_id || '').toLowerCase().includes(q) || (r.title || '').toLowerCase().includes(q)) : [];
    },
    get sessionsFiltered() {
      const q = this.sq.trim().toLowerCase();
      const attention = new Set(this.attention?.sessions || []);
      const rows = this.sessions.filter(x =>
        this.sessionMatches(x, q) &&
        (!this.sStage || x.stage_reached === this.sStage) &&
        (!this.sBag || (this.sBag === 'yes') === x.add_to_bag) &&
        (!this.s3d || (this.s3d === 'any') === !!x.three_d_status) &&
        this.galleryMatches(x) &&
        (!this.sProduct || (x.product_type || 'ring') === this.sProduct) &&
        (!this.sAttention || attention.has(x.session_id)));
      const k = this.sSort;
      return rows.sort((a, b) => k === 'activity' ? (b.last_activity_at || '').localeCompare(a.last_activity_at || '')
        : k === 'customer' ? (a.customer_name || a.customer_email || '').localeCompare(b.customer_name || b.customer_email || '')
        : k === 'price' ? ((b.fixed_price?.unit_price || 0) - (a.fixed_price?.unit_price || 0))
        : (b.started_at || '').localeCompare(a.started_at || ''));
    },
    get attentionCount() { return this.attention?.count || 0; },
    attentionFor(sessionId) { return (this.attention?.items || []).filter(i => i.href === '#/sessions/' + sessionId); },
    openSession(id) { this._listScroll = window.scrollY; this.go('#/sessions/' + encodeURIComponent(id)); },
    // Previous / next session in the current list order
    sessionNeighbours() {
      const ids = this.sessionsFiltered.map(x => x.session_id); const i = ids.indexOf(this.sessionId);
      return { prev: i > 0 ? ids[i - 1] : null, next: i >= 0 && i < ids.length - 1 ? ids[i + 1] : null, index: i, total: ids.length };
    },
    sectionToggle(k) { this.sect[k] = !this.sect[k]; },
    scrollToId(id) { document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }); },
    // Rename a design (gallery masters should have distinctive names; a shared name needs confirmation)
    async openRename() {
      this.rename = { open: true, title: this.sd?.session.title || '', busy: false, error: '', force: false, suggestions: [], lineage: null };
      try {                                                                   // local word rules on the prompt — no AI call
        const r = await this.api('GET', '/api/admin/designs/' + encodeURIComponent(this.sd.design_id || this.sd.session.design_id) + '/names');
        if (this.rename.open) { this.rename.suggestions = r.suggestions || []; this.rename.lineage = r.lineage; }
      } catch (e) { /* suggestions are a convenience only */ }
    },
    pickName(n) { this.rename.title = n; this.rename.force = false; this.rename.error = ''; },
    // A refinement that landed inside a master (before masters became untouchable) becomes a design of its own
    async splitBatch(b) {
      const ok = await this.ask({ title: `Move this refinement into a design of its own?`,
        text: `“${b.user_text}” and everything made from its images (movie, choices, bag lines, orders, 3D requests) move to a new design named in the lineage of ${this.sd.session.title}. ` +
              `${this.sd.session.ring_id} keeps its original images, its gallery image, its 3D model and its orders, and its selection goes back to the option that was refined. This cannot be undone.`,
        confirmLabel: 'Move into its own design', danger: true });
      if (!ok) return;
      try {
        const r = await this.api('POST', '/api/admin/batches/' + encodeURIComponent(b.id) + '/split', {});
        this.notify(`Refinement moved to ${r.title} (${r.ring_id})`);
        this.sessions = []; this.attention = null;
        this.go('#/sessions/' + encodeURIComponent(r.design_id));
      } catch (e) { this.fail(e); }
    },
    // A legacy gallery copy is the same ring as its master: fold it back (one Ring ID, one 3D model)
    async mergeCopy() {
      const m = this.sd?.merge; if (!m) return;
      const ring = this.sd.session.ring_id;
      const ok = await this.ask({ title: `Merge ${ring} into ${m.master_title} (${m.master_ring_id})?`,
        text: `“${this.sd.session.title}” is a copy of the gallery design made before shared designs existed — the same ring under a second Ring ID. ` +
              `Merging moves this journey (choices, bag lines, orders, 3D requests, history) to ${m.master_ring_id}, whose 3D model and STL then serve it. ` +
              `${ring} is retired and never reused. This cannot be undone.`,
        confirmLabel: 'Merge into master', danger: true });
      if (!ok) return;
      try {
        const r = await this.api('POST', '/api/admin/designs/' + encodeURIComponent(this.sd.design_id || this.sd.session.design_id) + '/merge', {});
        this.notify(`${r.copy.ring_id} merged into ${r.master.title} (${r.master.ring_id})` + (r.orders.length ? ' · orders ' + r.orders.join(', ') : ''));
        this.sessions = []; this.attention = null;
        this.go('#/sessions/' + encodeURIComponent(r.session_id));
      } catch (e) { this.fail(e); }
    },
    async saveRename() {
      this.rename.busy = true; this.rename.error = '';
      try {
        const r = await this.api('PATCH', '/api/admin/designs/' + encodeURIComponent(this.sd.design_id || this.sd.session.design_id), { title: this.rename.title, force: this.rename.force });
        this.rename.open = false; this.notify('Renamed to “' + r.title + '”');
        await this.loadSession();
      } catch (e) {
        this.rename.error = e.message;
        if (e.code === 'duplicate_title') this.rename.force = true;      // a second Save confirms
      } finally { this.rename.busy = false; }
    },
    async loadSession() {
      this.sdError = ''; this.g3.error = '';
      if (!this.sessions.length) this.loadSessions().catch(() => {});       // for Previous / Next when opened from a link
      try {
        const sd = await this.api('GET', '/api/admin/sessions/' + encodeURIComponent(this.sessionId));
        const keep = this.sd?.session.session_id === sd.session.session_id;    // keep the admin's choice on refresh
        const size = keep ? this.g3.size : sd.three_d_defaults.production_size;
        const material = keep ? this.g3.material : sd.three_d_defaults.material_id;
        this.g3.size = null; this.g3.material = '';
        this.sd = sd;
        this.showChoices = false;
        for (const t of sd.three_d) this.setLive(t.id, t.live);
        const latest = sd.three_d.find(t => this.previewReady(t));       // visual only: also for "needs review"
        const sid = sd.session.session_id;                               // still this session when the viewer starts
        if (latest && this.viewer3d.id !== latest.id) setTimeout(() => { if (this.sd?.session.session_id === sid) this.show3d(latest); }, 50);
        if (!latest) this.clear3d();
        await this.$nextTick();                 // the <option>s must exist before the selects get their value
        await new Promise(r => setTimeout(r));  // (a freshly created detail block renders its options a tick later)
        this.g3.size = size; this.g3.material = material;
        this.poll3d();
      } catch (e) { this.sd = null; this.sdError = e.message; }
    },
    // 3D sizes: a ring's US size, a charm's height in mm
    size3dLabel(size) { return this.isCharm(this.sd?.session) ? size + ' mm' : 'US ' + size; },
    default3dSize() { return this.isCharm(this.sd?.session) ? this.sd.catalog.charm_default_size : 10; },
    charm3dSizes() {
      const s = [...(this.sd?.catalog?.charm_sizes || [])];
      for (const v of [this.g3.size, this.sd?.three_d_defaults?.customer_size]) if (v != null && !s.includes(v)) s.push(v);   // e.g. a size no longer offered
      return s.sort((a, b) => a - b);
    },
    // '14 mm height — Classic · Recommended' (a size no longer offered: just its height)
    charmSizeText(z) {
      const o = (this.sd?.catalog?.charm_size_options || []).find(x => x.size === z);
      return z + ' mm height' + (o?.name ? ' — ' + o.name : '') + (o?.recommended ? ' · Recommended' : '');
    },
    async generate3d() {
      const custom = this.sd.three_d_defaults.customer_size ?? this.default3dSize();
      const existing = this.sd.three_d_defaults.existing_model;
      // With an existing model this only recalculates geometry for the size/material (no Hi3D call).
      if (!existing) {
        const ok = await this.ask({ title: 'Generate 3D with Hi3D v3.0?',
          text: `This is a ${this.mode === 'mock' ? 'mock (free, simulated)' : 'PAID live'} Hi3D call. Size ${this.size3dLabel(this.g3.size)}${this.g3.size !== custom ? ' (manual override)' : ''} · ${this.materialLabelFor(this.g3.material, this.sd.session.product_type)}.`,
          confirmLabel: this.mode === 'mock' ? 'Generate (mock)' : 'Generate — paid call', danger: this.mode !== 'mock' });
        if (!ok) return;
      }
      this.g3.busy = true; this.g3.error = '';
      try {
        await this.api('POST', '/api/admin/sessions/' + encodeURIComponent(this.sessionId) + '/3d',
          { production_size: this.g3.size, material_id: this.g3.material });
        await this.loadSession();
      } catch (e) { this.g3.error = e.message; } finally { this.g3.busy = false; }
    },
    // A second Hi3D model for a design that already has one: explicit warning + typed confirmation,
    // enforced again on the server (the request is refused without the exact phrase).
    openNewModel() {
      const opts = this.sd.three_d_defaults.options || [];
      const sel = opts.find(o => o.selected) || opts[0];
      this.newModel = { open: true, text: '', candidate: sel ? sel.id : '', error: '', busy: false };
    },
    async confirmNewModel() {
      if (this.newModel.text !== NEW_MODEL_PHRASE) return;
      this.newModel.busy = true; this.newModel.error = '';
      try {
        await this.api('POST', '/api/admin/sessions/' + encodeURIComponent(this.sessionId) + '/3d',
          { production_size: this.g3.size, material_id: this.g3.material, candidate_id: this.newModel.candidate, override: this.newModel.text });
        this.newModel = { open: false, text: '', candidate: '', error: '', busy: false };
        await this.loadSession();
      } catch (e) { this.newModel.error = e.message; this.newModel.busy = false; }
    },
    // Existing (master) model: the normal actions reuse it — view, recalculate, scaled STL — no Hi3D call.
    viewMasterModel() {
      const id = this.sd?.three_d_defaults.existing_model?.latest_3d_id;
      const t = this.sd.three_d.find(x => x.id === id && this.previewReady(x)) || this.sd.three_d.find(x => this.previewReady(x));
      if (!t) return;
      this.show3d(t);
      this.$refs.viewer?.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
    },
    async exportJourneyStl() {
      const id = this.sd?.three_d_defaults.existing_model?.journey_3d_id;
      const t = this.sd.three_d.find(x => x.id === id);
      if (t) await this.exportStl(t);
    },
    prodLabel(s) { return (PROD_STATE[s] || [s || '—'])[0]; },
    prodClass(s) { return (PROD_STATE[s] || [, 'bg-zinc-100 text-zinc-500'])[1]; },
    // Large hover preview next to a thumbnail, kept inside the window.
    // Lists and cards show a thumbnail (made on first request, cached); data-full keeps the original for the hover preview
    small(url, w = 320) { return url && /\/assets\//.test(url) ? url.replace(/\/assets\//, '/thumb/') + '?w=' + w : (url || ''); },
    showZoom(ev, src, label, el = null) {
      const r = (el || ev.currentTarget).getBoundingClientRect(), size = Math.min(420, window.innerWidth - 32, window.innerHeight - 80);
      let x = r.right + 12, y = r.top + r.height / 2 - size / 2;
      if (x + size > window.innerWidth - 16) x = Math.max(16, r.left - size - 12);
      y = Math.max(16, Math.min(y, window.innerHeight - size - 48));
      this.zoom = { src, label, x, y, size };
    },
    // Every image in the Admin enlarges on hover (thumbnails in tables, cards, option grids) — one delegated
    // handler, so new screens get it for free. Big images and the preview itself are left alone.
    installZoom() {
      document.addEventListener('mouseover', (e) => {
        const img = e.target.closest?.('img');
        if (!img || img.closest('#zoom-preview') || !img.src || img.getBoundingClientRect().width > 260) return;
        if (this.zoom && this.zoom.src === (img.dataset.full || img.src)) return;
        const row = img.closest('tr, li, article, .card, button');
        const first = row?.querySelector('.font-medium, .font-semibold, .font-mono');
        const label = (img.dataset.label || img.alt || first?.childNodes?.[0]?.textContent || first?.textContent || '').replace(/\s+/g, ' ').trim();
        this.showZoom(null, img.dataset.full || img.src, label, img);
      });
      document.addEventListener('mouseout', (e) => { if (e.target.closest?.('img') && !e.relatedTarget?.closest?.('img')) this.zoom = null; });
    },
    // ── live 3D status: real persisted stages, polled lightly; the clock ticks locally ──
    setLive(id, s) {
      if (!s) return;
      this.skew = new Date(s.server_now).getTime() - Date.now();
      this.live3d = { ...this.live3d, [id]: s };
    },
    busy3d(s) { return !!s && (!s.done || Object.keys(s.raw?.background || {}).length > 0); },
    poll3d() {
      clearTimeout(this._poll3d);
      if (!this._tick) this._tick = setInterval(() => { this.clock = Date.now(); }, 1000);
      const sid = this.sd?.session.session_id;
      const ids = (this.sd?.three_d || []).filter(t => this.busy3d(this.live3d[t.id])).map(t => t.id);
      if (!ids.length) return;
      this._poll3d = setTimeout(async () => {
        if (this.sessionId !== sid || this.tab !== 'sessions') return;
        let reload = false;
        for (const id of ids) {
          const was = this.live3d[id];
          try {
            const s = await this.api('GET', `/api/admin/3d/${encodeURIComponent(id)}/status`);
            this.setLive(id, s);
            if (s.done && !was?.done) reload = true;                      // numbers are ready: show them
            const t = this.sd?.three_d.find(x => x.id === id);
            if (t && s.raw?.preview_ready && !was?.raw?.preview_ready) this.show3d(t);
          } catch (e) { /* transient: keep polling */ }
        }
        if (reload) await this.loadSession(); else this.poll3d();
      }, 2000);
    },
    now() { return this.clock + this.skew; },
    clockText(ms) {
      const s = Math.max(0, Math.round(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
      return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
    },
    span(st) { return new Date(st.ended_at || this.now()).getTime() - new Date(st.started_at).getTime(); },
    stageLine(t) {
      const s = this.live3d[t.id]; if (!s || !s.stages.length) return '';
      const first = s.stages[0], last = s.stages[s.stages.length - 1];
      const total = this.clockText(new Date(last.ended_at || this.now()).getTime() - new Date(first.started_at).getTime());
      // Done ≠ ready: warnings keep the result at "production review required" (also when the
      // background edge check finds open edges after the numbers were shown).
      if (s.done) return ({ failed: 'Failed', cancelled: 'Cancelled', review_required: 'Processing complete — production review required' }[s.production_state] || 'Ready') + ' — Total ' + total;
      const open = [...s.stages].reverse().find(x => !x.ended_at) || last;
      const parts = [open.label];
      if (open.stage === 'queued' && s.queue) parts.push(s.queue.ahead + ' ahead');
      if (open.stage === 'waiting_hi3d' && open.detail?.position != null) parts.push('position ' + open.detail.position);
      if (open.stage === 'downloading_stl' && open.detail?.total) parts.push(Math.floor(100 * open.detail.bytes / open.detail.total) + '%');
      parts.push(this.clockText(this.span(open)));
      return parts.join(' — ');
    },
    stageSteps(t) {
      const s = this.live3d[t.id];
      return (s?.stages || []).filter(x => STAGE_SHORT[x.stage]).map(x => ({ label: STAGE_SHORT[x.stage], time: this.clockText(this.span(x)), open: !x.ended_at }));
    },
    background(t) {
      const r = this.live3d[t.id]?.raw; if (!r || !t.geometry?.production) return '';
      const bg = r.background || {};
      const pv = r.preview_ready ? 'ready' : bg.preview ? (bg.preview === 'running' ? 'building…' : 'queued') : '—';
      const ig = { closed: 'closed (edge check passed)', open: 'open edges found — check the model', unknown: 'could not run', pending: bg.integrity ? 'running in background…' : 'pending' }[r.integrity] || r.integrity;
      return `Light preview: ${pv} · Mesh integrity: ${ig}`;
    },
    previewReady(t) { return !!this.live3d[t.id]?.raw?.preview_ready || t.scaled_stl === 'stored'; },
    thumb() {
      const t = this.sd?.three_d.find(x => this.live3d[x.id]?.thumbnail_url);
      return t ? this.live3d[t.id].thumbnail_url : '';
    },
    async cancel3d(t) {
      try { this.setLive(t.id, (await this.api('POST', `/api/admin/3d/${encodeURIComponent(t.id)}/cancel`)).live); await this.loadSession(); }
      catch (e) { this.fail(e); }
    },
    async retry3d(t) {
      const local = this.live3d[t.id]?.retry_is_local;
      if (!local && !await this.ask({ title: 'Retry Hi3D?', text: `Hi3D itself failed, so a retry is a new ${this.mode === 'mock' ? 'mock' : 'PAID live'} Hi3D request.`, confirmLabel: 'Retry — new request', danger: this.mode !== 'mock' })) return;
      try { await this.api('POST', `/api/admin/3d/${encodeURIComponent(t.id)}/retry`); await this.loadSession(); }
      catch (e) { this.fail(e); }
    },
    // Scaled STL: export on demand to a temporary file (queued like any heavy job), then a native download.
    async exportStl(t, orderRef = null) {
      if (t.scaled_stl === 'stored') return this.downloadStl(t.id, 'production');      // earlier requests kept one
      const tail = orderRef ? '?order=' + encodeURIComponent(orderRef) : '';          // ORD-… in the file name
      try {
        let e = await this.api('POST', `/api/admin/3d/${encodeURIComponent(t.id)}/export`);
        this.exports3d = { ...this.exports3d, [t.id]: e };
        if (!this._tick) this._tick = setInterval(() => { this.clock = Date.now(); }, 1000);
        while (['queued', 'running'].includes(e.status)) {
          await new Promise(r => setTimeout(r, 1000));
          e = await this.api('GET', `/api/admin/3d/${encodeURIComponent(t.id)}/export/${e.job_id}${tail}`);
          this.exports3d = { ...this.exports3d, [t.id]: e };
        }
        if (e.status === 'done') {
          e = await this.api('GET', `/api/admin/3d/${encodeURIComponent(t.id)}/export/${e.job_id}${tail}`);   // fresh signed link
          const a = Object.assign(document.createElement('a'), { href: e.url });
          document.body.appendChild(a); a.click(); a.remove();
          this.downloadNote = `Downloading the scaled STL (${this.gb(e.bytes)}) — see your browser's downloads. The temporary file is deleted after an hour.`;
          setTimeout(() => { this.downloadNote = ''; }, 8000);
        } else this.notify('Scaled STL failed: ' + (e.error || e.status), 'error');
      } catch (err) { this.notify('Scaled STL failed: ' + err.message, 'error'); }
      finally { const x = { ...this.exports3d }; delete x[t.id]; this.exports3d = x; }
    },
    exportLine(t) {
      const e = this.exports3d[t.id]; if (!e) return '';
      if (e.status === 'queued') return `Preparing scaled STL — Queued — ${e.ahead} ahead — ${this.clockText(this.now() - new Date(e.created_at).getTime())}`;
      return `Preparing scaled STL — ${this.clockText(this.now() - new Date(e.started_at || e.created_at).getTime())}`;
    },
    gb(b) { return b == null ? '—' : b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(0) + ' MB' : Math.ceil(b / 1e3) + ' KB'; },

    // Native browser download (streams to disk with the browser's progress bar) via a signed link,
    // so a 250 MB STL is never loaded into the page.
    async downloadStl(id, stage) {
      try {
        const r = await this.api('POST', `/api/admin/3d/${encodeURIComponent(id)}/download-link`, { stage });
        const a = Object.assign(document.createElement('a'), { href: r.url });
        document.body.appendChild(a); a.click(); a.remove();
        this.downloadNote = `Downloading ${(r.bytes / 1e6).toFixed(0)} MB — see your browser's downloads.`;
        setTimeout(() => { this.downloadNote = ''; }, 6000);
      } catch (e) { this.notify('Download failed: ' + e.message, 'error'); }
    },

    // ── detail ─────────────────────────────────────────────────────────
    async loadDetail() {
      this.detailError = '';
      try { this.d = await this.api('GET', '/api/admin/users/' + encodeURIComponent(this.userId)); }
      catch (e) { this.d = null; this.detailError = e.message; }
    },
    statCards() {
      const t = this.d.totals;
      const jobs = (j) => `${j.by_status.ready || 0} ready · ${(j.by_status.failed || 0) + (j.by_status.interrupted || 0)} failed`;
      const prov = (j) => Object.entries(j.by_provider).map(([k, v]) => `${v} ${k === 'fal' ? 'live' : k.replace('_', ' ')}`).join(' · ');
      return [
        { label: 'Image generations', value: t.images.total, sub: jobs(t.images) },
        { label: 'Refinements', value: t.refinements, sub: `${t.refinement_images.total} images · ${jobs(t.refinement_images)}` },
        { label: '360° movies', value: t.movies.total, sub: jobs(t.movies) },
        { label: '3D generations', value: t.meshes.total, sub: t.meshes.total ? jobs(t.meshes) : 'Developer tool only' },
        { label: 'Designs created', value: t.designs, sub: 'Every design is saved automatically' },
        { label: 'Added to bag', value: t.bag_lines, sub: `${t.bag_units} ring${t.bag_units === 1 ? '' : 's'}` },
        { label: 'AI requests by provider', value: t.images.total + t.refinement_images.total + t.movies.total + t.meshes.total,
          sub: prov({ by_provider: this.sumProviders(t) }) || '—' },
      ];
    },
    sumProviders(t) {
      const out = {};
      for (const j of [t.images, t.refinement_images, t.movies, t.meshes])
        for (const [k, v] of Object.entries(j.by_provider)) out[k] = (out[k] || 0) + v;
      return out;
    },
    barClass() {
      const r = this.d.totals.generations_used / Math.max(1, this.d.totals.generations_max);
      return r >= 1 ? 'bg-red-500' : r >= 0.75 ? 'bg-amber-500' : 'bg-emerald-500';
    },
    chartDays() {
      const byDay = Object.fromEntries((this.d?.daily || []).map(x => [x.day, x]));
      const out = [];
      const today = new Date();
      for (let i = 89; i >= 0; i--) {
        const dt = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - i));
        const day = dt.toISOString().slice(0, 10);
        out.push(byDay[day] || { day });
      }
      return out;
    },
    chartMax() {
      return Math.max(1, ...this.chartDays().map(x => this.chartKinds.reduce((s, k) => s + (x[k.key] || 0), 0)));
    },
    dayTitle(x) {
      const parts = this.chartKinds.filter(k => x[k.key]).map(k => `${x[k.key]} ${k.label.toLowerCase()}`);
      if (x.designs) parts.push(`${x.designs} design${x.designs === 1 ? '' : 's'}`);
      if (x.sign_ins) parts.push(`${x.sign_ins} sign-in${x.sign_ins === 1 ? '' : 's'}`);
      return x.day + (parts.length ? ': ' + parts.join(', ') : ': no activity');
    },

    // ── AI prompts & params ────────────────────────────────────────────
    async loadModels(id) {
      this.prices = await this.api('GET', '/api/admin/ai-prices').catch(() => null);
      this.mprices = await this.api('GET', '/api/admin/material-prices').catch(() => null);
      const r = await this.api('GET', '/api/admin/models');
      this.models = r.models; this.runtimePlaceholders = r.runtime_placeholders;
      const want = id || this.mid || (this.models.find(m => (m.model.product || 'ring') === this.mProduct) || this.models[0]).model.id;
      if (want !== this.mid || !this.mc || !this.dirty) await this.selectModel(want);
    },
    modelHash(id) { return '#/settings/models/' + id; },
    // Ring | Charm: the configuration being edited. Each product has its own models, versions and history.
    get modelsShown() { return this.models.filter(m => (m.model.product || 'ring') === this.mProduct); },
    chooseConfigProduct(p) {
      if (p === this.mProduct) return;
      const first = this.models.find(m => (m.model.product || 'ring') === p);
      if (first) this.go(this.modelHash(first.model.id));       // the unsaved-changes guard in route() still applies
    },
    async selectModel(id) {
      const mc = await this.api('GET', '/api/admin/models/' + encodeURIComponent(id));
      // The parameter rows are fixed slots that read the current model (web/admin.html), so the model and its draft
      // (exactly its saved settings) change together, in one step.
      this.draft = this.draftFrom(mc.active.params, mc);
      this.mc = mc;
      this.mProduct = mc.model.product || 'ring';
      this.mid = id; this.note = ''; this.mProblems = []; this.mMessage = ''; this.preview = null;
      this.dirty = false;
    },
    // The parameter editor's slots: as many as the largest model has parameters (they persist across models)
    get paramSlots() { return this.models.reduce((n, m) => Math.max(n, m.model.params.length), 0); },
    emptyParam: { name: '', kind: '', description: '', default: null, enum: [], allowed: [], allowed_reason: null, min: null, max: null,
                  required: false, placeholders: [], required_placeholders: [], max_length: null, internal: false, group: null },
    draftFrom(params, mc = this.mc) {
      const d = {};
      for (const p of mc.model.params) {
        const has = Object.prototype.hasOwnProperty.call(params, p.name);
        let v = has ? params[p.name] : p.default;
        if (p.kind === 'keyframes') v = JSON.parse(JSON.stringify(has ? v : this.orbit()));
        if (v === null || v === undefined) v = p.kind === 'bool' ? false : p.kind === 'enum' ? p.enum[0] : (p.kind === 'text' || p.kind === 'template') ? '' : null;
        d[p.name] = { set: has || p.required, value: v };
      }
      return d;
    },
    draftParams() {
      const out = {};
      for (const p of this.mc.model.params) if (this.draft[p.name]?.set) out[p.name] = this.draft[p.name].value;
      return out;
    },
    setParam(p, on) { this.draft[p.name].set = on; this.dirty = true; },
    resetDraft() { this.draft = this.draftFrom(this.mc.active.params); this.dirty = false; this.mProblems = []; },
    loadVersion(v) {
      this.draft = this.draftFrom(v.params); this.dirty = !v.active; this.mProblems = [];
      this.mMessage = v.active ? '' : `Loaded v${v.number} into the form — not active until you Save & Activate (or use Restore).`;
      window.scrollTo({ top: 0, behavior: 'smooth' });
    },
    placeholdersFor(mc) { return [...new Set(mc.model.params.flatMap(p => p.placeholders))]; },
    rangeText(p) {
      if (p.internal) return '';                                   // pipeline-only text: no provider default to show
      const parts = [];
      if (p.kind === 'enum') parts.push('Options: ' + p.allowed.join(', '));
      if ((p.kind === 'int' || p.kind === 'number') && (p.min != null || p.max != null))
        parts.push('Range: ' + (p.min ?? '−∞') + ' – ' + (p.max ?? '∞'));
      if (p.kind === 'keyframes') parts.push('2–12 keyframes');
      if (p.max_length) parts.push('max ' + p.max_length.toLocaleString() + ' characters');
      if (p.kind !== 'keyframes' && p.kind !== 'template') parts.push('Provider default: ' + this.defaultText(p));
      if (p.allowed_reason) parts.push(p.allowed_reason);
      return parts.join(' · ');
    },
    defaultText(p) {
      if (p.default === null || p.default === undefined) return p.kind === 'keyframes' ? 'provider-defined' : 'none (provider decides)';
      if (p.default === '') return 'empty';
      const s = typeof p.default === 'string' ? p.default : JSON.stringify(p.default);
      return s.length > 90 ? '“' + s.slice(0, 90) + '…”' : s;
    },
    orbit() { return [0, 0.25, 0.5, 0.75, 1].map((t, i) => ({ time: t, azimuth: i * 90, elevation: 10, distance: 1 })); },
    orbitPreset(name) { this.draft[name].value = this.orbit(); this.dirty = true; },
    addKey(name) {
      const k = this.draft[name].value, last = k[k.length - 1] || { time: 0, azimuth: 0, elevation: 10, distance: 1 };
      k.push({ time: Math.min(1, +(last.time + 0.1).toFixed(3)), azimuth: last.azimuth + 45, elevation: last.elevation, distance: last.distance });
      this.dirty = true;
    },
    removeKey(name, i) { this.draft[name].value.splice(i, 1); this.dirty = true; },
    moveKey(name, i, d) { const k = this.draft[name].value; [k[i], k[i + d]] = [k[i + d], k[i]]; this.dirty = true; },
    azimuthTravel(k) { return (k || []).slice(1).reduce((s, x, i) => s + Math.abs((x?.azimuth || 0) - (k[i]?.azimuth || 0)), 0); },
    async validateModel() {
      this.mMessage = ''; this.mProblems = [];
      const r = await this.api('POST', `/api/admin/models/${this.mid}/validate`, { params: this.draftParams() });
      if (r.ok) this.mMessage = 'Valid — ready to activate.'; else this.mProblems = r.problems;
    },
    async previewModel() {
      this.mProblems = [];
      const r = await fetch(BASE + `/api/admin/models/${this.mid}/preview`, { method: 'POST',
        headers: { ...this.authHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify({ params: this.draftParams() }) });
      const data = await r.json();
      if (!r.ok) { this.mProblems = data?.error?.problems || [data?.error?.message || 'Preview failed']; this.preview = null; return; }
      this.preview = data;
    },
    async activateModel() {
      const live = this.mc.model.connected ? 'Every new pipeline request will use it immediately.' : 'This model is not used by the P3 pipeline yet.';
      if (!await this.ask({ title: `Save & activate a new version of ${this.mc.model.label}?`, text: live + ' Requests already created keep their settings.', confirmLabel: 'Save & activate' })) return;
      this.mBusy = true; this.mProblems = []; this.mMessage = '';
      try {
        const r = await fetch(BASE + `/api/admin/models/${this.mid}/activate`, { method: 'POST',
          headers: { ...this.authHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify({ params: this.draftParams(), note: this.note }) });
        const data = await r.json();
        if (!r.ok) { this.mProblems = data?.error?.problems || [data?.error?.message || 'Activation failed']; return; }
        this.dirty = false;
        await this.loadModels(this.mid);
        await this.selectModel(this.mid);
        this.mMessage = data.changed ? `Saved and activated v${data.active.number}.` : 'No changes — the active version already has these settings.';
      } finally { this.mBusy = false; }
    },
    async restoreVersion(v) {
      if (!await this.ask({ title: `Restore v${v.number}?`, text: 'It becomes a new active version; the current one stays in the history.', confirmLabel: 'Restore & activate' })) return;
      const r = await fetch(BASE + `/api/admin/models/${this.mid}/restore`, { method: 'POST',
        headers: { ...this.authHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify({ version_id: v.id }) });
      const data = await r.json();
      if (!r.ok) { this.mProblems = data?.error?.problems || ['Restore failed']; return; }
      this.dirty = false;
      await this.loadModels(this.mid); await this.selectModel(this.mid);
      this.mMessage = data.changed ? `Restored v${v.number} as v${data.active.number} (active).` : 'Already active.';
    },
    pricesDoc() {
      const { version, updated_at, updated_by, update_note, fal_key_configured, ...doc } = this.prices || {};
      return doc;
    },
    priceRate(p) {
      if (p.per_image != null) return '$' + p.per_image + ' / image';
      if (p.per_second) return Object.entries(p.per_second).map(([r, v]) => r + ' $' + v + '/s').join(' · ');
      if (p.per_credit != null) return '$' + p.per_credit + ' / credit · ' + Object.entries(p.credits?.geometry || {}).map(([r, v]) => r + ' ' + v).join(', ') + ' cr (+texture ' + (p.credits?.texture ?? 0) + ', PBR ' + (p.credits?.pbr ?? 0) + ')';
      if (p.per_request != null) return '$' + p.per_request + ' / request';
      return '—';
    },
    async refreshPrices() {
      this.pricesBusy = true; this.pricesMsg = ''; this.pricesErr = false;
      try {
        const r = await this.api('POST', '/api/admin/ai-prices/refresh');
        this.prices = await this.api('GET', '/api/admin/ai-prices');
        this.pricesMsg = r.changes?.length ? 'Updated: ' + r.changes.join('; ') : 'Prices are up to date (no changes from fal.ai).';
      } catch (e) { this.pricesErr = true; this.pricesMsg = e.message; } finally { this.pricesBusy = false; }
    },
    async savePrices() {
      this.pricesBusy = true; this.pricesMsg = ''; this.pricesErr = false;
      try {
        const doc = JSON.parse(this.pricesEdit);
        await this.api('PUT', '/api/admin/ai-prices', { prices: doc, note: 'Edited in Admin' });
        this.prices = await this.api('GET', '/api/admin/ai-prices'); this.pricesEdit = null; this.pricesMsg = 'Prices saved.';
      } catch (e) { this.pricesErr = true; this.pricesMsg = e instanceof SyntaxError ? 'Invalid JSON: ' + e.message : e.message; }
      finally { this.pricesBusy = false; }
    },
    // ── Inspiration Gallery: the XJet designs shown on the customer site ──
    async loadGallery() {
      this.galleryMsg = ''; this.galleryState = 'loading';
      try { this.galleryItems = (await this.api('GET', '/api/admin/gallery')).items; this.galleryState = 'loaded'; }
      catch (e) { this.galleryMsg = e.message; this.galleryState = 'failed'; }
    },
    gallerySorted() {
      const k = this.gallerySort, items = this.galleryItems.filter(g => !this.gProduct || (g.product_type || 'ring') === this.gProduct);
      if (k === 'position') return items.sort((a, b) => (a.in_gallery ? a.position : 1e9) - (b.in_gallery ? b.position : 1e9));
      if (k === 'last_used_at') return items.sort((a, b) => (b.last_used_at || '').localeCompare(a.last_used_at || ''));
      return items.sort((a, b) => (b[k] || 0) - (a[k] || 0) || (a.in_gallery ? a.position : 1e9) - (b.in_gallery ? b.position : 1e9));
    },
    galleryTop(k) { const best = [...this.galleryItems].sort((a, b) => (b[k] || 0) - (a[k] || 0))[0]; return best && best[k] ? best : null; },
    async galleryToggle(g) {
      if (this.galleryOpen === g.design_id) { this.galleryOpen = null; return; }
      this.galleryOpen = g.design_id;
      try { const r = await this.api('GET', `/api/admin/gallery/usage/${encodeURIComponent(g.design_id)}`); this.galleryUsage = { ...this.galleryUsage, [g.design_id]: r.uses }; }
      catch (e) { this.galleryMsg = e.message; }
    },
    // The customer share link by design name (/design/aurora-twist) — the same link the Share button gives customers
    async galleryLink(g) {
      let url;
      try { url = (await this.api('GET', `/api/gallery/${encodeURIComponent(g.id)}/share`)).url; }
      catch (e) { this.galleryMsg = e.message; return; }
      if (await copyText(url)) { this.galleryLinkCopied = g.id; setTimeout(() => { this.galleryLinkCopied = ''; }, 2000); this.notify('Share link copied — ' + url); }
      else await this.ask({ title: 'Share link', text: 'Copy this link:', copy: url, confirmLabel: 'Done', cancelLabel: '' });
    },
    // An image may have several movies (made under different movie configurations). The one the customer sees in
    // Customize and this page shows is the Admin's choice (c.movie_id), else the newest ready one.
    shownMovie(c) {
      const ready = (c.movies || []).filter(m => m.status === 'ready');
      const chosen = ready.find(m => m.id === c.movie_id);
      return chosen ? chosen.id : (ready.length ? ready.reduce((a, b) => (b.created_at > a.created_at ? b : a)).id : null);
    },
    async useMovie(c, m) {
      try {
        await this.api('POST', `/api/admin/candidates/${encodeURIComponent(c.id)}/movie`, { movie_id: m.id });
        await this.loadSession();
        this.notify('This movie is now the one shown for ' + (c.ring_id || 'this image'));
      } catch (e) { this.fail(e); }
    },
    // A new movie with the movie configuration active now (the current one stays shown until the new one is ready)
    async newMovie(c) {
      const ok = await this.ask({ title: `Make a new 360° movie for ${c.ring_id || 'this image'}?`,
        text: `This is a ${this.mode === 'mock' ? 'mock (free, simulated)' : 'PAID live'} MiniMax call with the movie settings active now. ` +
              'The movie shown today stays until the new one is ready, which then becomes the one shown. The customer’s allowance is not used.',
        confirmLabel: this.mode === 'mock' ? 'Make it (mock)' : 'Make it — paid call', danger: this.mode !== 'mock' });
      if (!ok) return;
      try {
        await this.api('POST', `/api/admin/candidates/${encodeURIComponent(c.id)}/movies`);
        await this.loadSession();
        this.notify('A new movie is being made for ' + (c.ring_id || 'this image') + ' — refresh in a minute');
      } catch (e) { this.fail(e); }
    },
    // A ring whose bore is not round: scale it along the bore's two axes so the bore is a circle of the target size,
    // then measure again — local work on the existing Hi3D model, no Hi3D call
    async fixBore(t) {
      const r = (t.review || []).find(x => x.code === 'bore_not_round'), exp = t.bore_expected_after;
      // What the correction will leave (known from the measurement): a true ellipse comes out round, a lobed wall does not
      const outlook = exp == null ? '' : (exp > 0.04
        ? ` This inner wall is not an ellipse: about ${(exp * 100).toFixed(1)}% deviation would remain, so the corrected result stays for review (with undo).`
        : ` Expected: round within about ${(exp * 100).toFixed(1)}%.`);
      const ok = await this.ask({ title: `Make the bore of ${t.ring_id || 'this model'} round at ${t.size_label || 'US ' + t.production_size}?`,
        text: `${r ? r.text + ' ' : ''}The model will be scaled along the bore’s two axes so the bore becomes a circle of the size’s inner diameter; ` +
              'the outer shape stretches by the same few percent. This is local work on the existing model — no Hi3D call — and it replaces this result’s numbers and STL.' + outlook,
        confirmLabel: 'Make it round' });
      if (!ok) return;
      try { await this.api('POST', `/api/admin/3d/${encodeURIComponent(t.id)}/fix-bore`, {}); await this.loadSession(); this.notify('Making the bore round — the numbers follow in a moment'); }
      catch (e) { this.fail(e); }
    },
    // Undo a bore made round: the result goes back to the uniform scaling of the model as generated (local; the bore is flagged again)
    async undoBore(t) {
      const ok = await this.ask({ title: `Undo making the bore of ${t.ring_id || 'this model'} round?`,
        text: 'The result goes back to the uniform scaling of the model as generated, and the bore is flagged as not round again. Local work — no Hi3D call.',
        confirmLabel: 'Undo' });
      if (!ok) return;
      try { await this.api('POST', `/api/admin/3d/${encodeURIComponent(t.id)}/retry`); await this.loadSession(); this.notify('Back to the model as generated'); }
      catch (e) { this.fail(e); }
    },
    // The Admin's decision on a flagged 3D result: produce it as measured
    async accept3d(t) {
      const reasons = (t.review || []).map(r => r.text).join(' ') || t.error || '';
      const ok = await this.ask({ title: `Accept ${t.ring_id || 'this result'} for production as measured?`,
        text: `${reasons} The numbers (${t.size_label || 'US ' + t.production_size} · ${t.material_label}) stand as they are; the reasons stay on the record with your name and the time.`,
        confirmLabel: 'Accept — produce as measured' });
      if (!ok) return;
      try { await this.api('POST', `/api/admin/3d/${encodeURIComponent(t.id)}/accept`, {}); await this.loadSession(); this.notify('Accepted for production'); }
      catch (e) { this.fail(e); }
    },
    async galleryAdd(candidateId) {
      try {
        await this.api('POST', '/api/admin/gallery', { design_id: this.sessionId, candidate_id: candidateId });
        await this.loadSession();
      } catch (e) { this.notify('Could not add this design to the gallery: ' + e.message, 'error'); }
    },
    async galleryRemove(g) {
      if (!await this.ask({ title: `Remove “${g.title}” (${g.ring_id}) from the gallery?`, text: 'Customers who already started from it keep their designs.', confirmLabel: 'Remove from gallery', danger: true })) return;
      try {
        this.galleryItems = (await this.api('DELETE', `/api/admin/gallery/${encodeURIComponent(g.id)}`)).items;
        if (this.tab === 'sessions' && this.sessionId) await this.loadSession();
        this.notify('Removed from the gallery');
      } catch (e) { this.fail(e); }
    },
    async galleryMove(id, direction) {
      try { this.galleryItems = (await this.api('POST', `/api/admin/gallery/${encodeURIComponent(id)}/move`, { direction })).items; }
      catch (e) { this.galleryMsg = e.message; }
    },

    // ── material pricing: density · price $/g · cost $/g · website fixed price ──
    editMaterialPrices() {
      this.mpMsg = ''; this.mpNote = '';
      this.mpEdit = this.mprices.rows.map(r => ({ id: r.id, label: r.label, group: r.group,
        density_g_cm3: r.density_g_cm3 ?? '', price_per_g: r.price_per_g ?? '', cost_per_g: r.cost_per_g ?? '', fixed_price: r.fixed_price ?? '' }));
    },
    async saveMaterialPrices() {
      this.mpBusy = true; this.mpMsg = ''; this.mpErr = false;
      try {
        const materials = Object.fromEntries(this.mpEdit.map(r => [r.id,
          Object.fromEntries(['density_g_cm3', 'price_per_g', 'cost_per_g', 'fixed_price'].map(k => [k, r[k] === '' ? null : Number(r[k])]))]));
        this.mprices = await this.api('PUT', '/api/admin/material-prices', { materials, note: this.mpNote || 'Edited in Admin' });
        this.mpEdit = null; this.mpMsg = 'Saved as ' + this.mprices.version + '. New 3D calculations and the website use it now.';
      } catch (e) { this.mpErr = true; this.mpMsg = e.message; } finally { this.mpBusy = false; }
    },
    // ── charm pricing: a fixed price per material and size, price and cost per gram (never a ring price) ──
    async loadCharmPrices() { this.cprices = await this.api('GET', '/api/admin/charm-prices').catch(() => null); },
    // The size columns: the sizes on offer, then any older size that still has a price
    charmSizeCols() { const c = this.cprices; return c ? [...c.sizes, ...(c.other_priced_sizes || [])] : []; },
    sizeKey(s) { return String(Number(s)); },          // "20", "22.5": the server's key for a size
    editCharmPrices() {
      this.cpMsg = ''; this.cpNote = '';
      this.cpEdit = this.cprices.rows.map(r => ({ id: r.id, label: r.label, fixed_price_allowed: r.fixed_price_allowed,
        price_per_g: r.price_per_g ?? '', cost_per_g: r.cost_per_g ?? '',
        fixed: Object.fromEntries(this.charmSizeCols().map(s => [this.sizeKey(s), r.fixed_prices?.[this.sizeKey(s)] ?? ''])) }));
    },
    async saveCharmPrices() {
      this.cpBusy = true; this.cpMsg = ''; this.cpErr = false;
      try {
        const n = v => (v === '' || v == null) ? null : Number(v);
        const materials = Object.fromEntries(this.cpEdit.map(r => [r.id, { price_per_g: n(r.price_per_g), cost_per_g: n(r.cost_per_g),
          fixed_prices: r.fixed_price_allowed ? Object.fromEntries(Object.entries(r.fixed).filter(([, v]) => n(v) != null).map(([k, v]) => [k, n(v)])) : {} }]));
        this.cprices = await this.api('PUT', '/api/admin/charm-prices', { materials, note: this.cpNote || 'Edited in Admin' });
        this.cpEdit = null; this.cpMsg = 'Saved as ' + this.cprices.version + '. Charm quotes use it now; ring prices are unchanged.';
      } catch (e) { this.cpErr = true; this.cpMsg = e.message; } finally { this.cpBusy = false; }
    },
    // ── Settings → Products: the charm sizes on offer, their names and the recommended size ──
    editCharmSizes() {
      this.sizesMsg = ''; this.sizesErr = false; this.sizesNote = ''; this.sizeAdd = ''; this.sizeAddName = '';
      this.sizesEdit = [...(this.products?.charm_sizes || [])];
      this.sizeNames = { ...(this.products?.charm_size_names || {}) };
      this.sizeDefault = this.products?.charm_default_size ?? null;
    },
    addCharmSize() {
      const v = Number(String(this.sizeAdd).replace(',', '.'));
      if (this.sizeAdd === '' || !isFinite(v)) return;
      if (!this.sizesEdit.includes(v)) this.sizesEdit = [...this.sizesEdit, v].sort((a, b) => a - b);
      if (this.sizeAddName.trim()) this.sizeNames[this.sizeKey(v)] = this.sizeAddName.trim();
      this.sizeAdd = ''; this.sizeAddName = '';
    },
    removeCharmSize(v) {
      this.sizesEdit = this.sizesEdit.filter(x => x !== v);
      delete this.sizeNames[this.sizeKey(v)];
      if (this.sizeDefault === v) this.sizeDefault = null;        // the server falls back to the middle size
    },
    charmSizeName(s) { return (this.cprices?.size_names || this.products?.charm_size_names || {})[this.sizeKey(s)] || ''; },
    async saveCharmSizes() {
      this.sizesBusy = true; this.sizesMsg = ''; this.sizesErr = false;
      try {
        const names = Object.fromEntries(Object.entries(this.sizeNames).filter(([, n]) => n && n.trim()));
        const r = await this.api('PUT', '/api/admin/products/charm-sizes', { sizes: this.sizesEdit, names, default: this.sizeDefault, note: this.sizesNote });
        this.products = r; this.sizesEdit = null; this.cprices = null;          // the charm price columns follow the sizes
        this.sizesMsg = 'Charm sizes saved.' + (r.removed_in_bags?.length ? ' Still in customers\u2019 bags: ' +
          r.removed_in_bags.map(x => x.size + ' mm (' + x.bag_lines + (x.bag_lines === 1 ? ' line' : ' lines') + ')').join(', ') +
          ' \u2014 those lines will ask for another size.' : '');
      } catch (e) { this.sizesErr = true; this.sizesMsg = e.message; } finally { this.sizesBusy = false; }
    },
    num(v, d = 2) { return v == null ? '—' : Number(v).toFixed(d).replace(/\.?0+$/, ''); },
    async exportModels(model, fmt) {
      const product = model === 'all' ? this.mProduct : '';
      const r = await fetch(BASE + `/api/admin/models/export?model=${encodeURIComponent(model)}&format=${fmt}` + (product ? `&product=${product}` : ''), { headers: this.authHeaders() });
      if (!r.ok) { this.notify('Export failed (' + r.status + ')', 'error'); return; }
      const url = URL.createObjectURL(await r.blob());
      const a = Object.assign(document.createElement('a'), { href: url, download: `p3-ai-config-${model}${product && product !== 'ring' ? '-' + product : ''}.${fmt}` });
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    },

    // ── 3D viewer (three.js; the STL is fetched with the admin key) ─────
    measured3d() { return !!this.sd?.three_d.some(t => this.previewReady(t)); },
    // One WebGL renderer for the whole page, kept outside Alpine's reactive state and reused for every
    // model (browsers allow only ~16 contexts; making a new one per view used them all up).
    clear3d() {
      if (GL.raf) cancelAnimationFrame(GL.raf);
      GL.raf = 0;
      if (GL.mesh) { GL.scene.remove(GL.mesh); GL.mesh.geometry.dispose(); GL.mesh.material.dispose(); GL.mesh = null; }
      if (GL.controls) { GL.controls.dispose(); GL.controls = null; }
      if (GL.io) { GL.io.disconnect(); GL.io = null; }
      if (GL.soft) { GL.soft.dispose(); GL.soft = null; }
      // The shared canvas keeps showing its last frame: take it off the page (show3d puts it back once the next model
      // is drawn), so a session without a 3D model never shows the previous session's model.
      if (GL.renderer?.domElement.parentNode) GL.renderer.domElement.remove();
      this.viewer3d = { id: null, label: '', loading: false, error: '', note: '' };
    },
    glRenderer() {
      if (GL.renderer) return GL.renderer;
      if (GL.failed) return null;
      try {
        GL.renderer = new THREE.WebGLRenderer({ antialias: true });
        GL.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
        GL.renderer.domElement.addEventListener('webglcontextlost', (e) => { e.preventDefault(); });
        GL.scene = new THREE.Scene(); GL.scene.background = new THREE.Color(0xfafafa);
        GL.scene.add(new THREE.HemisphereLight(0xffffff, 0x777777, 1.1));
        GL.light = new THREE.DirectionalLight(0xffffff, 0.9); GL.scene.add(GL.light);
        GL.camera = new THREE.PerspectiveCamera(35, 1, 0.01, 1000);
        return GL.renderer;
      } catch (e) { GL.failed = true; GL.renderer = null; return null; }
    },
    async show3d(t) {
      if (!window.THREE || !THREE.STLLoader || !THREE.OrbitControls) { this.viewer3d.error = '3D viewer library not loaded'; return; }
      this.clear3d();
      const renderer = this.glRenderer();                // null when the browser cannot start WebGL
      this.viewer3d = { id: t.id, label: (t.ring_id ? t.ring_id + ' · ' : '') + (t.size_label || 'US ' + t.production_size) + ' · ' + t.material_label, loading: true, error: '',
                        note: renderer ? '' : 'Basic 3D view (WebGL is off in this browser) · drag to rotate' };
      try {
        const r = await fetch(BASE + `/api/admin/3d/${encodeURIComponent(t.id)}/stl/preview`, { headers: this.authHeaders() });   // light, visual only
        if (!r.ok) throw new Error('Preview download failed (' + r.status + ')');
        const buf = await r.arrayBuffer();
        if (this.viewer3d.id !== t.id) return;        // another model was opened meanwhile
        const geo = this.parsePreview(buf) || new THREE.STLLoader().parse(buf);
        geo.computeVertexNormals(); geo.center(); geo.computeBoundingSphere();
        const el = this.$refs.viewer; if (!el) return;
        if (!renderer) {                                // no WebGL: draw it with the 2D canvas instead
          GL.soft = SoftViewer(el, geo, this.swatch(t.material_id));
          this.viewer3d.loading = false;
          return;
        }
        const w = el.clientWidth || 300, h = el.clientHeight || 300, R = geo.boundingSphere.radius || 10;
        renderer.setSize(w, h);
        if (renderer.domElement.parentNode !== el) { el.innerHTML = ''; el.appendChild(renderer.domElement); }
        const camera = GL.camera;
        camera.aspect = w / h; camera.near = R / 100; camera.far = R * 100; camera.updateProjectionMatrix();
        camera.position.set(R * 0.8, R * 1.2, R * 4.2);          // ring axis is Z: look at the ring's face, slightly from above
        GL.light.position.set(R, R * 2, R * 3);
        GL.mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: new THREE.Color(this.swatch(t.material_id)), metalness: 0.6, roughness: 0.35 }));
        GL.scene.add(GL.mesh);
        const controls = GL.controls = new THREE.OrbitControls(camera, renderer.domElement);
        controls.enableDamping = true; controls.autoRotate = true; controls.autoRotateSpeed = 1.5;
        // Draw only while the viewer is on screen and the tab is visible.
        let onScreen = true;
        GL.io = new IntersectionObserver(([e]) => { onScreen = e.isIntersecting; }); GL.io.observe(el);
        const tick = () => {
          if (!GL.controls || !el.isConnected) { GL.raf = 0; return; }
          if (onScreen && !document.hidden) { controls.update(); renderer.render(GL.scene, camera); }
          GL.raf = requestAnimationFrame(tick);
        };
        tick();
        this.viewer3d.loading = false;
      } catch (e) { this.viewer3d.loading = false; this.viewer3d.error = e.message; }
    },
    // P3PV: "P3PV" · uint32 vertices · uint32 faces · float32 xyz… · uint32 i j k… (indexed, ~25k faces)
    parsePreview(buf) {
      if (buf.byteLength < 12 || new TextDecoder().decode(new Uint8Array(buf, 0, 4)) !== 'P3PV') return null;
      const h = new Uint32Array(buf, 4, 2), nv = h[0], nf = h[1];
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(buf, 12, nv * 3), 3));
      geo.setIndex(new THREE.BufferAttribute(new Uint32Array(buf, 12 + nv * 12, nf * 3), 1));
      return geo;
    },
    adjustments(t) {
      if ((t.geometry.production?.method_version || '').startsWith('charm-measure')) return this.adjustmentsCharm(t);
      if ((t.geometry.production?.method_version || '').startsWith('ring-measure-once')) return this.adjustmentsV3(t);
      const raw = t.geometry.raw || {}, prod = t.geometry.production || {}, c = prod.checks || {};
      const n = (v, d) => v == null ? '?' : Number(v).toFixed(d);
      const out = [
        `Received the Hi3D model (${(raw.stl_path || '').split('.').pop().toUpperCase() || 'file'}): ${n(raw.size_x_mm, 3)} × ${n(raw.size_y_mm, 3)} × ${n(raw.size_z_mm, 3)} model units, watertight: ${raw.watertight ? 'yes' : 'no'}.`,
        c.watertight_before_repair === false
          ? `Repaired the mesh (merged vertices, removed degenerate faces, fixed normals, filled holes) — watertight after repair: ${c.watertight_after_repair ? 'yes' : 'no'}.`
          : 'Checked the mesh: already watertight — only merged duplicate vertices and fixed normals.',
        `Found the ring bore: inner diameter ${n(raw.inner_diameter_mm, 4)} model units, roundness deviation ${c.bore_roundness == null ? '?' : (c.bore_roundness * 100).toFixed(2) + '%'}.`,
        `Scaled uniformly ×${n(prod.scale_factor, 3)} so the inner diameter is ${t.target_inner_diameter_mm} mm (US ${t.production_size}); units are now millimetres.`,
        'Aligned the ring: bore centre at the origin, ring axis along Z.',
        `Measured: inner diameter ${n(prod.inner_diameter_mm, 3)} mm, volume ${prod.volume_mm3 == null ? 'not reliable (not watertight)' : (prod.volume_mm3 / 1000).toFixed(3) + ' cc'}, surface ${prod.surface_area_mm2 == null ? '?' : (prod.surface_area_mm2 / 100).toFixed(2) + ' cm²'}.`,
      ];
      if (t.price?.weight_g != null) out.push(`Weight = ${(prod.volume_mm3 / 1000).toFixed(3)} cc × ${t.density_g_cm3} g/cm³ (${t.material_label}) = ${t.price.weight_g.toFixed(2)} g.`);
      out.push('Note: uniform scaling also scales band width and thickness.');
      return out;
    },

    // A charm: its own path — no bore, scaled by its overall height (the attachment loop is not detected)
    adjustmentsCharm(t) {
      const raw = t.geometry.raw || {}, prod = t.geometry.production || {}, c = prod.checks || {};
      const n = (v, d) => v == null ? '?' : Number(v).toFixed(d);
      const r = this.live3d[t.id]?.raw || {};
      const out = [
        `Received the Hi3D STL: ${Number(c.faces || r.faces || 0).toLocaleString()} faces${r.bytes ? ', ' + this.gb(r.bytes) : ''}${r.sha256 ? ', SHA-256 ' + r.sha256.slice(0, 12) + '…' : ''}. Measured once, exactly, on the full model — never on the preview.`,
        `Charm frame: thickness = the direction of least spread; height = the model's up direction${c.up_source === 'largest_spread' ? ' (the model lay flat, so its longest direction was used)' : ''}. Raw model: ${n(raw.size_x_mm, 3)} wide × ${n(raw.size_y_mm, 3)} high × ${n(raw.size_z_mm, 3)} thick, in model units.`,
        `Closed-mesh heuristic (volume from two reference points ${c.closed_heuristic ? 'agrees' : 'DISAGREES'}) — a cheap check, not proof of watertightness; the edge check runs in the background.`,
        `Scaled by arithmetic ×${n(prod.scale_factor, 4)} so the overall height (main body and attachment loop together) is ${t.target_height_mm} mm: lengths × s, area × s², volume × s³.`,
        `Result: ${n(prod.size_x_mm, 2)} × ${n(prod.size_y_mm, 2)} × ${n(prod.size_z_mm, 2)} mm, volume ${prod.volume_mm3 == null ? '?' : (prod.volume_mm3 / 1000).toFixed(3) + ' cc'}, surface ${prod.surface_area_mm2 == null ? '?' : (prod.surface_area_mm2 / 100).toFixed(2) + ' cm²'}.`,
      ];
      if (t.price?.weight_g != null) out.push(`Weight = ${(prod.volume_mm3 / 1000).toFixed(3)} cc × ${t.density_g_cm3} g/cm³ (${t.material_label}) = ${t.price.weight_g.toFixed(2)} g; cost and 3D price from the charm price book.`);
      out.push('The scaled STL (lying flat: width along X, height along Y, thickness along Z, centred, millimetres) is created on demand and deleted after an hour.');
      out.push('A charm size is its total height, the attachment loop included: the whole charm is scaled to it (no loop measurement).');
      return out;
    },

    adjustmentsV3(t) {
      const raw = t.geometry.raw || {}, prod = t.geometry.production || {}, c = prod.checks || {};
      const n = (v, d) => v == null ? '?' : Number(v).toFixed(d);
      const r = this.live3d[t.id]?.raw || {};
      const out = [
        `Received the Hi3D STL: ${Number(c.faces || r.faces || 0).toLocaleString()} faces${r.bytes ? ', ' + this.gb(r.bytes) : ''}${r.sha256 ? ', SHA-256 ' + r.sha256.slice(0, 12) + '…' : ''}. Measured once, exactly, on the full model — never on the preview.`,
        `Raw model: ${n(raw.size_x_mm, 3)} × ${n(raw.size_y_mm, 3)} × ${n(raw.size_z_mm, 3)} model units, bore ${n(raw.inner_diameter_mm, 4)} (the largest circle that passes — what a ring gauge reads), roundness deviation ${c.roundness == null ? '?' : (c.roundness * 100).toFixed(2) + '%'}.`,
        `Closed-mesh heuristic (volume from two reference points ${c.closed_heuristic ? 'agrees' : 'DISAGREES'}) — a cheap check, not proof of watertightness; the edge check runs in the background.`,
        `Scaled by arithmetic ×${n(prod.scale_factor, 4)} so a Ø ${t.target_inner_diameter_mm} mm gauge passes exactly (US ${t.production_size}): lengths × s, area × s², volume × s³ — no file is re-read for a new size or material.`,
        `Result: inner diameter ${n(prod.inner_diameter_mm, 3)} mm, volume ${prod.volume_mm3 == null ? '?' : (prod.volume_mm3 / 1000).toFixed(3) + ' cc'}, surface ${prod.surface_area_mm2 == null ? '?' : (prod.surface_area_mm2 / 100).toFixed(2) + ' cm²'}.`,
      ];
      if (t.price?.weight_g != null) out.push(`Weight = ${(prod.volume_mm3 / 1000).toFixed(3)} cc × ${t.density_g_cm3} g/cm³ (${t.material_label}) = ${t.price.weight_g.toFixed(2)} g.`);
      out.push('The scaled STL (bore centre at the origin, ring axis along Z, millimetres) is created on demand and deleted after an hour.');
      out.push('Note: uniform scaling also scales band width and thickness.');
      return out;
    },

    // ── formatting ─────────────────────────────────────────────────────
    swatch(id) { return this.swatches[id] || '#d4d4d8'; },
    money(v) { return v == null ? '—' : '$' + Number(v).toFixed(2); },
    duration(s) { if (s == null) return '—'; return s < 60 ? Math.round(s) + ' s' : s < 3600 ? Math.floor(s / 60) + ' min ' + Math.round(s % 60) + ' s' : (s / 3600).toFixed(1) + ' h'; },
    materialLabel(id) { return this.materials[id] || id || '—'; },
    // A charm's material reads as the customer saw it ("Sterling Silver"); a ring's as before
    materialLabelFor(id, product) { return product === 'charm' && this.charmMaterials[id] ? this.charmMaterials[id] : this.materialLabel(id); },
    priceText(p) { return !p ? '—' : p.unit_price == null ? 'Unavailable' : '$' + Number(p.unit_price).toFixed(2); },
    fixedSource(p) { return !p ? '' : { bag_snapshot: 'price at Add to Bag', shown_at_customize: 'price shown in Customize', current_quote: 'current list price' }[p.source] || ''; },
    threeDLabel(s) { return (THREE_D[s] || [s])[0]; },
    threeDClass(s) { return (THREE_D[s] || [, 'bg-zinc-100'])[1]; },
    sizeSource(s) { return { customer: 'customer', default: 'default', admin_override: 'manual override' }[s] || s; },
    mm(v) { return v == null ? '—' : Number(v).toFixed(2); },
    innerDia(size) { return (11.63 + 0.8128 * size).toFixed(2); },
    since(a, b) {
      const s = Math.max(0, (new Date(b) - new Date(a)) / 1000);
      return s < 60 ? Math.round(s) + 's' : s < 3600 ? Math.round(s / 60) + 'm' : s < 86400 ? (s / 3600).toFixed(1) + 'h' : (s / 86400).toFixed(1) + 'd';
    },
    stepLabel(e) { const l = (STEPS[e.kind] || [e.kind])[0]; return e.status && e.status !== 'ready' ? `${l} · ${e.status}` : l; },
    // Journey rows that are the customer's own words (prompt, refinement) read as input: muted and italic
    isCustomerInput(e) { return !!e && ['started', 'generate_requested', 'refine_requested'].includes(e.kind); },
    openRef(e) { if (!e?.reference_url) return; this.zoom = null; this.refBox = { url: e.reference_url, label: e.reference_label || 'Reference image', name: e.download_name || 'reference.png', text: e.text || '' }; },
    stepClass(e) { return e.status === 'failed' ? 'bg-red-100 text-red-700' : (STEPS[e.kind] || [, 'bg-zinc-100'])[1]; },
    stepText(e) {
      const d = e.data || {};
      if (e.text) return e.text;
      if (e.kind === 'customization_changed' || e.kind === 'customize_opened')
        return [d.material_id && this.materialLabel(d.material_id), d.ring_size != null && 'US ' + d.ring_size, d.charm_size != null && d.charm_size + ' mm',
                d.quantity && d.quantity > 1 && '×' + d.quantity, d.unit_price != null && '$' + Number(d.unit_price).toFixed(2)].filter(Boolean).join(' · ');
      if (e.kind === 'bag_added') return [this.materialLabel(d.material_id), d.ring_size != null && 'US ' + d.ring_size, d.charm_size != null && d.charm_size + ' mm',
                                          d.unit_price != null && '$' + Number(d.unit_price).toFixed(2)].filter(Boolean).join(' · ');
      if (e.kind === 'gallery_started') return 'Linked to the shared design ' + (d.source_ring_id || '') + ' — nothing generated or charged';
      if (e.kind === 'gallery_reopened') return 'Opened the shared design again';
      if (e.kind === 'legacy_copy_merged') return `${d.copy_ring_id || ''} (${d.copy_title || ''}) — a copy of this ring from before shared designs — was merged into ${d.master_ring_id || 'this ring'}` + (d.orders && d.orders.length ? '; orders ' + d.orders.join(', ') : '') + (d.by ? ` (${d.by})` : '');
      if (e.kind === 'gallery_refined') return 'Refinement of ' + (d.source_ring_id || 'the gallery image') + ' into a design of their own';
      if (e.kind === 'design_forked') {
        const why = { movie: 'already had a 360° movie', '3d': 'already had a 3D request', order: 'already had an order', quote: 'already had a quote request',
                      gallery: 'is in the gallery', split: 'was split off by ' + (d.by || 'the admin') }[d.reason] || 'is a master';
        return 'Refinement of ' + (d.source_ring_id || 'the selected image') + ' into this new design — the original ' + why + ', so it stays as it was';
      }
      if (e.kind === 'admin_refinement_split') return `Refinement “${d.text || ''}” moved into its own design ${d.new_ring_id || ''} (${d.new_title || ''}) by ${d.by || 'admin'}`;
      if (e.kind === 'admin_movie_chosen') return 'The movie shown for this image was chosen by ' + (d.by || 'admin');
      if (e.kind === 'admin_movie_requested') return 'A new 360° movie was requested by ' + (d.by || 'admin') + (d.config_version ? ' (' + d.config_version + ')' : '');
      if (e.kind === 'admin_3d_fix_requested') return 'Making the bore round for ' + (d.production_size ? this.size3dLabel(d.production_size) : 'this result') + (d.roundness != null ? ' (it deviated ' + (d.roundness * 100).toFixed(1) + '%)' : '') + ' — ' + (d.by || 'admin');
      if (e.kind === 'admin_3d_bore_fixed' && d.kept === false) return (d.reason || 'Making the bore round did not work.') + ' The model stays as measured.';
      if (e.kind === 'admin_3d_bore_fixed') return 'Bore made round: ×' + (d.scale_major || 0).toFixed(3) + ' / ×' + (d.scale_minor || 0).toFixed(3) + (d.inner_diameter_mm ? ' · inner Ø ' + d.inner_diameter_mm.toFixed(2) + ' mm' : '') + (d.roundness != null ? ' · deviation ' + (d.roundness * 100).toFixed(1) + '%' : '') + (d.weight_g != null ? ' · ' + d.weight_g + ' g' : '') + (d.status === 'needs_review' ? ' · still needs review' : '');
      if (e.kind === 'admin_3d_accepted') return 'Accepted for production as measured by ' + (d.by || 'admin') + (d.reasons ? ' — ' + d.reasons : '') + (d.note ? ' · ' + d.note : '');
      if (e.kind === 'admin_3d_new_model_override') return `A model of ${d.existing_ring_id} already existed — a new paid Hi3D model was requested for ${d.candidate_ring_id} (${d.by || 'admin'} typed the confirmation)`;
      // The size in the product's own unit: the row's product (Dashboard activity), else the open session's
      const charm = e.product_type ? e.product_type === 'charm' : this.isCharm(this.sd?.session);
      if (e.kind.startsWith('admin_3d')) return [d.production_size && (charm ? d.production_size + ' mm' : 'US ' + d.production_size), d.material_id && this.materialLabel(d.material_id),
                d.reused_raw_mesh && 'reused Hi3D model', d.status, d.weight_g != null && d.weight_g + ' g'].filter(Boolean).join(' · ');
      return '';
    },
    statusLabel(s) { return (STATUS[s] || [s])[0]; },
    statusClass(s) { return (STATUS[s] || [, 'bg-zinc-100'])[1]; },
    eventLabel(e) { const l = (EVENTS[e.kind] || [e.kind])[0]; return e.status && e.status !== 'ready' ? `${l} · ${e.status}` : l; },
    eventClass(e) { return e.status === 'failed' || e.status === 'interrupted' ? 'bg-red-100 text-red-700' : (EVENTS[e.kind] || [, 'bg-zinc-100'])[1]; },
    kindLabel(k) { return { image: 'Image', movie: '360° movie', mesh: '3D' }[k] || k; },
    date(iso) { return iso ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '—'; },
    dateTime(iso) { return iso ? new Date(iso).toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'; },
    ago(iso) {
      if (!iso) return '—';
      const s = (Date.now() - new Date(iso).getTime()) / 1000;
      if (s < 60) return 'just now';
      if (s < 3600) return Math.floor(s / 60) + ' min ago';
      if (s < 86400) return Math.floor(s / 3600) + ' h ago';
      if (s < 86400 * 30) return Math.floor(s / 86400) + ' d ago';
      return this.date(iso);
    },
  };
}
