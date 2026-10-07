// Pipeline 3 customer UI (Alpine.js) — P2 look and feel, P3 logic.
//
// Rules this file follows (spec sections 4 and 10):
//   * the server is authoritative for batches, selection, quotes and the bag;
//   * every async response is checked against the design/batch it was requested
//     for, so late results never land in a different design;
//   * reload only READS state — it never re-submits paid generation work.

// Base path injected by the server (<meta name="p3-base">), e.g. "/JewelryB2C3" or "" at the root.
// EVERY request goes through url() so nothing ever escapes to root /api, /static or /assets.
const ASSET_PATH = /\/assets\//;        // generated files live under BASE/assets/…; their thumbnails under BASE/thumb/…, posters under BASE/poster/…
const BASE = (document.querySelector('meta[name="p3-base"]')?.content || '').replace(/\/+$/, '');
const url = (path) => BASE + path;
const STORE_KEY = 'p3_state' + (BASE ? ':' + BASE : '');
// P2 keeps its session in 'xjet_session' / 'xjet_profile'. P3 shares the proto origin with P2 but
// has its own token store, so it uses its own keys and never reads or overwrites P2's session.
const SESSION_KEY = 'p3_session' + (BASE ? ':' + BASE : '');
const PROFILE_KEY = 'p3_profile' + (BASE ? ':' + BASE : '');
const GALLERY_KEY = 'p3_gallery_pending' + (BASE ? ':' + BASE : '');   // the gallery design chosen before signing in
const FAV_KEY = 'p3_fav_pending' + (BASE ? ':' + BASE : '');           // the gallery design ♥-ed before signing in
// Studio screens: Design · Customize · Bag ('checkout', historical name) · Checkout steps ('order') · Confirmation
const STUDIO_VIEWS = ['ai-studio', 'review', 'checkout', 'order', 'confirmation'];
const CUSTOMER_FIELDS = ['first_name', 'last_name', 'email', 'phone'];
const ADDRESS_FIELDS = ['recipient', 'line1', 'line2', 'city', 'region', 'postal_code', 'country'];
const PAGE_VIEWS = ['home', 'inspiration', 'materials', 'technology', 'faq', 'designers',
                    'terms', 'privacy', 'shipping-returns', 'contact'];

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

// Customer copy per material id (from P2's metals[] descriptions).
const MATERIAL_COPY = {
  stainless_steel: { sub: 'Durable brushed steel', desc: 'Durable, hypoallergenic stainless steel with a cool brushed finish. Everyday strength at an accessible price.' },
  silver:          { sub: 'Sterling Silver 925', desc: 'Classic 92.5% pure silver, digitally jetted for a crisp mirror finish and exceptional detail resolution.' },
  vermeil:         { sub: 'Sterling silver · thick 14K gold plating', desc: 'Sterling silver core with thick 14K gold plating. Luxury look and feel at an accessible price point.' },
  gold_10k_yellow: { desc: '41.7% pure gold — hardest gold alloy, ideal for everyday fine jewelry.' },
  gold_10k_rose:   { desc: 'Warm blush tone with 41.7% gold content — the most durable gold colour.' },
  gold_14k_yellow: { desc: '58.3% pure gold — perfect balance of purity and strength for fine jewellery.' },
  gold_14k_rose:   { desc: 'A warm blush alloy of gold and copper delivering a romantic tone that flatters every skin tone.' },
  gold_18k_yellow: { desc: '75% pure gold — a rich, warm yellow prized for heirloom fine jewellery.' },
  gold_18k_rose:   { desc: 'A romantic blush alloy at 75% gold content — warm, refined, and timeless.' },
};

// Materials page: what each metal looks like, how it wears, how to care for it (wording may be refined later).
const MATERIAL_INFO = {
  stainless_steel: { appearance: 'Cool, light grey metal with a soft sheen.', finish: 'Brushed matte as standard; a polished finish on request.',
                     durability: 'Very hard and scratch-resistant, hypoallergenic, does not tarnish — the most carefree metal for everyday wear.',
                     care: 'Wipe with a soft cloth. Fine for water, sport and daily wear.' },
  silver:          { appearance: 'Bright white precious metal with a classic, cool shine.', finish: 'Mirror polish.',
                     durability: 'A soft precious metal: it picks up fine marks over time and can tarnish slowly, but polishes back to new easily.',
                     care: 'Store dry, polish with a silver cloth; take it off for swimming and chlorinated water.' },
  vermeil:         { appearance: 'The warm colour of 14K gold over a sterling silver core.', finish: 'Polished, with a thick 14K gold plating.',
                     durability: 'The look of gold at a fraction of the price; the plating wears gradually with heavy daily use and can be renewed.',
                     care: 'Take it off for swimming, sport and showering; keep away from perfume and lotion.' },
  gold_10k_yellow: { appearance: 'Soft, pale yellow — 41.7% gold.', durability: 'The hardest and most wear-resistant gold alloy.' },
  gold_14k_yellow: { appearance: 'Warm classic yellow — 58.3% gold.', durability: 'The everyday fine-jewellery standard: rich colour, good hardness.' },
  gold_18k_yellow: { appearance: 'Deep, rich yellow — 75% gold.', durability: 'The most precious colour; a little softer, for pieces worn with care.' },
  gold_10k_rose:   { appearance: 'Warm blush with a copper note — 41.7% gold.', durability: 'The most durable rose alloy.' },
  gold_14k_rose:   { appearance: 'Romantic pink-gold — 58.3% gold.', durability: 'Rich colour with everyday hardness.' },
  gold_18k_rose:   { appearance: 'Soft, luxurious rose — 75% gold.', durability: 'Precious and warm; worn with care.' },
};

// Waiting-screen copy from Pipeline 2 (app-p2.js showLoading presets 'design' / 'refine').
const WAIT_PRESETS = {
  design: {
    title: 'Your jewelry is coming to life',
    message: 'Turning your vision into a detailed design, ready for precision 3D printing.',
    statuses: ['Creating your design...', 'Interpreting your idea...', 'Developing the jewelry geometry...', 'Refining the design details...', 'Preparing your result...'],
  },
  refine: {
    title: 'Refining your jewelry design',
    message: 'Applying your ideas while preserving the character of your creation.',
    statuses: ['Refining your design...', 'Interpreting your changes...', 'Updating the jewelry geometry...', 'Refining the details...', 'Preparing your result...'],
  },
};

function loadStore() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}'); } catch { return {}; }
}
function saveStore(obj) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(obj)); } catch { /* storage unavailable */ }
}
function newRequestId() {
  return (crypto.randomUUID && crypto.randomUUID()) || String(Date.now()) + Math.random().toString(16).slice(2);
}

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

