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
  var lastPage = null, pageSent = '', pendingDefault = null;

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
    gtag('config', CFG.ga4, {
      send_page_view: false,                                  // page views are sent by sendPage() with clean addresses
      page_location: cleanUrl(location.href), page_referrer: cleanReferrer(),
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
    else {                                                    // a page without the app (the quote page): its own address
      clearTimeout(pendingDefault);
      pendingDefault = setTimeout(function () { if (!pageSent) sendPage(location.pathname, document.title); }, window.__p3 ? 1500 : 0);
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
  function cleanReferrer() {
    if (!document.referrer) return '';
    try { var u = new URL(document.referrer); return u.origin + u.pathname; } catch (e) { return ''; }
  }

  // ── page views and events ─────────────────────────────────────────────
  function sendPage(path, title) {
    if (!running.ga4) return;
    var loc = cleanUrl(location.origin + path + location.search);
    if (loc === pageSent) return;                            // the same screen again (a re-render, a reload of a view)
    var referrer = pageSent || cleanReferrer();
    pageSent = loc;
    window.gtag('set', { page_location: loc, page_referrer: referrer, page_title: title || document.title });
    window.gtag('event', 'page_view', { page_location: loc, page_referrer: referrer, page_title: title || document.title });
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

  // ── the banner and Cookie Settings ────────────────────────────────────
  var CSS = [
    '.ac-bar{position:fixed;left:16px;right:16px;bottom:16px;z-index:2147483000;max-width:760px;margin:0 auto;background:#fff;color:#1A1A1A;',
    'border:1px solid #E4E4E7;box-shadow:0 12px 40px rgba(0,0,0,.18);padding:16px 18px;font:14px/1.5 Inter,Helvetica,Arial,sans-serif}',
    '.ac-bar p{margin:0 0 12px}.ac-bar a{color:inherit;text-decoration:underline;text-underline-offset:2px}',
    '.ac-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}',
    '.ac-btn{appearance:none;border:1px solid #1A1A1A;background:#1A1A1A;color:#fff;padding:10px 16px;font:600 11px/1 Inter,Helvetica,Arial,sans-serif;',
    'letter-spacing:.15em;text-transform:uppercase;cursor:pointer;min-height:40px}',
    '.ac-btn.ac-out{background:#fff;color:#1A1A1A}.ac-link{appearance:none;border:0;background:none;color:#1A1A1A;padding:10px 6px;',
    'font:600 11px/1 Inter,Helvetica,Arial,sans-serif;letter-spacing:.15em;text-transform:uppercase;text-decoration:underline;cursor:pointer;min-height:40px}',
    '.ac-btn:focus-visible,.ac-link:focus-visible,.ac-sw:focus-visible{outline:2px solid #C9A96E;outline-offset:2px}',
    '.ac-shade{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;padding:16px}',
    '.ac-card{background:#fff;color:#1A1A1A;width:100%;max-width:560px;max-height:92vh;overflow:auto;padding:24px;font:14px/1.55 Inter,Helvetica,Arial,sans-serif;',
    'box-shadow:0 20px 60px rgba(0,0,0,.25)}',
    '.ac-card h2{font:600 20px/1.3 Cinzel,Georgia,serif;margin:0 0 8px}.ac-card p{margin:0 0 12px;color:#3F3F46}',
    '.ac-cat{border-top:1px solid #E4E4E7;padding:14px 0;display:flex;gap:14px;align-items:flex-start;justify-content:space-between}',
    '.ac-cat h3{font-size:14px;font-weight:600;margin:0 0 4px}.ac-cat p{font-size:13px;margin:0}.ac-fixed{font-size:12px;color:#52525B;white-space:nowrap;padding-top:2px}',
    '.ac-sw{appearance:none;width:44px;height:24px;border-radius:12px;background:#D4D4D8;position:relative;cursor:pointer;flex-shrink:0;transition:background .15s}',
    '.ac-sw::after{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .15s}',
    '.ac-sw:checked{background:#1A1A1A}.ac-sw:checked::after{left:23px}.ac-sw:disabled{opacity:.5;cursor:not-allowed}',
    '.ac-foot{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end;border-top:1px solid #E4E4E7;padding-top:16px}',
    '@media (max-width:560px){.ac-bar{left:8px;right:8px;bottom:8px}.ac-row .ac-btn{flex:1 1 40%}}'
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
  function toolNames() {
    return VENDORS.map(function (v) { return v === 'ga4' ? 'Google Analytics' : 'Microsoft Clarity'; }).join(' and ');
  }
  var privacyUrl = BASE + '/privacy';

  var bar = null;
  function showBanner() {
    if (bar || !ACTIVE) return;
    style();
    bar = el('div', { 'class': 'ac-bar', role: 'region', 'aria-label': 'Cookie consent', 'data-clarity-mask': 'true' },
      '<p>Atelier uses the storage it needs to work: your sign-in, your current design and your bag. With your permission ' +
      'we would also use analytics (' + toolNames() + ') to see how the site is used and improve it. ' +
      '<a href="' + privacyUrl + '">Privacy</a></p>' +
      '<div class="ac-row"><button type="button" class="ac-btn" data-ac="reject">Reject non-essential</button>' +
      '<button type="button" class="ac-btn" data-ac="accept">Accept all</button>' +
      '<button type="button" class="ac-link" data-ac="customize">Customize</button></div>');
    document.body.appendChild(bar);
  }
  function closeBanner() { if (bar) { bar.remove(); bar = null; } }

  var shade = null, returnFocus = null;
  function openSettings() {
    style();
    closeSettings();
    returnFocus = document.activeElement;
    var d = read(), on = !!(d && d.analytics);
    var analyticsText = !ACTIVE ? 'Not used on this site right now.'
      : (CFG.ga4 ? 'Google Analytics: which pages and steps of the design journey are used, and on which kinds of device. ' : '') +
        (CFG.clarity ? 'Microsoft Clarity: how pages are used — clicks, scrolling and session replays, with personal details masked. ' : '') +
        'Never used for advertising.';
    shade = el('div', { 'class': 'ac-shade', 'data-clarity-mask': 'true' });
    var card = el('div', { 'class': 'ac-card', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'ac-title' },
      '<h2 id="ac-title">Cookie settings</h2>' +
      '<p>Choose which optional storage Atelier may use. You can change this at any time from Cookie Settings at the foot of the page. ' +
      '<a href="' + privacyUrl + '" style="color:inherit;text-decoration:underline">Privacy</a></p>' +
      '<div class="ac-cat"><div><h3>Necessary</h3><p>Sign-in, your current design, your bag and checkout, and security. Kept in your ' +
      'browser by Atelier only; the site does not work without it.</p></div><span class="ac-fixed">Always on</span></div>' +
      '<div class="ac-cat"><div><h3 id="ac-an">Analytics</h3><p>' + analyticsText + '</p></div>' +
      '<input type="checkbox" class="ac-sw" id="ac-analytics" aria-label="Analytics" aria-labelledby="ac-an"' + (on && ACTIVE ? ' checked' : '') + (ACTIVE ? '' : ' disabled') + '></div>' +
      '<div class="ac-cat"><div><h3 id="ac-mk">Marketing</h3><p>Not used on this site.</p></div>' +
      '<input type="checkbox" class="ac-sw" aria-label="Marketing (not used)" aria-labelledby="ac-mk" disabled></div>' +
      '<div class="ac-foot">' + (ACTIVE
        ? '<button type="button" class="ac-btn ac-out" data-ac="reject">Reject non-essential</button>' +
          '<button type="button" class="ac-btn ac-out" data-ac="save">Save choices</button>' +
          '<button type="button" class="ac-btn" data-ac="accept">Accept all</button>'
        : '<button type="button" class="ac-btn" data-ac="close">Close</button>') + '</div>');
    shade.appendChild(card);
    document.body.appendChild(shade);
    var first = card.querySelector('input:not([disabled]), button');
    if (first) first.focus();
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
    if (a === 'accept') decide(true);
    else if (a === 'reject') decide(false);
    else if (a === 'customize') openSettings();
    else if (a === 'save') decide(!!(document.getElementById('ac-analytics') || {}).checked);
    else if (a === 'close') closeSettings();
  });
  document.addEventListener('keydown', function (e) {
    if (!shade) return;
    if (e.key === 'Escape') { closeSettings(); return; }
    if (e.key !== 'Tab') return;                             // focus stays inside the dialog
    var items = Array.prototype.slice.call(shade.querySelectorAll('a[href], button, input:not([disabled])'));
    if (!items.length) return;
    var first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  // ── what the app and the pages call ──────────────────────────────────
  window.AtelierAnalytics = {
    track: track,
    // The screen the visitor is on (a page of the site, or a step of the design journey under BASE/app/…)
    page: function (path, title) {
      lastPage = { path: path, title: title };
      clearTimeout(pendingDefault);
      sendPage(path, title);
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
