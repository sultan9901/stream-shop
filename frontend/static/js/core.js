/* ===========================================================================
   STREAM CORPORATION — core runtime (no dependencies).

   Provides: API client (CSRF + device id headers), toasts, modals, confirm
   dialogs, formatters, the intro splash, the particle field, the realtime
   WebSocket client and the notification drawer.

   Security notes:
   * nothing here is trusted by the server — every privileged action is
     re-validated server side (coins, orders, roles, refunds).
   * the CSRF token is read from the readable `sc_csrf` cookie and echoed in
     the `X-CSRF-Token` header (double-submit); the session cookie itself is
     httponly and never visible to this file.
   ======================================================================== */
(function () {
  'use strict';

  var SC = (window.SC = window.SC || {});

  /* ------------------------------------------------------------------ dom */
  SC.$ = function (sel, root) { return (root || document).querySelector(sel); };
  SC.$$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  SC.esc = function (value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };

  SC.attr = function (value) { return SC.esc(value); };

  SC.on = function (root, event, selector, handler) {
    root.addEventListener(event, function (ev) {
      var node = ev.target.closest(selector);
      if (node && root.contains(node)) handler.call(node, ev, node);
    });
  };

  SC.debounce = function (fn, wait) {
    var timer = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait || 260);
    };
  };
  /* -------------------------------------------------------------- cookies */
  SC.cookie = function (name) {
    var parts = ('; ' + document.cookie).split('; ' + name + '=');
    if (parts.length < 2) return '';
    return decodeURIComponent(parts.pop().split(';').shift() || '');
  };

  SC.csrf = function () { return SC.cookie('sc_csrf'); };

  /* Persistent client device id — half of the staff device lock. The server
     also plants a signed httponly cookie, so this value alone proves nothing. */
  SC.deviceId = function () {
    var key = 'sc.device_id';
    var value = '';
    try { value = window.localStorage.getItem(key) || ''; } catch (e) { value = ''; }
    if (!value) {
      var bytes = new Uint8Array(18);
      (window.crypto || window.msCrypto).getRandomValues(bytes);
      value = Array.prototype.map
        .call(bytes, function (b) { return ('0' + b.toString(16)).slice(-2); })
        .join('');
      try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ }
    }
    return value;
  };

  /* ----------------------------------------------------------- formatting */
  SC.n = function (value) {
    var num = Number(value || 0);
    return isFinite(num) ? num.toLocaleString('en-US') : '0';
  };

  SC.coins = function (value) { return SC.n(value); };

  SC.bdt = function (value) {
    var num = Number(value || 0);
    return '৳' + num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };

  SC.bytes = function (value) {
    var num = Number(value || 0), units = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (num >= 1024 && i < units.length - 1) { num /= 1024; i += 1; }
    return (i === 0 ? num : num.toFixed(1)) + ' ' + units[i];
  };

  SC.dt = function (iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit'
    });
  };
  SC.rel = function (iso) {
    if (!iso) return '—';
    var then = new Date(iso).getTime();
    if (isNaN(then)) return '—';
    var secs = Math.round((Date.now() - then) / 1000);
    if (secs < 45) return 'just now';
    var steps = [[60, 'min'], [24, 'hr'], [7, 'day'], [4.35, 'wk'], [12, 'mo']];
    var value = secs / 60, unit = 'min';
    for (var i = 0; i < steps.length; i += 1) {
      if (value < steps[i][0]) { unit = steps[i][1]; break; }
      value /= steps[i][0];
      unit = steps[i + 1] ? steps[i + 1][1] : 'yr';
    }
    var rounded = Math.max(1, Math.round(value));
    return rounded + ' ' + unit + (rounded > 1 ? 's' : '') + ' ago';
  };

  SC.statusBadge = function (status) {
    var key = String(status || '—');
    return '<span class="badge st-' + SC.esc(key) + '">' + SC.esc(key.replace(/_/g, ' ')) + '</span>';
  };

  /* ------------------------------------------------------------- api client */
  function ApiError(message, status, detail, payload) {
    this.name = 'ApiError';
    this.message = message || 'Request failed.';
    this.status = status || 0;
    this.detail = detail;
    this.payload = payload;
  }
  ApiError.prototype = Object.create(Error.prototype);
  SC.ApiError = ApiError;

  function messageFrom(payload, fallback) {
    if (!payload) return fallback;
    var detail = payload.detail !== undefined ? payload.detail : payload;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object') {
      if (detail.message) return detail.message;
      if (Array.isArray(detail) && detail.length && detail[0].msg) return detail[0].msg;
    }
    if (payload.message) return payload.message;
    return fallback;
  }

  SC.api = function (path, options) {
    var opts = options || {};
    var method = (opts.method || 'GET').toUpperCase();
    var headers = { 'X-Requested-With': 'fetch', 'X-Device-Id': SC.deviceId() };
    var body = null;
    if (opts.form) {
      body = opts.form;                       // FormData: let the browser set the boundary
    } else if (opts.json !== undefined) {
      body = JSON.stringify(opts.json);
      headers['Content-Type'] = 'application/json';
    }
    if (method !== 'GET' && method !== 'HEAD') headers['X-CSRF-Token'] = SC.csrf();
    Object.keys(opts.headers || {}).forEach(function (k) { headers[k] = opts.headers[k]; });

    var url = path;
    if (opts.query) {
      var qs = Object.keys(opts.query)
        .filter(function (k) {
          var v = opts.query[k];
          return v !== undefined && v !== null && v !== '';
        })
        .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(opts.query[k]); })
        .join('&');
      if (qs) url += (url.indexOf('?') === -1 ? '?' : '&') + qs;
    }

    return fetch(url, {
      method: method,
      headers: headers,
      body: body,
      credentials: 'same-origin',
      cache: 'no-store'
    }).then(function (response) {
      var ctype = response.headers.get('content-type') || '';
      var parse = ctype.indexOf('application/json') !== -1
        ? response.json().catch(function () { return null; })
        : response.text().then(function (t) { return { message: t }; }).catch(function () { return null; });
      return parse.then(function (payload) {
        if (!response.ok) {
          throw new ApiError(
            messageFrom(payload, 'HTTP ' + response.status),
            response.status,
            payload && payload.detail,
            payload
          );
        }
        return payload;
      });
    }, function () {
      throw new ApiError('Network unreachable. Check your connection.', 0, null, null);
    });
  };

  SC.get = function (path, query) { return SC.api(path, { query: query }); };
  SC.post = function (path, json) { return SC.api(path, { method: 'POST', json: json || {} }); };
  /* ---------------------------------------------------------------- toasts */
  function toastHost() {
    var host = SC.$('#sc-toasts');
    if (!host) {
      host = document.createElement('div');
      host.id = 'sc-toasts';
      host.className = 'toast-host';
      host.setAttribute('role', 'status');
      host.setAttribute('aria-live', 'polite');
      document.body.appendChild(host);
    }
    return host;
  }

  SC.toast = function (message, kind, title) {
    var host = toastHost();
    var node = document.createElement('div');
    node.className = 'toast ' + (kind || '');
    node.innerHTML = (title ? '<b>' + SC.esc(title) + '</b>' : '') + SC.esc(message);
    host.appendChild(node);
    var life = kind === 'err' ? 7000 : 4200;
    setTimeout(function () {
      node.classList.add('out');
      setTimeout(function () { node.remove(); }, 260);
    }, life);
    return node;
  };

  SC.ok = function (m, t) { return SC.toast(m, 'ok', t); };
  SC.err = function (m, t) { return SC.toast(m, 'err', t || 'Error'); };
  SC.warn = function (m, t) { return SC.toast(m, 'warn', t); };

  /* Turn any thrown ApiError into a user-visible message. */
  SC.fail = function (error) {
    var message = (error && error.message) || 'Something went wrong.';
    if (error && error.status === 401) message = 'Please sign in again.';
    SC.err(message);
    return error;
  };

  /* ---------------------------------------------------------------- modals */
  var modalStack = [];

  SC.modal = function (html, options) {
    var opts = options || {};
    var host = document.createElement('div');
    host.className = 'modal-host open';
    host.innerHTML =
      '<div class="modal-back" data-close="1"></div>' +
      '<div class="modal-box' + (opts.wide ? ' modal-wide' : '') + '" role="dialog" aria-modal="true">' +
        '<div class="modal-head"><h3>' + SC.esc(opts.title || '') + '</h3>' +
        '<button class="modal-x" type="button" data-close="1" aria-label="Close">&times;</button></div>' +
        '<div class="modal-content"></div>' +
      '</div>';
    var content = host.querySelector('.modal-content');
    if (typeof html === 'string') content.innerHTML = html; else content.appendChild(html);
    document.body.appendChild(host);
    document.body.style.overflow = 'hidden';

    var api = {
      host: host,
      box: host.querySelector('.modal-box'),
      content: content,
      close: function (result) {
        if (host.dataset.closed) return;
        host.dataset.closed = '1';
        host.remove();
        modalStack = modalStack.filter(function (m) { return m !== api; });
        if (!modalStack.length) document.body.style.overflow = '';
        if (opts.onClose) opts.onClose(result);
      }
    };
    modalStack.push(api);

    host.addEventListener('click', function (ev) {
      if (ev.target.closest('[data-close]') && !opts.sticky) api.close(null);
    });
    if (opts.onOpen) opts.onOpen(api);
    var focusable = content.querySelector('input, select, textarea, button');
    if (focusable) setTimeout(function () { focusable.focus(); }, 60);
    return api;
  };

  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && modalStack.length) modalStack[modalStack.length - 1].close(null);
  });

  /* Confirm dialog. `reason: true` makes an explanatory reason mandatory —
     the Master wallet adjustments and refunds require one (spec §41). */
  SC.confirm = function (options) {
    var opts = options || {};
    return new Promise(function (resolve) {
      var needsReason = !!opts.reason;
      var html =
        '<p>' + SC.esc(opts.message || 'Are you sure?') + '</p>' +
        (needsReason
          ? '<div class="field"><label for="sc-reason">' + SC.esc(opts.reasonLabel || 'Reason (required)') +
            '</label><textarea id="sc-reason" maxlength="500" placeholder="' +
            SC.esc(opts.reasonPlaceholder || 'Explain why — this is written to the audit log.') +
            '"></textarea><div class="err hidden" data-reason-err>A reason is required.</div></div>'
          : '') +
        '<div class="row-wrap" style="justify-content:flex-end;margin-top:1rem">' +
          '<button class="btn btn-ghost" type="button" data-no>Cancel</button>' +
          '<button class="btn ' + (opts.danger ? 'btn-danger' : 'btn-primary') + '" type="button" data-yes>' +
          SC.esc(opts.confirmLabel || 'Confirm') + '</button></div>';
      var settled = false;
      var modal = SC.modal(html, {
        title: opts.title || 'Please confirm',
        onClose: function () { if (!settled) { settled = true; resolve({ ok: false, reason: '' }); } }
      });
      modal.content.querySelector('[data-no]').addEventListener('click', function () { modal.close(); });
      modal.content.querySelector('[data-yes]').addEventListener('click', function () {
        var reason = '';
        if (needsReason) {
          var box = modal.content.querySelector('#sc-reason');
          reason = (box.value || '').trim();
          if (reason.length < 3) {
            modal.content.querySelector('[data-reason-err]').classList.remove('hidden');
            box.focus();
            return;
          }
        }
        settled = true;
        modal.close();
        resolve({ ok: true, reason: reason });
      });
    });
  };

  /* -------------------------------------------------------- intro / splash */
  SC.splash = function () {
    var node = SC.$('#sc-splash');
    if (!node) return;
    var lines = [
      'INITIALIZING STREAM CORPORATION...',
      'CONNECTING TO SECURE SERVER...',
      'LOADING MARKETPLACE...',
      'SYSTEM ONLINE'
    ];
    var duration = Math.max(1200, parseInt(node.dataset.duration || '3200', 10));
    var log = node.querySelector('.splash-log');
    var fill = node.querySelector('.splash-bar i');
    var pct = node.querySelector('.splash-pct');
    var slot = duration / (lines.length + 1);

    lines.forEach(function (line, index) {
      setTimeout(function () {
        if (!log) return;
        var row = document.createElement('div');
        row.innerHTML = '<span class="pfx">&gt;</span> ' + SC.esc(line);
        log.appendChild(row);
      }, slot * index);
    });

    var started = Date.now();
    var timer = setInterval(function () {
      var ratio = Math.min(1, (Date.now() - started) / duration);
      var shown = Math.round(ratio * 100);
      if (fill) fill.style.width = shown + '%';
      if (pct) pct.textContent = 'LOADING ' + shown + '%';
      if (ratio >= 1) {
        clearInterval(timer);
        node.classList.add('done');
        try { window.sessionStorage.setItem('sc.intro_seen', '1'); } catch (e) { /* ignore */ }
        setTimeout(function () { node.remove(); }, 700);
        document.dispatchEvent(new CustomEvent('sc:intro-done'));
      }
    }, 40);

    /* never trap the user behind the splash */
    node.addEventListener('click', function () {
      clearInterval(timer);
      node.classList.add('done');
      setTimeout(function () { node.remove(); }, 600);
    });
  };

  /* ------------------------------------------------------------- particles */
  SC.particles = function () {
    var canvas = SC.$('#sc-particles');
    if (!canvas || !canvas.getContext) return;
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    var ctx = canvas.getContext('2d');
    var dots = [];
    var raf = null;
    var wide = window.innerWidth;
    var count = wide < 620 ? 26 : wide < 1100 ? 44 : 66;

    function resize() {
      var ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.floor(window.innerWidth * ratio);
      canvas.height = Math.floor(window.innerHeight * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    }

    function seed() {
      dots = [];
      for (var i = 0; i < count; i += 1) {
        dots.push({
          x: Math.random() * window.innerWidth,
          y: Math.random() * window.innerHeight,
          vx: (Math.random() - 0.5) * 0.28,
          vy: (Math.random() - 0.5) * 0.28,
          r: Math.random() * 1.6 + 0.5
        });
      }
    }
    function frame() {
      ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
      for (var i = 0; i < dots.length; i += 1) {
        var d = dots[i];
        d.x += d.vx;
        d.y += d.vy;
        if (d.x < 0 || d.x > window.innerWidth) d.vx *= -1;
        if (d.y < 0 || d.y > window.innerHeight) d.vy *= -1;
        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(0,240,255,0.55)';
        ctx.fill();
        for (var j = i + 1; j < dots.length; j += 1) {
          var o = dots[j];
          var dx = d.x - o.x, dy = d.y - o.y;
          var dist = dx * dx + dy * dy;
          if (dist < 17000) {
            ctx.beginPath();
            ctx.moveTo(d.x, d.y);
            ctx.lineTo(o.x, o.y);
            ctx.strokeStyle = 'rgba(0,240,255,' + (0.16 * (1 - dist / 17000)).toFixed(3) + ')';
            ctx.lineWidth = 0.6;
            ctx.stroke();
          }
        }
      }
      raf = window.requestAnimationFrame(frame);
    }

    function start() { if (raf === null) frame(); }
    function stop() { if (raf !== null) { window.cancelAnimationFrame(raf); raf = null; } }

    resize();
    seed();
    start();
    window.addEventListener('resize', SC.debounce(function () { resize(); seed(); }, 220));
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) stop(); else start();
    });
  };
  /* ---------------------------------------------------- realtime websocket */
  /* The socket carries no credentials of its own: the server authenticates the
     handshake with the same httponly session cookie the HTTP API uses, so an
     anonymous socket can never subscribe to a user or role topic. */
  SC.ws = (function () {
    var socket = null;
    var handlers = {};
    var retries = 0;
    var closedByUs = false;
    var pinger = null;

    function fire(type, data) {
      (handlers[type] || []).forEach(function (fn) {
        try { fn(data); } catch (e) { /* one bad listener must not kill the rest */ }
      });
      (handlers['*'] || []).forEach(function (fn) {
        try { fn({ type: type, data: data }); } catch (e) { /* ignore */ }
      });
    }

    function setLive(on) {
      SC.$$('.live-dot').forEach(function (node) {
        node.classList.toggle('on', !!on);
        var label = node.querySelector('span');
        if (label) label.textContent = on ? 'LIVE' : 'OFFLINE';
      });
    }

    function connect() {
      if (socket && (socket.readyState === 0 || socket.readyState === 1)) return;
      closedByUs = false;
      var scheme = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
      try {
        socket = new WebSocket(scheme + window.location.host + '/ws');
      } catch (e) {
        return;
      }

      socket.onopen = function () {
        retries = 0;
        setLive(true);
        clearInterval(pinger);
        pinger = setInterval(function () {
          if (socket && socket.readyState === 1) socket.send('ping');
        }, 24000);
        fire('open', {});
      };

      socket.onmessage = function (event) {
        var packet = null;
        try { packet = JSON.parse(event.data); } catch (e) { return; }
        if (!packet || !packet.type) return;
        fire(packet.type, packet.data);
      };
      socket.onclose = function () {
        clearInterval(pinger);
        setLive(false);
        fire('close', {});
        if (closedByUs) return;
        retries += 1;
        var wait = Math.min(20000, 900 * Math.pow(1.7, retries));
        setTimeout(connect, wait);
      };

      socket.onerror = function () { setLive(false); };
    }

    return {
      connect: connect,
      on: function (type, fn) {
        (handlers[type] = handlers[type] || []).push(fn);
        return this;
      },
      close: function () {
        closedByUs = true;
        clearInterval(pinger);
        if (socket) socket.close();
      },
      get state() { return socket ? socket.readyState : -1; }
    };
  })();

  /* ------------------------------------------------- notification drawer */
  SC.notify = (function () {
    var loaded = false;

    function render(items) {
      var body = SC.$('#sc-note-body');
      if (!body) return;
      if (!items.length) {
        body.innerHTML = '<div class="empty"><div class="big">◎</div>No notifications yet.</div>';
        return;
      }
      body.innerHTML = items.map(function (n) {
        return '<div class="note' + (n.is_read ? '' : ' unread') + '">' +
          '<b>' + SC.esc(n.title) + '</b>' +
          (n.body ? '<div class="bd">' + SC.esc(n.body) + '</div>' : '') +
          (n.link ? '<a class="mono" style="font-size:.74rem" href="' + SC.esc(n.link) + '">open →</a>' : '') +
          '<span class="ts">' + SC.esc(SC.rel(n.created_at)) + '</span>' +
          '</div>';
      }).join('');
    }

    function setCount(count) {
      var dot = SC.$('#sc-bell-dot');
      if (!dot) return;
      var value = Number(count || 0);
      dot.textContent = value > 99 ? '99+' : String(value);
      dot.classList.toggle('hidden', value < 1);
    }

    function load() {
      return SC.get('/api/notifications', { limit: 60 }).then(function (data) {
        loaded = true;
        render(data.notifications || []);
        setCount(data.unread || 0);
        return data;
      }).catch(function () { /* signed out: the bell simply stays empty */ });
    }

    function refreshCount() {
      return SC.get('/api/notifications/unread-count')
        .then(function (data) { setCount(data.unread || 0); })
        .catch(function () { /* ignore */ });
    }

    function open() {
      var drawer = SC.$('#sc-drawer');
      var scrim = SC.$('#sc-scrim');
      if (!drawer) return;
      drawer.classList.add('open');
      if (scrim) scrim.classList.add('open');
      load().then(function () {
        SC.api('/api/notifications/read', { method: 'POST', json: {} })
          .then(function () { setCount(0); })
          .catch(function () { /* ignore */ });
      });
    }

    function close() {
      var drawer = SC.$('#sc-drawer');
      var scrim = SC.$('#sc-scrim');
      if (drawer) drawer.classList.remove('open');
      if (scrim) scrim.classList.remove('open');
    }

    function init() {
      var bell = SC.$('#sc-bell');
      if (!bell) return;
      bell.addEventListener('click', function () {
        var drawer = SC.$('#sc-drawer');
        if (drawer && drawer.classList.contains('open')) close(); else open();
      });
      SC.$$('[data-drawer-close]').forEach(function (node) {
        node.addEventListener('click', close);
      });
      refreshCount();
      SC.ws.on('notification', function (data) {
        SC.toast(data && data.body ? data.body : (data && data.title) || 'New notification',
          'ok', data && data.title);
        if (SC.$('#sc-drawer') && SC.$('#sc-drawer').classList.contains('open')) load();
        else refreshCount();
      });
    }

    return { init: init, load: load, open: open, close: close, refreshCount: refreshCount, setCount: setCount };
  })();

  /* ------------------------------------------------------ lazy images */
  SC.lazy = function (root) {
    var nodes = SC.$$('img[data-src]', root || document);
    if (!nodes.length) return;
    if (!('IntersectionObserver' in window)) {
      nodes.forEach(function (img) { img.src = img.dataset.src; img.classList.add('loaded'); });
      return;
    }
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var img = entry.target;
        img.src = img.dataset.src;
        img.removeAttribute('data-src');
        img.addEventListener('load', function () { img.classList.add('loaded'); });
        observer.unobserve(img);
      });
    }, { rootMargin: '200px' });
    nodes.forEach(function (img) { img.classList.add('lazy'); observer.observe(img); });
  };

  SC.copy = function (text) {
    var done = function () { SC.ok('Copied to clipboard.'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () { SC.warn('Copy failed.'); });
      return;
    }
    var area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); done(); } catch (e) { SC.warn('Copy failed.'); }
    area.remove();
  };

  SC.busy = function (button, on) {
    if (!button) return;
    if (on) {
      button.dataset.label = button.innerHTML;
      button.innerHTML = '<span class="spin"></span>';
      button.disabled = true;
      button.classList.add('is-busy');
    } else {
      if (button.dataset.label) button.innerHTML = button.dataset.label;
      delete button.dataset.label;
      button.disabled = false;
      button.classList.remove('is-busy');
    }
  };

  /* Guard every money-moving click: disable the button for the whole round
     trip so a double-click cannot fire two requests (the server is idempotent
     as well — this is only the first line of defence). */
  SC.guard = function (button, work) {
    if (!button || button.disabled) return Promise.resolve(null);
    SC.busy(button, true);
    return Promise.resolve()
      .then(work)
      .then(function (result) { SC.busy(button, false); return result; })
      .catch(function (error) { SC.busy(button, false); throw error; });
  };

  /* -------------------------------------------------------------- boot */
  function boot() {
    var splash = SC.$('#sc-splash');
    if (splash) {
      var seen = false;
      try { seen = window.sessionStorage.getItem('sc.intro_seen') === '1'; } catch (e) { seen = false; }
      if (seen || splash.dataset.enabled === '0') splash.remove(); else SC.splash();
    }

    SC.particles();
    SC.lazy();
    SC.notify.init();
    SC.ws.connect();

    var burger = SC.$('#sc-burger');
    var nav = SC.$('#sc-nav');
    if (burger && nav) {
      burger.addEventListener('click', function () {
        var open = nav.classList.toggle('open');
        burger.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
      nav.addEventListener('click', function (ev) {
        if (ev.target.tagName === 'A') nav.classList.remove('open');
      });
    }

    SC.on(document, 'click', '[data-copy]', function (ev, node) {
      ev.preventDefault();
      SC.copy(node.dataset.copy);
    });

    /* wallet balance is pushed by the server, never computed here */
    SC.ws.on('wallet', function (data) {
      if (!data) return;
      SC.$$('[data-coin-balance]').forEach(function (node) {
        node.textContent = SC.coins(data.balance);
      });
      document.dispatchEvent(new CustomEvent('sc:wallet', { detail: data }));
    });

    /* surface a Google login failure that came back as ?login_error= */
    var params = new URLSearchParams(window.location.search);
    if (params.get('login_error')) {
      SC.err(params.get('login_error'), 'Sign-in failed');
      history.replaceState({}, '', window.location.pathname);
    }

    document.documentElement.classList.add('sc-ready');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
