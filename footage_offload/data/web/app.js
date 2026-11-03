/* Footage Offload Station web UI. Plain JS, no dependencies. */
(function () {
'use strict';
var LANGS = ['en', 'de', 'fr', 'it'];
var $ = function (s, r) { return (r || document).querySelector(s); };
var S = { session: null, cat: {}, fallback: {}, lang: 'en', status: null, route: null,
          es: null, poll: null, settings: null, gen: 0 };

/* ---------- DOM helper ---------- */
function h(tag, attrs) {
  var el = document.createElement(tag);
  if (attrs) Object.keys(attrs).forEach(function (k) {
    var v = attrs[k];
    if (v === null || v === undefined || v === false) return;
    if (k === 'class') el.className = v;
    else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
    else if (k === 'text') el.textContent = v;
    else el.setAttribute(k, v === true ? '' : v);
  });
  for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
  return el;
}
function add(el, c) {
  if (c === null || c === undefined || c === false) return;
  if (Array.isArray(c)) c.forEach(function (x) { add(el, x); });
  else el.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
}
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

/* ---------- i18n ---------- */
function plural(lang, n) {
  if (lang === 'fr') return (n === 0 || n === 1) ? 'one' : 'other';
  return n === 1 ? 'one' : 'other';
}
function fmtNum(n, dec) {
  if (n === null || n === undefined || isNaN(n)) return '–';
  var s = Math.abs(Number(n)).toFixed(dec || 0), parts = s.split('.');
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, "'");
  return (n < 0 ? '-' : '') + parts.join('.');
}
function t(id, params) {
  var e = S.cat[id];
  if (e === undefined) e = S.fallback[id];
  if (e === undefined) return id;
  params = params || {};
  if (typeof e === 'object') e = e[plural(S.lang, Number(params.n))] || e.other;
  return e.replace(/\{(\w+)\}/g, function (m, k) {
    if (!(k in params)) return m;
    return (k === 'n' && typeof params[k] === 'number') ? fmtNum(params[k]) : params[k];
  });
}
function has(id) { return S.cat[id] !== undefined || S.fallback[id] !== undefined; }
function pad(n) { return n < 10 ? '0' + n : '' + n; }
function fmtDate(d) { return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear(); }
function fmtTime(d) { return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
function fmtDateTime(x) {
  var d = typeof x === 'number' ? new Date(x * 1000) : new Date(x);
  return isNaN(d) ? '–' : fmtDate(d) + ' ' + fmtTime(d);
}
function fmtRun(name) {
  var m = /^(\d{4})-(\d\d)-(\d\d)_(\d\d)-(\d\d)/.exec(name || '');
  return m ? m[3] + '.' + m[2] + '.' + m[1] + ' ' + m[4] + ':' + m[5] : (name || '–');
}
function fmtBytes(b) {
  if (b === null || b === undefined) return '–';
  if (b >= 1e9) return fmtNum(b / 1e9, 1) + ' ' + t('unit.gb');
  if (b >= 1e6) return fmtNum(b / 1e6, 1) + ' ' + t('unit.mb');
  if (b >= 1e3) return fmtNum(b / 1e3, 1) + ' ' + t('unit.kb');
  return fmtNum(b) + ' ' + t('unit.b');
}
function fmtSpeed(bps) { return fmtNum((bps || 0) / 1e6, 1) + ' ' + t('unit.mbps'); }
function fmtDur(s) {
  if (s === null || s === undefined || isNaN(s)) return '–';
  s = Math.round(s);
  var hh = Math.floor(s / 3600), mm = Math.floor(s % 3600 / 60), ss = s % 60;
  if (hh) return hh + ' ' + t('duration.h') + ' ' + pad(mm) + ' ' + t('duration.min');
  if (mm) return mm + ' ' + t('duration.min') + ' ' + pad(ss) + ' ' + t('duration.s');
  return ss + ' ' + t('duration.s');
}
function msg(id, params) {
  params = Object.assign({}, params || {});
  if (params.camera_time) params.camera_time = fmtDateTime(params.camera_time);
  if (typeof params.difference_s === 'number') params.difference_s = fmtDur(Math.abs(params.difference_s));
  if (params.detail !== undefined && params.n === undefined && /^\d+$/.test(String(params.detail))) params.n = Number(params.detail);
  return has(id) ? t(id, params) : (id || t('error.generic'));
}
function errText(e) {
  if (!e) return '';
  var s = msg(e.id, { detail: e.detail || '' });
  if (e.detail && s.indexOf(e.detail) < 0 && !/^\d+$/.test(String(e.detail))) s += ' (' + e.detail + ')';
  return s;
}
function loadCat(lang) {
  return fetch('/api/v1/i18n/' + lang).then(function (r) { return r.json(); });
}
function pickLang(pref) {
  if (LANGS.indexOf(pref) >= 0) return pref;
  var l = navigator.languages || [navigator.language || 'en'];
  for (var i = 0; i < l.length; i++) {
    var c = String(l[i]).toLowerCase().slice(0, 2);
    if (LANGS.indexOf(c) >= 0) return c;
  }
  return 'en';
}
function setLang(pref) {
  var lang = pickLang(pref);
  var p = Promise.all([lang === 'en' ? null : loadCat(lang), S.fallback.__loaded ? null : loadCat('en')]);
  return p.then(function (r) {
    if (!S.fallback.__loaded) { S.fallback = r[1] || {}; S.fallback.__loaded = true; }
    S.cat = r[0] || S.fallback;
    S.lang = lang;
    document.documentElement.lang = lang;
    document.title = t('app.title');
    applyStatic();
  }).catch(function () { /* keep current catalogue */ });
}
function applyStatic() {
  document.querySelectorAll('[data-i18n]').forEach(function (el) { el.textContent = t(el.getAttribute('data-i18n')); });
  $('#nav').setAttribute('aria-label', t('nav.main'));
  $('#app-title').textContent = t('app.title');
  $('#logout').textContent = t('login.logout');
  $('#dlg-cancel').textContent = t('common.cancel');
  $('#dlg-media-close').textContent = t('common.close');
  var sk = $('.skip'); sk.textContent = t('nav.status') + ' ↓';
  setConn(S.connected !== false);
}

/* ---------- API ---------- */
function api(method, path, body) {
  var opt = { method: method, headers: {}, credentials: 'same-origin' };
  if (method !== 'GET') {
    opt.headers['X-CSRF-Token'] = (S.session && S.session.csrf_token) || '';
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  }
  return fetch(path, opt).then(function (r) {
    return r.text().then(function (txt) {
      var data = null;
      try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = null; }
      if (r.status === 401 && path.indexOf('/login') < 0) { showLogin(); }
      if (!r.ok) { var err = new Error('http ' + r.status); err.status = r.status; err.data = data; throw err; }
      return data;
    });
  }, function () { var err = new Error('network'); err.status = 0; err.data = { error: 'error.network' }; throw err; });
}
function errId(e) { return (e && e.data && e.data.error) || (e && e.status === 0 ? 'error.network' : 'error.generic'); }

/* ---------- dialogs and toast ---------- */
function confirmDlg(text, okLabel, danger) {
  return new Promise(function (resolve) {
    var d = $('#dlg-confirm');
    $('#dlg-confirm-text').textContent = text;
    var ok = $('#dlg-ok'); ok.textContent = okLabel || t('common.confirm');
    ok.className = 'btn ' + (danger ? 'danger' : 'primary');
    $('#dlg-cancel').textContent = t('common.cancel');
    d.returnValue = '';
    d.addEventListener('close', function f() { d.removeEventListener('close', f); resolve(d.returnValue === 'ok'); });
    d.showModal();
  });
}
var toastTimer;
function toast(text) {
  var el = $('#toast'); el.textContent = text; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(function () { el.hidden = true; }, 5000);
}
function setConn(ok) {
  S.connected = ok;
  var c = $('#conn'); c.className = 'conn' + (ok ? '' : ' off'); c.textContent = t(ok ? 'conn.live' : 'conn.offline');
}

/* ---------- session / login ---------- */
function showLogin() {
  stopLive();
  S.route = null;
  $('#nav').hidden = true; $('#logout').hidden = true;
  var view = $('#view'); clear(view);
  var pw = h('input', { type: 'password', id: 'lp', autocomplete: 'current-password', required: true });
  var err = h('p', { class: 'ferr', role: 'alert' });
  var form = h('form', { class: 'card login', onsubmit: function (ev) {
    ev.preventDefault(); err.textContent = '';
    api('POST', '/api/v1/login', { password: pw.value }).then(function () { return boot(); })
      .catch(function (e) { err.textContent = msg(errId(e)); pw.select(); });
  } },
    h('h2', { text: t('login.title') }),
    h('div', { class: 'field' }, h('label', { for: 'lp', text: t('login.password') }), pw, err),
    h('button', { class: 'btn primary', type: 'submit', text: t('login.submit') }));
  view.appendChild(form); pw.focus();
}
function boot() {
  return fetch('/api/v1/session', { credentials: 'same-origin' }).then(function (r) { return r.json(); })
    .then(function (s) {
      S.session = s;
      return setLang(s.language || 'auto').then(function () {
        if (s.login_enabled && !s.logged_in) { showLogin(); return; }
        $('#nav').hidden = false;
        $('#logout').hidden = !s.login_enabled;
        startLive();
        route();
      });
    }).catch(function () {
      $('#view').textContent = t('error.network');
      setTimeout(boot, 5000);
    });
}

/* ---------- live status ---------- */
function stopLive() {
  if (S.es) { S.es.close(); S.es = null; }
  clearInterval(S.poll); S.poll = null;
}
function startLive() {
  stopLive();
  function fallback() { if (!S.poll) { S.poll = setInterval(pollOnce, 5000); pollOnce(); } }
  function open() {
    if (typeof EventSource === 'undefined') { fallback(); return; }
    var es = new EventSource('/api/v1/events'); S.es = es;
    es.addEventListener('status', function (e) { try { applyStatus(JSON.parse(e.data)); } catch (x) { } });
    es.onopen = function () { setConn(true); clearInterval(S.poll); S.poll = null; };
    es.onerror = function () {
      setConn(false); fallback();
      if (es.readyState === 2) { setTimeout(function () { if (S.es === es) open(); }, 5000); }
    };
  }
  open();
}
function pollOnce() {
  api('GET', '/api/v1/status').then(function (s) { setConn(true); applyStatus(s); })
    .catch(function () { setConn(false); });
}
function applyStatus(s) {
  var old = S.status; S.status = s;
  var announce = !old || old.state !== s.state || JSON.stringify(old.error) !== JSON.stringify(s.error);
  if (announce) {
    var txt = t('status.' + s.state) + (s.error ? '. ' + errText(s.error) : '');
    $('#live').textContent = txt;
  }
  if (S.route === 'status') renderStatus();
  if (S.route === 'stick' && old && JSON.stringify(old.stick) !== JSON.stringify(s.stick)) renderStick();
}

/* ---------- routing ---------- */
var ROUTES = { status: renderStatus, stick: renderStick, history: renderHistory,
               gallery: renderGallery, settings: renderSettings, system: renderSystem };
function route() {
  var r = (location.hash || '#status').slice(1);
  if (!ROUTES[r]) r = 'status';
  var changed = S.route !== r;
  S.route = r; S.gen++;
  document.querySelectorAll('nav a').forEach(function (a) {
    if (a.getAttribute('data-route') === r) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  if (changed) { clear($('#view')); }
  ROUTES[r]();
}
window.addEventListener('hashchange', function () { if (S.session) route(); });

function view() { return $('#view'); }
function loading(el) { clear(el); el.appendChild(h('p', { class: 'muted', text: t('common.loading') })); }
function failed(el, e, again) {
  clear(el);
  el.appendChild(h('div', { class: 'alert err', role: 'alert' }, msg(errId(e)), ' ',
    h('button', { class: 'btn small', text: t('common.retry'), onclick: again })));
}
function title(key) { return h('h2', { text: t(key) }); }
function kv(rows) {
  var dl = h('dl', { class: 'kv' });
  rows.forEach(function (r) { if (r) { dl.appendChild(h('dt', { text: r[0] })); dl.appendChild(h('dd', null, r[1])); } });
  return dl;
}

/* ---------- 1. status ---------- */
function renderStatus() {
  var el = view(), s = S.status;
  if (!s) { loading(el); pollOnce(); return; }
  clear(el);
  el.appendChild(title('status.title'));
  var pct = s.bytes_total > 0 ? Math.min(100, s.bytes_done * 100 / s.bytes_total) : (s.state === 'done' ? 100 : 0);
  var card = h('section', { class: 'card' }, h('p', { class: 'state ' + s.state, text: t('status.' + s.state) }));
  if (s.error) card.appendChild(h('div', { class: 'alert err', role: 'alert' }, errText(s.error)));
  var d = s.device;
  var rows = [[t('status.device'), d ? [(d.manufacturer && d.model.indexOf(d.manufacturer) !== 0 ? d.manufacturer + ' ' : '') + d.model] : t('status.no_device')]];
  if (d && d.serial) rows.push([t('status.serial'), d.serial]);
  if (s.state === 'transferring' || s.files_total) {
    card.appendChild(h('progress', { max: 100, value: pct.toFixed(1), 'aria-label': t('status.progress') }));
    card.appendChild(h('p', { class: 'small-text muted' },
      fmtNum(pct, 0) + ' % · ' + fmtBytes(s.bytes_done) + ' / ' + fmtBytes(s.bytes_total) + ' · ' +
      t('status.files_of', { done: fmtNum(s.files_done), total: fmtNum(s.files_total) })));
    if (s.current_file && s.state === 'transferring') rows.push([t('status.current_file'), s.current_file]);
    if (s.state === 'transferring') {
      rows.push([t('status.speed'), fmtSpeed(s.speed_bps)]);
      rows.push([t('status.eta'), s.eta_s === null || s.eta_s === undefined ? '–' : fmtDur(s.eta_s)]);
    }
  }
  if (s.run_folder) rows.push([t('status.run_folder'), fmtRun(s.run_folder)]);
  if (s.nas && s.nas.state && s.nas.state !== 'idle') rows.push([t('status.nas'), t('status.nas.' + s.nas.state) + (s.nas.detail ? ' (' + s.nas.detail + ')' : '')]);
  card.appendChild(kv(rows));
  el.appendChild(card);
  var w = h('section', { class: 'card' }, h('h3', { text: t('status.warnings') }));
  var ws = s.warnings || [];
  if (!ws.length) w.appendChild(h('p', { class: 'muted', text: t('status.no_warnings') }));
  ws.forEach(function (x) {
    w.appendChild(h('div', { class: 'alert ' + (x.id === 'warning.camera_clock_set' ? 'ok' : 'warn') }, msg(x.id, x.params)));
  });
  el.appendChild(w);
  el.appendChild(h('p', { class: 'small-text muted' }, t('status.mtp_notice')));
}

/* ---------- 2. stick ---------- */
function ejectStick(btn) {
  confirmDlg(t('stick.eject_confirm'), t('stick.eject'), true).then(function (ok) {
    if (!ok) return;
    btn.disabled = true;
    api('POST', '/api/v1/stick/eject', {}).then(function () { toast(t('stick.eject_done')); renderStick(); })
      .catch(function (e) { btn.disabled = false; toast(msg(errId(e))); });
  });
}
function renderStick() {
  var el = view(), gen = S.gen;
  if (!el.firstChild) loading(el);
  api('GET', '/api/v1/stick').then(function (st) {
    if (gen !== S.gen) return;
    clear(el); el.appendChild(title('stick.title'));
    if (!st || !st.present) { el.appendChild(h('p', { class: 'card', text: st && st.ejected ? t('stick.ejected') : t('stick.none') })); return; }
    var used = st.total - st.free, pct = st.total ? used * 100 / st.total : 0;
    var low = st.total && st.free / st.total < 0.1;
    var card = h('section', { class: 'card' },
      h('div', { class: 'meter' + (low ? ' low' : ''), role: 'img', 'aria-label': t('stick.free') + ': ' + fmtBytes(st.free) },
        h('i', { style: 'width:' + pct.toFixed(1) + '%' })),
      kv([[t('stick.capacity'), fmtBytes(st.total)], [t('stick.free'), fmtBytes(st.free)],
          [t('stick.used'), fmtBytes(used)], [t('stick.fs'), (st.label ? st.label + ' · ' : '') + (st.fstype || '–')],
          [t('stick.oldest'), st.oldest ? fmtRun(st.oldest) : '–'], [t('stick.newest'), st.newest ? fmtRun(st.newest) : '–']]));
    if (st.read_only) card.appendChild(h('div', { class: 'alert warn' }, t('stick.read_only')));
    el.appendChild(card);
    var runs = st.runs || [];
    var list = h('section', { class: 'card' }, h('h3', { text: t('stick.runs') + ' (' + fmtNum(runs.length) + ')' }));
    if (!runs.length) list.appendChild(h('p', { class: 'muted', text: t('common.empty') }));
    var ul = h('ul', { class: 'plain' });
    runs.slice().reverse().forEach(function (r) {
      ul.appendChild(h('li', null, h('span', { text: fmtRun(r.name) }),
        h('span', { class: 'muted', text: t('files.count', { n: r.files }) + ' · ' + fmtBytes(r.size) })));
    });
    list.appendChild(ul); el.appendChild(list);
    var btn = h('button', { class: 'btn danger', text: t('stick.eject') });
    btn.addEventListener('click', function () { ejectStick(btn); });
    el.appendChild(btn);
  }).catch(function (e) { if (gen === S.gen) failed(el, e, renderStick); });
}

/* ---------- 3. history ---------- */
function renderHistory() {
  var el = view(), gen = S.gen, limit = 20, offset = 0;
  loading(el);
  var out = h('div'), more = h('div');
  function load() {
    return api('GET', '/api/v1/history?limit=' + limit + '&offset=' + offset).then(function (d) {
      if (gen !== S.gen) return;
      if (offset === 0) {
        clear(el); el.appendChild(title('history.title')); el.appendChild(out); el.appendChild(more);
        if (!(d.runs || []).length) out.appendChild(h('p', { class: 'card muted', text: t('history.empty') }));
        el.appendChild(h('h2', { text: t('history.devices') }));
        var dv = h('div');
        (d.devices || []).forEach(function (x) {
          dv.appendChild(h('div', { class: 'card' }, h('strong', { text: devName(x) }),
            kv([[t('status.serial'), x.serial], [t('history.files'), t('files.count', { n: x.files })],
                [t('history.transferred'), fmtBytes(x.bytes)], [t('history.runs'), t('runs.count', { n: x.runs })],
                [t('history.first_seen'), fmtDateTime(x.first_seen)], [t('history.last_seen'), fmtDateTime(x.last_seen)]])));
        });
        if (!(d.devices || []).length) dv.appendChild(h('p', { class: 'muted', text: t('history.no_devices') }));
        el.appendChild(dv);
      }
      var card = out.lastChild && out.lastChild.className === 'card' ? out.lastChild : out.appendChild(h('section', { class: 'card' }));
      (d.runs || []).forEach(function (r) { card.appendChild(runRow(r)); });
      clear(more);
      if ((d.runs || []).length === limit) {
        more.appendChild(h('button', { class: 'btn', text: t('common.show_more'), onclick: function () { offset += limit; load(); } }));
      }
    }).catch(function (e) { if (gen === S.gen) failed(el, e, renderHistory); });
  }
  load();
}
function devName(x) { return ((x.manufacturer && String(x.model).indexOf(x.manufacturer) !== 0 ? x.manufacturer + ' ' : '') + (x.model || '')).trim() || t('common.unknown'); }
function runRow(r) {
  var errs = r.errors || [];
  var res = r.result || 'running';
  var row = h('article', { class: 'run' },
    h('div', { class: 'head' }, h('strong', { text: fmtDateTime(r.started_at) }),
      h('span', { class: 'badge ' + res, text: t('history.result.' + res) })),
    h('div', { text: devName(r) + (r.serial ? ' · ' + r.serial : '') }),
    h('div', { class: 'muted' }, t('files.count', { n: r.files }) + ' · ' + fmtBytes(r.bytes) + ' · ' + fmtDur(r.duration_s)));
  if (r.files_left) row.appendChild(h('div', { class: 'small-text muted' }, t('history.files_failed', { n: r.files_left })));
  if (r.error_id || errs.length) {
    var d = h('details', null, h('summary', { text: t('history.errors') + (errs.length ? ' (' + fmtNum(errs.length) + ')' : '') }));
    if (r.error_id) d.appendChild(h('div', { class: 'alert err' }, errText({ id: r.error_id, detail: r.error_detail })));
    errs.forEach(function (e) {
      d.appendChild(h('div', { class: 'small-text' }, h('strong', { text: e.file ? e.file + ': ' : '' }), errText(e)));
    });
    row.appendChild(d);
  }
  return row;
}

/* ---------- 4. gallery ---------- */
function openMedia(it) {
  var d = $('#dlg-media'), body = $('#dlg-media-body');
  clear(body);
  $('#dlg-media-title').textContent = it.name;
  if (it.preview) body.appendChild(h('video', { controls: true, autoplay: true, playsinline: true, src: it.preview, poster: it.thumb || null }));
  else if (it.thumb) body.appendChild(h('img', { src: it.thumb, alt: it.name }));
  else body.appendChild(h('p', { class: 'muted', text: t('gallery.no_preview') }));
  body.appendChild(h('p', { class: 'small-text muted', text: fmtBytes(it.size) + ' · ' + t('gallery.note') }));
  var dl = $('#dlg-media-dl'); dl.href = it.download; dl.textContent = t('gallery.download'); dl.setAttribute('download', it.name);
  $('#dlg-media-close').textContent = t('common.close');
  d.showModal();
}
function renderGallery() {
  var el = view(), gen = S.gen;
  loading(el);
  api('GET', '/api/v1/gallery').then(function (g) {
    if (gen !== S.gen) return;
    clear(el); el.appendChild(title('gallery.title'));
    el.appendChild(h('p', { class: 'small-text muted', text: t('gallery.note') }));
    var runs = (g.runs || []).slice().reverse();
    if (!runs.length) el.appendChild(h('p', { class: 'card muted', text: t('gallery.empty') }));
    runs.forEach(function (run, i) {
      var grid = h('div', { class: 'grid' });
      (run.items || []).forEach(function (it) {
        var label = it.name;
        var b = h('button', { class: 'tile' + (it.thumb ? '' : ' ph'), 'aria-label': label + ' (' + t('gallery.' + (it.kind || 'other')) + ')', onclick: function () { openMedia(it); } },
          it.thumb ? h('img', { src: it.thumb, alt: '', loading: 'lazy', decoding: 'async' }) : t('gallery.' + (it.kind || 'other')),
          h('span', { text: label }));
        grid.appendChild(b);
      });
      el.appendChild(h('details', { class: 'card', open: i === 0 },
        h('summary', null, fmtRun(run.name) + ' · ' + t('files.count', { n: (run.items || []).length })), grid));
    });
  }).catch(function (e) { if (gen === S.gen) failed(el, e, renderGallery); });
}
$('#dlg-media-close').addEventListener('click', function () { $('#dlg-media').close(); });
$('#dlg-media').addEventListener('close', function () {
  var v = $('#dlg-media video'); if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
  clear($('#dlg-media-body'));
});

/* ---------- 5. settings ---------- */
var FIELDS = [
  { sec: 'general', key: 'language', type: 'lang' },
  { sec: 'general', key: 'min_free_mb', type: 'int', hint: true },
  { sec: 'security', key: 'login_enabled', type: 'bool' },
  { sec: 'security', key: 'password', type: 'newpw', hint: true },
  { sec: 'camera_wifi', key: 'camera_wifi_enabled', type: 'bool' },
  { sec: 'camera_wifi', key: 'camera_wifi_ssid', type: 'text' },
  { sec: 'camera_wifi', key: 'camera_wifi_password', type: 'secret', hintId: 'settings.camera_wifi.hint' },
  { sec: 'samba', key: 'samba_enabled', type: 'bool' },
  { sec: 'nas', key: 'nas_enabled', type: 'bool' },
  { sec: 'nas', key: 'nas_method', type: 'select', options: ['rsync', 'smb'] },
  { sec: 'nas', key: 'nas_target', type: 'text', hint: true },
  { sec: 'nas', key: 'nas_username', type: 'text' },
  { sec: 'nas', key: 'nas_password', type: 'secret' },
  { sec: 'mqtt', key: 'mqtt_enabled', type: 'bool' },
  { sec: 'mqtt', key: 'mqtt_host', type: 'text' },
  { sec: 'mqtt', key: 'mqtt_port', type: 'int' },
  { sec: 'mqtt', key: 'mqtt_username', type: 'text' },
  { sec: 'mqtt', key: 'mqtt_password', type: 'secret' },
  { sec: 'mqtt', key: 'mqtt_topic', type: 'text' },
  { sec: 'updates', key: 'update_check_enabled', type: 'bool' },
  { sec: 'advanced', key: 'transfer_mode', type: 'select', options: ['auto', 'partial', 'whole'] },
  { sec: 'advanced', key: 'chunk_size_kb', type: 'int' },
  { sec: 'advanced', key: 'retry_count', type: 'int' },
  { sec: 'advanced', key: 'retry_base_delay_s', type: 'number' },
  { sec: 'advanced', key: 'web_port', type: 'int', hint: true }
];
function renderSettings() {
  var el = view(), gen = S.gen;
  loading(el);
  api('GET', '/api/v1/settings').then(function (cfg) {
    if (gen !== S.gen) return;
    var inputs = {}, errEls = {}, wraps = {};
    var form = h('form', { novalidate: true });
    var secs = {};
    FIELDS.forEach(function (f) {
      var id = 'f-' + f.key, input, wrap;
      var label = t('settings.' + f.key);
      if (f.type === 'bool') {
        input = h('input', { type: 'checkbox', id: id });
        input.checked = !!cfg[f.key];
      } else if (f.type === 'select' || f.type === 'lang') {
        input = h('select', { id: id });
        var opts = f.type === 'lang' ? ['auto'].concat(LANGS) : f.options;
        opts.forEach(function (o) {
          var txt = f.type === 'lang' ? (o === 'auto' ? t('settings.language.auto') : S.fallback && o) : t('settings.' + f.key + '.' + o);
          input.appendChild(h('option', { value: o, text: txt === o && f.type === 'select' ? o : txt }));
        });
        input.value = cfg[f.key];
      } else if (f.type === 'secret' || f.type === 'newpw') {
        input = h('input', { type: 'password', id: id, autocomplete: 'new-password',
          placeholder: f.type === 'newpw' ? '' : (cfg[f.key + '_set'] ? t('settings.secret_set') : t('settings.secret_empty')) });
        if (f.type === 'newpw' && cfg.password_hash_set) input.placeholder = t('settings.secret_set');
      } else {
        input = h('input', { type: f.type === 'text' ? 'text' : 'number', id: id, autocomplete: 'off', inputmode: f.type === 'int' ? 'numeric' : (f.type === 'number' ? 'decimal' : null), step: f.type === 'number' ? 'any' : null });
        input.value = cfg[f.key] === undefined || cfg[f.key] === null ? '' : cfg[f.key];
      }
      inputs[f.key] = input;
      var hid = id + '-d', desc = [];
      var errEl = h('p', { class: 'ferr', id: id + '-e', role: 'alert' });
      errEls[f.key] = errEl;
      var hint = f.hintId ? t(f.hintId) : (f.hint ? t('settings.' + f.key + '.hint') : null);
      var hintEl = hint ? h('p', { class: 'hint', id: hid, text: hint }) : null;
      if (hintEl) input.setAttribute('aria-describedby', hid + ' ' + id + '-e'); else input.setAttribute('aria-describedby', id + '-e');
      if (f.type === 'bool') wrap = h('div', { class: 'field check' }, h('label', { for: id }, input, label), hintEl, errEl);
      else wrap = h('div', { class: 'field' }, h('label', { for: id, text: label }), input, hintEl, errEl);
      wraps[f.key] = wrap;
      if (!secs[f.sec]) {
        if (f.sec === 'advanced') {
          var det = h('details', { class: 'card' }, h('summary', { text: t('settings.section.advanced') }));
          secs[f.sec] = det;
        } else {
          secs[f.sec] = h('fieldset', null, h('legend', { text: t('settings.section.' + f.sec) }));
        }
        form.appendChild(secs[f.sec]);
      }
      secs[f.sec].appendChild(wrap);
    });
    var banner = h('div', { role: 'alert' });
    var save = h('button', { class: 'btn primary', type: 'submit', text: t('common.save') });
    form.appendChild(banner); form.appendChild(save);
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var body = {};
      FIELDS.forEach(function (f) {
        var i = inputs[f.key], v;
        if (f.type === 'bool') v = i.checked;
        else if (f.type === 'secret' || f.type === 'newpw') { if (i.value === '') return; v = i.value; }
        else if (f.type === 'int') { v = i.value === '' ? null : parseInt(i.value, 10); if (v === null || isNaN(v)) v = i.value; }
        else if (f.type === 'number') { v = i.value === '' ? null : parseFloat(i.value); if (v === null || isNaN(v)) v = i.value; }
        else v = i.value;
        body[f.key] = v;
      });
      clearErrors();
      save.disabled = true;
      api('PUT', '/api/v1/settings', body).then(function () {
        save.disabled = false;
        FIELDS.forEach(function (f) { if (f.type === 'secret' || f.type === 'newpw') { if (inputs[f.key].value) { inputs[f.key].placeholder = t('settings.secret_set'); inputs[f.key].value = ''; } } });
        toast(t('settings.saved'));
        var lang = body.language;
        return api('GET', '/api/v1/session').then(function (s) {
          S.session = s; return setLang(s.language || lang || 'auto');
        }).then(function () { $('#logout').hidden = !S.session.login_enabled; renderSettings(); });
      }).catch(function (e) {
        save.disabled = false;
        var errs = e.data && e.data.errors;
        if (e.status === 400 && errs) {
          var first = null;
          Object.keys(errs).forEach(function (k) {
            if (errEls[k]) { errEls[k].textContent = msg(errs[k]); wraps[k].classList.add('bad'); inputs[k].setAttribute('aria-invalid', 'true'); first = first || inputs[k]; }
            else banner.appendChild(h('div', { class: 'alert err' }, k + ': ' + msg(errs[k])));
          });
          banner.insertBefore(h('div', { class: 'alert err', text: t('settings.fix_errors') }), banner.firstChild);
          if (first) first.focus();
        } else if (e.status !== 401) banner.appendChild(h('div', { class: 'alert err' }, msg(errId(e))));
      });
    });
    function clearErrors() {
      clear(banner);
      Object.keys(errEls).forEach(function (k) { errEls[k].textContent = ''; wraps[k].classList.remove('bad'); inputs[k].removeAttribute('aria-invalid'); });
    }
    clear(el); el.appendChild(title('settings.title')); el.appendChild(form);
    // language names are shown in their own language
    ['en', 'de', 'fr', 'it'].forEach(function (c) {
      var o = inputs.language.querySelector('option[value=' + c + ']');
      if (o) o.textContent = { en: 'English', de: 'Deutsch', fr: 'Français', it: 'Italiano' }[c];
    });
  }).catch(function (e) { if (gen === S.gen) failed(el, e, renderSettings); });
}

/* ---------- 6. system ---------- */
function powerAction(kind, btn) {
  confirmDlg(t('system.' + kind + '_confirm'), t('system.' + kind), true).then(function (ok) {
    if (!ok) return;
    function send(body) {
      return api('POST', '/api/v1/system/' + kind, body).then(function () {
        toast(t(kind === 'shutdown' ? 'system.shutting_down' : 'system.rebooting'));
      }).catch(function (e) {
        if (e.status === 409 && errId(e) === 'error.busy' && !body.force) {
          return confirmDlg(t('system.force_confirm'), t('system.' + kind), true).then(function (ok2) { if (ok2) return send({ force: true }); });
        }
        toast(msg(errId(e)));
      });
    }
    send({});
  });
}
function renderSystem() {
  var el = view(), gen = S.gen;
  loading(el);
  api('GET', '/api/v1/system').then(function (sy) {
    if (gen !== S.gen) return;
    clear(el); el.appendChild(title('system.title'));
    var card = h('section', { class: 'card' });
    var thr = sy.throttled, now = false, occ = false;
    if (thr && typeof thr === 'object') { now = !!thr.undervoltage_now; occ = !!thr.undervoltage_occurred; }
    else if (thr) occ = true;
    var ws = (S.status && S.status.warnings) || [];
    ws.forEach(function (w) { if (w.id === 'warning.undervoltage_now') now = true; if (w.id === 'warning.undervoltage_occurred') occ = true; });
    if (now) card.appendChild(h('div', { class: 'alert err' }, t('warning.undervoltage_now')));
    else if (occ) card.appendChild(h('div', { class: 'alert warn' }, t('warning.undervoltage_occurred')));
    else card.appendChild(h('div', { class: 'alert ok' }, t('system.power') + ': ' + t('system.power_ok')));
    if (sy.ntp_synchronised === false) card.appendChild(h('div', { class: 'alert warn' }, t('warning.ntp_not_synchronised')));
    else if (sy.ntp_synchronised) card.appendChild(h('div', { class: 'alert ok' }, t('system.ntp') + ': ' + t('system.ntp_ok')));
    if (sy.update_available) card.appendChild(h('div', { class: 'alert warn' }, t('warning.update_available', { version: sy.latest_version || '?' })));
    else card.appendChild(h('p', { class: 'muted', text: sy.latest_version ? t('system.up_to_date') : t('system.update_unknown') }));
    card.appendChild(kv([[t('system.version'), sy.version || '–'], sy.latest_version ? [t('system.latest'), sy.latest_version] : null,
                         sy.hostname ? [t('system.hostname'), sy.hostname] : null]));
    el.appendChild(card);
    var pw = h('section', { class: 'card' }, h('h3', { text: t('system.power_actions') }), h('div', { class: 'row' },
      h('button', { class: 'btn', id: 'b-reboot', text: t('system.reboot'), onclick: function () { powerAction('reboot'); } }),
      h('button', { class: 'btn danger', id: 'b-shutdown', text: t('system.shutdown'), onclick: function () { powerAction('shutdown'); } })));
    el.appendChild(pw);
    el.appendChild(h('section', { class: 'card' }, h('h3', { text: t('system.diagnostics') }),
      h('p', { class: 'muted small-text', text: t('system.diagnostics.hint') }),
      h('a', { class: 'btn', href: '/api/v1/diagnostics', download: '', text: t('system.diagnostics.download') })));
  }).catch(function (e) { if (gen === S.gen) failed(el, e, renderSystem); });
}

/* ---------- start ---------- */
$('#logout').addEventListener('click', function () {
  api('POST', '/api/v1/logout', {}).catch(function () { }).then(function () { return boot(); });
});
boot();
})();
