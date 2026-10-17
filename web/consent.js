/* XJet Atelier — cookie consent and analytics (docs/ANALYTICS.md, p3/analytics.py).
 *
 * Nothing optional runs before the visitor decides. The page says what may run (window.__p3.analytics on the site,
 * window.__p3Analytics on the quote page: {version, host, ga4, clarity}); without it — analytics off, or no ID — or on
 * another host than the configured one, there is nothing to consent to: no banner, no tool, and Cookie Settings says so.
 *
 * The decision stays in this browser (localStorage "atelier_consent": the categories, the consent version, the tools it
 * covered and when — nothing about the person). A new version, or a tool the decision did not cover, asks again.
 * Accepting Analytics starts Google Analytics 4 (consent mode, basic: the Google tag is loaded only now, every signal
 * starts denied and only analytics_storage is granted — no advertising) and Microsoft Clarity (consent API v2:
 * analytics granted, ads denied). Withdrawing it tells both, deletes their cookies and reloads the page, so no tool keeps
 * running. Clarity never starts on a page whose address carries a secret (a quote link's or a sign-in link's token).
 *
 * Placeholder IDs — G-TEST… for GA4, test… for Clarity — are for functional tests (Proto): the banner, the choice and
 * the events run as usual, but neither tool is loaded and nothing is sent anywhere; what would have been sent stays in
 * the page (window.dataLayer, window.clarity.q) for checks.
 *
 * The banner names no tool and says what for: Accept all, Reject optional, Manage preferences. Privacy preferences shows
 * Essential (always active), Analytics & Performance (the switch; Learn more names the tools) and Marketing & Advertising
 * (not currently used: no tool, no switch). Accept all therefore allows Analytics & Performance only.
 *
 * Events go through AtelierAnalytics.track(): only the events and fields listed below, with values from fixed lists,
 * numbers or design references — never free text, names, emails, addresses, phone numbers, prompts or file names — and
 * page addresses without their query string or fragment (UTM campaign tags excepted).
 */