function p3App() {
  return {
    SUPPORT_EMAIL: (window.__p3 && window.__p3.support_email) || '',   // P3_SUPPORT_EMAIL (the server never invents one)

    // ── app / session ────────────────────────────────────────────────
    view: 'home',
    health: null,
    catalog: null,

    // ── Session / token accounting (P2 app-p2.js) ─────────────────────
    userSession: null,                 // {token, quotaUsed, quotaMax, name, email}
    userProfile: { name: '', email: '' },
    signInNotice: null,                // {title, text} banner on the Design screen after sign-in
    showRegModal: false,
    regMode: 'email',                  // 'email' = self-registration, 'token' = enter existing token
    regForm: { token: '', name: '', email: '' },
    regError: '', regInfo: '', regLoading: false,
    quotaUsed: 0, quotaMax: 10,
    get quotaRemaining() { return Math.max(0, this.quotaMax - this.quotaUsed); },
    get token() { return (this.userSession && this.userSession.token) || ''; },
    get displayName() {
      if (!this.userSession) return '';
      return String((this.userProfile && this.userProfile.name) || this.userSession.name || '').trim();
    },
    get displayEmail() {
      if (!this.userSession) return '';
      return String((this.userProfile && this.userProfile.email) || this.userSession.email || '').trim();
    },
    accountPanelOpen: false, tokenCopied: false,
    accountPanelPos: { top: 64, right: 24 },   // the account menu opens right under the header's account button

    // ── studio: compose ──────────────────────────────────────────────
    userInput: '', uploadedFile: null, uploadedPreview: null, rightsConfirmed: false,
    composeError: '', submitting: false,

    // ── studio: current design ───────────────────────────────────────
    design: null,
    viewBatchId: null,
    pendingRefineBatchId: null,
    actionError: '',
    sidebarOpen: window.innerWidth >= 1024,
    designs: [], designsLoading: false, designsError: '', projectSearch: '',

    // ── preview overlay (P2 fullscreen zoom) ─────────────────────────
    previewOpen: false, previewMedia: 'image', previewSrc: null, previewCandidate: null,
    gallery: [], galleryState: 'loading', galleryItem: null, galleryBusy: false, galleryError: '', galleryGridOpen: false,   // Inspiration Gallery
    galleryMetal: '', shareBusy: false, shareUrl: '', shareCopied: false, shareInfo: null, _restoreTitle: '',   // lightbox: metal preview (visual only) and Share
    cardMetal: {},                                                             // Gallery cards: the metal each card is previewed in (visual only)
    favorites: [], favBusy: '', sidebarTab: 'designs', _pendingPanel: '',                       // ♥ Favorites: saved references to gallery masters
    homeGallery: [],                                                           // the 8 gallery designs this visit features (random per page load)
    heroIndex: 0, heroPaused: false, _heroQueue: [], _heroTimer: null,       // the home hero takes turns through the featured rings
    zoom: null,                                                                // hover preview of a My Designs thumbnail {src, label, x, y, size}
    openedFromList: false,                                                     // a saved design opened from My Designs: images only, no prompt echo
    candIndex: 0,                                                              // phone: which of the four options is in view
    previewZoom: 1, previewPanX: 0, previewPanY: 0, _panning: false, _panStart: null, _swipeX: null,
    menuOpen: false,                   // mobile navigation
    aboutOpen: false,                  // the header's About menu (About XJet, Technology, FAQ, Support)
    sizeConfirmed: false,              // Customize opens on a suggested size; the customer confirms or changes it
    _returnFocus: null,                // element to focus again when a modal closes

    // ── rings and charms: offered only while the catalog lists them (charms visible to this browser) ──
    newProduct: 'ring',                // what a new design will be (Design screen; Ring by default; kept through sign-in)
    galleryProduct: '',                // Inspiration Gallery filter: '' all · 'ring' · 'charm'

    // ── customize ────────────────────────────────────────────────────
    cust: null, custError: '', mediaTab: 'movie', quotePending: false, bagMessage: '',
    lastMaterialByGroup: { fashion: 'silver', luxury: null },
    groupOpen: { fashion: true, luxury: false },
    showSizeGuide: false,

    // ── bag ──────────────────────────────────────────────────────────
    bag: null,

    // ── checkout & orders ─────────────────────────────────────────────
    checkout: null, coQuote: null, order: null, orders: [], ordersLoading: false,
    co: { step: 'details', customer: { first_name: '', last_name: '', email: '', phone: '' },
          address: { recipient: '', line1: '', line2: '', city: '', region: '', postal_code: '', country: 'US' },
          shipping_method: 'standard', promo_code: '', promo_input: '', terms: false, busy: false, error: '',
          problems: {}, addrCheck: null, useSuggested: false, requestId: '' },
    quoteReq: { open: false, busy: false, error: '', done: null, message: '', quantity: 1, problems: {},
                customer: { first_name: '', last_name: '', email: '', phone: '' } },

    // ── developer AI-mode control (internal; needs P3_ADMIN_KEY) ──────
    devKey: '', devKeyInput: '', devPromptOpen: false, devError: '', devMode: null,
    liveConfirmOpen: false, liveConfirmText: '', devBusy: false,

    _poll: null, _pollCust: null,
    waitStatusIndex: 0,

    // ── lifecycle ─────────────────────────────────────────────────────
    async init() {
      const st = loadStore();
      // Rotate the waiting-screen status line every 3.5 s (P2 cadence); skipped for reduced motion.
      if (!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)) {
        setInterval(() => { this.waitStatusIndex++; }, 3500);
      }
      try { this.health = await this.api('GET', '/api/health', null, { noAuth: true }); } catch { this.health = null; }
      this.catalog = await this.api('GET', '/api/catalog', null, { noAuth: true });
      // The product a new design will be: the one chosen before sign-in while it is still on offer, else the default on offer
      this.newProduct = this.productsList.includes(st.newProduct) ? st.newProduct : (this.catalog?.products?.default || this.productsList[0] || 'ring');
      this.loadGallery().then(() => { this._openSharedGallery(); this.startHeroRotation(); });
      // Developer tools exist only where the server says so (never in production, p3/app.py); the hash is internal, not linked
      if ((location.hash || '') === '#developer') { if (window.__p3?.dev_tools) this.devPromptOpen = true; history.replaceState(null, '', location.pathname); }
      const lux = this.materialsOf('luxury');
      this.lastMaterialByGroup.luxury = lux.length ? lux[0].id : null;
      try {
        const P = JSON.parse(localStorage.getItem(PROFILE_KEY) || 'null');
        if (P) this.userProfile = { name: P.name || '', email: P.email || '' };
      } catch (_) {}
      // A #token=… link (verification email / verify page) signs the user straight in and takes
      // precedence over a saved session; otherwise restore the saved session (P2 behaviour).
      const fromLink = this._loginFromUrlToken();
      if (!fromLink) {
        try {
          const S = JSON.parse(localStorage.getItem(SESSION_KEY) || 'null');
          if (S && S.token) {
            this.userSession = S;
            this.quotaUsed = S.quotaUsed || 0;
            this.quotaMax = S.quotaMax || 10;
            if (S.name && !this.userProfile.name) this._saveUserProfile(S.name, null);
          }
        } catch (_) { try { localStorage.removeItem(SESSION_KEY); } catch (__) {} }
      }
      if (this.userSession) { await this._refreshQuota(); this.loadFavorites(); }
      try { this.devKey = sessionStorage.getItem('p3_dev_key') || ''; } catch { this.devKey = ''; }
      if (this.devKey) this.loadDevMode();
      const hashView = (location.hash || '').replace('#', '');
      if (PAGE_VIEWS.includes(hashView)) { this.view = hashView; if (hashView === 'materials') this.loadMaterialQuotes(); }
      if (!this.token) return;
      this.refreshBag();
      if (fromLink) {
        // Record the sign-in (verify page / token email link); the session is already set.
        this.api('POST', '/api/register-token', { Token: this.token, Via: 'link' }, { noAuth: true }).catch(() => {});
        await this._afterSignIn(true);
        return;
      }
      // A link to a page (#materials, #terms from the checkout …) wins over restoring the studio.
      if (st.designId && STUDIO_VIEWS.includes(st.view) && !PAGE_VIEWS.includes(hashView)) {
        try { await this.openDesign(st.designId, { restoreView: st.view }); }
        catch { this.persist({ designId: null }); }
      }
    },

    persist(patch) { saveStore({ ...loadStore(), ...patch }); },

    // Delivery: small places get a thumbnail (WebP or JPEG at 320 / 800 px, made on first request and cached);
    // the original full-size image stays behind every large view, the zoom and the download.
    thumb(url, w = 320) { return url && ASSET_PATH.test(url) ? url.replace(ASSET_PATH, '/thumb/') + '?w=' + w : (url || ''); },
    srcsetFor(url) { return url && ASSET_PATH.test(url) ? `${this.thumb(url, 320)} 320w, ${this.thumb(url, 800)} 800w, ${url} 1024w` : ''; },
    poster(url) { return url && ASSET_PATH.test(url) ? url.replace(ASSET_PATH, '/poster/') : ''; },
    async api(method, path, body, opts = {}) {
      const headers = {};
      if (!opts.noAuth && this.token) headers['X-Access-Token'] = this.token;
      let payload;
      if (body instanceof FormData) payload = body;
      else if (body !== null && body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
      let resp;
      try {
        resp = await fetch(url(path), { method, headers, body: payload });
      } catch (e) {
        throw new ApiError(0, 'network', 'Network error — please check your connection.');
      }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const err = data.error || {};
        if (resp.status === 402) this._refreshQuota();
        const ex = new ApiError(resp.status, err.code || 'error', err.message || `Request failed (${resp.status})`);
        if (err.problems) ex.problems = err.problems;          // field-level problems (checkout forms)
        throw ex;
      }
      return data;
    },

    // ── toast: short confirmation of an action, with an optional follow-up ──
    toast: null, _toastTimer: null,
    showToast(text, opts = {}) {
      clearTimeout(this._toastTimer);
      this.toast = { text, label: opts.label || '', action: opts.action || null, kind: opts.kind || 'ok' };
      this._toastTimer = setTimeout(() => { this.toast = null; }, opts.ms || 5000);
    },
    toastAction() { const a = this.toast?.action; this.toast = null; if (a) a(); },

    // ── mode (mock vs live) ───────────────────────────────────────────
    get isMock() { return this.health?.mode === 'mock'; },
    get isLive() { return this.health?.mode === 'live'; },

    // ── developer AI-mode control ──────────────────────────────────────
    async devRequest(method, path, body) {
      const resp = await fetch(url(path), { method, headers: { 'Authorization': 'Bearer ' + this.devKey,
        ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new ApiError(resp.status, data.error?.code || 'error', data.error?.message || ('HTTP ' + resp.status));
      return data;
    },
    async loadDevMode() {
      try { this.devMode = await this.devRequest('GET', '/api/dev/mode'); }
      catch (e) { this.devMode = null; if (e.status === 403 || e.status === 503) this.forgetDevKey(); }
    },
    async devConnect() {
      this.devError = ''; this.devKey = this.devKeyInput.trim();
      try {
        this.devMode = await this.devRequest('GET', '/api/dev/mode');
        try { sessionStorage.setItem('p3_dev_key', this.devKey); } catch {}
        this.devPromptOpen = false; this.devKeyInput = '';
      } catch (e) { this.devError = e.message; this.devKey = ''; }
    },
    forgetDevKey() { this.devKey = ''; this.devMode = null; try { sessionStorage.removeItem('p3_dev_key'); } catch {} },
    async switchAiMode(target) {
      this.devError = ''; this.devBusy = true;
      try {
        const body = { mode: target };
        if (target === 'live') body.confirmation = this.liveConfirmText.trim();
        this.devMode = await this.devRequest('POST', '/api/dev/mode', body);
        this.liveConfirmOpen = false; this.liveConfirmText = '';
        try { this.health = await this.api('GET', '/api/health', null, { noAuth: true }); } catch {}
      } catch (e) { this.devError = e.message; }
      finally { this.devBusy = false; }
    },

    // ── navigation ────────────────────────────────────────────────────
    navigateTo(v) {
      if (this.previewOpen) this.closePreview();
      this.menuOpen = false; this.aboutOpen = false;
      this.view = v;
      if (PAGE_VIEWS.includes(v)) history.replaceState(null, '', v === 'home' ? location.pathname : '#' + v);
      else history.replaceState(null, '', location.pathname);
      if (STUDIO_VIEWS.includes(v)) this.persist({ view: v });
      window.scrollTo({ top: 0 });
      document.querySelector('main')?.scrollTo?.({ top: 0 });
      if (v === 'checkout') { this.refreshBag(); this.track('bag_viewed'); }
      if (v === 'materials') this.loadMaterialQuotes();
    },
    // Session analytics for the Admin (best effort; never blocks the customer).
    track(kind, designId = null) {
      if (!this.token) return;
      this.api('POST', '/api/events', { kind, design_id: designId }).catch(() => {});
    },
    scrollToHowItWorks() {
      this.navigateTo('home');
      this.$nextTick(() => setTimeout(() => document.getElementById('how-it-works')?.scrollIntoView({ behavior: 'smooth' }), 80));
    },

    // Start Designing (P2 resetAIFlow): sign in if needed, then open a fresh studio.
    resetAIFlow() {
      if (!this.userSession) { this.openRegModal(); return; }
      this._doResetAIFlow();
    },
    _doResetAIFlow() {
      this.startNew();
      this.navigateTo('ai-studio');
      this._refreshQuota();             // authoritative count every time the Design screen opens
    },

    // ── Inspiration Gallery: a real XJet design as the starting point ─────
    // Three distinct states: loading (skeleton tiles) · loaded (tiles, or a real empty message) · failed (retry).
    async loadGallery() {
      this.galleryState = 'loading';
      try { this.gallery = (await this.api('GET', '/api/gallery', null, { noAuth: true })).items || []; this.galleryState = 'loaded'; }
      catch (_) { this.galleryState = 'failed'; }
      this.homeGallery = this._pickHome(this.gallery);
    },
    // The homepage features at most 8 gallery designs, chosen at random on every page load (a quiet shuffle,
    // so returning visitors see different rings); with 8 or fewer, all of them. "View all designs" opens the rest.
    _pickHome(list) {
      const a = [...list];
      for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
      return a.slice(0, 8);
    },
    // A My Designs thumbnail enlarges on hover: a floating preview beside the list, on whichever side has room
    showZoom(ev, src, label) {
      if (!window.matchMedia || !window.matchMedia('(hover: hover)').matches) return;        // no hover on touch screens
      const r = ev.currentTarget.getBoundingClientRect(), size = Math.min(360, window.innerWidth - 32, window.innerHeight - 96);
      let x = r.left - size - 14;                                                           // the list sits at the right edge: open to the left
      if (x < 16) x = Math.min(window.innerWidth - size - 16, r.right + 14);
      const y = Math.max(16, Math.min(r.top + r.height / 2 - size / 2, window.innerHeight - size - 56));
      this.zoom = { src, label, x, y, size };
    },
    get heroRing() { return this.homeGallery[Math.min(this.heroIndex, Math.max(this.homeGallery.length - 1, 0))] || null; },
    // The hero shows every gallery ring in turn: random order (each ring once per round), a smooth
    // crossfade every few seconds, paused while the visitor hovers or looks at a design; a tap on a
    // thumbnail shows that ring at once. Nothing moves for visitors who prefer reduced motion.
    startHeroRotation() {
      if (this._heroTimer) clearInterval(this._heroTimer);
      this.heroIndex = 0; this._heroQueue = [];
      if (this.homeGallery.length < 2 || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)) return;
      this._heroTimer = setInterval(() => {
        if (this.heroPaused || this.galleryItem || this.view !== 'home' || document.hidden) return;
        this.heroGo(this._nextHero(), true);
      }, 5000);
    },
    _nextHero() {
      if (!this._heroQueue.length) {                       // a fresh shuffled round, never the ring that is showing
        const rest = this.homeGallery.map((_, i) => i).filter(i => i !== this.heroIndex);
        for (let i = rest.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [rest[i], rest[j]] = [rest[j], rest[i]]; }
        this._heroQueue = rest;
      }
      return this._heroQueue.shift();
    },
    heroGo(i, auto = false) {
      if (i == null || i < 0 || i >= this.homeGallery.length) return;
      this.heroIndex = i;
      this._heroQueue = this._heroQueue.filter(x => x !== i);
      if (!auto && this._heroTimer) this.startHeroRotationFrom(i);    // a tap restarts the clock from this ring
    },
    startHeroRotationFrom(i) {
      clearInterval(this._heroTimer);
      this._heroTimer = setInterval(() => {
        if (this.heroPaused || this.galleryItem || this.view !== 'home' || document.hidden) return;
        this.heroGo(this._nextHero(), true);
      }, 5000);
      this.heroIndex = i;
    },
    openGallery(g) {
      this.galleryItem = g; this.galleryError = ''; this.galleryBusy = false; this.galleryMetal = this.cardMetal[g.id] || ''; this.shareUrl = ''; this.shareCopied = false;
      // The share link is fetched as the preview opens, so Share / Copy link act at once inside the tap (browsers
      // only allow the share sheet and copying during the click itself)
      this.shareInfo = null;
      this.api('GET', `/api/gallery/${encodeURIComponent(g.id)}/share`, null, { noAuth: true })
        .then(s => { if (this.galleryItem === g) this.shareInfo = s; }).catch(() => {});
    },
    // The lightbox can show the ring in another metal — the same visual filter the 360° movie uses, applied to the
    // preview image only: nothing is generated, saved, priced or selected; the master design is untouched.
    metalLabel(id) { return this.material(id)?.label || ''; },
    // ── Inspiration Gallery: metal preview on every card and in View — visual only (the existing metal filters): no AI,
    // the master design, the customer's own material and every price stay as they are. Gold is the 18K yellow look,
    // shown as "Gold": it is not a choice of karat.
    galleryMetalLabel(id) { return id === 'gold_18k_yellow' ? 'Gold' : this.metalLabel(id); },
    metalSwatch(id) { return this.material(id)?.swatch || '#d4d4d8'; },
    pickCardMetal(g, id) { this.cardMetal = { ...this.cardMetal, [g.id]: this.cardMetal[g.id] === id ? '' : id }; },
    setGalleryMetal(id) {
      this.galleryMetal = id;
      if (this.galleryItem) this.cardMetal = { ...this.cardMetal, [this.galleryItem.id]: id };   // the card shows it too
    },
    // Share: on a phone the native share sheet; on a computer "Copy link". The link is by design name
    // (/design/aurora-twist) with the ring image, the name and "Designed with XJet Atelier" as its preview.
    get canNativeShare() {
      try { return !!navigator.share && window.matchMedia('(hover: none) and (pointer: coarse)').matches; } catch { return false; }
    },
    async shareGallery(g) {
      if (!g || this.shareBusy) return;
      this.shareBusy = true; this.shareUrl = '';
      try {
        const s = this.shareInfo || await this.api('GET', `/api/gallery/${encodeURIComponent(g.id)}/share`, null, { noAuth: true });
        if (this.canNativeShare) {
          try { await navigator.share({ title: s.title, text: s.text, url: s.url }); return; }
          catch (e) { if (e && e.name === 'AbortError') return; }      // dismissed — nothing to do
        }
        if (await copyText(s.url)) {
          this.shareCopied = true; setTimeout(() => { this.shareCopied = false; }, 2200);
          this.showToast(`Link copied — ${s.title}`);
        } else this.shareUrl = s.url;                                   // no clipboard access: show the link to copy
      } catch (e) { this.showToast(e.message || 'The link could not be prepared.'); }
      finally { this.shareBusy = false; }
    },
    // ♥ Favorites — a saved reference to the gallery master (never a copy, never a My Design), per account.
    isFav(g) { return !!g && this.favorites.some(f => f.design_id === g.design_id); },
    get filteredFavorites() {
      const q = (this.projectSearch || '').trim().toLowerCase();
      return q ? this.favorites.filter(f => f.title.toLowerCase().includes(q)) : this.favorites;
    },
    async loadFavorites() {
      if (!this.userSession) { this.favorites = []; return; }
      try { this.favorites = (await this.api('GET', '/api/favorites')).items || []; } catch (_) {}
    },
    async toggleFav(g) {
      if (!g || this.favBusy) return;
      if (!this.userSession) {                 // sign in first; the ♥ is saved right after and the design shown again
        this._setPendingFav(g.id);
        this.closeGallery();
        this.openRegModal();
        return;
      }
      const was = this.isFav(g);
      this.favBusy = g.design_id;
      try {
        const r = await this.api(was ? 'DELETE' : 'PUT', `/api/favorites/${encodeURIComponent(g.design_id)}`);
        this.favorites = r.items || [];
        this.showToast(was ? 'Removed from Favorites' : 'Saved to Favorites');
      } catch (e) { this.showToast(e.message); }
      finally { this.favBusy = ''; }
    },
    _pendingFav() { try { return localStorage.getItem(FAV_KEY) || ''; } catch { return ''; } },
    _setPendingFav(id) { try { id ? localStorage.setItem(FAV_KEY, id) : localStorage.removeItem(FAV_KEY); } catch {} },
    closeGallery() {
      this.galleryItem = null; this.galleryBusy = false;
      if (this._restoreTitle) { document.title = this._restoreTitle; this._restoreTitle = ''; }   // after a shared link's preview
    },
    _pendingGallery() { try { return localStorage.getItem(GALLERY_KEY) || ''; } catch { return ''; } },
    _setPendingGallery(id) { try { id ? localStorage.setItem(GALLERY_KEY, id) : localStorage.removeItem(GALLERY_KEY); } catch {} },
    async makeItYours() {
      const g = this.galleryItem; if (!g) return;
      if (!this.userSession) {               // sign in first; the chosen design is remembered and opened right after
        this._setPendingGallery(g.id);
        this.closeGallery();
        this.openRegModal();
        return;
      }
      await this.startFromGallery(g.id);
    },
    // Share link (#gallery=<id>): open that design's preview straight away.
    _openSharedGallery() {
      let id = '';
      // A link by design name (/design/aurora-twist) arrives with the design to open already resolved by the server;
      // the address becomes the plain site again so every later link and reload behaves as usual.
      const shared = window.__p3Open;
      if (shared && typeof shared === 'object') {
        window.__p3Open = null;
        try { if (location.pathname !== (BASE + '/')) history.replaceState(null, '', (BASE || '') + '/' + (location.hash || '')); } catch (_) {}
        if (shared.gallery === null) { this.showToast('This design is no longer in the Inspiration Gallery.', { ms: 6000 }); return; }
        id = shared.gallery || '';
        this._restoreTitle = shared.site_title || '';                  // the page title is the design's until it closes
      }
      try {
        const P = new URLSearchParams((location.hash || '').replace(/^#/, ''));
        id = P.get('gallery') || id;
        if (P.get('gallery')) { P.delete('gallery'); const F = P.toString(); history.replaceState(history.state, '', location.pathname + location.search + (F ? '#' + F : '')); }
      } catch (_) {}
      const g = id && this.gallery.find(x => x.id === id);
      if (g) this.openGallery(g);
    },
    // The customer is linked to the shared XJet design (nothing is copied, generated or charged), with
    // the gallery image selected, and continues exactly as with a design of their own.
    async startFromGallery(id) {
      this.galleryBusy = true; this.galleryError = '';
      try {
        const d = await this.api('POST', `/api/gallery/${encodeURIComponent(id)}/start`, { client_request_id: newRequestId() });
        this._setPendingGallery('');
        this.closeGallery(); this.galleryGridOpen = false; this.showRegModal = false; this.closePreview(); this.signInNotice = null;
        await this.openDesign(d.id);
        this.loadDesigns(); this.loadFavorites(); this._refreshQuota();
      } catch (e) {
        this.galleryBusy = false;
        if (this.galleryItem) { this.galleryError = e.message; return; }
        this._setPendingGallery('');           // resumed after sign-in and the design is gone: plain Design screen
        this._enterDesignAfterSignIn(false);
        this.actionError = e.message;
      }
    },
    // After any sign-in: straight into the gallery design the customer chose, else the Design screen.
    async _afterSignIn(fromVerification) {
      const pending = this._pendingGallery();
      if (pending) { await this.startFromGallery(pending); return; }
      const fav = this._pendingFav();
      if (fav) {                               // they tapped ♥ before signing in: save it and show the design again
        this._setPendingFav('');
        this.showRegModal = false;
        await this.loadFavorites();
        const g = this.gallery.find(x => x.id === fav);
        if (g) { if (!this.isFav(g)) await this.toggleFav(g); this.openGallery(g); return; }
      }
      const panel = this._pendingPanel;       // they asked for My Designs, ♥ Favorites or the bag before signing in
      this._pendingPanel = '';
      this._enterDesignAfterSignIn(fromVerification);
      if (panel === 'bag') { this.signInNotice = null; this.goToCheckout(); }
      else if (panel) this.openPanel(panel);
    },

    // ── Sign-in / registration (P2 JewelryB2C2) ───────────────────────
    // A proper dialog: focus moves inside when it opens, stays inside (Tab cycles), and returns to the
    // element that opened it when it closes; Escape and the Close button close it.
    openRegModal(mode = 'email') {
      this.regMode = mode;
      this.regError = ''; this.regInfo = '';
      this.regForm = { token: '', name: '', email: '' };
      this._returnFocus = document.activeElement;
      this.showRegModal = true;
      // The dialog is teleported to <body>, so it is found by id rather than through $refs.
      this.$nextTick(() => setTimeout(() => document.getElementById('reg-dialog')?.querySelector('input:not([disabled]), button:not([disabled])')?.focus(), 30));
    },
    closeRegModal() {
      this.showRegModal = false;
      this._setPendingGallery(''); this._setPendingFav(''); this._pendingPanel = '';
      const el = this._returnFocus; this._returnFocus = null;
      if (el && el.isConnected) setTimeout(() => el.focus(), 0);
    },
    trapFocus(ev, id) {
      if (ev.key !== 'Tab') return;
      const root = document.getElementById(id); if (!root) return;
      const items = [...root.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
        .filter(el => el.offsetParent !== null);
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
      else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
    },
    async submitEmailRegistration() {
      this.regLoading = true; this.regError = ''; this.regInfo = '';
      try {
        const Name = this.regForm.name.trim(), Email = this.regForm.email.trim();
        const Result = await this.api('POST', '/api/register', { Name, Email }, { noAuth: true });
        this._saveUserProfile(Name, Email);
        // The token is never returned here — it is emailed. Show a confirmation message.
        this.regInfo = Result.message
          || "We've sent a verification email to your inbox. Please verify your email to save your designs and continue creating your jewelry.";
      } catch (E) {
        this.regError = E.message;
      } finally {
        this.regLoading = false;
      }
    },
    async submitRegistration() {
      this.regLoading = true; this.regError = '';
      try {
        const Token = this.regForm.token.toUpperCase().trim();
        const Result = await this.api('POST', '/api/register-token', { Token, Name: '', Email: '' }, { noAuth: true });
        this._setSession({ token: Token, quotaUsed: Result.used, quotaMax: Result.max, name: Result.name || '' });
        if (Result.name) this._saveUserProfile(Result.name, null);
        this.showRegModal = false; this._returnFocus = null;
        this.regForm = { token: '', name: '', email: '' };
        await this._afterSignIn(false);        // the chosen gallery design, else the Design screen (as P2)
        this.refreshBag();
      } catch (E) {
        this.regError = E.message;
      } finally {
        this.regLoading = false;
      }
    },
    _setSession(S) {
      this.userSession = S;
      this.quotaUsed = S.quotaUsed || 0;
      this.quotaMax = S.quotaMax || 10;
      try { localStorage.setItem(SESSION_KEY, JSON.stringify(S)); } catch (_) {}
    },
    // Log in from a #token=… fragment (the "Continue designing" link). A fragment is never sent to
    // the server, so the token stays out of access logs. Returns true when a token was present.
    _loginFromUrlToken() {
      let UrlToken = '';
      try {
        const Params = new URLSearchParams((location.hash || '').replace(/^#/, ''));
        UrlToken = (Params.get('token') || '').trim();
        if (!UrlToken) return false;
        Params.delete('token');
        const Frag = Params.toString();
        history.replaceState(history.state, '', location.pathname + location.search + (Frag ? '#' + Frag : ''));
      } catch (_) { return false; }
      if (!UrlToken.startsWith('p3_')) UrlToken = UrlToken.toUpperCase();
      this._setSession({ token: UrlToken, quotaUsed: 0, quotaMax: 10 });
      return true;
    },
    // Pull the authoritative quota (P2 _refreshQuota): on load, when the Design screen opens,
    // after generations, and on a quota error.
    async _refreshQuota() {
      if (!this.userSession) return;
      let Q = null;
      try { Q = await this.api('GET', '/api/token-status'); } catch (_) { Q = null; }
      if (!Q || !this.userSession) return;
      this.quotaUsed = Q.used; this.quotaMax = Q.max;
      Object.assign(this.userSession, { quotaUsed: Q.used, quotaMax: Q.max });
      if (Q.name) this.userSession.name = Q.name;
      if (Q.email) this.userSession.email = Q.email;
      try { localStorage.setItem(SESSION_KEY, JSON.stringify(this.userSession)); } catch (_) {}
      if ((Q.name && !this.userProfile.name) || (Q.email && !this.userProfile.email)) {
        this._saveUserProfile(Q.name || null, Q.email || null);
      }
    },
    _saveUserProfile(name, email) {
      this.userProfile = { name: name || this.userProfile.name || '', email: email || this.userProfile.email || '' };
      try { localStorage.setItem(PROFILE_KEY, JSON.stringify(this.userProfile)); } catch (_) {}
    },
    // Where a freshly signed-in user lands: the Design screen, with a clear confirmation.
    _enterDesignAfterSignIn(fromVerification = false) {
      this.showRegModal = false;
      this.closePreview();
      this._doResetAIFlow();
      this.signInNotice = {
        title: fromVerification ? 'Your email has been verified successfully.' : "You're signed in.",
        text: 'You can now continue designing your jewelry.',
      };
      this.loadDesigns(); this.loadFavorites();
    },
    dismissSignInNotice() {
      this.signInNotice = null;
      this.$nextTick(() => { const t = document.getElementById('p3-composer-input'); if (t) t.focus(); });
    },
    logout() {
      try { localStorage.removeItem(SESSION_KEY); } catch (_) {}
      try { localStorage.removeItem(PROFILE_KEY); } catch (_) {}        // nothing of the person stays in the browser
      this.stopPolling();
      this.userSession = null; this.quotaUsed = 0; this.quotaMax = 10;
      this.userProfile = { name: '', email: '' };
      this.favorites = []; this.sidebarTab = 'designs'; this._setPendingFav('');
      this.signInNotice = null;
      this.design = null; this.cust = null; this.bag = null; this.designs = []; this.designsError = '';
      this.order = null; this.orders = []; this.checkout = null; this.coQuote = null;
      this.co = { ...this.co, step: 'details', customer: { first_name: '', last_name: '', email: '', phone: '' },
                  address: { recipient: '', line1: '', line2: '', city: '', region: '', postal_code: '', country: 'US' },
                  promo_code: '', promo_input: '', terms: false, error: '', problems: {}, addrCheck: null, requestId: '' };
      this.persist({ designId: null, view: 'home' });
      this.navigateTo('home');
    },

    // ── Account panel (opens from the token coin / balance line) ───────
    toggleAccountPanel() {
      this.accountPanelOpen = !this.accountPanelOpen;
      this.tokenCopied = false;
      if (this.accountPanelOpen) { this.menuOpen = false; this.aboutOpen = false; this._placeAccountPanel(); this._refreshQuota(); this.loadOrders(); }
    },
    // A dropdown under the header's account button, right edges aligned (on a phone: full width under the header).
    // Opened from the composer's balance line, or with the button hidden, it sits under the header.
    _placeAccountPanel() {
      const btn = document.querySelector('[data-account-button]');
      const shown = btn && btn.getClientRects().length > 0;
      const r = (shown ? btn : document.querySelector('nav.site-header'))?.getBoundingClientRect();
      const top = Math.round((r ? r.bottom : 56) + (shown ? 8 : 6));
      const right = window.innerWidth < 640 || !shown ? 16 : Math.max(16, Math.round(window.innerWidth - r.right));
      this.accountPanelPos = { top, right };
    },
    closeAccountPanel() { this.accountPanelOpen = false; this.tokenCopied = false; },
    async copyAccessToken() {
      const t = this.token;
      if (!t) return;
      try { await navigator.clipboard.writeText(t); }
      catch (_) {
        const ta = document.createElement('textarea');
        ta.value = t; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); } catch (__) {}
        document.body.removeChild(ta);
      }
      this.tokenCopied = true;
      setTimeout(() => { this.tokenCopied = false; }, 2000);
    },
    openMyDesignsFromAccount() { this.openPanel('designs'); },
    // The header's bag: the bag page; signed out, sign in first and the bag opens right after.
    openBag() {
      this.menuOpen = false; this.closeAccountPanel();
      if (!this.userSession) { this._pendingPanel = 'bag'; this.openRegModal(); return; }
      this.goToCheckout();
    },
    // My Designs and ♥ Favorites live in the Design screen's side panel. The header, the studio toolbar, the phone
    // menu and the account panel all open it on the right tab; signed out, sign in first and it opens right after.
    get inStudio() { return STUDIO_VIEWS.includes(this.view); },
    openPanel(tab = 'designs') {
      this.menuOpen = false; this.closeAccountPanel();
      if (!this.userSession) { this._pendingPanel = tab; this.openRegModal(); return; }
      if (this.view !== 'ai-studio') { if (this.design) this.navigateTo('ai-studio'); else this._doResetAIFlow(); }
      this.sidebarTab = tab === 'favorites' ? 'favorites' : 'designs';
      this.sidebarOpen = true;
      if (tab === 'favorites') this.loadFavorites(); else this.loadDesigns();
    },
    signOutFromAccount() { this.closeAccountPanel(); this.logout(); },

    // ── catalog helpers ───────────────────────────────────────────────
    group(id) { return this.catalog?.groups.find(g => g.id === id); },
    materialsOf(groupId) { return this.group(groupId)?.materials || []; },
    material(id) {
      for (const g of this.catalog?.groups || []) for (const m of g.materials) if (m.id === id) return m;
      return null;
    },
    groupOfMaterial(id) { return this.material(id)?.group; },
    copy(id) { return MATERIAL_COPY[id] || {}; },
    info(id) { return MATERIAL_INFO[id] || {}; },
    get allMaterials() { return (this.catalog?.groups || []).flatMap(g => g.materials.map(m => ({ ...m, groupLabel: g.label }))); },
    // Materials & Pricing: the fixed price per metal is public (no sign-in needed to see what a ring costs)
    materialQuotes: {}, materialQuotesState: 'idle',
    async loadMaterialQuotes() {
      if (this.materialQuotesState === 'loading' || this.materialQuotesState === 'loaded') return;
      this.materialQuotesState = 'loading';
      try {
        const out = {};
        await Promise.all(this.materialsOf('fashion').map(async m => { out[m.id] = await this.api('GET', `/api/quote?material_id=${encodeURIComponent(m.id)}`, null, { noAuth: true }); }));
        this.materialQuotes = out; this.materialQuotesState = 'loaded';
      } catch (_) { this.materialQuotesState = 'failed'; }
    },
    materialPriceText(id) {
      if (this.soleProduct === 'charm') return this.charmPriceText(id, '');            // charms only: the fixed price per size
      const q = this.materialQuotes[id];
      if (this.materialQuotesState !== 'loaded' || !q) return this.materialQuotesState === 'failed' ? 'Price unavailable' : '…';
      return q.pricing_status === 'available' ? this.money(q.unit_price, q.currency) + ' per ring, any size' : 'Price on request';
    },
    charmMaterial(id) { return (this.catalog?.products?.charm?.materials || []).find(m => m.id === id); },
    charmPriceText(id, prefix = 'Charm: ') {
      const prices = (this.charmMaterial(id)?.prices || []).filter(p => p.price != null);
      const text = prices.length ? 'from ' + this.money(Math.min(...prices.map(p => p.price)), prices[0].currency) + ' per charm, by size' : 'price on request';
      return prefix ? prefix + text : text[0].toUpperCase() + text.slice(1);
    },
    // Support: one address everywhere, with the right context in the subject line
    mailto(subject, body = '') {
      const q = new URLSearchParams(); q.set('subject', subject); if (body) q.set('body', body);
      if (!this.SUPPORT_EMAIL) return '#contact';                       // no address configured: the Contact page explains
      return 'mailto:' + this.SUPPORT_EMAIL + '?' + q.toString().replace(/\+/g, '%20');
    },
    get supportContext() {
      const bits = [];
      if (this.order?.ref) bits.push('Order ' + this.order.ref);
      if (this.design?.title) bits.push(this.design.title);
      return bits.join(' · ');
    },
    get metalsFaqAnswer() {
      const fashion = this.materialsOf('fashion').map(m => m.label).join(', ');
      const gold = this.materialsOf('luxury').map(m => m.label.replace(' Gold', '')).join(', ');
      return `Fashion jewelry: ${fashion}. Luxury: solid gold in ${gold} — luxury pieces can be previewed but are not yet available to order.`;
    },

    // ── compose / new design ──────────────────────────────────────────
    requestUpload() { this.$refs.uploader?.click(); },
    handleUpload(ev) {
      const f = ev.target.files[0];
      ev.target.value = '';
      if (!f) return;
      this.setReference(f);
    },
    setReference(f) {
      this.uploadedFile = f;
      this.uploadedPreview = URL.createObjectURL(f);
      this.rightsConfirmed = false;
      this.composeError = '';
    },
    // Ctrl+V / Cmd+V on the Design screen: a pasted image (a screenshot, a copied picture) becomes the
    // reference image exactly as if it had been chosen with the upload button; pasted text is left to the
    // composer. Pastes meant for another field (the sign-in form, a search box) are never touched.
    handlePaste(ev) {
      if (this.view !== 'ai-studio' || ev.defaultPrevented) return;
      const t = ev.target;
      if (t && t !== this.$refs.composer && t.matches && t.matches('input, textarea, select, [contenteditable]')) return;
      const items = [...(ev.clipboardData?.items || [])];
      const file = items.map(i => i.kind === 'file' ? i.getAsFile() : null).find(f => f && /^image\/(png|jpeg|webp)$/.test(f.type));
      if (!file) return;
      ev.preventDefault();
      if (this.design) {
        this.composeError = 'A pasted image starts a new design: click New Design, then paste it again.';
        return;
      }
      this.setReference(new File([file], file.name && file.name !== 'image.png' ? file.name : 'pasted-image.' + (file.type.split('/')[1] === 'jpeg' ? 'jpg' : file.type.split('/')[1]), { type: file.type }));
      this.$nextTick(() => this.$refs.composer?.focus());
    },
    clearUpload() { this.uploadedFile = null; this.uploadedPreview = null; this.rightsConfirmed = false; },
    handleEnterKey(ev) { if (!ev.shiftKey) { ev.preventDefault(); this.sendComposer(); } },

    // ── the products on offer (Admin → Settings → Products). The catalog's products block is absent while rings are
    //    the only product on offer (the ring-only site); otherwise it lists what this browser may design. ──
    get productsList() { return this.catalog?.products?.available || ['ring']; },
    get productsOn() { return this.productsList.length > 1; },                 // a choice to make: selector, filters, badges
    get soleProduct() { return this.productsList.length === 1 ? this.productsList[0] : null; },
    get nothingOnOffer() { return !!this.catalog && this.productsList.length === 0; },
    get previewedProducts() { return this.catalog?.products?.previewed || []; },  // hidden products an Admin's browser sees
    get charmPreview() { return this.previewedProducts.length > 0; },
    get previewText() { return this.previewedProducts.map(p => this.productPlural(p).toLowerCase()).join(' and ') + ' are hidden from customers'; },
    productLabel(p) { return window.P3Products ? window.P3Products.label(p) : (p === 'charm' ? 'Charm' : 'Ring'); },
    productPlural(p) { return window.P3Products ? window.P3Products.plural(p) : (p === 'charm' ? 'Charms' : 'Rings'); },
    productIcon(p, cls = 'w-4 h-4') { return window.P3Products ? window.P3Products.icon(p, cls) : ''; },
    // Wording that follows the products on offer: "ring" / "charm" for a single product, "piece" for a choice
    get wording() {
      const sole = this.soleProduct, list = this.productsList;
      const noun = sole ? this.productLabel(sole).toLowerCase() : 'piece', nouns = sole ? this.productPlural(sole).toLowerCase() : 'pieces';
      return { noun, nouns, Noun: noun[0].toUpperCase() + noun.slice(1), Nouns: nouns[0].toUpperCase() + nouns.slice(1),
               list: list.map(p => this.productPlural(p)).join(' and '), ring: list.includes('ring'), charm: list.includes('charm'),
               sizeWord: sole === 'ring' ? 'ring size' : 'size' };
    },
    chooseProduct(p) {
      if (!this.productsList.includes(p)) return;
      this.newProduct = p; this.persist({ newProduct: p });
    },
    get newNoun() { return this.productLabel(this.productsList.includes(this.newProduct) ? this.newProduct : (this.productsList[0] || 'ring')).toLowerCase(); },
    get designLabel() { return this.isCharm(this.design) ? 'Charm' : 'Ring'; },
    get galleryFilters() { return [['', 'All'], ...this.productsList.map(p => [p, this.productPlural(p)])]; },
    get galleryIntro() {
      const tail = ' — real XJet designs. Open one to see it large; make it yours to start from its four options.';
      if (this.nothingOnOffer) return 'Real XJet designs. Open one to see it large.';
      return (this.productsOn ? this.wording.list : this.soleProduct === 'charm' ? 'Charms' : 'Bands, signets, statement and stackable rings') + tail;
    },
    get composerNote() {
      if (this.nothingOnOffer) return 'Nothing is available to design right now. Please check back soon.';
      return 'XJet Atelier makes ' + this.wording.nouns + ' — describe the ' + this.wording.noun + ' you have in mind.';
    },
    // The marketing, FAQ and terms sentences that name the product
    get faqWhat() {
      if (this.nothingOnOffer) return 'No product is available at the moment. Additional jewelry categories will be introduced in future updates.';
      if (this.productsOn) return this.wording.list + ': choose one when you start a design. Additional jewelry categories will be introduced in future updates.';
      return 'Currently, XJet Atelier supports ' + this.wording.Nouns + ' only. Additional jewelry categories will be introduced in future updates.';
    },
    get faqPrice() {
      const r = this.wording.ring, c = this.wording.charm;
      if (r && c) return 'Fashion jewelry pieces are priced per piece. A ring is priced by material — its size does not change the price. A charm is priced by material and size.';
      if (c) return 'Fashion jewelry pieces are priced per piece by material and size.';
      return 'Fashion jewelry pieces are priced per piece by material. Your ring size does not change the price.';
    },
    get faqUsd() {
      const r = this.wording.ring, c = this.wording.charm;
      const ring = 'Ring sizes are US sizes (see the size guide on the Customize screen)', charm = 'charm size is its total height in millimetres, including the loop at the top';
      return 'Yes — all prices are in US dollars.' + (r && c ? ' ' + ring + '; a ' + charm + '.' : r ? ' ' + ring + '.' : c ? ' A ' + charm + '.' : '');
    },
    get materialsTitle() { return 'Real metal, one fixed price per ' + this.wording.noun; },
    get materialsIntro() {
      const r = this.wording.ring, c = this.wording.charm;
      const how = r && c ? 'Fashion metals have a fixed price per piece — a ring’s price does not depend on its size; a charm is priced by material and size.'
                : c ? 'Fashion metals have a fixed price per charm, set by material and size — any design.'
                : 'Fashion metals have a fixed price per ring — any size, any design.';
      return 'Every ' + this.wording.noun + ' is printed to order in the metal you choose. ' + how + ' Gold is quoted individually.';
    },
    get materialsNote() {
      const r = this.wording.ring, c = this.wording.charm;
      const fixed = r && c ? 'Prices in US dollars, fixed for the material — a ring’s size does not change its price; a charm is priced by size.'
                  : c ? 'Prices in US dollars, per charm, fixed for the material and size.'
                  : 'Prices in US dollars, per ring, fixed for the material — the ring size does not change the price.';
      return fixed + ' Each ' + this.wording.noun + ' is made to order; the final weight is measured at production. Vermeil is sterling silver with a thick 14K gold plating.';
    },
    get goldIntro() { return 'Solid gold ' + this.wording.nouns + ' are priced per piece. Design your ' + this.wording.noun + ', choose a gold option in Customize and send a quote request — a specialist replies within one business day with a price and the next steps. Nothing is ordered or charged until you confirm.'; },
    get termsService() {
      const list = this.productsList, names = list.map(p => this.productLabel(p).toLowerCase()), plurals = list.map(p => this.productPlural(p).toLowerCase());
      const idea = names.length ? names.map(n => 'a ' + n).join(' or ') : 'a piece of jewelry';
      const supports = plurals.length > 1 ? plurals.join(' and ') : plurals.length === 1 ? plurals[0] + ' only' : 'no products at the moment';
      return 'XJet Atelier, operated by XJet Ltd., lets you describe or upload an idea for ' + idea + ', generates four design options and a 360° preview with AI, and lets you configure the metal and size of the design you choose. The current release supports ' + supports + '.';
    },
    get termsOrders() {
      const r = this.wording.ring, c = this.wording.charm;
      const fixed = r && c ? 'fixed per piece and material (a charm’s price also depends on its size)' : c ? 'fixed per charm, material and size' : 'fixed per ring and material';
      return 'Prices are in US dollars and are ' + fixed + '. An order is a reservation until our team has reviewed the design for production feasibility and confirmed it to you; payment is arranged after you place the order. Each ' + this.wording.noun + ' is made to order (design and size) and is not returnable for change of mind; your design is not exclusive. Gold ' + this.wording.nouns + ' are quoted individually.';
    },
    get sizingHelp() {
      const r = this.wording.ring, c = this.wording.charm;
      const ring = 'Not sure about your ring size? Use the size guide on the Customize screen (tap any row to pick a size) or send us your finger circumference in mm.';
      const charm = 'A charm size is its total height in millimetres, including the loop — ask us if you are unsure which size suits you.';
      return [r ? ring : '', c ? charm : ''].filter(Boolean).join(' ') || 'Ask us anything about sizes and a specialist will help.';
    },
    // The bag's and an order's wording: "ring" when every line is a ring, "charm" — or "piece(s)" for a mix
    kindOf(types, count = 1) {
      const set = [...new Set((types || []).filter(Boolean))];
      if (count === 1) return set.length === 1 ? this.productLabel(set[0]).toLowerCase() : 'piece';
      return set.length === 1 ? this.productPlural(set[0]).toLowerCase() : 'pieces';
    },
    get bagKind() { return this.kindOf(this.bagLines.map(l => l.product_type || 'ring'), 1); },
    get bagReserved() { const n = this.bagLines.length || 1; return 'Your ' + this.kindOf(this.bagLines.map(l => l.product_type || 'ring'), n) + (n > 1 ? ' are' : ' is') + ' reserved'; },
    orderMade(o) { const n = o?.count || 1; return 'Your ' + this.kindOf(o?.product_types || ['ring'], n) + (n > 1 ? ' are' : ' is') + ' printed to order in real metal — 7–10 business days.'; },
    isCharm(x) { return (x?.product_type || 'ring') === 'charm'; },
    get custIsCharm() { return this.isCharm(this.cust); },
    charmMaterialLabel(id) { return (this.catalog?.products?.charm?.materials || []).find(m => m.id === id)?.label; },
    matName(m) { return this.custIsCharm ? (this.charmMaterialLabel(m.id) || m.label) : m.label; },   // "Sterling Silver" for a charm
    get currentMaterialLabel() {
      return this.custIsCharm ? (this.charmMaterialLabel(this.cust?.material_id) || this.cust?.material_label || this.currentMaterial?.label)
                              : this.currentMaterial?.label;
    },
    get galleryShown() {
      const f = this.productsOn && this.productsList.includes(this.galleryProduct) ? this.galleryProduct : '';
      return f ? this.gallery.filter(g => (g.product_type || 'ring') === f) : this.gallery;
    },
    // Lines of the bag, checkout and orders: "US 7" for a ring (as before), "20 mm" for a charm
    lineSize(l) { return this.isCharm(l) ? (l.size_label || 'size to be confirmed') : 'US ' + l.ring_size; },
    lineIdLabel(l) { return this.isCharm(l) ? 'Charm ID' : 'Ring ID'; },
    orderCountText(o) {
      const p = o.product_types || ['ring'], noun = p.length > 1 ? 'piece' : (p[0] === 'charm' ? 'charm' : 'ring');
      return o.count + ' ' + noun + (o.count === 1 ? '' : 's');
    },
    scrollToSize() { document.getElementById(this.custIsCharm ? 'charm-size-heading' : 'ring-size-heading')?.scrollIntoView?.({ behavior: 'smooth', block: 'center' }); },

    get composerMode() { return this.design ? 'refine' : 'create'; },
    get composerPlaceholder() {
      if (!this.design) return this.uploadedFile ? 'Describe what to make from this image… (optional)' : 'Describe your ' + this.newNoun + '… or paste an image (Ctrl+V)';
      return this.canActOnSelection ? 'Describe how to refine the selected design…' : 'Select one of the designs above to refine it…';
    },
    get canSend() {
      if (this.submitting) return false;
      if (this.composerMode === 'create') return this.userInput.trim().length > 0 || !!this.uploadedFile;
      return this.canActOnSelection && !this.pendingBatch && this.userInput.trim().length > 0;
    },

    async sendComposer() {
      if (this.composerMode === 'refine') return this.refine();
      return this.generate();
    },

    async generate() {
      this.composeError = '';
      const text = this.userInput.trim() || (this.uploadedFile ? 'Process this image' : '');
      if (text.length < 3) { this.composeError = 'Please describe your ' + this.newNoun + ' in a few words.'; return; }
      if (this.uploadedFile && !this.rightsConfirmed) { this.composeError = 'Please confirm you have the rights to use the uploaded image.'; return; }
      if (this.nothingOnOffer) { this.composeError = 'Nothing is available to design right now.'; return; }
      const fd = new FormData();
      fd.append('prompt', text);
      fd.append('client_request_id', newRequestId());
      if (this.uploadedFile) { fd.append('reference', this.uploadedFile); fd.append('rights_confirmed', 'true'); }
      if (this.catalog?.products) fd.append('product', this.newProduct);   // named whenever the catalog lists products (not the ring-only site)
      this.submitting = true;
      try {
        const batch = await this.api('POST', '/api/designs', fd);
        this.openedFromList = false; this.candIndex = 0;
        this.userInput = ''; this.clearUpload();
        await this.openDesign(batch.design_id);
        this.loadDesigns();
      } catch (e) {
        this.composeError = e.message;          // prompt and reference are kept for retry
      } finally {
        this.submitting = false;
      }
    },

    startNew() {
      // Clears the active selection/associations only. Saved designs and the bag are untouched.
      this.track('new_design_clicked');
      this.stopPolling();
      this.design = null; this.cust = null; this.viewBatchId = null; this.pendingRefineBatchId = null;
      this.actionError = ''; this.composeError = ''; this.closePreview();
      this.userInput = ''; this.clearUpload();
      this.persist({ designId: null, view: 'ai-studio' });
      if (this.view !== 'ai-studio' && STUDIO_VIEWS.includes(this.view)) this.view = 'ai-studio';
    },

    // ── design state ──────────────────────────────────────────────────
    async openDesign(designId, { restoreView } = {}) {
      this.stopPolling();
      const d = await this.api('GET', `/api/designs/${designId}`);
      this.design = d; this.cust = null; this.pendingRefineBatchId = null;
      this.openedFromList = true; this.candIndex = 0;
      this.actionError = ''; this.composeError = '';
      const last = d.batches[d.batches.length - 1];
      // Show the newest batch that has something to show; a still-running refinement is shown as progress.
      const shown = [...d.batches].reverse().find(b => !this.anyActive(b)) || last;
      this.viewBatchId = shown.id;
      if (last.id !== shown.id) this.pendingRefineBatchId = last.id;
      this.persist({ designId });
      if (restoreView === 'review' && d.customization) {
        this.showCustomization(d.customization);
      } else if (['checkout', 'order', 'confirmation'].includes(restoreView)) {
        this.navigateTo('checkout');                      // a reload lands on the bag, never mid-order
      } else {
        this.navigateTo('ai-studio');
      }
      if (window.innerWidth < 1024) this.sidebarOpen = false;
      this.ensurePolling();
    },

    batch(id) { return this.design?.batches.find(b => b.id === id); },
    get viewBatch() { return this.batch(this.viewBatchId); },
    // Phone: the four options are a swipeable row; the dots follow the scroll position
    candScroll(ev) {
      const el = ev.target, first = el.querySelector('.cand-card');      // (the first child is Alpine's template)
      if (!first) return;
      const step = first.getBoundingClientRect().width + 12;
      this.candIndex = Math.max(0, Math.min((this.viewBatch?.candidates || []).length - 1, Math.round(el.scrollLeft / step)));
    },
    scrollCand(i) {
      const el = this.$refs.candRow, card = el?.querySelectorAll('.cand-card')[i];
      if (card) el.scrollTo({ left: card.offsetLeft - 16, behavior: 'smooth' });
    },
    get pendingBatch() { return this.batch(this.pendingRefineBatchId); },
    get visibleBatches() { return (this.design?.batches || []).filter(b => b.id !== this.pendingRefineBatchId); },
    get selectedId() { return this.design?.selected_candidate_id || null; },
    get selectedCandidate() {
      for (const b of this.design?.batches || []) for (const c of b.candidates) if (c.id === this.selectedId) return c;
      return null;
    },
    get canActOnSelection() { return this.selectedCandidate?.status === 'ready'; },
    readyCount(b) { return b ? b.candidates.filter(c => c.status === 'ready').length : 0; },
    batchLabel(b) {
      if (b.kind === 'initial') return 'Original';
      const n = this.design.batches.filter(x => x.kind === 'refine').indexOf(b) + 1;
      return `Variation ${n}`;
    },
    // Credits: 1 per design request, 1 per refinement request, 1 per extra option, 1 per finished 360° movie (the server's tariff)
    get quotaText() { return `${this.quotaRemaining} of ${this.quotaMax} credits remaining`; },
    anyActive(b) { return !!b && (b.status === 'queued' || b.status === 'generating'); },
    // Muted autoplay can still be limited by the browser (e.g. Edge "Limit media autoplay", power
    // saving): set the muted property, call play(), and if it is refused retry on the first interaction.
    startLoadingMovie(el) {
      el.muted = true; el.defaultMuted = true;
      const tryPlay = () => el.play().catch(() => {
        const again = () => { if (el.isConnected) el.play().catch(() => {}); };
        window.addEventListener('pointerdown', again, { once: true });
        window.addEventListener('keydown', again, { once: true });
      });
      if (el.readyState >= 2) tryPlay(); else el.addEventListener('canplay', tryPlay, { once: true });
    },
    // The P2 waiting movie is shown while a new design or a refinement is being generated.
    get waitBatch() {
      if (this.pendingBatch) return this.pendingBatch;
      return this.anyActive(this.viewBatch) ? this.viewBatch : null;
    },
    get waitVariant() { return this.waitBatch ? (this.waitBatch.kind === 'refine' ? 'refine' : 'design') : null; },
    get waitPreset() { return WAIT_PRESETS[this.waitVariant || 'design']; },
    get waitStatus() { const s = this.waitPreset.statuses; return s[this.waitStatusIndex % s.length]; },
    get bubbleBatch() { return this.pendingBatch || this.viewBatch; },
    get viewBatchGenerating() { return this.anyActive(this.viewBatch) && this.readyCount(this.viewBatch) === 0; },

    ensurePolling() {
      if (this._poll) return;
      const designId = this.design?.id;
      const tick = async () => {
        if (!this.design || this.design.id !== designId) { this.stopPolling(); return; }
        if (document.hidden) return;                       // a background tab asks nothing; the next tick after it returns does
        const active = this.design.batches.filter(b => this.anyActive(b));
        if (!active.length) { this.stopPolling(); return; }
        for (const b of active) {
          try {
            const fresh = await this.api('GET', `/api/batches/${b.id}`);
            // Stale-result guard: only apply to the design this poll belongs to.
            if (!this.design || this.design.id !== designId || fresh.design_id !== designId) return;
            const idx = this.design.batches.findIndex(x => x.id === fresh.id);
            if (idx >= 0) this.design.batches.splice(idx, 1, fresh);
            this.onBatchUpdated(fresh);
          } catch (e) { /* transient: next tick retries the same read */ }
        }
      };
      this._poll = setInterval(tick, 1500);
      tick();
    },
    stopPolling() { if (this._poll) { clearInterval(this._poll); this._poll = null; } },

    onBatchUpdated(b) {
      if (!this.anyActive(b)) this._refreshQuota();
      // Refresh My Designs thumbnails once a batch has images to show.
      if (this.sidebarOpen && !this.anyActive(b)) this.loadDesigns();
      if (b.id === this.pendingRefineBatchId && !this.anyActive(b)) {
        this.pendingRefineBatchId = null;
        if (b.status === 'failed') this.actionError = 'The refinement could not be generated. You can retry the failed designs.';
        // Switch to the new batch so the user can choose again; the earlier batch stays in history.
        this.viewBatchId = b.id;
        this.setSelection(null);
      }
    },

    async select(c) {
      if (!c || c.status !== 'ready') return;
      await this.setSelection(c.id);
    },
    async setSelection(candidateId) {
      if (!this.design) return;
      const designId = this.design.id;
      const prev = this.design.selected_candidate_id;
      this.design.selected_candidate_id = candidateId;          // optimistic
      try {
        await this.api('PUT', `/api/designs/${designId}/selection`, { candidate_id: candidateId });
      } catch (e) {
        if (this.design?.id === designId) { this.design.selected_candidate_id = prev; this.actionError = e.message; }
      }
    },

    async retrySlot(c) {
      this.actionError = '';
      try {
        const b = await this.api('POST', `/api/candidates/${c.id}/retry`);
        this.replaceBatch(b);
        this.ensurePolling();
      } catch (e) { this.actionError = e.message; }
    },
    async retryBatch(b) {
      this.actionError = '';
      try {
        this.replaceBatch(await this.api('POST', `/api/batches/${b.id}/retry-failed`));
        this.ensurePolling();
      } catch (e) { this.actionError = e.message; }
    },
    replaceBatch(b) {
      const idx = this.design?.batches.findIndex(x => x.id === b.id);
      if (idx >= 0) this.design.batches.splice(idx, 1, b);
    },

    // ── refine ────────────────────────────────────────────────────────
    focusRefine() {
      this.actionError = '';
      if (!this.canActOnSelection) { this.actionError = 'Select one of the designs to refine it.'; return; }
      if (this.userInput.trim().length >= 3) { this.refine(); return; }
      this.$refs.composer?.focus();
    },
    async refine() {
      this.composeError = '';
      if (!this.canActOnSelection) { this.composeError = 'Select one of the designs to refine it.'; return; }
      const text = this.userInput.trim();
      if (text.length < 3) { this.composeError = 'Describe the change you want.'; return; }
      const designId = this.design.id;
      this.submitting = true;
      try {
        const b = await this.api('POST', `/api/designs/${designId}/batches`, {
          parent_candidate_id: this.selectedId, instruction: text, client_request_id: newRequestId(),
        });
        if (this.design?.id !== designId) return;
        if (b.design_id !== designId) {           // a shared gallery design, or one that already has a movie / 3D / order: a design of your own
          const was = this.design?.title, own = !this.design?.shared;
          this.userInput = '';
          await this.openDesign(b.design_id);
          this.openedFromList = false;           // just typed: the refinement's prompt echo shows
          this.loadDesigns();
          if (own && this.design?.title) this.showToast(`Saved as a new design “${this.design.title}” — “${was}” stays as it is`, { ms: 7000 });
          return;
        }
        this.design.batches.push(b);
        this.openedFromList = false; this.candIndex = 0;
        this.pendingRefineBatchId = b.id;      // keep the current grid until the new batch is ready
        this.userInput = '';
        this.ensurePolling();
      } catch (e) {
        this.composeError = e.message;         // e.g. reference_unavailable — no text-only fallback
      } finally {
        this.submitting = false;
      }
    },

    // ── my designs sidebar ────────────────────────────────────────────
    async loadDesigns() {
      if (!this.token) return;
      this.designsLoading = true; this.designsError = '';
      try { this.designs = (await this.api('GET', '/api/designs')).designs; }
      catch (e) { this.designsError = e.message; }
      finally { this.designsLoading = false; }
    },
    // Remove from My Designs: a design of your own is hidden, a shared gallery design is unlinked
    // (it can be started again from the gallery). The bag is not affected.
    async removeDesign(p) {
      const msg = p.shared ? `Remove "${p.title}" from My Designs?\n\nYou can start it again from the Inspiration Gallery at any time.`
                           : `Remove "${p.title}" from My Designs?\n\nYour bag is not affected.`;
      if (!confirm(msg)) return;
      try {
        await this.api('DELETE', `/api/designs/${encodeURIComponent(p.id)}`);
        this.designs = this.designs.filter(d => d.id !== p.id);
        if (this.design?.id === p.id) {
          this.stopPolling();
          this.design = null; this.cust = null; this.viewBatchId = null; this.pendingRefineBatchId = null;
          this.actionError = ''; this.composeError = ''; this.closePreview();
          this.persist({ designId: null });
          if (STUDIO_VIEWS.includes(this.view)) this.navigateTo('ai-studio');
        }
      } catch (e) { this.designsError = e.message; }
    },
    get filteredDesigns() {
      const q = this.projectSearch.trim().toLowerCase();
      return q ? this.designs.filter(d => d.title.toLowerCase().includes(q)) : this.designs;
    },
    toggleSidebar() { this.sidebarOpen = !this.sidebarOpen; if (this.sidebarOpen) this.loadDesigns(); },

    // ── preview overlay: one option at a time, large; arrows / keys / swipe step through the four ──
    openPreview(media, src, candidate = null) {
      this.previewMedia = media; this.previewSrc = src; this.previewCandidate = candidate;
      this.previewResetZoom(); this.previewOpen = true;
    },
    closePreview() { this.previewOpen = false; this.previewCandidate = null; },
    get previewSiblings() {
      if (!this.previewCandidate) return [];
      const b = this.batch(this.previewCandidate.batch_id);
      return b ? b.candidates.filter(c => c.status === 'ready') : [];
    },
    get previewIndex() { return this.previewSiblings.findIndex(c => c.id === this.previewCandidate?.id); },
    previewStep(delta) {
      const s = this.previewSiblings; if (s.length < 2) return;
      const c = s[(this.previewIndex + delta + s.length) % s.length];
      this.previewCandidate = c; this.previewSrc = c.image_url; this.previewResetZoom();
    },
    previewShow(c) { if (c?.status === 'ready') { this.previewCandidate = c; this.previewSrc = c.image_url; this.previewResetZoom(); } },
    previewKey(ev) {
      if (!this.previewOpen || this.previewMedia !== 'image') return;
      if (ev.key === 'ArrowRight') this.previewStep(1);
      if (ev.key === 'ArrowLeft') this.previewStep(-1);
    },
    // Swipe (phone): a horizontal move of 50 px at 1× zoom steps to the next / previous option.
    previewSwipeStart(ev) { this._swipeX = this.previewZoom <= 1 ? ev.clientX : null; },
    previewSwipeEnd(ev) {
      if (this._swipeX === null || this.previewZoom > 1) { this._swipeX = null; return; }
      const dx = ev.clientX - this._swipeX; this._swipeX = null;
      if (Math.abs(dx) > 50) this.previewStep(dx < 0 ? 1 : -1);
    },
    previewZoomBy(f) {
      this.previewZoom = Math.min(6, Math.max(1, this.previewZoom * f));
      if (this.previewZoom === 1) { this.previewPanX = 0; this.previewPanY = 0; }
    },
    previewResetZoom() { this.previewZoom = 1; this.previewPanX = 0; this.previewPanY = 0; },
    previewPanDown(ev) {
      if (this.previewZoom <= 1) { this.previewSwipeStart(ev); return; }   // 1×: a swipe steps, a tap on the image zooms (see previewTap)
      this._panning = true; this._panStart = { x: ev.clientX - this.previewPanX, y: ev.clientY - this.previewPanY };
      ev.target.setPointerCapture?.(ev.pointerId);
    },
    previewPanMove(ev) {
      if (!this._panning) return;
      this.previewPanX = ev.clientX - this._panStart.x; this.previewPanY = ev.clientY - this._panStart.y;
    },
    previewPanEnd(ev) {
      if (this._panning) { this._panning = false; return; }
      if (this._swipeX !== null && ev) {
        const dx = ev.clientX - this._swipeX; this._swipeX = null;
        if (Math.abs(dx) > 50) { this.previewStep(dx < 0 ? 1 : -1); return; }
        this.previewZoomBy(2);                                            // a tap: zoom in
      }
    },
    async selectFromPreview() { if (this.previewCandidate) await this.select(this.previewCandidate); },

    // ── customize ─────────────────────────────────────────────────────
    async proceed() {
      if (!this.canActOnSelection) { this.actionError = 'Select one of the designs to customize it.'; return; }
      this.actionError = '';
      const designId = this.design.id, candidateId = this.selectedId;
      // Show the selected image immediately; the server returns the authoritative customization.
      this.cust = { candidate_id: candidateId, image_url: this.selectedCandidate.image_url, material_id: 'silver',
                    quote: null, movie: { status: 'queued' }, ring_size: null, quantity: 1,
                    product_type: this.design?.product_type || 'ring', charm_size: null };
      this.mediaTab = 'movie'; this.custError = ''; this.bagMessage = '';
      this.groupOpen = { fashion: true, luxury: false };
      this.navigateTo('review');
      try {
        const c = await this.api('POST', `/api/designs/${designId}/customize`, { candidate_id: candidateId });
        if (this.design?.id !== designId || this.cust?.candidate_id !== candidateId) return;
        this.showCustomization(c);
      } catch (e) {
        this.custError = e.message;
      }
    },

    showCustomization(c) {
      const same = this.cust?.id && this.cust.id === c.id;
      this.cust = c;
      // A ring opens on a suggested size until the customer confirms it; a charm has no suggestion — its size was chosen
      if (!same) this.sizeConfirmed = this.isCharm(c) && c.charm_size != null;
      this.mediaTab = 'movie';          // the movie is the default Customize view
      const g = this.groupOfMaterial(c.material_id) || 'fashion';
      this.lastMaterialByGroup[g] = c.material_id;
      this.groupOpen = { fashion: g === 'fashion', luxury: g === 'luxury' };
      if (this.view !== 'review') this.navigateTo('review');
      this.pollCustomization();
    },

    pollCustomization() {
      if (this._pollCust) clearInterval(this._pollCust);
      const custId = this.cust?.id;
      const tick = async () => {
        if (!this.cust || this.cust.id !== custId || this.view !== 'review') { clearInterval(this._pollCust); this._pollCust = null; return; }
        const st = this.cust.movie?.status;
        if (st !== 'queued' && st !== 'running') { clearInterval(this._pollCust); this._pollCust = null; return; }
        try {
          const fresh = await this.api('GET', `/api/customizations/${custId}`);
          if (this.cust?.id === custId) {
            if (fresh.movie?.status === 'ready' && this.cust.movie?.status !== 'ready') this._refreshQuota();
            this.cust.movie = fresh.movie;
          }
        } catch { /* retry next tick */ }
      };
      this._pollCust = setInterval(tick, 2000);
    },

    async retryMovie() {
      if (!this.cust) return;
      const custId = this.cust.id;
      this.custError = '';
      try {
        const m = await this.api('POST', `/api/candidates/${this.cust.candidate_id}/movie`);
        if (this.cust?.id === custId) { this.cust.movie = m; this.mediaTab = 'movie'; this.pollCustomization(); }
      } catch (e) { this.custError = e.message; }
    },

    get movieOff() { return !!this.cust && this.cust.movie_available === false; },   // the product's movie switch is OFF: the still image only
    get movieStatus() { return this.cust?.movie?.status || null; },
    get movieBusy() { return this.movieStatus === 'queued' || this.movieStatus === 'running'; },
    get movieFailed() { return this.movieStatus === 'failed' || this.movieStatus === 'interrupted'; },
    get currentGroup() { return this.groupOfMaterial(this.cust?.material_id) || 'fashion'; },
    get currentMaterial() { return this.material(this.cust?.material_id); },
    get tint() { return this.metalFilter(this.cust?.material_id); },
    // Metal colour preview. The former CSS filters (sepia/hue-rotate) recoloured the whole picture,
    // white background included. Each material now has an SVG filter that (1) maps the image's
    // luminance onto that metal's colour ramp (shadow → base swatch → highlight) and (2) applies it
    // only where the image is not near-white, so the studio background stays white.
    metalFilter(id) { return this.material(id) ? `url(#p3-metal-${id})` : ''; },
    get metalFilterDefs() { return P3MetalFilterDefs(this.allMaterials); },     // web/metal.js

    toggleGroup(groupId) {
      // Selecting a group reveals its options and selects that group's last-used option;
      // clicking the active group again just collapses/expands it.
      if (this.currentGroup === groupId) { this.groupOpen[groupId] = !this.groupOpen[groupId]; return; }
      this.groupOpen = { fashion: groupId === 'fashion', luxury: groupId === 'luxury' };
      const target = this.lastMaterialByGroup[groupId] || this.materialsOf(groupId)[0]?.id;
      if (target) this.chooseMaterial(target);
    },

    async chooseMaterial(materialId) {
      if (!this.cust?.id || this.cust.material_id === materialId) return;
      const custId = this.cust.id;
      const g = this.groupOfMaterial(materialId);
      this.lastMaterialByGroup[g] = materialId;
      this.groupOpen = { fashion: g === 'fashion', luxury: g === 'luxury' };
      // Never leave a previous fashion price visible while the new quote loads.
      this.cust.material_id = materialId;
      this.cust.quote = null; this.cust.line_total = null; this.cust.can_add_to_bag = false;
      this.quotePending = true; this.bagMessage = '';
      await this.patchCustomization(custId, { material_id: materialId });
    },

    async setSize(v) {
      if (!this.cust?.id) return;
      const size = v === '' || v === null ? null : Number(v);
      this.sizeConfirmed = size !== null;
      // Confirming the suggested size is still a choice: the server records it (customization_changed).
      await this.patchCustomization(this.cust.id, { [this.custIsCharm ? 'charm_size' : 'ring_size']: size }, { force: true });
    },
    get canAddToBag() { return !!this.cust?.can_add_to_bag && this.sizeConfirmed && !this.quotePending; },
    async setQuantity(delta) {
      if (!this.cust?.id) return;
      const q = Math.min(10, Math.max(1, (this.cust.quantity || 1) + delta));
      if (q !== this.cust.quantity) await this.patchCustomization(this.cust.id, { quantity: q });
    },

    async patchCustomization(custId, patch, opts = {}) {
      this.custError = '';
      try {
        const fresh = await this.api('PATCH', `/api/customizations/${custId}`, patch);
        if (this.cust?.id !== custId) return;
        // Ignore responses for a material the user has already moved away from.
        if (patch.material_id && this.cust.material_id !== patch.material_id) return;
        const movie = this.cust.movie;
        this.cust = { ...fresh, movie: fresh.movie_available === false ? null : (fresh.movie || movie) };
      } catch (e) {
        this.custError = e.message;
      } finally {
        this.quotePending = false;
      }
    },

    get isLuxury() { return this.currentGroup === 'luxury'; },
    get priceAvailable() { return !this.isLuxury && this.cust?.quote?.pricing_status === 'available'; },
    get priceText() {
      if (this.isLuxury) return 'Price unavailable';
      if (this.custIsCharm && this.cust?.charm_size == null) return 'Choose a size';     // a charm's price depends on its size
      const q = this.cust?.quote;
      if (!q) return '—';
      if (q.pricing_status !== 'available') return 'Price unavailable';
      return this.money(q.unit_price, q.currency);
    },
    money(v, cur = 'USD') {
      return new Intl.NumberFormat('en-US', { style: 'currency', currency: cur }).format(v);
    },
    get bagButtonText() {
      if (!this.cust || this.quotePending) return 'Updating price…';
      switch (this.cust.add_to_bag_blocked_reason) {
        case 'luxury_preview_only': return 'Request a quote';
        case 'price_unavailable': return 'Price Unavailable';
        case 'ring_size_required': return 'Choose your ring size';
        case 'charm_size_required': return 'Choose your charm size';
        case 'charm_size_not_offered': return 'Choose another size';
        default: return this.sizeConfirmed ? 'Add to Bag' : (this.custIsCharm ? 'Confirm your charm size' : 'Confirm your ring size');
      }
    },
    get bagBlockedText() {
      if (!this.cust) return '';
      switch (this.cust.add_to_bag_blocked_reason) {
        case 'luxury_preview_only': return this.custIsCharm ? 'Gold charms are quoted individually.' : 'Gold rings are quoted individually.';
        case 'price_unavailable': return 'Price unavailable — this piece cannot be added to the bag yet.';
        case 'ring_size_required': return 'Choose your ring size to continue.';
        case 'charm_size_required': return 'Choose your charm size to continue.';
        case 'charm_size_not_offered': return 'This size is no longer offered — please choose another size.';
        default: return this.sizeConfirmed ? '' : (this.custIsCharm ? 'Tap your charm size above — the recommended size is only a suggestion.' : 'Tap your ring size above — the suggested size is only a suggestion.');
      }
    },
    // The suggested size: a ring's US 10 (or its stored size), a charm's recommended size (e.g. 14 mm — Classic) until confirmed
    sizeIsSuggested(size) {
      if (this.sizeConfirmed) return false;
      return (this.custIsCharm ? (this.cust?.charm_size ?? this.cust?.recommended_size) : this.cust?.ring_size) == size;
    },
    suggestedCharmSize() { return this.custIsCharm && !this.sizeConfirmed ? (this.cust?.charm_size ?? this.cust?.recommended_size ?? null) : null; },
    suggestedCharmLabel() {
      const s = this.suggestedCharmSize(), o = (this.cust?.charm_sizes || []).find(x => x.size == s);
      return o ? o.label.replace(' · Recommended', '') : (s != null ? s + ' mm' : '');
    },
    // Sticky purchase summary (Customize): "$200 · US 10 · Silver"
    get purchaseSummary() {
      const size = this.custIsCharm ? (this.cust?.size_label || 'Size?') : (this.cust?.ring_size != null ? 'US ' + this.cust.ring_size : 'Size?');
      return [this.priceText, size, this.currentMaterialLabel || ''].filter(Boolean).join(' · ');
    },
    sizeGuideRows(kind) {
      const mm = { 4: '14.9', 4.5: '15.3', 5: '15.7', 5.5: '16.1', 6: '16.5', 6.5: '16.9', 7: '17.3', 7.5: '17.7', 8: '18.1',
                   8.5: '18.5', 9: '19.0', 9.5: '19.4', 10: '19.8', 10.5: '20.2', 11: '20.6', 11.5: '21.0', 12: '21.4' };
      const circ = { 4: '46.8', 4.5: '48.1', 5: '49.3', 5.5: '50.6', 6: '51.9', 6.5: '53.1', 7: '54.4', 7.5: '55.6', 8: '57.0',
                     8.5: '58.1', 9: '59.5', 9.5: '60.9', 10: '62.1', 10.5: '63.5', 11: '64.6', 11.5: '66.0', 12: '67.2' };
      const uk = { 4: 'H 1/2', 4.5: 'I 1/2', 5: 'J 1/2', 5.5: 'K 1/2', 6: 'L 1/2', 6.5: 'M 1/2', 7: 'O', 7.5: 'P', 8: 'Q',
                   8.5: 'R', 9: 'S', 9.5: 'T', 10: 'U', 10.5: 'V', 11: 'W', 11.5: 'X', 12: 'Y' };
      const de = { 4: '15', 4.5: '15.25', 5: '15.75', 5.5: '16', 6: '16.5', 6.5: '16.75', 7: '17.25', 7.5: '17.75', 8: '18',
                   8.5: '18.5', 9: '19', 9.5: '19.5', 10: '19.75', 10.5: '20.25', 11: '20.5', 11.5: '21', 12: '21.5' };
      return (this.catalog?.ring_sizes.values || []).map(us => ({ us, m: (kind === 'ring' ? mm : circ)[us], uk: uk[us], de: de[us] }));
    },
    pickGuideSize(us) { this.setSize(us); this.showSizeGuide = false; },

    async addToBag() {
      if (!this.cust?.can_add_to_bag) return;
      if (!this.sizeConfirmed) {                       // the suggested size must be confirmed on purpose
        this.bagMessage = ''; this.custError = this.custIsCharm ? 'Please choose your charm size first — tap the size you want.'
                                                                 : 'Please confirm your ring size first — tap the size you want.';
        this.scrollToSize();
        return;
      }
      this.bagMessage = ''; this.custError = '';
      try {
        this.bag = await this.api('POST', '/api/bag', { customization_id: this.cust.id });
        this.bagMessage = 'Added to your bag.';
        this.showToast('Added to your bag', { label: 'View bag', action: () => this.goToCheckout() });
      } catch (e) { this.custError = e.message; }
    },

    backToDesign() { this.navigateTo('ai-studio'); },

    // ── bag ───────────────────────────────────────────────────────────
    get bagLines() { return this.bag?.lines || []; },
    get bagCount() { return this.bagLines.reduce((n, l) => n + l.quantity, 0); },
    async refreshBag() { if (!this.token) return; try { this.bag = await this.api('GET', '/api/bag'); } catch { /* ignore */ } },
    async removeLine(l) {
      try { this.bag = await this.api('DELETE', `/api/bag/${l.id}`); this.showToast('Removed from your bag'); }
      catch (e) { this.custError = e.message; }
    },
    goToCheckout() { this.navigateTo('checkout'); },

    // ── checkout: Bag → details → shipping → review & pay → confirmation ──
    // The server is authoritative for prices, promo discounts, totals and validation; the browser
    // only mirrors the rules for instant feedback. Known profile details pre-fill the form but never
    // overwrite what the customer typed.
    get checkoutSteps() { return [['details', 'Your details'], ['shipping', 'Shipping'], ['review', 'Review & place order']]; },
    // The truth about a confirmation email: only "sent" / "pending" mean one is (or is about to be) on its way
    emailOnItsWay(state) { const s = (state && state.status) || state; return s === 'sent' || s === 'pending'; },
    emailText(state, to) {
      const s = (state && state.status) || state;
      if (s === 'sent') return `A confirmation email has been sent to ${to}.`;
      if (s === 'pending') return `A confirmation email is on its way to ${to}.`;
      if (s === 'failed') return `We could not send a confirmation email to ${to} — please keep your reference.`;
      return 'Email confirmations are not active yet — please keep your reference.';
    },
    get coStepIndex() { return this.checkoutSteps.findIndex(s => s[0] === this.co.step); },
    get coCountry() { return (this.checkout?.countries || []).find(c => c.code === this.co.address.country); },
    get coRegionRequired() { return (this.checkout?.region_required || []).includes(this.co.address.country); },
    get coNoPostal() { return (this.checkout?.no_postal_code || []).includes(this.co.address.country); },
    get coRegionLabel() { return { US: 'State', CA: 'Province', AU: 'State / territory', JP: 'Prefecture' }[this.co.address.country] || 'State / province / region'; },
    get coPostalLabel() { return this.co.address.country === 'US' ? 'ZIP code' : 'Postal code'; },
    get coShipping() { return (this.checkout?.shipping_options || []).find(s => s.id === this.co.shipping_method); },
    get coCanPlace() { return !!this.coQuote?.ok && this.co.terms === true && !this.co.busy && !this.coQuote?.promo_error; },
    coProblem(field) { return this.co.problems[field] || ''; },
    _setProblems(list) { this.co.problems = Object.fromEntries((list || []).map(p => [p.field, p.message])); },
    _scrollTop() { window.scrollTo({ top: 0 }); document.querySelector('main')?.scrollTo?.({ top: 0 }); },
    async startCheckout() {
      if (!this.bag?.checkout_available || this.co.busy) return;
      this.track('checkout_clicked');
      this.co.busy = true; this.co.error = '';
      try {
        const info = await this.api('GET', '/api/checkout');
        this.checkout = info; this.coQuote = info.quote;
        const c = this.co.customer, p = info.customer || {};
        for (const k of CUSTOMER_FIELDS) if (!c[k] && p[k]) c[k] = p[k];
        if (!this.co.address.recipient) this.co.address.recipient = [c.first_name, c.last_name].filter(Boolean).join(' ');
        if (!this.co.requestId) this.co.requestId = newRequestId();      // one order per checkout, however many clicks
        this.co.step = 'details'; this.co.problems = {}; this.co.addrCheck = null; this.co.useSuggested = false;
        this.navigateTo('order');
      } catch (e) { this.co.error = e.message; }
      finally { this.co.busy = false; }
    },
    coGoTo(step) {
      const idx = this.checkoutSteps.findIndex(s => s[0] === step);
      if (idx <= this.coStepIndex) { this.co.step = step; this.co.error = ''; this._scrollTop(); }
    },
    coBack() {
      if (this.co.step === 'details') { this.navigateTo('checkout'); return; }
      this.co.step = this.co.step === 'review' ? 'shipping' : 'details'; this.co.error = ''; this._scrollTop();
    },
    coNext() { return this.co.step === 'details' ? this.coDetailsNext() : this.co.step === 'shipping' ? this.coShippingNext() : this.placeOrder(); },
    coDetailsNext() {
      const c = this.co.customer, probs = [];
      for (const k of CUSTOMER_FIELDS) c[k] = String(c[k] || '').trim();
      if (!c.first_name) probs.push({ field: 'first_name', message: 'Please enter your first name.' });
      if (!c.last_name) probs.push({ field: 'last_name', message: 'Please enter your last name.' });
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(c.email)) probs.push({ field: 'email', message: 'Please enter a valid email address.' });
      if (!/^\+?[0-9 ()./-]{7,24}$/.test(c.phone) || (c.phone.match(/\d/g) || []).length < 7) probs.push({ field: 'phone', message: 'Please enter a phone number we can reach you on (with the country code).' });
      this._setProblems(probs);
      if (probs.length) return;
      if (!this.co.address.recipient) this.co.address.recipient = `${c.first_name} ${c.last_name}`;
      this.co.step = 'shipping'; this.co.error = ''; this._scrollTop();
    },
    async coShippingNext() {
      this.co.busy = true; this.co.error = '';
      try {
        const v = await this.api('POST', '/api/checkout/address', { address: this.co.address });
        this._setProblems(v.problems);
        if (v.problems.length) return;
        this.co.addrCheck = v.validation; this.co.useSuggested = false;
        // A validation service suggested a correction: the customer decides, nothing is replaced silently.
        if (v.validation.status === 'corrected' && v.validation.suggestion) return;
        await this.coReview();
      } catch (e) { this.co.error = e.message; }
      finally { this.co.busy = false; }
    },
    async coChooseAddress(useSuggested) {
      this.co.useSuggested = useSuggested;
      if (useSuggested) Object.assign(this.co.address, this.co.addrCheck.suggestion);
      this.co.addrCheck = { ...this.co.addrCheck, suggestion: null };
      await this.coReview();
    },
    async coReview() { await this.coRequote(); this.co.step = 'review'; this._scrollTop(); },
    async coRequote() {
      try { this.coQuote = await this.api('POST', '/api/checkout/quote', { promo_code: this.co.promo_code || null, shipping_method: this.co.shipping_method }); }
      catch (e) { this.co.error = e.message; }
    },
    async applyPromo() {
      const code = this.co.promo_input.trim();
      if (!code) return;
      this.co.promo_code = code; this.co.busy = true;
      try { await this.coRequote(); } finally { this.co.busy = false; }
      if (this.coQuote?.promo) this.showToast(`Promo ${this.coQuote.promo.code} applied — ${this.coQuote.promo.label}`);
    },
    async removePromo() { this.co.promo_code = ''; this.co.promo_input = ''; await this.coRequote(); },
    async setShipping(m) { this.co.shipping_method = m; if (this.view === 'order') await this.coRequote(); },
    async placeOrder() {
      if (!this.coCanPlace) return;
      this.co.busy = true; this.co.error = ''; this.co.problems = {};
      try {
        const o = await this.api('POST', '/api/orders', {
          customer: this.co.customer, address: this.co.address, shipping_method: this.co.shipping_method,
          promo_code: this.co.promo_code || null, terms_accepted: this.co.terms === true,
          client_request_id: this.co.requestId, use_suggested_address: this.co.useSuggested,
        });
        this.order = o;
        this.co.requestId = ''; this.co.promo_code = ''; this.co.promo_input = ''; this.co.terms = false; this.co.addrCheck = null;
        await this.refreshBag();
        this.navigateTo('confirmation');
        this.showToast(`Order ${o.ref} received` + (this.emailOnItsWay(o.confirmation_email) ? ` — a confirmation email is on its way to ${o.customer.email}` : ' — please keep your Order ID'), { ms: 7000 });
      } catch (e) {
        this.co.error = e.message;
        if (e.problems) {
          this._setProblems(e.problems);
          const f = e.problems.map(p => p.field);
          this.co.step = f.some(x => CUSTOMER_FIELDS.includes(x)) ? 'details' : f.some(x => ADDRESS_FIELDS.includes(x) || x === 'shipping_method') ? 'shipping' : 'review';
        }
        if (e.code === 'bag_empty' || e.code === 'bag_not_orderable') { await this.refreshBag(); this.navigateTo('checkout'); }
      } finally { this.co.busy = false; }
    },
    async loadOrders() {
      if (!this.token) return;
      this.ordersLoading = true;
      try { this.orders = (await this.api('GET', '/api/orders')).orders; } catch (_) { /* keep what we have */ }
      finally { this.ordersLoading = false; }
    },
    openOrder(o) { this.order = o; this.closeAccountPanel(); this.navigateTo('confirmation'); },
    orderDate(iso) { return iso ? new Date(iso).toLocaleDateString('en-US', { day: 'numeric', month: 'long', year: 'numeric' }) : ''; },

    // ── gold: "Request a quote" instead of the fixed-price path ──────────
    openQuoteRequest() {
      const c = this.quoteReq.customer, name = (this.displayName || '').trim().split(/\s+/);
      if (!c.first_name && name[0]) c.first_name = name[0];
      if (!c.last_name && name.length > 1) c.last_name = name.slice(1).join(' ');
      if (!c.email && this.displayEmail) c.email = this.displayEmail;
      this.quoteReq = { ...this.quoteReq, open: true, busy: false, error: '', done: null, problems: {}, quantity: this.cust?.quantity || 1 };
    },
    closeQuoteRequest() { this.quoteReq.open = false; },
    async sendQuoteRequest() {
      if (!this.cust || !this.design) return;
      this.quoteReq.busy = true; this.quoteReq.error = ''; this.quoteReq.problems = {};
      try {
        const r = await this.api('POST', '/api/quote-requests', {
          design_id: this.design.id, candidate_id: this.cust.candidate_id, material_id: this.cust.material_id,
          ...(this.custIsCharm ? { charm_size: this.cust.charm_size } : { ring_size: this.cust.ring_size }),
          quantity: this.quoteReq.quantity, customer: this.quoteReq.customer, message: this.quoteReq.message,
        });
        this.quoteReq.done = r;
      } catch (e) {
        this.quoteReq.error = e.message;
        if (e.problems) this.quoteReq.problems = Object.fromEntries(e.problems.map(p => [p.field, p.message]));
      } finally { this.quoteReq.busy = false; }
    },
  };
}

window.p3App = p3App;
