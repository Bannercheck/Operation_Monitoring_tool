/*! Watchover browser sensor (rum.js) · no dependencies · KVKK-safe by default
 *
 *  <script src="https://<watchover>:8600/rum.js" data-key="pk_..." async></script>
 *    data-key        app key (Kullanıcı deneyimi › Uygulamalar)                          required
 *    data-endpoint   where batches go (default: the origin this script came from)
 *    data-propagate  extra origins whose calls get a traceparent header, comma separated (same origin always does)
 *    data-sample     percent of sessions to record (default 100)
 *    data-env        prod / test ... (optional)
 *  window.watchover.setUser(id)   pseudonymised on the server, never stored as given
 *  window.watchover.action(name)  mark a business step yourself ("Bordro indir")
 *
 *  What it records: page / route views with load time and Web Vitals (LCP, INP, CLS, FCP, TTFB), clicks on buttons and links
 *  (their label, digits masked), fetch / XHR calls (method, URL without query string, status, duration), JS errors.
 *  What it never records: input values, form contents, cookies, query strings, page text, keystrokes.
 *  Each click starts a trace; the calls it triggers carry a W3C `traceparent` header with that trace id, so backend logs that
 *  record the header join the same waterfall in Watchover.
 */
(function () {
  "use strict";
  if (window.watchover && window.watchover.__v) return;
  var me = document.currentScript || (function () { var s = document.querySelectorAll("script[data-key]"); return s[s.length - 1]; })();
  if (!me) return;
  var ds = me.dataset || {};
  var KEY = ds.key || "";
  if (!KEY) return;
  var BASE = (ds.endpoint || (me.src ? new URL(me.src, location.href).origin : location.origin)).replace(/\/$/, "");
  var URL_ = BASE + "/rum?k=" + encodeURIComponent(KEY);
  var PROP = [location.origin].concat((ds.propagate || "").split(",").map(function (x) { return x.trim().replace(/\/$/, ""); }).filter(Boolean));
  var SAMPLE = Math.max(0, Math.min(100, parseInt(ds.sample || "100", 10) || 100));
  var now = function () { return Date.now(); };
  var hex = function (n) { var a = new Uint8Array(n); (window.crypto || window.msCrypto).getRandomValues(a); return Array.prototype.map.call(a, function (b) { return ("0" + b.toString(16)).slice(-2); }).join(""); };
  function store(k, v) { try { if (v === undefined) return sessionStorage.getItem(k); sessionStorage.setItem(k, v); } catch (e) { return null; } }
  // session: one per tab, renewed after 30 minutes without activity
  var sid = store("wo_rum_sid"), seen = +(store("wo_rum_seen") || 0);
  if (!sid || now() - seen > 30 * 60 * 1000) { sid = hex(12); store("wo_rum_sid", sid); store("wo_rum_in", Math.random() * 100 < SAMPLE ? "1" : "0"); }
  if (store("wo_rum_in") === "0") { window.watchover = { __v: 1, setUser: function () {}, action: function () {}, flush: function () {} }; return; }
  var user = store("wo_rum_user") || "";
  var q = [], timer = null;

  function strip(u) { try { var x = new URL(u, location.href); return x.origin + x.pathname; } catch (e) { return String(u || "").split(/[?#]/)[0]; } }
  function label(t) { return String(t || "").replace(/\s+/g, " ").trim().replace(/\d{4,}/g, "#").slice(0, 40); }
  function push(e) {
    e.ts = e.ts || now(); q.push(e); store("wo_rum_seen", String(now()));
    if (q.length >= 20) flush(); else if (!timer) timer = setTimeout(flush, 5000);
  }
  function payload() {
    var b = { session: sid, platform: "web", version: ds.version || "", env: ds.env || "", user: user || undefined, events: q.splice(0, 200) };
    return JSON.stringify(b);
  }
  function flush(beacon) {
    clearTimeout(timer); timer = null;
    if (!q.length) return;
    var body = payload();
    try {
      if (beacon && navigator.sendBeacon) { navigator.sendBeacon(URL_, new Blob([body], { type: "text/plain" })); return; }
      ofetch(URL_, { method: "POST", body: body, keepalive: true, headers: { "Content-Type": "text/plain" }, credentials: "omit" })["catch"](function () {});
    } catch (e) { /* the sensor never breaks the page */ }
  }

  // ---- current user step (a click or a route change): the trace its calls belong to
  var step = null;   // {trace, span, start, open, last, name, t}
  function begin(t, name) {
    end();
    step = { t: t, name: name, trace: hex(16), span: hex(8), start: now(), open: 0, last: now() };
    step.timer = setTimeout(maybeEnd, 1000);
  }
  function maybeEnd() { if (step && step.open === 0) end(); else if (step) step.timer = setTimeout(maybeEnd, 250); }
  function end() {
    if (!step) return;
    clearTimeout(step.timer);
    var s = step; step = null;
    if (s.t === "view") return endView(s);
    push({ t: "action", ts: s.start, name: s.name, dur: Math.max(0, s.last - s.start), trace: s.trace, span: s.span });
  }
  function inStep() { return step && now() - step.start < 30000 ? step : null; }

  // ---- network: fetch and XMLHttpRequest, timed, traceparent on propagated origins
  function propagate(u) { try { var o = new URL(u, location.href).origin; return PROP.indexOf(o) >= 0; } catch (e) { return false; } }
  function tp(trace, span) { return "00-" + trace + "-" + span + "-01"; }
  var ofetch = window.fetch ? window.fetch.bind(window) : null;
  if (ofetch) {
    window.fetch = function (input, init) {
      var url = typeof input === "string" ? input : (input && input.url) || "";
      if (url.indexOf(BASE + "/rum") === 0) return ofetch(input, init);
      var s = inStep(), trace = s ? s.trace : hex(16), span = hex(8), t0 = now();
      init = init || {};
      if (propagate(url)) {
        try { var h = new Headers(init.headers || (typeof input !== "string" && input.headers) || {}); h.set("traceparent", tp(trace, span)); init.headers = h; } catch (e) { /* opaque request */ }
      }
      if (s) s.open++;
      var done = function (st) {
        if (s) { s.open--; s.last = now(); }
        push({ t: "http", ts: t0, method: (init.method || (input && input.method) || "GET").toUpperCase(), url: strip(url), status: st, dur: now() - t0, trace: trace, span: span, parent: s ? s.span : undefined });
      };
      return ofetch(input, init).then(function (r) { done(r.status); return r; }, function (e) { done(0); throw e; });
    };
  }
  var XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__wo = { m: m, u: u }; return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    var x = this, w = x.__wo;
    if (w && String(w.u).indexOf(BASE + "/rum") !== 0) {
      var s = inStep(); w.trace = s ? s.trace : hex(16); w.span = hex(8); w.t0 = now(); w.s = s;
      if (propagate(w.u)) { try { x.setRequestHeader("traceparent", tp(w.trace, w.span)); } catch (e) { /* ignore */ } }
      if (s) s.open++;
      x.addEventListener("loadend", function () {
        if (w.s) { w.s.open--; w.s.last = now(); }
        push({ t: "http", ts: w.t0, method: String(w.m || "GET").toUpperCase(), url: strip(w.u), status: x.status, dur: now() - w.t0, trace: w.trace, span: w.span, parent: w.s ? w.s.span : undefined });
      });
    }
    return XS.apply(this, arguments);
  };

  // ---- clicks: buttons, links, explicit data-wo-action; label only, never values
  document.addEventListener("click", function (ev) {
    var el = ev.target && ev.target.closest ? ev.target.closest("[data-wo-action],button,a,[role=button],input[type=submit],input[type=button]") : null;
    if (!el) return;
    if (el.closest("[data-wo-mask]")) return begin("action", "click: " + el.tagName.toLowerCase());
    var name = el.getAttribute("data-wo-action") || el.getAttribute("aria-label") || (el.tagName === "INPUT" ? el.getAttribute("value") : el.innerText) || el.title || el.id || el.tagName.toLowerCase();
    begin("action", "click: " + label(name));
  }, true);

  // ---- views: first load with navigation timing and Web Vitals, then SPA route changes
  var vitals = {}, viewName = location.pathname, viewStart = 0, firstView = true;
  function endView(s) {
    var e = { t: "view", ts: s.start, name: s.name, dur: Math.max(0, (s.loaded || s.last) - s.start), trace: s.trace, span: s.span };
    if (s.first) e.vitals = vitals;
    push(e);
  }
  function routeView() {
    if (location.pathname === viewName) return;
    viewName = location.pathname; firstView = false;
    begin("view", viewName);
  }
  ["pushState", "replaceState"].forEach(function (k) { var o = history[k]; history[k] = function () { var r = o.apply(this, arguments); setTimeout(routeView, 0); return r; }; });
  window.addEventListener("popstate", routeView);
  try {
    new PerformanceObserver(function (l) { var es = l.getEntries(); if (es.length) vitals.lcp = Math.round(es[es.length - 1].startTime); }).observe({ type: "largest-contentful-paint", buffered: true });
    new PerformanceObserver(function (l) { l.getEntries().forEach(function (e) { if (!e.hadRecentInput) vitals.cls = Math.round(((vitals.cls || 0) + e.value) * 10000) / 10000; }); }).observe({ type: "layout-shift", buffered: true });
    new PerformanceObserver(function (l) { l.getEntries().forEach(function (e) { if (e.interactionId) vitals.inp = Math.max(vitals.inp || 0, Math.round(e.duration)); }); }).observe({ type: "event", buffered: true, durationThreshold: 40 });
    new PerformanceObserver(function (l) { l.getEntries().forEach(function (e) { if (e.name === "first-contentful-paint") vitals.fcp = Math.round(e.startTime); }); }).observe({ type: "paint", buffered: true });
  } catch (e) { /* older browser: no vitals */ }
  var firstStep = null;
  function onLoad() {
    var nav = (performance.getEntriesByType && performance.getEntriesByType("navigation")[0]) || null;
    var start = performance.timeOrigin ? Math.round(performance.timeOrigin) : now();
    if (nav) vitals.ttfb = Math.round(nav.responseStart);
    var loaded = start + Math.round(nav ? (nav.loadEventEnd || nav.domContentLoadedEventEnd || performance.now()) : performance.now());
    firstStep = { t: "view", name: location.pathname, trace: hex(16), span: hex(8), start: start, loaded: loaded, last: loaded, first: true };
    setTimeout(function () { if (firstStep) { var f = firstStep; firstStep = null; endView(f); } }, 10000);   // vitals settle; report after 10 s at most
  }
  if (document.readyState === "complete") setTimeout(onLoad, 0); else window.addEventListener("load", function () { setTimeout(onLoad, 0); });

  // ---- errors
  function err(msg, stack) { push({ t: "error", msg: label(msg).length ? String(msg).slice(0, 300) : "error", stack: String(stack || "").slice(0, 2000), trace: step ? step.trace : undefined }); }
  window.addEventListener("error", function (e) { if (e && e.message) err(e.message, e.error && e.error.stack); });
  window.addEventListener("unhandledrejection", function (e) { var r = e && e.reason; err(r && r.message ? r.message : String(r), r && r.stack); });

  // ---- leaving: close the open step, send what is left with a beacon
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") { if (firstStep) { var f = firstStep; firstStep = null; endView(f); } end(); flush(true); }
  });
  window.addEventListener("pagehide", function () { end(); flush(true); });

  window.watchover = {
    __v: 1,
    setUser: function (id) { user = String(id || ""); store("wo_rum_user", user); },
    action: function (name) { begin("action", label(name)); },
    error: function (e) { err(e && e.message ? e.message : String(e), e && e.stack); },
    flush: function () { end(); flush(); },
    session: function () { return sid; }
  };
})();