(function () {
  'use strict';
  var STORE = 'atelier_consent';
  var CFG = window.__p3Analytics || (window.__p3 && window.__p3.analytics) || null;
  var BASE = ((document.querySelector('meta[name="p3-base"]') || {}).content || '').replace(/\/+$/, '');
  var HOST = location.hostname.toLowerCase();
  var ACTIVE = !!(CFG && CFG.host && (CFG.ga4 || CFG.clarity) && HOST === String(CFG.host).toLowerCase());
  var VERSION = ACTIVE ? CFG.version : 0;
  var VENDORS = ACTIVE ? ['ga4', 'clarity'].filter(function (k) { return !!CFG[k]; }) : [];
  var DRY = { ga4: ACTIVE && /^G-TEST/i.test(CFG.ga4 || ''), clarity: ACTIVE && /^test/i.test(CFG.clarity || '') };   // placeholders
  var running = { ga4: false, clarity: false };
  var lastPage = null, pageSent = '', pageLoc = '', pageTitle = '', pendingDefault = null, pageTimer = null;

  // ── the decision ──────────────────────────────────────────────────────
  function read() {
    var d = null;
    try { d = JSON.parse(localStorage.getItem(STORE) || 'null'); } catch (e) { d = null; }
    if (!d || typeof d !== 'object' || d.v !== VERSION) return null;
    for (var i = 0; i < VENDORS.length; i++) if ((d.vendors || []).indexOf(VENDORS[i]) < 0) return null;   // a tool added since
    return d;
  }
  function save(analytics) {
    var d = { v: VERSION, analytics: !!analytics, marketing: false, vendors: VENDORS.slice(), at: new Date().toISOString() };
    try { localStorage.setItem(STORE, JSON.stringify(d)); } catch (e) { /* storage blocked: asked again next visit */ }
    return d;
  }
  function decide(analytics) {
    var before = read(), wasOn = !!(before && before.analytics) || running.ga4 || running.clarity;
    save(analytics);
    closeBanner(); closeSettings();
    if (analytics) start();
    else if (wasOn) stopAndReload();
  }

  // ── Google Analytics 4: consent mode, basic ───────────────────────────
  function cookieDomain() { return HOST.indexOf('.') > 0 ? HOST : 'none'; }
  function startGa4() {
    if (running.ga4 || !CFG.ga4) return;
    running.ga4 = true;
    window.dataLayer = window.dataLayer || [];
    window.gtag = window.gtag || function () { window.dataLayer.push(arguments); };
    var gtag = window.gtag;
    gtag('consent', 'default', { ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied', analytics_storage: 'denied' });
    gtag('consent', 'update', { analytics_storage: 'granted' });            // the visitor accepted Analytics, nothing else
    if (!DRY.ga4) {                                          // a placeholder ID: the calls stay in dataLayer, nothing loads
      var s = document.createElement('script');
      s.async = true;
      s.src = 'https://www.googletagmanager.com/gtag/js?id=' + encodeURIComponent(CFG.ga4);
      document.head.appendChild(s);
    }
    gtag('js', new Date());
    // The clean address of the screen in front, for every hit — the page views and GA4's own events alike; sendPage()
    // moves it with each screen. Set, never in config: an address in config would stay on every later event.
    gtag('set', { page_location: cleanUrl(location.href), page_referrer: cleanReferrer(), page_title: document.title });
    gtag('config', CFG.ga4, {
      send_page_view: false,                                  // page views are sent by sendPage() with clean addresses
      allow_google_signals: false, allow_ad_personalization_signals: false,
      cookie_domain: cookieDomain(), cookie_flags: location.protocol === 'https:' ? 'SameSite=Lax;Secure' : 'SameSite=Lax'
    });
  }

  // ── Microsoft Clarity: consent API v2 ─────────────────────────────────
  function urlHasSecret() { return /(?:^|[?&#])(?:token|sig|key)=/i.test(location.search + '&' + location.hash.replace(/^#/, '')); }
  function startClarity() {
    if (running.clarity || !CFG.clarity || window.__p3NoClarity) return;
    if (urlHasSecret()) {                                    // a sign-in link's #token=… is removed by the app right away
      if (!startClarity._tries) startClarity._tries = 0;
      if (++startClarity._tries < 40) setTimeout(startClarity, 250);
      return;
    }
    running.clarity = true;
    (function (c, l, a, r, i, t, y) {
      c[a] = c[a] || function () { (c[a].q = c[a].q || []).push(arguments); };
      if (DRY.clarity) return;                               // a placeholder ID: the calls stay in clarity.q, nothing loads
      t = l.createElement(r); t.async = 1; t.src = 'https://www.clarity.ms/tag/' + i;
      y = l.getElementsByTagName(r)[0]; y.parentNode.insertBefore(t, y);
    })(window, document, 'clarity', 'script', CFG.clarity);
    window.clarity('consentv2', { ad_Storage: 'denied', analytics_Storage: 'granted' });
  }

  function start() {
    if (!ACTIVE) return;
    startGa4();
    startClarity();
    if (lastPage) sendPage(lastPage.path, lastPage.title);
    else if (!window.__p3) sendPage(location.pathname, document.title);   // a page without the app (the quote page), first
    else {                                                    // the app names its screen; if it never does, the address
      clearTimeout(pendingDefault);
      pendingDefault = setTimeout(function () { if (!pageSent) sendPage(location.pathname, document.title); }, 1500);
    }
    flushPageEvents();
  }

  // Withdrawn: both tools are told, their first-party cookies deleted, and the page reloads with neither loaded
  function stopAndReload() {
    try { if (window.gtag && CFG.ga4) { window['ga-disable-' + CFG.ga4] = true; window.gtag('consent', 'update', { analytics_storage: 'denied' }); } } catch (e) {}
    try { if (window.clarity) window.clarity('consent', false); } catch (e) {}
    dropCookies(['_ga', '_gid', '_gat', '_clck', '_clsk']);
    setTimeout(function () { location.reload(); }, 150);
  }
  function dropCookies(prefixes) {
    var parts = HOST.split('.'), domains = [''];
    for (var i = 0; i < parts.length - 1; i++) domains.push('.' + parts.slice(i).join('.'));
    document.cookie.split(';').forEach(function (c) {
      var name = c.split('=')[0].trim();
      if (!prefixes.some(function (p) { return name === p || name.indexOf(p + '_') === 0; })) return;
      domains.forEach(function (d) { document.cookie = name + '=; Max-Age=0; path=/' + (d ? '; domain=' + d : ''); });
    });
  }

  // ── clean addresses: no query string or fragment (quote and sign-in links carry tokens) ──
  var UTM = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'];
  function cleanUrl(href) {
    try {
      var u = new URL(href, location.href), keep = new URLSearchParams();
      UTM.forEach(function (k) { var v = u.searchParams.get(k); if (v && /^[\w.\- ]{1,100}$/.test(v)) keep.set(k, v); });
      var q = keep.toString();
      return u.origin + u.pathname + (q ? '?' + q : '');
    } catch (e) { return location.origin + location.pathname; }
  }
  // Google's and Apple's sign-in pages are a step of signing in here, never where a visitor came from (GA4's "unwanted
  // referrals", in the page itself): the return from them names no referrer
  var SIGN_IN_HOSTS = /(?:^|\.)(?:accounts\.google\.com|appleid\.apple\.com)$/i;
  function cleanReferrer() {
    if (!document.referrer) return '';
    try { var u = new URL(document.referrer); return SIGN_IN_HOSTS.test(u.hostname) ? '' : u.origin + u.pathname; } catch (e) { return ''; }
  }

  // ── page views and events ─────────────────────────────────────────────
  // A screen is its address and its title: an order's confirmation keeps Checkout's address under its own title. The
  // same screen again sends nothing (a re-render, the app showing the screen the page was opened at).
  function sendPage(path, title) {
    if (!running.ga4) return;
    var loc = cleanUrl(location.origin + path + location.search), t = title || document.title;
    if (loc + '\n' + t === pageSent) return;
    var referrer = pageLoc || cleanReferrer();
    pageSent = loc + '\n' + t; pageLoc = loc; pageTitle = t;
    scrollArmed = true;                                       // this screen's scroll depth, once
    window.gtag('set', { page_location: loc, page_referrer: referrer, page_title: t });
    window.gtag('event', 'page_view', { page_location: loc, page_referrer: referrer, page_title: t });
  }
  // The app's screens arrive in bursts (a reload's restore passes through the Design screen on its way to My Designs):
  // the page view goes out once the burst is over, for the screen it ended on — or at once when an event follows
  function flushPage() {
    if (!pageTimer) return;
    clearTimeout(pageTimer); pageTimer = null;
    if (lastPage) sendPage(lastPage.path, lastPage.title);
  }

  // Scroll depth. GA4's own scroll event (enhanced measurement, switched off for this site: docs/ANALYTICS.md) watches the
  // window alone — on wide screens the page scrolls inside its <main> — fires on short pages without any scrolling, and
  // only once per page load. This one is "scroll" (percent_scrolled 90), once per screen, when the visitor has scrolled
  // the window or the screen's <main> to 90 % of a page that does not fit the window: their own scrolling (a wheel, a
  // touch, a key or the scroll bar in the last 4 s), never the app's.
  var scrollArmed = false, lastGesture = 0;
  function gesture() { lastGesture = Date.now(); }
  function scrolled(e) {
    if (!scrollArmed || !running.ga4 || pageTimer || Date.now() - lastGesture > 4000) return;
    var root = document.scrollingElement || document.documentElement, el = e.target === document ? root : e.target;
    if (el !== root && el.tagName !== 'MAIN') return;        // a list or a dialog scrolling inside the page
    var seen = el === root ? window.innerHeight : el.clientHeight;
    if (el.scrollHeight * 0.9 <= seen || el.scrollTop + seen < el.scrollHeight * 0.9) return;
    scrollArmed = false;
    window.gtag('event', 'scroll', { percent_scrolled: 90, page_location: pageLoc, page_title: pageTitle });
  }
  if (ACTIVE && CFG.ga4) {
    ['wheel', 'touchmove', 'keydown', 'pointerdown'].forEach(function (t) {
      document.addEventListener(t, gesture, { capture: true, passive: true });
    });
    document.addEventListener('scroll', scrolled, { capture: true, passive: true });     // the window's, and <main>'s
  }

  var P = ['ring', 'charm'], KIND = ['new', 'refine', 'retry'], GROUP = ['fashion', 'luxury'], CUR = ['USD'];
  var SCHEMA = {
    sign_up: { method: ['email'] },
    login: { method: ['code', 'link'] },
    design_session_started: { product_type: P, entry: ['home', 'gallery'] },
    prompt_submitted: { product_type: P, kind: ['new', 'refine'], has_reference_image: 'bool', prompt_length: ['short', 'medium', 'long'] },
    design_generation_started: { product_type: P, kind: KIND },
    design_generation_completed: { product_type: P, kind: KIND, options_ready: 'int', options_failed: 'int', duration_s: 'int' },
    design_generation_failed: { product_type: P, kind: KIND, reason: ['content_policy', 'prompt_refused', 'no_credits', 'provider', 'error'] },
    design_selected: { product_type: P, option: 'int' },
    gallery_design_opened: { product_type: P, design: 'title' },
    customize_opened: { product_type: P, source: ['results', 'gallery'] },
    movie_generation_started: { product_type: P, retry: 'bool' },
    movie_generation_completed: { product_type: P },
    movie_generation_failed: { product_type: P },
    material_selected: { product_type: P, material_id: 'id', material_group: GROUP },
    size_selected: { product_type: P, size: 'num' },
    add_to_cart: { currency: CUR, value: 'num', items: 'items' },
    begin_checkout: { currency: CUR, value: 'num', items: 'items' },
    quote_requested: { product_type: P, material_group: GROUP },
    quote_viewed: { product_type: P },
    quote_approved: { product_type: P, currency: CUR, value: 'num' },
    quote_declined: { product_type: P },
    order_created: { source: ['checkout', 'quote'], currency: CUR, value: 'num', item_count: 'int' },
    purchase: { transaction_id: 'order', currency: CUR, value: 'num', items: 'items' }
  };
  var ITEM = { item_id: ['custom-ring', 'custom-charm'], item_name: ['Custom ring', 'Custom charm'], item_category: P,
               item_variant: 'id', price: 'num', quantity: 'int' };
  var TYPES = {
    bool: function (v) { return typeof v === 'boolean' ? v : undefined; },
    int: function (v) { return typeof v === 'number' && isFinite(v) && v >= 0 && v <= 1e6 ? Math.round(v) : undefined; },
    num: function (v) { return typeof v === 'number' && isFinite(v) && v >= 0 && v <= 1e7 ? Math.round(v * 100) / 100 : undefined; },
    id: function (v) { return typeof v === 'string' && /^[a-z0-9_]{2,40}$/.test(v) ? v : undefined; },
    title: function (v) { return typeof v === 'string' && /^[\p{L}\p{N} '’.-]{2,60}$/u.test(v) && !/\d{5,}/.test(v) ? v : undefined; },
    order: function (v) { return typeof v === 'string' && /^ORD-\d{1,9}$/.test(v) ? v : undefined; },
    items: function (v) {
      if (!Array.isArray(v)) return undefined;
      var out = v.slice(0, 20).map(function (x) { return clean(x, ITEM); }).filter(function (x) { return x && x.item_id; });
      return out.length ? out : undefined;
    }
  };
  // Only listed fields, each from its fixed list or type; anything else is dropped (never sent "as is")
  function clean(params, schema) {
    if (!params || typeof params !== 'object') return {};
    var out = {};
    Object.keys(schema).forEach(function (k) {
      if (!(k in params)) return;
      var rule = schema[k], v = params[k], ok;
      if (Array.isArray(rule)) ok = rule.indexOf(v) >= 0 ? v : undefined;
      else ok = TYPES[rule](v);
      if (ok !== undefined) out[k] = ok;
    });
    return out;
  }
  function track(name, params) {
    if (!SCHEMA[name] || !(running.ga4 || running.clarity)) return false;     // no consent, no tool: nothing is sent
    var data = clean(params, SCHEMA[name]);
    flushPage();                                              // the screen's page view first: the event happens on it
    if (pageLoc) { data.page_location = pageLoc; data.page_title = pageTitle; }
    if (running.ga4) window.gtag('event', name, data);
    if (running.clarity && window.clarity) window.clarity('event', name);       // the name only: a filter for recordings
    return true;
  }

  // Events a server-rendered page states (the quote page: approved, declined, viewed): sent once consent allows
  var pageEventsSent = false;
  function flushPageEvents() {
    if (pageEventsSent || !Array.isArray(window.__p3Events)) return;
    pageEventsSent = true;
    window.__p3Events.forEach(function (e) { if (e && typeof e.name === 'string') track(e.name, e.params || {}); });
  }

  // ── the banner and Privacy preferences ────────────────────────────────
  // Atelier's own look (web/index.html): warm ivory surfaces, beige hairlines, Cinzel titles, Inter text, small spaced
  // capitals on the buttons, 6 px corners. Accept all wears Atelier's gold (the site's btn-gold: a warm gold gradient,
  // dark text); Reject optional sits beside it at the same size, outlined; Manage preferences is a quiet gold text
  // action. The same order and weight in Privacy preferences. The styles are the script's own, so the
  // quote page (none of the site's stylesheets) shows the same. The banner names no tool: Google Analytics 4 and
  // Microsoft Clarity are named under Analytics & Performance → Learn more, and on the Privacy page. Marketing &
  // Advertising is shown so the choices are complete, as "Not currently used": no tool, no switch.
  var SANS = "Inter,'Helvetica Neue',Helvetica,Arial,sans-serif", SERIF = "Cinzel,Georgia,'Times New Roman',serif";
  var CSS = [
    '@keyframes ac-in{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}',
    '@keyframes ac-fade{from{opacity:0}to{opacity:1}}',
    '.ac-bar,.ac-card{box-sizing:border-box;background:#FBF8F3;color:#2A2520;border:1px solid #E8DCC4;border-radius:10px;',
    'font:400 13.5px/1.6 ' + SANS + ';text-align:left;-webkit-font-smoothing:antialiased}',
    '.ac-bar *,.ac-card *{box-sizing:border-box}',
    // the first layer: a card in the corner on wide screens, a sheet along the bottom on phones
    '.ac-bar{position:fixed;left:24px;bottom:24px;z-index:2147483000;width:424px;max-width:calc(100vw - 48px);padding:22px 22px 12px;',
    'box-shadow:0 18px 44px -18px rgba(58,46,18,.32),0 2px 8px rgba(58,46,18,.06);animation:ac-in .32s cubic-bezier(.2,.7,.2,1) both}',
    '.ac-bar .ac-title{margin:0 0 8px;padding:0;font:600 15px/1.35 ' + SERIF + ';letter-spacing:.06em;color:#1A1A1A;text-transform:none}',
    '.ac-bar p,.ac-card p{margin:0;padding:0;color:#4A443B;font-size:13.5px;line-height:1.6}',
    '.ac-bar .ac-text{margin:0 0 16px}',
    '.ac-bar a,.ac-card a{color:#2A2520;text-decoration:underline;text-decoration-color:#C9A96E;text-decoration-thickness:1px;text-underline-offset:3px}',
    '.ac-bar a:hover,.ac-card a:hover{color:#8A6420}',
    '.ac-row{display:flex;gap:10px}.ac-row .ac-btn{flex:1 1 0}',
    '.ac-bar .ac-btn,.ac-card .ac-btn{-webkit-appearance:none;appearance:none;margin:0;display:inline-flex;align-items:center;',
    'justify-content:center;min-height:44px;padding:0 18px;border:1px solid #B48A4C;border-radius:6px;',
    'background:linear-gradient(90deg,#C9A96E 0%,#B08040 100%);color:#1A1A1A;box-shadow:0 4px 12px rgba(138,86,27,.22);',
    'font:600 11.5px/1.2 ' + SANS + ';letter-spacing:.14em;text-transform:uppercase;text-align:center;cursor:pointer;',
    'transition:background-color .2s,border-color .2s,color .2s,filter .2s}',
    '.ac-bar .ac-btn:hover,.ac-card .ac-btn:hover{filter:brightness(1.06)}',
    '.ac-bar .ac-soft,.ac-card .ac-soft{background:transparent;color:#2A2520;border-color:#9C8A6C;box-shadow:none}',
    '.ac-bar .ac-soft:hover,.ac-card .ac-soft:hover{background:#F2EBDD;border-color:#7E6D52;color:#1A1A1A;filter:none}',
    '.ac-bar .ac-quiet,.ac-card .ac-quiet{-webkit-appearance:none;appearance:none;margin:0;padding:10px 0;min-height:40px;border:0;',
    'background:none;color:#8A6420;font:600 11.5px/1.2 ' + SANS + ';letter-spacing:.14em;text-transform:uppercase;cursor:pointer;',
    'text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:4px}',
    '.ac-bar .ac-quiet:hover,.ac-card .ac-quiet:hover{color:#5E430F}',
    '.ac-bar .ac-quiet{margin-top:4px}',
    '.ac-bar :focus-visible,.ac-card :focus-visible{outline:2px solid #8A6420;outline-offset:2px}',
    '.ac-card:focus{outline:none}',
    // Privacy preferences: a dialog in the middle on wide screens, a sheet from the bottom on phones
    '.ac-shade{position:fixed;inset:0;z-index:2147483001;display:flex;align-items:center;justify-content:center;padding:16px;',
    'background:rgba(32,26,18,.42);-webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px);animation:ac-fade .2s ease-out both}',
    '.ac-card{position:relative;display:flex;flex-direction:column;width:100%;max-width:580px;max-height:calc(100vh - 32px);',
    'max-height:calc(100dvh - 32px);box-shadow:0 30px 80px -24px rgba(32,26,18,.5);animation:ac-in .28s cubic-bezier(.2,.7,.2,1) both}',
    '.ac-body{overflow:auto;-webkit-overflow-scrolling:touch;padding:28px 28px 6px}',
    '.ac-card h2{margin:0 44px 10px 0;padding:0;font:600 21px/1.3 ' + SERIF + ';letter-spacing:.05em;color:#1A1A1A;text-transform:none}',
    '.ac-card .ac-intro{margin:0 0 18px}',
    '.ac-card .ac-x{-webkit-appearance:none;appearance:none;position:absolute;top:12px;right:12px;width:40px;height:40px;margin:0;',
    'padding:0;display:flex;align-items:center;justify-content:center;border:0;border-radius:6px;background:#FBF8F3;color:#4A443B;',
    'cursor:pointer;z-index:1}',                              // over the text that scrolls beneath it
    '.ac-card .ac-x:hover{background:#F2EBDD;color:#1A1A1A}',
    '.ac-cat{border-top:1px solid #EAE0CC;padding:16px 0}',
    '.ac-head{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:28px;margin:0 0 6px}',
    '.ac-card h3{margin:0;padding:0;font:600 11.5px/1.4 ' + SANS + ';letter-spacing:.14em;text-transform:uppercase;color:#1A1A1A}',
    '.ac-card .ac-note{margin-top:4px;color:#6F6A60;font-size:12.5px}',
    '.ac-status{flex-shrink:0;font:600 10.5px/1.2 ' + SANS + ';letter-spacing:.12em;text-transform:uppercase;color:#6F6A60;text-align:right}',
    '.ac-status.ac-on{color:#8A6420}',
    '.ac-status.ac-on::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:#C9A96E;margin:0 7px 1px 0;',
    'vertical-align:middle}',
    '.ac-card .ac-switch{display:flex;align-items:center;gap:10px;flex-shrink:0;margin:0;cursor:pointer;font-weight:400}',
    '.ac-state{font:600 10.5px/1 ' + SANS + ';letter-spacing:.12em;text-transform:uppercase;color:#4A443B;min-width:26px;text-align:right}',
    '.ac-card .ac-sw{-webkit-appearance:none;appearance:none;position:relative;width:46px;height:26px;margin:0;border:0;border-radius:13px;',
    'background:#968669;cursor:pointer;flex-shrink:0;transition:background-color .2s}',
    '.ac-card .ac-sw::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#FFFDF9;',
    'box-shadow:0 1px 3px rgba(42,37,32,.35);transition:transform .2s}',
    '.ac-card .ac-sw:checked{background:#8A6420}.ac-card .ac-sw:checked::after{transform:translateX(20px)}',
    '.ac-card .ac-more{margin-top:4px;padding:8px 0;min-height:36px;display:inline-flex;align-items:center;gap:7px;text-decoration:none;',
    'letter-spacing:.12em}',
    '.ac-more svg{transition:transform .2s}.ac-more[aria-expanded="true"] svg{transform:rotate(180deg)}',
    '.ac-card .ac-detail{margin:6px 0 2px;padding:12px 14px;border-radius:6px;background:#F3EDE2;color:#4A443B;font-size:13px}',
    '.ac-foot{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:16px 28px 22px;border-top:1px solid #EAE0CC}',
    '.ac-foot .ac-btn{min-width:150px}.ac-foot .ac-end{margin-left:auto}',
    '@media (max-width:640px){',
    '.ac-bar{left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom,0px));width:auto;max-width:none;padding:18px 16px 8px}',
    '.ac-bar p{font-size:13px}',
    '.ac-bar .ac-row .ac-btn{padding:0 10px;font-size:11px;letter-spacing:.08em}',      // both labels on one line from 360 px
    '.ac-bar .ac-quiet{display:block;margin:4px auto 0}',
    '.ac-shade{align-items:flex-end;padding:0}',
    '.ac-card{max-width:none;max-height:92vh;max-height:92dvh;border-radius:12px 12px 0 0;border-bottom:0}',
    '.ac-body{padding:24px 20px 6px}',
    '.ac-foot{flex-direction:column;align-items:stretch;padding:14px 20px calc(16px + env(safe-area-inset-bottom,0px))}',
    '.ac-foot .ac-btn{width:100%;min-width:0}.ac-foot .ac-end{margin-left:0}}',
    '@media (max-width:359px){.ac-row{flex-direction:column}}',
    '@media (prefers-reduced-motion:reduce){.ac-bar,.ac-shade,.ac-card{animation:none}.ac-card .ac-sw,.ac-card .ac-sw::after,.ac-more svg{transition:none}}'
  ].join('');
  function style() {
    if (document.getElementById('ac-style')) return;
    var s = document.createElement('style'); s.id = 'ac-style'; s.textContent = CSS; document.head.appendChild(s);
  }
  function el(tag, attrs, html) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (html != null) e.innerHTML = html;
    return e;
  }
  var privacyUrl = BASE + '/privacy';
  var NAMES = { ga4: 'Google Analytics 4', clarity: 'Microsoft Clarity' };
  // Behind Analytics & Performance → Learn more: the tools this site has switched on, by name
  function analyticsDetail() {
    var names = VENDORS.map(function (v) { return NAMES[v]; }), many = names.length > 1;
    return 'Includes ' + names.join(' and ') + '. ' + (many ? 'These services are' : 'This service is') + ' activated only after ' +
      'you give consent and ' + (many ? 'are' : 'is') + ' not used for advertising. Customer details and design prompts are not ' +
      'sent to ' + (many ? 'them' : 'it') + (CFG.clarity ? ', and Microsoft Clarity is configured to mask sensitive customer information' : '') +
      '. More in our <a href="' + privacyUrl + '">Privacy Policy</a>.';
  }
  var X_ICON = '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9" ' +
    'fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
  var CHEVRON = '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" focusable="false"><path d="M1.5 3.5L5 7l3.5-3.5" ' +
    'fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var bar = null;
  function showBanner() {
    if (bar || !ACTIVE) return;
    style();
    bar = el('div', { 'class': 'ac-bar', role: 'region', 'aria-labelledby': 'ac-bar-title', 'data-clarity-mask': 'true' },
      '<h2 class="ac-title" id="ac-bar-title">Your privacy matters</h2>' +
      '<p class="ac-text">Atelier uses essential cookies and similar technologies to make the site work. With your permission, we also ' +
      'use optional technologies to understand how the site is used and improve your experience. You can change your choices at any ' +
      'time. <a href="' + privacyUrl + '">Privacy Policy</a></p>' +
      '<div class="ac-row"><button type="button" class="ac-btn ac-soft" data-ac="reject">Reject optional</button>' +
      '<button type="button" class="ac-btn" data-ac="accept">Accept all</button></div>' +
      '<button type="button" class="ac-quiet" data-ac="manage">Manage preferences</button>');
    document.body.appendChild(bar);
  }
  function closeBanner() { if (bar) { bar.remove(); bar = null; } }

  // One category of Privacy preferences: its name, what is on the right (a status or the switch), its words, more below
  function category(id, name, side, text, more) {
    return '<div class="ac-cat" role="group" aria-labelledby="' + id + '"><div class="ac-head"><h3 id="' + id + '">' + name + '</h3>' +
      side + '</div><p id="' + id + '-d">' + text + '</p>' + (more || '') + '</div>';
  }
  var shade = null, returnFocus = null;
  function openSettings() {
    style();
    closeSettings();
    returnFocus = document.activeElement;
    var d = read(), on = ACTIVE && !!(d && d.analytics);
    var analyticsSide = ACTIVE
      ? '<label class="ac-switch"><span class="ac-state" id="ac-state" aria-hidden="true">' + (on ? 'On' : 'Off') + '</span>' +
        '<input type="checkbox" role="switch" class="ac-sw" id="ac-analytics" aria-labelledby="ac-an" aria-describedby="ac-an-d"' +
        (on ? ' checked' : '') + '></label>'
      : '<span class="ac-status">Not currently used</span>';
    shade = el('div', { 'class': 'ac-shade', 'data-clarity-mask': 'true' });
    var card = el('div', { 'class': 'ac-card', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'ac-title', tabindex: '-1' },
      '<button type="button" class="ac-x" data-ac="close" aria-label="Close">' + X_ICON + '</button>' +
      '<div class="ac-body"><h2 id="ac-title">Privacy preferences</h2>' +
      '<p class="ac-intro">Choose how Atelier may use optional cookies and similar technologies. Essential technologies are always ' +
      'active because they are required for the site to function. You can change your preferences at any time. ' +
      '<a href="' + privacyUrl + '">Privacy Policy</a></p>' +
      category('ac-es', 'Essential', '<span class="ac-status ac-on">Always active</span>',
        'Required for sign-in, your designs, bag, checkout, security and your privacy preferences.',
        '<p class="ac-note">This category cannot be switched off.</p>') +
      category('ac-an', 'Analytics &amp; Performance', analyticsSide,
        'Helps us understand how visitors use Atelier, measure site performance and improve the design and shopping experience.',
        ACTIVE ? '<button type="button" class="ac-quiet ac-more" data-ac="learn" aria-expanded="false" aria-controls="ac-an-more">' +
                 'Learn more' + CHEVRON + '</button><p class="ac-detail" id="ac-an-more" hidden>' + analyticsDetail() + '</p>' : '') +
      category('ac-mk', 'Marketing &amp; Advertising', '<span class="ac-status">Not currently used</span>',
        'Used to measure advertising campaigns and, when enabled, help make advertising more relevant.',
        '<p class="ac-note">Atelier does not currently use marketing or advertising technology.</p>') +
      '</div><div class="ac-foot">' + (ACTIVE
        ? '<button type="button" class="ac-btn ac-soft" data-ac="reject">Reject optional</button>' +
          '<button type="button" class="ac-btn ac-soft" data-ac="save">Save preferences</button>' +
          '<button type="button" class="ac-btn ac-end" data-ac="accept">Accept all</button>'
        : '<button type="button" class="ac-btn ac-end" data-ac="close">Close</button>') + '</div>');
    shade.appendChild(card);
    document.body.appendChild(shade);
    card.focus();                                            // the dialog is announced by its title; Tab reaches its controls
  }
  function closeSettings() {
    if (!shade) return;
    shade.remove(); shade = null;
    if (returnFocus && returnFocus.isConnected) { try { returnFocus.focus(); } catch (e) {} }
    returnFocus = null;
  }

  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('[data-ac], [data-cookie-settings]') : null;
    if (!t) { if (shade && e.target === shade) closeSettings(); return; }
    if (t.hasAttribute('data-cookie-settings')) { e.preventDefault(); openSettings(); return; }
    var a = t.getAttribute('data-ac');
    if (a === 'accept') decide(true);                        // Analytics & Performance; Marketing is not used
    else if (a === 'reject') decide(false);
    else if (a === 'manage') openSettings();
    else if (a === 'save') decide(!!(document.getElementById('ac-analytics') || {}).checked);
    else if (a === 'close') closeSettings();
    else if (a === 'learn') {
      var box = document.getElementById(t.getAttribute('aria-controls') || ''), open = t.getAttribute('aria-expanded') !== 'true';
      if (box) { box.hidden = !open; t.setAttribute('aria-expanded', open ? 'true' : 'false'); }
    }
  });
  document.addEventListener('change', function (e) {         // the switch's visible On / Off
    if (!e.target || e.target.id !== 'ac-analytics') return;
    var s = document.getElementById('ac-state');
    if (s) s.textContent = e.target.checked ? 'On' : 'Off';
  });
  document.addEventListener('keydown', function (e) {
    if (!shade) return;
    if (e.key === 'Escape') { closeSettings(); return; }
    if (e.key !== 'Tab') return;                             // focus stays inside the dialog
    var items = Array.prototype.slice.call(shade.querySelectorAll('a[href], button, input:not([disabled])'))
      .filter(function (x) { return x.offsetParent !== null; });
    if (!items.length) return;
    var i = items.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) { e.preventDefault(); items[items.length - 1].focus(); }
    else if (!e.shiftKey && (i < 0 || i === items.length - 1)) { e.preventDefault(); items[0].focus(); }
  });

  // ── what the app and the pages call ──────────────────────────────────
  window.AtelierAnalytics = {
    track: track,
    // The screen the visitor is on: its address (a page of the site, a step of the journey, a gallery design) and title
    page: function (path, title) {
      lastPage = { path: path, title: title };
      clearTimeout(pendingDefault);
      clearTimeout(pageTimer);
      pageTimer = setTimeout(flushPage, 0);
    },
    openSettings: openSettings,
    allowed: function () { return ACTIVE && !!(read() || {}).analytics; },
    placeholder: { ga4: DRY.ga4, clarity: DRY.clarity }
  };

  function boot() {
    if (!ACTIVE) return;
    var d = read();
    if (d) { if (d.analytics) start(); }
    else showBanner();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
