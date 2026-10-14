// Watchover mobile sensor for React Native (reference implementation, no native module needed).
//
//   import Watchover from './watchover';
//   Watchover.start({ endpoint: 'https://watchover.sirket.com.tr/mobile', key: 'pk_...', version: '3.4.1', platform: Platform.OS });
//   <NavigationContainer onStateChange={Watchover.onNavigationChange}>       // react-navigation screen views
//   Watchover.setUser(personnelNo);                                            // pseudonymised by the server
//
// Records: app start, screen views, fetch calls (method, URL without query, status, duration, traceparent), JS errors and fatal
// errors (crashes). Never records: text inputs, screen content, location, query strings, bodies.
const state = { endpoint: '', key: '', version: '', platform: 'react-native', user: undefined, session: hex(12), queue: [], current: null, started: false };
const t0 = Date.now();

function hex(n) {
  let s = '';
  for (let i = 0; i < n; i++) s += Math.floor(Math.random() * 256).toString(16).padStart(2, '0');
  return s;
}

function push(e) {
  state.queue.push(e);
  if (state.queue.length >= 30) flush();
}

async function flush() {
  if (!state.queue.length || !state.endpoint) return;
  const batch = state.queue.splice(0, state.queue.length);
  const body = { session: state.session, platform: state.platform, version: state.version, user: state.user, events: batch };
  try {
    const r = await state.fetch(`${state.endpoint}?k=${encodeURIComponent(state.key)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (r.status >= 500) throw new Error(`receiver ${r.status}`);
  } catch (e) {
    if (state.queue.length < 1000) state.queue.unshift(...batch);
  }
}

function strip(u) { return String(u || '').split(/[?#]/)[0]; }

function screen(name) {
  state.current = { trace: hex(16), span: hex(8), start: Date.now() };
  push({ t: 'view', ts: state.current.start, name: String(name).slice(0, 80), trace: state.current.trace, span: state.current.span });
}

function action(name) {
  state.current = { trace: hex(16), span: hex(8), start: Date.now() };
  push({ t: 'action', ts: state.current.start, name: String(name).replace(/\d{4,}/g, '#').slice(0, 60), trace: state.current.trace, span: state.current.span });
}

function start({ endpoint, key, version = '', platform = 'react-native' }) {
  if (state.started) return;
  state.started = true;
  state.endpoint = String(endpoint).replace(/\/$/, '');
  state.key = key; state.version = version; state.platform = platform;
  state.fetch = global.fetch.bind(global);
  push({ t: 'start', ts: t0, dur: Date.now() - t0, cold: true });
  global.fetch = (input, init = {}) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (url.startsWith(state.endpoint)) return state.fetch(input, init);
    const c = state.current && Date.now() - state.current.start < 10000 ? state.current : null;
    const trace = c ? c.trace : hex(16), span = hex(8), ts = Date.now();
    const headers = Object.assign({}, init.headers || {}, { traceparent: `00-${trace}-${span}-01` });
    const done = (status) => push({ t: 'http', ts, method: (init.method || 'GET').toUpperCase(), url: strip(url), status, dur: Date.now() - ts, trace, span, parent: c ? c.span : undefined });
    return state.fetch(input, Object.assign({}, init, { headers })).then((r) => { done(r.status); return r; }, (e) => { done(0); throw e; });
  };
  const prev = global.ErrorUtils && global.ErrorUtils.getGlobalHandler();
  if (global.ErrorUtils) {
    global.ErrorUtils.setGlobalHandler((error, isFatal) => {
      push({ t: isFatal ? 'crash' : 'error', ts: Date.now(), msg: String((error && error.message) || error).slice(0, 300), stack: String((error && error.stack) || '').split('\n').slice(0, 15).join('\n') });
      flush();
      if (prev) prev(error, isFatal);
    });
  }
  setInterval(flush, 10000);
}

function onNavigationChange(navState) {
  let s = navState;
  while (s && s.routes && s.index !== undefined) s = s.routes[s.index].state || s.routes[s.index];
  if (s && s.name) screen(s.name);
}

const Watchover = { start, screen, action, flush, onNavigationChange, setUser: (id) => { state.user = id || undefined; }, error: (e) => push({ t: 'error', ts: Date.now(), msg: String((e && e.message) || e).slice(0, 300) }) };
export default Watchover;
