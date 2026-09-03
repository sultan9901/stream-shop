/* ===========================================================================
   STREAM CORPORATION — Master Control console.

   Three rules this file obeys without exception:
     1. It never decides who the caller is. Every pane calls an endpoint behind
        `require_master`; a 401/403 simply drops the operator back to the gate.
     2. It never computes money. Coin totals, balances, refund amounts and
        package prices are rendered from the server response, never derived here.
     3. Every destructive or money-moving action goes through SC.guard (one
        in-flight request per button) and, where the spec demands it, through
        SC.confirm({reason:true}) so the mandatory reason reaches the audit log.
   ======================================================================== */
(function () {
  'use strict';

  var SC = window.SC;
  var gate = SC.$('#m-gate');
  var shell = SC.$('#m-console');
  if (!gate || !shell) return;

  var me = null;
  /* server-provided reference lists, re-read whenever their pane is opened */
  var ref = { categories: [], sellers: [], masters: [], packages: [], methods: [] };
  var loaded = {};

  /* ------------------------------------------------------------- utilities */
  function bad(node, message) {
    node.innerHTML = '<div class="empty"><div class="big">⚠</div>' + SC.esc(message) + '</div>';
  }

  function none(node, message, icon) {
    node.innerHTML = '<div class="empty"><div class="big">' + (icon || '◎') + '</div>' +
      SC.esc(message) + '</div>';
  }

  /* Any call that comes back unauthorised means the staff session died — the
     device was unbound, the master disabled the account, or it simply expired. */
  function req(path, options) {
    return SC.api(path, options).catch(function (error) {
      if (error && (error.status === 401 || error.status === 403) && me) lock(error.message);
      throw error;
    });
  }

  function get(path, query) { return req(path, { query: query }); }
  function post(path, json) { return req(path, { method: 'POST', json: json || {} }); }
  function put(path, json) { return req(path, { method: 'PUT', json: json || {} }); }
  function patch(path, json) { return req(path, { method: 'PATCH', json: json || {} }); }
  function del(path) { return req(path, { method: 'DELETE' }); }
  function field(label, id, attrs, hint) {
    return '<div class="field"><label for="' + id + '">' + SC.esc(label) + '</label>' +
      '<input id="' + id + '" ' + (attrs || '') + '>' +
      (hint ? '<div class="hint">' + SC.esc(hint) + '</div>' : '') + '</div>';
  }

  function area(label, id, attrs) {
    return '<div class="field span-2"><label for="' + id + '">' + SC.esc(label) + '</label>' +
      '<textarea id="' + id + '" ' + (attrs || '') + '></textarea></div>';
  }

  function check(label, id, on) {
    return '<label class="check" style="margin:.2rem 0 .9rem"><input type="checkbox" id="' + id +
      '"' + (on ? ' checked' : '') + '> ' + SC.esc(label) + '</label>';
  }

  function options(list, selected, blank) {
    return (blank ? '<option value="">' + SC.esc(blank) + '</option>' : '') +
      list.map(function (item) {
        return '<option value="' + SC.esc(item.id) + '"' +
          (item.id === selected ? ' selected' : '') + '>' + SC.esc(item.label) + '</option>';
      }).join('');
  }

  function val(modal, id) { return (modal.content.querySelector('#' + id).value || '').trim(); }
  function flag(modal, id) { return !!modal.content.querySelector('#' + id).checked; }
  function intOf(modal, id) {
    var raw = val(modal, id);
    return raw === '' ? null : parseInt(raw, 10);
  }

  /* A form modal with a footer; `onSave` receives the modal and the button. */
  function formModal(title, body, onSave, opts) {
    var o = opts || {};
    var modal = SC.modal(
      '<form onsubmit="return false">' + body + '</form>' +
      '<div class="divider"></div>' +
      '<div class="row-wrap" style="justify-content:flex-end">' +
        (o.extra || '') +
        '<button class="btn btn-ghost" type="button" data-cancel>Cancel</button>' +
        '<button class="btn btn-primary" type="button" data-save>' +
        SC.esc(o.saveLabel || 'Save') + '</button>' +
      '</div>',
      { title: title, wide: o.wide !== false }
    );
    modal.content.querySelector('[data-cancel]').addEventListener('click', function () {
      modal.close();
    });
    var saveBtn = modal.content.querySelector('[data-save]');
    saveBtn.addEventListener('click', function () {
      SC.guard(saveBtn, function () { return onSave(modal, saveBtn); });
    });
    return modal;
  }
  /* ============================================================== auth gate */
  var loginForm = SC.$('#m-login');
  var loginErr = SC.$('#m-login-err');
  var loginGo = SC.$('#m-login-go');

  function lock(message) {
    me = null;
    loaded = {};
    shell.classList.add('hidden');
    gate.classList.remove('hidden');
    var bell = SC.$('#sc-bell');
    if (bell) bell.classList.add('hidden');
    SC.$('#sc-user').innerHTML = '';
    if (message) {
      loginErr.textContent = message;
      loginErr.classList.remove('hidden');
    }
  }

  loginForm.addEventListener('submit', function (ev) {
    ev.preventDefault();
    loginErr.classList.add('hidden');
    var username = (SC.$('#m-user').value || '').trim();
    var password = SC.$('#m-pass').value || '';
    if (!username || !password) {
      loginErr.textContent = 'Enter your username and password.';
      loginErr.classList.remove('hidden');
      return;
    }
    SC.guard(loginGo, function () {
      /* device_id travels in the body as well as the X-Device-Id header: the
         server binds the account to it and refuses a second device. */
      return SC.post('/api/auth/master/login', {
        username: username,
        password: password,
        device_id: SC.deviceId()
      }).then(function (data) {
        SC.$('#m-pass').value = '';
        return boot(data && data.must_change_password);
      }).catch(function (error) {
        loginErr.textContent = error.message;
        loginErr.classList.remove('hidden');
      });
    });
  });

  SC.$('#m-logout').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () {
      return post('/api/auth/staff/logout')
        .then(function () { lock('Signed out.'); })
        .catch(SC.fail);
    });
  });
  /* --------------------------------------------------------- password change */
  function passwordModal(forced) {
    var body =
      (forced
        ? '<p class="warn">This account still uses the password it was created with. ' +
          'Choose a new one before using the console.</p>'
        : '<p>Changing your password signs out every other session on this account.</p>') +
      field('Current password', 'pw-old', 'type="password" maxlength="256" autocomplete="current-password"') +
      field('New password', 'pw-new', 'type="password" maxlength="256" autocomplete="new-password"',
        'At least 6 characters.') +
      field('Repeat new password', 'pw-rep', 'type="password" maxlength="256" autocomplete="new-password"');

    var modal = formModal('Change password', body, function (m, button) {
      var oldPw = val(m, 'pw-old');
      var newPw = val(m, 'pw-new');
      if (newPw !== val(m, 'pw-rep')) { SC.err('The two new passwords do not match.'); return null; }
      if (newPw.length < 6) { SC.err('The new password must be at least 6 characters.'); return null; }
      return post('/api/auth/staff/change-password', {
        current_password: oldPw, new_password: newPw
      }).then(function (data) {
        m.close();
        SC.ok(data.message || 'Password updated.');
        if (forced) { me.must_change_password = false; openView('overview'); }
      }).catch(SC.fail);
    }, { saveLabel: 'Update password', wide: false });

    if (forced) {
      modal.content.querySelector('[data-cancel]').remove();
      modal.host.addEventListener('click', function (ev) {
        if (ev.target.closest('.modal-back')) ev.stopPropagation();
      }, true);
    }
    return modal;
  }

  SC.$('#m-password').addEventListener('click', function () { passwordModal(false); });

  /* ------------------------------------------------------------- staff chrome */
  function paintWho() {
    SC.$('#m-who').textContent = me.username || me.name || 'Master';
    SC.$('#m-who-code').textContent = (me.code || 'MASTER') +
      (me.is_root ? ' · ROOT' : '');
    var bell = SC.$('#sc-bell');
    if (bell) bell.classList.remove('hidden');
    SC.$('#sc-user').innerHTML =
      '<span class="badge badge-neon" title="Signed in as ' + SC.esc(me.username || '') + '">' +
      SC.esc(me.username || 'MASTER') + '</span>';
  }
  /* ================================================================== views */
  var LOADERS = {};

  function openView(name) {
    SC.$$('#m-nav button').forEach(function (button) {
      button.classList.toggle('active', button.dataset.view === name);
    });
    SC.$$('#m-views .view').forEach(function (pane) {
      pane.classList.toggle('active', pane.dataset.pane === name);
    });
    try { window.history.replaceState({}, '', '/master#' + name); } catch (e) { /* ignore */ }
    var loader = LOADERS[name];
    if (loader && !loaded[name]) { loaded[name] = true; loader(); }
  }

  SC.on(SC.$('#m-nav'), 'click', 'button', function (ev, node) { openView(node.dataset.view); });

  /* ------------------------------------------------------------- bootstrap */
  function boot(forcePassword) {
    return get('/api/auth/staff/me').then(function (data) {
      me = (data && data.user) || null;
      if (!me || me.role !== 'MASTER') {
        lock('This surface needs a Master account.');
        return null;
      }
      gate.classList.add('hidden');
      shell.classList.remove('hidden');
      loginErr.classList.add('hidden');
      paintWho();
      SC.notify.refreshCount();

      var wanted = (window.location.hash || '').replace('#', '');
      loaded = {};
      openView(LOADERS[wanted] ? wanted : 'overview');
      if (forcePassword || me.must_change_password) passwordModal(true);
      return me;
    }).catch(function (error) {
      if (error.status === 401 || error.status === 403) lock('');
      else SC.fail(error);
      return null;
    });
  }
  /* ============================================================== overview */
  /* Charts are hand-drawn on a canvas — no charting library is loaded, so the
     CSP stays at 'self' and the page ships nothing but our own code. */
  function drawChart(canvas, labels, sets) {
    if (!canvas || !canvas.getContext) return;
    var dpr = window.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || 480;
    var cssH = canvas.clientHeight || 200;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var padL = 8, padR = 8, padT = 10, padB = 20;
    var plotW = Math.max(10, cssW - padL - padR);
    var plotH = Math.max(10, cssH - padT - padB);
    var count = labels.length || 1;

    ctx.strokeStyle = 'rgba(255,255,255,.07)';
    ctx.lineWidth = 1;
    for (var g = 0; g <= 4; g++) {
      var gy = padT + (plotH / 4) * g;
      ctx.beginPath();
      ctx.moveTo(padL, gy);
      ctx.lineTo(padL + plotW, gy);
      ctx.stroke();
    }

    var step = plotW / count;
    sets.forEach(function (set, index) {
      var peak = Math.max.apply(null, [1].concat(set.values.map(Number)));
      var y = function (v) { return padT + plotH - (Number(v) / peak) * plotH; };
      if (set.mode === 'bar') {
        var width = Math.max(2, step * 0.42);
        ctx.fillStyle = set.color;
        set.values.forEach(function (v, i) {
          var height = padT + plotH - y(v);
          ctx.fillRect(padL + i * step + (step - width) / 2 - (index ? -1 : 1),
            y(v), width, Math.max(1, height));
        });
        return;
      }
      ctx.strokeStyle = set.color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      set.values.forEach(function (v, i) {
        var px = padL + i * step + step / 2;
        if (i === 0) ctx.moveTo(px, y(v)); else ctx.lineTo(px, y(v));
      });
      ctx.stroke();
      ctx.fillStyle = set.color;
      set.values.forEach(function (v, i) {
        ctx.beginPath();
        ctx.arc(padL + i * step + step / 2, y(v), 2.4, 0, Math.PI * 2);
        ctx.fill();
      });
    });

    ctx.fillStyle = 'rgba(255,255,255,.35)';
    ctx.font = '10px ui-monospace, monospace';
    ctx.textAlign = 'center';
    [0, Math.floor(count / 2), count - 1].forEach(function (i) {
      var label = labels[i];
      if (!label) return;
      ctx.fillText(label.slice(5), padL + i * step + step / 2, cssH - 6);
    });
  }
  var STAT_CARDS = [
    ['Customers', 'total_customers', null],
    ['Orders', 'total_orders', 'paid_orders'],
    ['Coins sold', 'total_coins_sold', null],
    ['Coins spent', 'total_coins_spent', null],
    ['Revenue ৳', 'total_bdt_payments', null],
    ['Pending payments', 'pending_payments', null],
    ['Products', 'total_products', 'active_products'],
    ['Sellers', 'total_sellers', null],
    ['Completed', 'completed_orders', null],
    ['Refunded', 'refunded_orders', 'total_refund_coins']
  ];

  function paintStats(stats) {
    SC.$('#m-stats').innerHTML = STAT_CARDS.map(function (card) {
      var value = stats[card[1]];
      var text = card[0] === 'Revenue ৳' ? SC.bdt(value || 0) : SC.n(value || 0);
      var sub = '';
      if (card[2] === 'paid_orders') sub = SC.n(stats.paid_orders || 0) + ' awaiting completion';
      else if (card[2] === 'active_products') sub = SC.n(stats.active_products || 0) + ' live';
      else if (card[2] === 'total_refund_coins') sub = SC.n(stats.total_refund_coins || 0) + ' coins returned';
      return '<div class="card stat"><div class="lbl">' + SC.esc(card[0]) + '</div>' +
        '<div class="val">' + SC.esc(text) + '</div>' +
        (sub ? '<div class="sub">' + SC.esc(sub) + '</div>' : '') + '</div>';
    }).join('');
  }

  function miniPayment(r) {
    return '<div class="lrow"><div class="main"><b>' + SC.esc(r.code) + '</b>' +
      '<span>' + SC.esc((r.customer && r.customer.email) || '—') + ' · ' +
      SC.esc(r.method_name || '—') + ' · ' + SC.esc(SC.bdt(r.amount_bdt)) + '</span></div>' +
      '<span class="coin">' + SC.coins(r.total_coins) + '</span>' +
      '<div class="acts"><button class="btn btn-sm btn-primary" type="button" data-pay-open="' +
      SC.esc(r.id) + '">Review</button></div></div>';
  }

  function miniOrder(o) {
    return '<div class="lrow"><div class="main"><b>' + SC.esc(o.order_code) + '</b>' +
      '<span>' + SC.esc((o.product && o.product.name) || '—') + ' · ' +
      SC.esc((o.customer && o.customer.email) || '—') + '</span></div>' +
      SC.statusBadge(o.status) +
      '<div class="acts"><button class="btn btn-sm btn-ghost" type="button" data-ord-open="' +
      SC.esc(o.id) + '">Open</button></div></div>';
  }
  function loadOverview() {
    var days = SC.$('#m-range').value || '14';
    return get('/api/master/overview', { days: days }).then(function (data) {
      var stats = data.stats || {};
      paintStats(stats);
      setPendingPill(stats.pending_payments || 0);

      var series = data.series || { labels: [] };
      drawChart(SC.$('#m-chart-orders'), series.labels || [], [
        { values: series.orders || [], color: 'rgba(0,240,255,.55)', mode: 'bar' },
        { values: series.coins_spent || [], color: '#a855f7', mode: 'line' }
      ]);
      drawChart(SC.$('#m-chart-coins'), series.labels || [], [
        { values: series.coins_purchased || [], color: 'rgba(34,227,162,.5)', mode: 'bar' },
        { values: series.revenue_bdt || [], color: '#ffb020', mode: 'line' }
      ]);

      var pend = data.pending_payments || [];
      var pendBox = SC.$('#m-ov-pending');
      if (pend.length) pendBox.innerHTML = pend.map(miniPayment).join('');
      else none(pendBox, 'Nothing is waiting for verification.', '✓');

      var recent = data.recent_orders || [];
      var ordBox = SC.$('#m-ov-orders');
      if (recent.length) ordBox.innerHTML = recent.map(miniOrder).join('');
      else none(ordBox, 'No orders yet.', '▤');

      var top = data.top_products || [];
      var topBox = SC.$('#m-ov-top');
      if (!top.length) { none(topBox, 'No sales recorded yet.', '◱'); return; }
      var peak = Math.max.apply(null, [1].concat(top.map(function (p) { return p.sold; })));
      topBox.innerHTML = top.map(function (p) {
        return '<div class="lrow"><div class="main"><b>' + SC.esc(p.name) + '</b>' +
          '<div class="bar" style="margin-top:.35rem"><i style="width:' +
          Math.round((p.sold / peak) * 100) + '%"></i></div></div>' +
          '<span class="mono">' + SC.n(p.sold) + ' sold</span>' +
          '<span class="coin">' + SC.coins(p.coin_price) + '</span></div>';
      }).join('');
    }).catch(function (error) { bad(SC.$('#m-stats'), error.message); });
  }

  LOADERS.overview = loadOverview;
  SC.$('#m-refresh').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, loadOverview);
  });
  SC.$('#m-range').addEventListener('change', loadOverview);

  function setPendingPill(count) {
    var pill = SC.$('#m-pending-pill');
    pill.textContent = SC.n(count);
    pill.classList.toggle('hidden', !count);
  }
  /* ============================================================== payments */
  var payState = { status: 'PENDING', offset: 0, limit: 20, total: 0 };

  function reviewCard(r) {
    var pending = r.status === 'PENDING';
    var shot = (r.screenshots || [])[0];
    return '<div class="card review" data-pay="' + SC.esc(r.id) + '" style="margin-bottom:.8rem">' +
      '<div class="review-top">' +
        '<div class="who"><b>' + SC.esc((r.customer && r.customer.label) || 'Customer') + '</b>' +
          '<span>' + SC.esc((r.customer && r.customer.email) || '') + '</span>' +
          '<span>' + SC.esc((r.customer && r.customer.code) || '') + '</span></div>' +
        '<div class="review-amt"><div class="bdt">' + SC.esc(SC.bdt(r.amount_bdt)) + '</div>' +
          '<div class="cn">' + SC.n(r.total_coins) + ' coins</div>' +
          '<div style="margin-top:.3rem">' + SC.statusBadge(r.status) + '</div></div>' +
      '</div>' +
      '<div class="review-meta">' +
        '<div><span>request</span>' + SC.esc(r.code) + '</div>' +
        '<div><span>package</span>' + SC.esc(r.package_name || '—') + '</div>' +
        '<div><span>method</span>' + SC.esc(r.method_name || '—') + ' · ' +
          SC.esc(r.method_number || '—') + '</div>' +
        '<div><span>sender</span>' + SC.esc(r.sender_number || '—') + '</div>' +
        '<div><span>trx ref</span>' + SC.esc(r.transaction_ref || '—') + '</div>' +
        '<div><span>submitted</span>' + SC.esc(SC.dt(r.created_at)) + '</div>' +
        (r.coins ? '<div><span>coins + bonus</span>' + SC.n(r.coins) + ' + ' +
          SC.n(r.bonus_coins) + '</div>' : '') +
        (r.reviewed_by ? '<div><span>reviewed by</span>' + SC.esc(r.reviewed_by) + ' · ' +
          SC.esc(SC.dt(r.reviewed_at)) + '</div>' : '') +
        (r.reject_reason ? '<div style="grid-column:1/-1"><span>reject reason</span>' +
          SC.esc(r.reject_reason) + '</div>' : '') +
        (r.note ? '<div style="grid-column:1/-1"><span>customer note</span>' +
          SC.esc(r.note) + '</div>' : '') +
      '</div>' +
      '<div class="review-acts">' +
        (shot
          ? '<button class="btn btn-sm" type="button" data-shot="' + SC.esc(shot.id) +
            '" data-shot-code="' + SC.esc(r.code) + '">View screenshot</button>'
          : '<span class="badge badge-warn">No screenshot</span>') +
        '<div class="grow"></div>' +
        (pending
          ? '<button class="btn btn-sm btn-danger" type="button" data-reject="' + SC.esc(r.id) +
            '">Reject</button>' +
            '<button class="btn btn-sm btn-ok" type="button" data-confirm="' + SC.esc(r.id) +
            '">Confirm &amp; credit</button>'
          : '<span class="hint">Already reviewed — coins can no longer change here.</span>') +
      '</div>' +
    '</div>';
  }
  function loadPayments(append) {
    var box = SC.$('#m-pay-list');
    if (!append) {
      payState.offset = 0;
      box.innerHTML = '<div class="skel" style="height:120px"></div>';
    }
    return get('/api/master/payments', {
      status: payState.status, limit: payState.limit, offset: payState.offset
    }).then(function (data) {
      payState.total = data.total || 0;
      var list = data.requests || [];
      var html = list.map(reviewCard).join('');
      if (append) box.insertAdjacentHTML('beforeend', html);
      else if (html) box.innerHTML = html;
      else none(box, 'No payment request in this state.', '₿');

      payState.offset += list.length;
      SC.$('#m-pay-more').classList.toggle('hidden', payState.offset >= payState.total);

      var counts = data.counts || {};
      var all = 0;
      Object.keys(counts).forEach(function (key) { all += counts[key]; });
      SC.$$('#m-pay-tabs [data-count]').forEach(function (node) {
        var key = node.dataset.count;
        node.textContent = SC.n(key ? (counts[key] || 0) : all);
      });
      setPendingPill(counts.PENDING || 0);
    }).catch(function (error) { bad(box, error.message); });
  }

  LOADERS.payments = function () { loadPayments(false); };

  SC.$$('#m-pay-tabs .tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      SC.$$('#m-pay-tabs .tab').forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      payState.status = tab.dataset.status || '';
      loadPayments(false);
    });
  });

  SC.$('#m-pay-more').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () { return loadPayments(true); });
  });

  /* The screenshot is never a public URL: /api/payments/screenshot/{id} is
     staff-only, so this <img> only resolves while a staff session is live. */
  SC.on(document, 'click', '[data-shot]', function (ev, node) {
    SC.modal(
      '<div class="shot-frame"><img src="/api/payments/screenshot/' +
      encodeURIComponent(node.dataset.shot) + '" alt="Payment screenshot"></div>' +
      '<p class="hint">Check the amount, the sender number and the transaction reference ' +
      'against the request before confirming.</p>',
      { title: 'Screenshot · ' + (node.dataset.shotCode || ''), wide: true }
    );
  });
  /* Confirm and reject both report `already_processed` when the state machine
     refused a second transition — that is a success, not an error, and it is
     the reason a double-click can never credit coins twice. */
  SC.on(document, 'click', '[data-confirm]', function (ev, node) {
    var id = node.dataset.confirm;
    SC.confirm({
      title: 'Confirm this payment?',
      message: 'The coins from the stored package are credited to the customer wallet and the ' +
        'ledger records the movement. This cannot be undone from here.',
      confirmLabel: 'Confirm & credit'
    }).then(function (answer) {
      if (!answer.ok) return;
      SC.guard(node, function () {
        return post('/api/master/payments/' + encodeURIComponent(id) + '/confirm')
          .then(function (data) {
            if (data.already_processed) SC.warn(data.message);
            else SC.ok(data.message);
            loadPayments(false);
            if (loaded.overview) loadOverview();
          })
          .catch(SC.fail);
      });
    });
  });

  SC.on(document, 'click', '[data-reject]', function (ev, node) {
    var id = node.dataset.reject;
    SC.confirm({
      title: 'Reject this payment?',
      message: 'No coins will be added. The customer is notified with the reason you give.',
      confirmLabel: 'Reject payment',
      danger: true,
      reason: true,
      reasonLabel: 'Reason (required — the customer sees this)',
      reasonPlaceholder: 'e.g. the screenshot amount does not match the package price.'
    }).then(function (answer) {
      if (!answer.ok) return;
      SC.guard(node, function () {
        return post('/api/master/payments/' + encodeURIComponent(id) + '/reject',
          { reason: answer.reason })
          .then(function (data) {
            if (data.already_processed) SC.warn(data.message);
            else SC.ok(data.message);
            loadPayments(false);
            if (loaded.overview) loadOverview();
          })
          .catch(SC.fail);
      });
    });
  });

  SC.on(document, 'click', '[data-pay-open]', function () { openView('payments'); });
  /* ================================================================ orders */
  var ordState = { status: '', q: '', seller: '', offset: 0, limit: 25, total: 0 };

  function ordRow(o) {
    var closed = o.status === 'REFUNDED' || o.status === 'CANCELLED';
    var d = o.delivery || {};
    return '<tr>' +
      '<td class="mono">' + SC.esc(o.order_code) + '</td>' +
      '<td><b style="font-size:.85rem">' + SC.esc((o.customer && o.customer.label) || '—') + '</b>' +
        '<div class="muted" style="font-size:.72rem">' +
        SC.esc((o.customer && o.customer.email) || '') + '</div></td>' +
      '<td>' + SC.esc((o.product && o.product.name) || 'Removed product') + '</td>' +
      '<td>' + SC.esc((o.seller && o.seller.label) || 'STREAM CORPORATION') +
        (o.seller && o.seller.id ? '<div class="mono muted" style="font-size:.7rem">' +
          SC.esc(o.seller.id) + '</div>' : '') + '</td>' +
      '<td class="right"><span class="coin">' + SC.coins(o.coin_total) + '</span></td>' +
      '<td>' + SC.statusBadge(o.status) + '</td>' +
      '<td>' + (d.status ? SC.statusBadge(d.status) : '<span class="muted">—</span>') + '</td>' +
      '<td class="mono" style="white-space:nowrap">' + SC.esc(SC.dt(o.created_at)) + '</td>' +
      '<td class="right"><div class="row-wrap" style="justify-content:flex-end">' +
        '<button class="btn btn-sm btn-ghost" type="button" data-ord-open="' + SC.esc(o.id) +
        '">Open</button>' +
        (closed ? '' : '<button class="btn btn-sm btn-ok" type="button" data-ord-done="' +
          SC.esc(o.id) + '">Complete</button>') +
      '</div></td>' +
    '</tr>';
  }

  function loadOrders(append) {
    var body = SC.$('#m-ord-body');
    if (!append) {
      ordState.offset = 0;
      body.innerHTML = '<tr><td colspan="9"><div class="skel"></div></td></tr>';
    }
    return get('/api/master/orders', {
      status: ordState.status, q: ordState.q, seller_id: ordState.seller,
      limit: ordState.limit, offset: ordState.offset
    }).then(function (data) {
      ordState.total = data.total || 0;
      var list = data.orders || [];
      var html = list.map(ordRow).join('');
      if (append) body.insertAdjacentHTML('beforeend', html);
      else body.innerHTML = html || '<tr><td colspan="9"><div class="empty">' +
        '<div class="big">▤</div>No order matches this filter.</div></td></tr>';

      ordState.offset += list.length;
      SC.$('#m-ord-more').classList.toggle('hidden', ordState.offset >= ordState.total);

      var counts = data.counts || {};
      SC.$$('#m-ord-tabs [data-count]').forEach(function (node) {
        node.textContent = SC.n(counts[node.dataset.count] || 0);
      });
    }).catch(function (error) {
      body.innerHTML = '<tr><td colspan="9"><div class="empty"><div class="big">⚠</div>' +
        SC.esc(error.message) + '</div></td></tr>';
    });
  }
  LOADERS.orders = function () {
    loadSellerOptions();
    loadOrders(false);
  };

  SC.$$('#m-ord-tabs .tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      SC.$$('#m-ord-tabs .tab').forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      ordState.status = tab.dataset.status || '';
      loadOrders(false);
    });
  });

  SC.$('#m-ord-q').addEventListener('input', SC.debounce(function (ev) {
    ordState.q = (ev.target.value || '').trim();
    loadOrders(false);
  }, 350));

  SC.$('#m-ord-seller').addEventListener('change', function (ev) {
    ordState.seller = ev.target.value || '';
    loadOrders(false);
  });

  SC.$('#m-ord-more').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () { return loadOrders(true); });
  });

  /* the seller dropdowns are filled from the account list, never hard-coded */
  function loadSellerOptions() {
    if (ref.sellers.length) return Promise.resolve(ref.sellers);
    return get('/api/master/sellers').then(function (data) {
      ref.sellers = (data.sellers || []).map(function (s) {
        return { id: s.id, label: s.username + (s.is_active ? '' : ' (disabled)') };
      });
      ['#m-ord-seller', '#m-prod-seller'].forEach(function (sel) {
        var node = SC.$(sel);
        if (node) node.innerHTML = options(ref.sellers, node.value, 'Every seller');
      });
      return ref.sellers;
    }).catch(function () { return []; });
  }

  function loadCategoryOptions() {
    if (ref.categories.length) return Promise.resolve(ref.categories);
    return get('/api/master/categories').then(function (data) {
      ref.categories = (data.categories || []).map(function (c) {
        return { id: c.id, label: c.name };
      });
      var node = SC.$('#m-prod-cat');
      if (node) node.innerHTML = options(ref.categories, node.value, 'Every category');
      return ref.categories;
    }).catch(function () { return []; });
  }
  function orderModal(id) {
    var modal = SC.modal('<div class="skel" style="height:200px"></div>',
      { title: 'Order', wide: true });
    get('/api/master/orders/' + encodeURIComponent(id)).then(function (data) {
      var o = data.order || {};
      var d = o.delivery || {};
      var closed = o.status === 'REFUNDED' || o.status === 'CANCELLED';
      modal.content.innerHTML =
        '<div class="row-wrap spread" style="margin-bottom:.9rem">' +
          '<div><b style="font-size:1rem">' + SC.esc((o.product && o.product.name) || '—') + '</b>' +
          '<div class="mono muted" style="font-size:.78rem">' + SC.esc(o.order_code) + '</div></div>' +
          SC.statusBadge(o.status) +
        '</div>' +
        '<dl class="kv">' +
          '<dt>Customer</dt><dd>' + SC.esc((o.customer && o.customer.label) || '—') + '</dd>' +
          '<dt>Gmail</dt><dd>' + SC.esc((o.customer && o.customer.email) || '—') + '</dd>' +
          '<dt>Customer ID</dt><dd>' + SC.esc((o.customer && o.customer.code) || '—') + '</dd>' +
          '<dt>Coins paid</dt><dd><span class="coin">' + SC.coins(o.coin_total) + '</span></dd>' +
          '<dt>Seller</dt><dd>' + SC.esc((o.seller && o.seller.label) || 'STREAM CORPORATION') + '</dd>' +
          '<dt>Delivery</dt><dd>' + (d.status ? SC.statusBadge(d.status) : '—') + '</dd>' +
          '<dt>Sent to</dt><dd>' + SC.esc(d.email_to || '—') + '</dd>' +
          '<dt>Sent at</dt><dd>' + SC.esc(d.sent_at ? SC.dt(d.sent_at) : 'not yet') + '</dd>' +
          '<dt>Ordered</dt><dd>' + SC.esc(SC.dt(o.created_at)) + '</dd>' +
          (o.completed_at ? '<dt>Completed</dt><dd>' + SC.esc(SC.dt(o.completed_at)) + '</dd>' : '') +
          (o.refunded_at ? '<dt>Refunded</dt><dd>' + SC.esc(SC.dt(o.refunded_at)) + '</dd>' : '') +
          (o.refund_reason ? '<dt>Refund reason</dt><dd>' + SC.esc(o.refund_reason) + '</dd>' : '') +
          (o.seller_note ? '<dt>Note</dt><dd>' + SC.esc(o.seller_note) + '</dd>' : '') +
        '</dl>' +
        (d.error ? '<p class="bad" style="font-size:.84rem;margin-top:.7rem">Delivery problem: ' +
          SC.esc(d.error) + '</p>' : '') +
        '<div class="divider"></div>' +
        '<div class="row-wrap" style="justify-content:flex-end">' +
          (closed ? '<span class="hint">This order is ' + SC.esc(o.status) +
            ' — coins were already returned to the wallet ledger.</span>' :
            '<button class="btn btn-sm" type="button" data-redeliver>Re-send delivery</button>' +
            '<button class="btn btn-sm btn-danger" type="button" data-refund>Refund coins</button>' +
            '<button class="btn btn-sm btn-ok" type="button" data-complete>Mark completed</button>') +
        '</div>';
      wireOrderActions(modal, o);
    }).catch(function (error) { bad(modal.content, error.message); });
    return modal;
  }
  function afterOrderChange() {
    if (loaded.orders) loadOrders(false);
    if (loaded.overview) loadOverview();
  }

  function completeOrder(button, id) {
    return SC.confirm({
      title: 'Mark completed?',
      message: 'The customer is notified that the order is finished.',
      confirmLabel: 'Mark completed',
      reason: true,
      reasonLabel: 'Note for the customer (required)',
      reasonPlaceholder: 'e.g. licence key delivered and activated.'
    }).then(function (answer) {
      if (!answer.ok) return null;
      return SC.guard(button, function () {
        return post('/api/master/orders/' + encodeURIComponent(id) + '/complete',
          { note: answer.reason })
          .then(function (data) { SC.ok(data.message); afterOrderChange(); return data; })
          .catch(SC.fail);
      });
    });
  }

  function wireOrderActions(modal, order) {
    var done = modal.content.querySelector('[data-complete]');
    if (done) {
      done.addEventListener('click', function () {
        completeOrder(done, order.id).then(function (data) { if (data) modal.close(); });
      });
    }
    var refund = modal.content.querySelector('[data-refund]');
    if (refund) {
      refund.addEventListener('click', function () {
        SC.confirm({
          title: 'Refund ' + order.order_code + '?',
          message: SC.n(order.coin_total) + ' coins go back to the customer wallet as a ledger ' +
            'entry, the order becomes REFUNDED and every download grant for it is revoked.',
          confirmLabel: 'Refund coins',
          danger: true,
          reason: true,
          reasonLabel: 'Refund reason (required — written to the audit log)',
          reasonPlaceholder: 'e.g. the product file was corrupt and could not be replaced.'
        }).then(function (answer) {
          if (!answer.ok) return;
          SC.guard(refund, function () {
            return post('/api/master/orders/' + encodeURIComponent(order.id) + '/refund',
              { reason: answer.reason, cancel: false })
              .then(function (data) {
                SC.ok(data.message);
                modal.close();
                afterOrderChange();
              })
              .catch(SC.fail);
          });
        });
      });
    }
    var again = modal.content.querySelector('[data-redeliver]');
    if (again) {
      again.addEventListener('click', function () {
        SC.guard(again, function () {
          return post('/api/master/orders/' + encodeURIComponent(order.id) + '/redeliver')
            .then(function (data) { SC.ok(data.message); })
            .catch(SC.fail);
        });
      });
    }
  }

  SC.on(document, 'click', '[data-ord-open]', function (ev, node) {
    orderModal(node.dataset.ordOpen);
  });
  SC.on(document, 'click', '[data-ord-done]', function (ev, node) {
    completeOrder(node, node.dataset.ordDone);
  });
  /* ============================================================== products */
  var prodState = { q: '', cat: '', seller: '', offset: 0, limit: 24, total: 0 };

  function prodRow(p) {
    var thumb = p.thumbnail_url
      ? '<img class="thumb" src="' + SC.esc(p.thumbnail_url) + '" alt="">'
      : '<div class="thumb"></div>';
    var flags = [
      p.is_active ? '' : '<span class="badge st-DISABLED">hidden</span>',
      p.is_featured ? '<span class="badge badge-violet">featured</span>' : '',
      p.file ? '<span class="badge badge-ok">file</span>' :
        (p.external_download_url ? '<span class="badge badge-info">link</span>' :
          '<span class="badge badge-warn">no deliverable</span>'),
      p.in_stock ? '' : '<span class="badge st-FAILED">out of stock</span>'
    ].join('');
    return '<div class="lrow">' + thumb +
      '<div class="main"><b>' + SC.esc(p.name) + '</b>' +
        '<span>' + SC.esc((p.category && p.category.name) || 'Uncategorised') + ' · ' +
        SC.esc((p.seller && p.seller.label) || 'STREAM CORPORATION') +
        (p.version ? ' · v' + SC.esc(p.version) : '') + ' · ' + SC.n(p.sold_count) + ' sold</span>' +
        '<div class="row-wrap" style="gap:.3rem;margin-top:.3rem">' + flags + '</div></div>' +
      '<span class="coin">' + SC.coins(p.coin_price) + '</span>' +
      '<div class="acts">' +
        '<button class="btn btn-sm btn-ghost" type="button" data-prod-media="' + SC.esc(p.id) +
        '">Media</button>' +
        '<button class="btn btn-sm" type="button" data-prod-edit="' + SC.esc(p.id) + '">Edit</button>' +
        '<button class="btn btn-sm btn-danger" type="button" data-prod-del="' + SC.esc(p.id) +
        '" data-name="' + SC.esc(p.name) + '">Delete</button>' +
      '</div></div>';
  }

  function loadProducts(append) {
    var box = SC.$('#m-prod-list');
    if (!append) {
      prodState.offset = 0;
      box.innerHTML = '<div class="skel" style="height:120px"></div>';
    }
    return get('/api/master/products', {
      q: prodState.q, category: prodState.cat, seller_id: prodState.seller,
      limit: prodState.limit, offset: prodState.offset
    }).then(function (data) {
      prodState.total = data.total || 0;
      var list = data.products || [];
      var html = list.map(prodRow).join('');
      if (append) box.insertAdjacentHTML('beforeend', html);
      else if (html) box.innerHTML = html;
      else none(box, 'No product matches this filter.', '◱');
      prodState.offset += list.length;
      SC.$('#m-prod-more').classList.toggle('hidden', prodState.offset >= prodState.total);
    }).catch(function (error) { bad(box, error.message); });
  }
  LOADERS.products = function () {
    Promise.all([loadCategoryOptions(), loadSellerOptions()]).then(function () {
      loadProducts(false);
    });
  };

  SC.$('#m-prod-q').addEventListener('input', SC.debounce(function (ev) {
    prodState.q = (ev.target.value || '').trim();
    loadProducts(false);
  }, 350));
  SC.$('#m-prod-cat').addEventListener('change', function (ev) {
    prodState.cat = ev.target.value || '';
    loadProducts(false);
  });
  SC.$('#m-prod-seller').addEventListener('change', function (ev) {
    prodState.seller = ev.target.value || '';
    loadProducts(false);
  });
  SC.$('#m-prod-more').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () { return loadProducts(true); });
  });

  function productForm(product) {
    var p = product || {};
    var editing = !!p.id;
    var body = '<div class="form-grid">' +
      '<div class="span-2">' +
        field('Product name', 'p-name', 'type="text" maxlength="200" value="' +
          SC.esc(p.name || '') + '"') +
      '</div>' +
      '<div class="span-2">' +
        '<label class="drop" for="p-thumb" id="p-thumb-drop">' +
          '<input id="p-thumb" type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden>' +
          (p.thumbnail_url
            ? '<img src="' + SC.esc(p.thumbnail_url) + '" alt="" style="' +
              'width:96px;height:72px;object-fit:cover;border-radius:8px;margin-bottom:.4rem">'
            : '') +
          '<b id="p-thumb-label">' +
            (p.thumbnail_url ? 'Replace product image' : 'Choose a product image') + '</b>' +
          '<span class="hint">PNG · JPEG · WebP · GIF — shown as the card/thumbnail image. ' +
            'Gallery shots, a banner and the deliverable file are added afterwards via ' +
            (editing ? '"Media".' : 'the "Media" button once the product is created.') + '</span>' +
        '</label>' +
      '</div>' +
      field('Price in coins', 'p-price', 'type="number" min="0" step="1" value="' +
        SC.esc(p.coin_price === undefined ? '' : p.coin_price) + '"') +
      field('Version', 'p-version', 'type="text" maxlength="40" value="' +
        SC.esc(p.version || '') + '"') +
      '<div class="field"><label for="p-cat">Category</label><select id="p-cat">' +
        options(ref.categories, (p.category_id || ''), 'No category') + '</select></div>' +
      '<div class="field"><label for="p-seller">Assigned seller</label><select id="p-seller">' +
        options(ref.sellers, (p.seller_id || ''), 'STREAM CORPORATION (house)') + '</select></div>' +
      field('Platform', 'p-platform', 'type="text" maxlength="120" placeholder="Windows / macOS / Web" value="' +
        SC.esc(p.platform || '') + '"') +
      field('Stock (blank = unlimited)', 'p-stock', 'type="number" min="0" step="1" value="' +
        SC.esc(p.stock === null || p.stock === undefined ? '' : p.stock) + '"') +
      field('Display order', 'p-order', 'type="number" step="1" value="' +
        SC.esc(p.display_order || 0) + '"') +
      field('Demo video URL', 'p-video', 'type="url" maxlength="2000" value="' +
        SC.esc(p.demo_video_url || '') + '"') +
      '<div class="span-2">' +
        field('Tagline', 'p-tagline', 'type="text" maxlength="255" value="' +
          SC.esc(p.tagline || '') + '"') +
      '</div>' +
      area('Description', 'p-desc', 'maxlength="20000"') +
      area('Delivery note (shown to the buyer after purchase)', 'p-delivery', 'maxlength="4000"') +
      '<div class="span-2">' +
        field('External download URL (used when no file is attached)', 'p-ext',
          'type="url" maxlength="2000" value="' + SC.esc(p.external_download_url || '') + '"',
          'A file attached below always wins — it is delivered through an expiring token instead.') +
      '</div>' +
      '<div class="span-2">' +
        check('Visible in the marketplace', 'p-active', p.is_active !== false) +
        check('Feature on the home page', 'p-featured', !!p.is_featured) +
        check('Allow the same customer to buy it again', 'p-repeat', !!p.allow_repurchase) +
      '</div>' +
    '</div>';
    var modal = formModal(editing ? 'Edit product' : 'New product', body, function (m, button) {
      var payload = {
        name: val(m, 'p-name'),
        coin_price: intOf(m, 'p-price') || 0,
        tagline: val(m, 'p-tagline') || null,
        description: val(m, 'p-desc') || null,
        version: val(m, 'p-version') || null,
        platform: val(m, 'p-platform') || null,
        category_id: val(m, 'p-cat') || null,
        seller_id: val(m, 'p-seller') || null,
        external_download_url: val(m, 'p-ext') || null,
        delivery_note: val(m, 'p-delivery') || null,
        demo_video_url: val(m, 'p-video') || null,
        is_active: flag(m, 'p-active'),
        is_featured: flag(m, 'p-featured'),
        allow_repurchase: flag(m, 'p-repeat'),
        stock: intOf(m, 'p-stock'),
        display_order: intOf(m, 'p-order') || 0
      };
      if (payload.name.length < 2) { SC.err('Give the product a name.'); return null; }
      var call = editing
        ? put('/api/master/products/' + encodeURIComponent(p.id), payload)
        : req('/api/master/products', { method: 'POST', json: payload });
      return call.then(function (data) {
        var saved = data.product;
        var thumbInput = m.content.querySelector('#p-thumb');
        var thumbFile = thumbInput && thumbInput.files[0];
        if (thumbFile && saved) {
          var form = new FormData();
          form.append('kind', 'thumbnail');
          form.append('image', thumbFile);
          return req('/api/master/products/' + encodeURIComponent(saved.id) + '/media',
            { method: 'POST', form: form }).then(function () {
              m.close();
              SC.ok(editing ? 'Product updated.' : 'Product created.');
              loadProducts(false);
            });
        }
        m.close();
        SC.ok(editing ? 'Product updated.' : 'Product created.');
        loadProducts(false);
      }).catch(SC.fail);
    }, { saveLabel: editing ? 'Save product' : 'Create product' });

    /* textarea values are set as properties, never interpolated into HTML */
    modal.content.querySelector('#p-desc').value = p.description || '';
    modal.content.querySelector('#p-delivery').value = p.delivery_note || '';
    modal.content.querySelector('#p-thumb').addEventListener('change', function (ev) {
      modal.content.querySelector('#p-thumb-label').textContent =
        ev.target.files[0] ? ev.target.files[0].name : 'Choose a product image';
    });
    return modal;
  }

  SC.$('#m-prod-new').addEventListener('click', function () {
    Promise.all([loadCategoryOptions(), loadSellerOptions()]).then(function () {
      productForm(null);
    });
  });

  SC.on(document, 'click', '[data-prod-edit]', function (ev, node) {
    var id = node.dataset.prodEdit;
    SC.guard(node, function () {
      return Promise.all([
        loadCategoryOptions(),
        loadSellerOptions(),
        get('/api/master/products/' + encodeURIComponent(id))
      ]).then(function (results) {
        productForm(results[2].product);
      }).catch(SC.fail);
    });
  });
  /* --------------------------------------------------------- media & payload

     Two uploads live here and they are not the same thing:

       • images go to ``uploads/media``, the one folder mounted publicly, so the
         URL the server returns is safe to put in an <img>.
       • the deliverable goes to a private folder and is never given a URL — the
         response only describes it (name, size, type). It reaches a customer
         solely through a per-order expiring token.                            */
  function mediaRow(m) {
    return '<div class="lrow">' +
      '<img class="thumb" src="' + SC.esc(m.url) + '" alt="">' +
      '<div class="main"><b style="font-size:.84rem">' + SC.esc(m.kind) + '</b>' +
        (m.caption ? '<div class="muted" style="font-size:.74rem">' + SC.esc(m.caption) +
          '</div>' : '') +
        '<div class="faint mono" style="font-size:.7rem">' + SC.esc(m.url) + '</div></div>' +
      '<div class="acts"><button class="btn btn-sm btn-danger" type="button" data-media-del="' +
        SC.esc(m.id) + '">Remove</button></div>' +
    '</div>';
  }

  function mediaModal(productId) {
    var modal = SC.modal('<div class="skel" style="height:200px"></div>',
      { title: 'Media & deliverable', wide: true });

    function paint(product) {
      var media = product.media || [];
      modal.content.innerHTML =
        '<div class="form-grid">' +
          '<div class="field"><label for="md-kind">Image slot</label>' +
            '<select id="md-kind">' +
              '<option value="thumbnail">Thumbnail (card image)</option>' +
              '<option value="banner">Banner (product page hero)</option>' +
              '<option value="gallery" selected>Gallery shot</option>' +
            '</select></div>' +
          '<div class="field"><label for="md-cap">Caption (gallery only)</label>' +
            '<input id="md-cap" type="text" maxlength="200" placeholder="Dashboard view"></div>' +
        '</div>' +
        '<label class="drop" for="md-img" id="md-drop">' +
          '<input id="md-img" type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden>' +
          '<b>Choose an image</b>' +
          '<span class="hint">PNG · JPEG · WebP · GIF — checked by magic bytes, not by name.</span>' +
        '</label>' +
        '<div class="row" style="justify-content:flex-end;margin-top:.6rem">' +
          '<button class="btn btn-sm btn-primary" type="button" id="md-up">Upload image</button>' +
        '</div>' +
        '<div class="divider"></div>' +
        '<div class="card-title">Current media</div>' +
        (product.thumbnail_url
          ? '<div class="lrow"><img class="thumb" src="' + SC.esc(product.thumbnail_url) +
            '" alt=""><div class="main"><b style="font-size:.84rem">thumbnail</b>' +
            '<div class="faint mono" style="font-size:.7rem">' +
            SC.esc(product.thumbnail_url) + '</div></div></div>'
          : '') +
        (media.length ? media.map(mediaRow).join('')
          : '<p class="hint">No gallery or banner image yet.</p>') +
        '<div class="divider"></div>' +
        '<div class="card-title">Deliverable file</div>' +
        (product.file
          ? '<div class="lrow"><div class="main"><b style="font-size:.84rem">' +
            SC.esc(product.file.name) + '</b><div class="muted" style="font-size:.74rem">' +
            SC.esc(SC.bytes(product.file.size_bytes)) + ' · ' +
            SC.esc(product.file.content_type || 'application/octet-stream') + '</div></div></div>'
          : '<p class="hint">No file attached — buyers will get the external link instead.</p>') +
        '<label class="drop" for="md-file" id="md-fdrop" style="margin-top:.6rem">' +
          '<input id="md-file" type="file" hidden>' +
          '<b>Choose the software file</b>' +
          '<span class="hint">Stored outside the web root. Uploading again replaces the ' +
            'previous file for every future download.</span>' +
        '</label>' +
        '<div class="row" style="justify-content:flex-end;margin-top:.6rem">' +
          '<button class="btn btn-sm" type="button" id="md-fup">Attach file</button>' +
        '</div>';

      var img = modal.content.querySelector('#md-img');
      var file = modal.content.querySelector('#md-file');
      img.addEventListener('change', function () {
        modal.content.querySelector('#md-drop').querySelector('b').textContent =
          img.files[0] ? img.files[0].name : 'Choose an image';
      });
      file.addEventListener('change', function () {
        modal.content.querySelector('#md-fdrop').querySelector('b').textContent =
          file.files[0] ? file.files[0].name : 'Choose the software file';
      });

      modal.content.querySelector('#md-up').addEventListener('click', function (ev) {
        if (!img.files[0]) { SC.warn('Pick an image first.'); return; }
        var form = new FormData();
        form.append('kind', modal.content.querySelector('#md-kind').value);
        form.append('caption', modal.content.querySelector('#md-cap').value);
        form.append('image', img.files[0]);
        SC.guard(ev.currentTarget, function () {
          return req('/api/master/products/' + encodeURIComponent(productId) + '/media',
            { method: 'POST', form: form }).then(function (data) {
              SC.ok('Image uploaded.');
              paint(data.product);
              if (loaded.products) loadProducts(false);
            }).catch(SC.fail);
        });
      });

      modal.content.querySelector('#md-fup').addEventListener('click', function (ev) {
        if (!file.files[0]) { SC.warn('Pick a file first.'); return; }
        var form = new FormData();
        form.append('file', file.files[0]);
        SC.guard(ev.currentTarget, function () {
          return req('/api/master/products/' + encodeURIComponent(productId) + '/file',
            { method: 'POST', form: form }).then(function () {
              SC.ok('File attached — delivery will use it from now on.');
              return get('/api/master/products/' + encodeURIComponent(productId));
            }).then(function (data) {
              paint(data.product);
              if (loaded.products) loadProducts(false);
            }).catch(SC.fail);
        });
      });

      SC.$$('[data-media-del]', modal.content).forEach(function (button) {
        button.addEventListener('click', function () {
          SC.confirm({
            title: 'Remove image',
            message: 'The file is deleted from disk as well. This cannot be undone.',
            confirmLabel: 'Remove',
            danger: true
          }).then(function (answer) {
            if (!answer.ok) return;
            SC.guard(button, function () {
              return del('/api/master/products/' + encodeURIComponent(productId) +
                '/media/' + encodeURIComponent(button.dataset.mediaDel))
                .then(function () {
                  SC.ok('Media removed.');
                  return get('/api/master/products/' + encodeURIComponent(productId));
                })
                .then(function (data) {
                  paint(data.product);
                  if (loaded.products) loadProducts(false);
                })
                .catch(SC.fail);
            });
          });
        });
      });
    }

    get('/api/master/products/' + encodeURIComponent(productId)).then(function (data) {
      paint(data.product);
    }).catch(function (error) {
      none(modal.content, error.message, '⚠');
    });
    return modal;
  }

  SC.on(document, 'click', '[data-prod-media]', function (ev, node) {
    mediaModal(node.dataset.prodMedia);
  });

  SC.on(document, 'click', '[data-prod-del]', function (ev, node) {
    SC.confirm({
      title: 'Delete product',
      message: 'A product with order history is archived and hidden instead of deleted, ' +
        'so existing buyers keep their downloads.',
      confirmLabel: 'Delete',
      danger: true
    }).then(function (answer) {
      if (!answer.ok) return;
      SC.guard(node, function () {
        return del('/api/master/products/' + encodeURIComponent(node.dataset.prodDel))
          .then(function (data) { SC.ok(data.message); loadProducts(false); })
          .catch(SC.fail);
      });
    });
  });
  /* ----------------------------------------------------------- categories --- */
  function catRow(c) {
    return '<div class="lrow">' +
      '<div class="main"><b style="font-size:.9rem">' + (c.icon ? SC.esc(c.icon) + ' ' : '') +
        SC.esc(c.name) + '</b>' +
        '<div class="muted" style="font-size:.76rem">' + SC.esc(c.slug) + ' · ' +
          SC.n(c.product_count) + ' product' + (c.product_count === 1 ? '' : 's') +
          ' · order ' + SC.n(c.display_order) + '</div>' +
        (c.description ? '<div class="faint" style="font-size:.76rem">' +
          SC.esc(c.description) + '</div>' : '') + '</div>' +
      '<div class="acts">' +
        SC.statusBadge(c.is_active ? 'ACTIVE' : 'DISABLED') +
        '<button class="btn btn-sm btn-ghost" type="button" data-cat-edit="' + SC.esc(c.id) +
          '">Edit</button>' +
        '<button class="btn btn-sm btn-danger" type="button" data-cat-del="' + SC.esc(c.id) +
          '">Delete</button>' +
      '</div></div>';
  }

  function loadCategories() {
    var host = SC.$('#m-cat-list');
    return get('/api/master/categories').then(function (data) {
      ref.categories = (data.categories || []).map(function (c) {
        return { id: c.id, label: c.name };
      });
      /* the products filter reads the same list, so keep it honest */
      var filter = SC.$('#m-prod-cat');
      if (filter) filter.innerHTML = options(ref.categories, filter.value, 'Every category');
      var list = data.categories || [];
      if (!list.length) { none(host, 'No category yet. Create one to group the catalogue.', '⌗'); return; }
      host.innerHTML = '<div class="card">' + list.map(catRow).join('') + '</div>';
    }).catch(function (error) { bad(host, error.message); });
  }
  LOADERS.categories = loadCategories;

  function categoryForm(c) {
    var v = c || {};
    var modal = formModal(v.id ? 'Edit category' : 'New category',
      '<div class="form-grid">' +
        field('Name', 'c-name', 'type="text" maxlength="120" required value="' +
          SC.attr(v.name || '') + '"') +
        field('Icon (one glyph)', 'c-icon', 'type="text" maxlength="16" value="' +
          SC.attr(v.icon || '') + '"', 'Rendered next to the name. Emoji or a symbol like ◱.') +
        field('Display order', 'c-order', 'type="number" min="0" step="1" value="' +
          SC.attr(v.display_order == null ? 0 : v.display_order) + '"') +
      '</div>' +
      area('Description', 'c-desc', 'rows="3" maxlength="600"') +
      check('Visible in the marketplace', 'c-active', v.id ? !!v.is_active : true),
      function (m) {
        var payload = {
          name: val(m, 'c-name'),
          icon: val(m, 'c-icon') || null,
          description: val(m, 'c-desc') || null,
          display_order: intOf(m, 'c-order') || 0,
          is_active: flag(m, 'c-active')
        };
        if (!payload.name || payload.name.length < 2) {
          SC.err('A category needs a name of at least 2 characters.');
          return null;
        }
        var call = v.id
          ? put('/api/master/categories/' + encodeURIComponent(v.id), payload)
          : post('/api/master/categories', payload);
        return call.then(function () {
          m.close();
          SC.ok(v.id ? 'Category updated.' : 'Category created.');
          loadCategories();
        }).catch(SC.fail);
      });
    modal.content.querySelector('#c-desc').value = v.description || '';
    return modal;
  }

  SC.$('#m-cat-new').addEventListener('click', function () { categoryForm(null); });

  SC.on(document, 'click', '[data-cat-edit]', function (ev, node) {
    var id = node.dataset.catEdit;
    SC.guard(node, function () {
      return get('/api/master/categories').then(function (data) {
        var found = (data.categories || []).filter(function (c) { return c.id === id; })[0];
        if (!found) { SC.err('That category no longer exists.'); loadCategories(); return; }
        categoryForm(found);
      }).catch(SC.fail);
    });
  });

  SC.on(document, 'click', '[data-cat-del]', function (ev, node) {
    SC.confirm({
      title: 'Delete category',
      message: 'A category still holding products cannot be deleted — move or delete those first.',
      confirmLabel: 'Delete',
      danger: true
    }).then(function (answer) {
      if (!answer.ok) return;
      SC.guard(node, function () {
        return del('/api/master/categories/' + encodeURIComponent(node.dataset.catDel))
          .then(function (data) { SC.ok(data.message); loadCategories(); })
          .catch(SC.fail);
      });
    });
  });
  /* -------------------------------------------------------- coin packages ---

     The Buy Coins popup a viewer sees is built from exactly this list, and the
     coins credited on confirmation are read from the stored package row — never
     from the browser. Editing a price here changes what future buyers pay; it
     never rewrites a payment request that is already pending.                 */
  function pkgRow(p) {
    return '<div class="lrow">' +
      '<div class="main"><b style="font-size:.9rem">' + SC.esc(p.name) + '</b>' +
        (p.badge ? ' <span class="badge badge-violet">' + SC.esc(p.badge) + '</span>' : '') +
        '<div class="muted" style="font-size:.78rem">' +
          '<span class="coin">' + SC.coins(p.coins) + '</span>' +
          (p.bonus_coins ? ' + ' + SC.n(p.bonus_coins) + ' bonus = ' +
            SC.n(p.total_coins) + ' total' : '') +
          ' · ' + SC.esc(SC.bdt(p.price_bdt)) + ' · order ' + SC.n(p.display_order) +
        '</div></div>' +
      '<div class="acts">' +
        SC.statusBadge(p.is_active ? 'ACTIVE' : 'DISABLED') +
        '<button class="btn btn-sm btn-ghost" type="button" data-pkg-edit="' + SC.esc(p.id) +
          '">Edit</button>' +
        '<button class="btn btn-sm btn-danger" type="button" data-pkg-del="' + SC.esc(p.id) +
          '">Delete</button>' +
      '</div></div>';
  }

  function loadPackages() {
    var host = SC.$('#m-pkg-list');
    return get('/api/master/coin-packages').then(function (data) {
      ref.packages = data.packages || [];
      if (!ref.packages.length) {
        none(host, 'No coin package yet — viewers cannot top up until one exists.', '◎');
        return;
      }
      host.innerHTML = '<div class="card">' + ref.packages.map(pkgRow).join('') + '</div>';
    }).catch(function (error) { bad(host, error.message); });
  }
  LOADERS.coins = loadPackages;

  function packageForm(p) {
    var v = p || {};
    formModal(v.id ? 'Edit coin package' : 'New coin package',
      '<div class="form-grid">' +
        field('Package name', 'k-name', 'type="text" maxlength="120" required value="' +
          SC.attr(v.name || '') + '"') +
        field('Badge (optional)', 'k-badge', 'type="text" maxlength="24" value="' +
          SC.attr(v.badge || '') + '"', 'Shown as a ribbon, e.g. BEST VALUE.') +
        field('Coins', 'k-coins', 'type="number" min="1" step="1" required value="' +
          SC.attr(v.coins == null ? '' : v.coins) + '"') +
        field('Bonus coins', 'k-bonus', 'type="number" min="0" step="1" value="' +
          SC.attr(v.bonus_coins == null ? 0 : v.bonus_coins) + '"',
          'Credited on top of the paid coins as a BONUS COIN ledger entry.') +
        field('Price (৳)', 'k-price', 'type="number" min="0" step="0.01" required value="' +
          SC.attr(v.price_bdt == null ? '' : v.price_bdt) + '"') +
        field('Display order', 'k-order', 'type="number" min="0" step="1" value="' +
          SC.attr(v.display_order == null ? 0 : v.display_order) + '"') +
      '</div>' +
      check('Offer this package to viewers', 'k-active', v.id ? !!v.is_active : true),
      function (m) {
        var coins = intOf(m, 'k-coins');
        var price = parseFloat(val(m, 'k-price'));
        if (!val(m, 'k-name')) { SC.err('Give the package a name.'); return null; }
        if (!coins || coins < 1) { SC.err('A package must sell at least 1 coin.'); return null; }
        if (isNaN(price) || price < 0) { SC.err('Enter the price in taka.'); return null; }
        var payload = {
          name: val(m, 'k-name'),
          badge: val(m, 'k-badge') || null,
          coins: coins,
          bonus_coins: intOf(m, 'k-bonus') || 0,
          price_bdt: price,
          display_order: intOf(m, 'k-order') || 0,
          is_active: flag(m, 'k-active')
        };
        var call = v.id
          ? put('/api/master/coin-packages/' + encodeURIComponent(v.id), payload)
          : post('/api/master/coin-packages', payload);
        return call.then(function () {
          m.close();
          SC.ok(v.id ? 'Package updated.' : 'Package created.');
          loadPackages();
        }).catch(SC.fail);
      });
  }

  SC.$('#m-pkg-new').addEventListener('click', function () { packageForm(null); });

  SC.on(document, 'click', '[data-pkg-edit]', function (ev, node) {
    var id = node.dataset.pkgEdit;
    var found = ref.packages.filter(function (p) { return p.id === id; })[0];
    if (found) packageForm(found);
  });

  SC.on(document, 'click', '[data-pkg-del]', function (ev, node) {
    SC.confirm({
      title: 'Delete coin package',
      message: 'Past purchases keep their own copy of the coins and price, so history stays intact.',
      confirmLabel: 'Delete',
      danger: true
    }).then(function (answer) {
      if (!answer.ok) return;
      SC.guard(node, function () {
        return del('/api/master/coin-packages/' + encodeURIComponent(node.dataset.pkgDel))
          .then(function (data) { SC.ok(data.message); loadPackages(); })
          .catch(SC.fail);
      });
    });
  });
  /* ------------------------------------------------------- payment methods ---

     A method is only a set of instructions plus the number a viewer sends money
     to. Nothing here credits coins — that still needs a Master or an authorised
     Seller to confirm the request the viewer files afterwards.                 */
  function pmRow(m) {
    return '<div class="lrow">' +
      '<div class="main"><b style="font-size:.9rem">' + SC.esc(m.name) + '</b>' +
        (m.account_type ? ' <span class="badge">' + SC.esc(m.account_type) + '</span>' : '') +
        '<div class="mono" style="font-size:.82rem">' + SC.esc(m.account_number) + '</div>' +
        '<div class="muted" style="font-size:.74rem">' +
          SC.esc(m.account_name || 'no account name') + ' · order ' + SC.n(m.display_order) +
        '</div></div>' +
      '<div class="acts">' +
        SC.statusBadge(m.is_active ? 'ACTIVE' : 'DISABLED') +
        '<button class="btn btn-sm btn-ghost" type="button" data-copy="' +
          SC.attr(m.account_number) + '">Copy</button>' +
        '<button class="btn btn-sm btn-ghost" type="button" data-pm-edit="' + SC.esc(m.id) +
          '">Edit</button>' +
        '<button class="btn btn-sm btn-danger" type="button" data-pm-del="' + SC.esc(m.id) +
          '">Delete</button>' +
      '</div></div>';
  }

  function loadMethods() {
    var host = SC.$('#m-pm-list');
    return get('/api/master/payment-methods').then(function (data) {
      ref.methods = data.methods || [];
      if (!ref.methods.length) {
        none(host, 'No cash-in channel yet — the Buy Coins popup needs at least one.', '⊞');
        return;
      }
      host.innerHTML = '<div class="card">' + ref.methods.map(pmRow).join('') + '</div>';
    }).catch(function (error) { bad(host, error.message); });
  }
  LOADERS.methods = loadMethods;

  function methodForm(pm) {
    var v = pm || {};
    var modal = formModal(v.id ? 'Edit payment method' : 'New payment method',
      '<div class="form-grid">' +
        field('Method name', 'x-name', 'type="text" maxlength="80" required value="' +
          SC.attr(v.name || '') + '"', 'bKash, Nagad, Rocket, bank transfer…') +
        field('Account type', 'x-type', 'type="text" maxlength="40" value="' +
          SC.attr(v.account_type || '') + '"', 'Personal, Agent, Merchant…') +
        field('Number / account', 'x-num', 'type="text" maxlength="80" required value="' +
          SC.attr(v.account_number || '') + '"') +
        field('Account holder', 'x-holder', 'type="text" maxlength="120" value="' +
          SC.attr(v.account_name || '') + '"') +
        field('Display order', 'x-order', 'type="number" min="0" step="1" value="' +
          SC.attr(v.display_order == null ? 0 : v.display_order) + '"') +
      '</div>' +
      area('Instructions shown to the viewer', 'x-note', 'rows="4" maxlength="4000"') +
      check('Offer this method in the Buy Coins popup', 'x-active', v.id ? !!v.is_active : true),
      function (m) {
        var number = val(m, 'x-num');
        if (!val(m, 'x-name')) { SC.err('Give the method a name.'); return null; }
        if (number.length < 3) { SC.err('Enter the number money should be sent to.'); return null; }
        var payload = {
          name: val(m, 'x-name'),
          account_type: val(m, 'x-type') || null,
          account_number: number,
          account_name: val(m, 'x-holder') || null,
          instructions: val(m, 'x-note') || null,
          display_order: intOf(m, 'x-order') || 0,
          is_active: flag(m, 'x-active')
        };
        var call = v.id
          ? put('/api/master/payment-methods/' + encodeURIComponent(v.id), payload)
          : post('/api/master/payment-methods', payload);
        return call.then(function () {
          m.close();
          SC.ok(v.id ? 'Method updated.' : 'Method created.');
          loadMethods();
        }).catch(SC.fail);
      });
    modal.content.querySelector('#x-note').value = v.instructions || '';
    return modal;
  }

  SC.$('#m-pm-new').addEventListener('click', function () { methodForm(null); });

  SC.on(document, 'click', '[data-pm-edit]', function (ev, node) {
    var id = node.dataset.pmEdit;
    var found = ref.methods.filter(function (m) { return m.id === id; })[0];
    if (found) methodForm(found);
  });

  SC.on(document, 'click', '[data-pm-del]', function (ev, node) {
    SC.confirm({
      title: 'Delete payment method',
      message: 'Requests already filed against it keep the method name they were sent with.',
      confirmLabel: 'Delete',
      danger: true
    }).then(function (answer) {
      if (!answer.ok) return;
      SC.guard(node, function () {
        return del('/api/master/payment-methods/' + encodeURIComponent(node.dataset.pmDel))
          .then(function (data) { SC.ok(data.message); loadMethods(); })
          .catch(SC.fail);
      });
    });
  });
  /* ------------------------------------------------------------ customers --- */
  var cusState = { q: '', offset: 0, limit: 25, total: 0 };

  function cusRow(c) {
    return '<tr>' +
      '<td class="mono">' + SC.esc(c.code || '—') + '</td>' +
      '<td><b style="font-size:.86rem">' + SC.esc(c.name || c.username || 'Viewer') + '</b>' +
        (c.is_active ? '' : ' <span class="badge badge-bad">disabled</span>') + '</td>' +
      '<td class="mono" style="font-size:.78rem">' + SC.esc(c.email || '—') + '</td>' +
      '<td class="right"><span class="coin">' + SC.coins(c.coin_balance) + '</span></td>' +
      '<td class="right">' + SC.n(c.order_count || 0) + '</td>' +
      '<td class="mono" style="white-space:nowrap;font-size:.76rem">' +
        SC.esc(SC.dt(c.created_at)) + '</td>' +
      '<td class="right"><button class="btn btn-sm btn-ghost" type="button" data-cus-open="' +
        SC.esc(c.id) + '">Open</button></td>' +
    '</tr>';
  }

  function loadCustomers(append) {
    var body = SC.$('#m-cus-body');
    if (!append) {
      cusState.offset = 0;
      body.innerHTML = '<tr><td colspan="7"><div class="skel"></div></td></tr>';
    }
    return get('/api/master/customers', {
      q: cusState.q, limit: cusState.limit, offset: cusState.offset
    }).then(function (data) {
      cusState.total = data.total || 0;
      var list = data.customers || [];
      var html = list.map(cusRow).join('');
      if (append) body.insertAdjacentHTML('beforeend', html);
      else {
        body.innerHTML = html ||
          '<tr><td colspan="7"><div class="empty"><div class="big">☺</div>' +
          (cusState.q ? 'Nobody matches that search.' : 'No customer has signed in yet.') +
          '</div></td></tr>';
      }
      cusState.offset += list.length;
      SC.$('#m-cus-total').textContent = SC.n(cusState.total) + ' total';
      SC.$('#m-cus-more').classList.toggle('hidden', cusState.offset >= cusState.total);
    }).catch(function (error) {
      body.innerHTML = '<tr><td colspan="7"><div class="empty"><div class="big">⚠</div>' +
        SC.esc(error.message) + '</div></td></tr>';
    });
  }
  LOADERS.customers = function () { return loadCustomers(false); };

  function runCustomerSearch() {
    cusState.q = (SC.$('#m-cus-q').value || '').trim();
    loadCustomers(false);
  }
  SC.$('#m-cus-go').addEventListener('click', runCustomerSearch);
  SC.$('#m-cus-q').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); runCustomerSearch(); }
  });
  SC.$('#m-cus-q').addEventListener('input', SC.debounce(runCustomerSearch, 400));
  SC.$('#m-cus-more').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () { return loadCustomers(true); });
  });
  /* ---------------------------------------------------------- wallet control ---

     Coins are never "set" here. Every adjustment appends one ledger row with a
     reason attached, the server recomputes the balance from that ledger, and the
     action lands in the audit log under the Master who signed it. The reason is
     mandatory in the schema too, so a blank one is rejected server-side even if
     this form were bypassed.                                                   */
  function walletForm(customer, onDone) {
    formModal('Adjust wallet — ' + (customer.name || customer.email || customer.code),
      '<div class="form-grid">' +
        '<div class="field"><label for="w-dir">Movement</label><select id="w-dir">' +
          '<option value="add">Add coins (COIN PURCHASE)</option>' +
          '<option value="bonus">Bonus coins (BONUS COIN)</option>' +
          '<option value="remove">Remove coins (COIN SPENT)</option>' +
        '</select></div>' +
        field('Coins', 'w-coins', 'type="number" min="1" step="1" required',
          'A whole number of coins. Removal cannot take the balance below zero.') +
      '</div>' +
      area('Reason (required — written to the ledger and the audit log)', 'w-reason',
        'rows="3" maxlength="1000" required'),
      function (m) {
        var coins = intOf(m, 'w-coins');
        var reason = val(m, 'w-reason');
        if (!coins || coins < 1) { SC.err('Enter at least 1 coin.'); return null; }
        if (reason.length < 3) { SC.err('A reason of at least 3 characters is required.'); return null; }
        return post('/api/master/customers/' + encodeURIComponent(customer.id) + '/wallet', {
          coins: coins, direction: val(m, 'w-dir'), reason: reason
        }).then(function (data) {
          m.close();
          SC.ok(data.message);
          if (onDone) onDone(data);
        }).catch(SC.fail);
      }, { saveLabel: 'Apply adjustment', wide: false });
  }

  function txRow(t) {
    var negative = t.amount < 0;
    return '<div class="lrow">' +
      '<div class="main"><b style="font-size:.82rem">' + SC.esc(t.type.replace(/_/g, ' ')) +
        '</b> ' + SC.statusBadge(t.status) +
        '<div class="mono faint" style="font-size:.7rem">' + SC.esc(t.reference_code) + '</div>' +
        (t.reason ? '<div class="muted" style="font-size:.74rem">' + SC.esc(t.reason) +
          '</div>' : '') +
        '<div class="faint" style="font-size:.72rem">' + SC.esc(SC.dt(t.created_at)) +
          (t.approved_by ? ' · by ' + SC.esc(t.approved_by) : '') +
          (t.payment_method ? ' · ' + SC.esc(t.payment_method) : '') +
          (t.bdt_amount ? ' · ' + SC.esc(SC.bdt(t.bdt_amount)) : '') + '</div></div>' +
      '<div class="acts"><div style="text-align:right">' +
        '<b class="' + (negative ? 'amt-neg' : 'amt-pos') + '" style="font-size:.9rem">' +
          (negative ? '' : '+') + SC.n(t.amount) + '</b>' +
        '<div class="faint" style="font-size:.7rem">bal ' + SC.n(t.balance_after) + '</div>' +
      '</div></div></div>';
  }

  function deliveryRow(d) {
    return '<div class="lrow"><div class="main">' +
      '<b style="font-size:.8rem">' + SC.esc(d.recipient || '—') + '</b> ' +
      SC.statusBadge(d.status) +
      '<div class="faint" style="font-size:.72rem">' + SC.esc(d.channel) + ' · ' +
        SC.n(d.attempts) + ' attempt' + (d.attempts === 1 ? '' : 's') + ' · ' +
        SC.esc(d.sent_at ? SC.dt(d.sent_at) : SC.dt(d.created_at)) + '</div>' +
      (d.error ? '<div class="bad" style="font-size:.72rem">' + SC.esc(d.error) + '</div>' : '') +
      '</div></div>';
  }
  function customerModal(id) {
    var modal = SC.modal('<div class="skel" style="height:220px"></div>',
      { title: 'Customer', wide: true });

    function paint(data) {
      var c = data.customer || {};
      var w = data.wallet || {};
      var cons = w.consistency || {};
      var orders = (data.orders && data.orders.items) || [];
      var pays = (data.payments && data.payments.items) || [];
      var txs = data.transactions || [];
      var dels = data.deliveries || [];

      modal.content.innerHTML =
        '<div class="row" style="gap:.9rem;margin-bottom:1rem">' +
          (c.avatar ? '<img class="avatar" src="' + SC.esc(c.avatar) + '" alt="">' : '') +
          '<div><b style="font-size:1rem">' + SC.esc(c.name || c.username || 'Viewer') + '</b>' +
          '<div class="mono muted" style="font-size:.78rem">' + SC.esc(c.email || '—') + '</div>' +
          '<div class="mono faint" style="font-size:.72rem">' + SC.esc(c.code || '') +
            ' · joined ' + SC.esc(SC.dt(c.created_at)) + '</div></div>' +
          '<div class="grow"></div>' +
          '<div style="text-align:right"><div class="coin coin-lg">' + SC.coins(w.balance) +
            '</div><div class="faint" style="font-size:.7rem">wallet balance</div></div>' +
        '</div>' +
        '<div class="row-wrap" style="margin-bottom:.9rem">' +
          SC.statusBadge(c.is_active ? 'ACTIVE' : 'DISABLED') +
          '<span class="badge ' + (cons.consistent ? 'badge-ok' : 'badge-bad') + '">' +
            (cons.consistent ? 'ledger balanced' : 'LEDGER MISMATCH') + '</span>' +
          '<span class="faint mono" style="font-size:.72rem">cached ' +
            SC.n(cons.cached_balance) + ' / ledger ' + SC.n(cons.ledger_sum) + '</span>' +
          '<div class="grow"></div>' +
          '<button class="btn btn-sm btn-primary" type="button" data-wallet>Adjust wallet</button>' +
        '</div>' +
        '<div class="tabs" data-cus-tabs>' +
          '<button class="tab active" type="button" data-cus-tab="ledger">Ledger ' +
            '<span class="cnt">' + SC.n(txs.length) + '</span></button>' +
          '<button class="tab" type="button" data-cus-tab="orders">Orders ' +
            '<span class="cnt">' + SC.n((data.orders && data.orders.total) || 0) + '</span></button>' +
          '<button class="tab" type="button" data-cus-tab="payments">Top-ups ' +
            '<span class="cnt">' + SC.n((data.payments && data.payments.total) || 0) + '</span></button>' +
          '<button class="tab" type="button" data-cus-tab="deliveries">Deliveries ' +
            '<span class="cnt">' + SC.n(dels.length) + '</span></button>' +
        '</div>' +
        '<div class="card" data-cus-pane="ledger">' +
          (txs.length ? txs.map(txRow).join('')
            : '<p class="hint">No coin movement yet.</p>') + '</div>' +
        '<div class="card hidden" data-cus-pane="orders">' +
          (orders.length ? orders.map(miniOrder).join('')
            : '<p class="hint">No order yet.</p>') + '</div>' +
        '<div class="card hidden" data-cus-pane="payments">' +
          (pays.length ? pays.map(function (p) {
            var shot = (p.screenshots || [])[0];
            return '<div class="lrow"><div class="main">' +
              '<b style="font-size:.82rem">' + SC.esc(p.package_name || 'Coin top-up') + '</b> ' +
              SC.statusBadge(p.status) +
              '<div class="mono faint" style="font-size:.7rem">' + SC.esc(p.code) + '</div>' +
              '<div class="faint" style="font-size:.72rem">' + SC.esc(SC.dt(p.created_at)) +
                (p.method_name ? ' · ' + SC.esc(p.method_name) : '') +
                (p.reviewed_by ? ' · by ' + SC.esc(p.reviewed_by) : '') + '</div></div>' +
              '<div class="acts"><div style="text-align:right">' +
                '<b style="font-size:.86rem">' + SC.esc(SC.bdt(p.amount_bdt)) + '</b>' +
                '<div class="faint" style="font-size:.7rem">' + SC.n(p.total_coins) +
                  ' coins</div></div>' +
                (shot
                  ? '<button class="btn btn-sm btn-ghost" type="button" data-shot="' +
                    SC.esc(shot.id) + '" data-shot-code="' + SC.esc(p.code) +
                    '">Screenshot</button>'
                  : '<span class="badge">No proof</span>') +
              '</div></div>';
          }).join('') : '<p class="hint">No top-up request yet.</p>') + '</div>' +
        '<div class="card hidden" data-cus-pane="deliveries">' +
          (dels.length ? dels.map(deliveryRow).join('')
            : '<p class="hint">Nothing has been emailed to this customer yet.</p>') + '</div>';

      SC.$$('[data-cus-tab]', modal.content).forEach(function (tab) {
        tab.addEventListener('click', function () {
          SC.$$('[data-cus-tab]', modal.content).forEach(function (t) {
            t.classList.remove('active');
          });
          tab.classList.add('active');
          SC.$$('[data-cus-pane]', modal.content).forEach(function (pane) {
            pane.classList.toggle('hidden', pane.dataset.cusPane !== tab.dataset.cusTab);
          });
        });
      });

      modal.content.querySelector('[data-wallet]').addEventListener('click', function () {
        walletForm(c, function () {
          reload();
          if (loaded.customers) loadCustomers(false);
          if (loaded.overview) loadOverview();
        });
      });
    }

    function reload() {
      return get('/api/master/customers/' + encodeURIComponent(id))
        .then(paint)
        .catch(function (error) { none(modal.content, error.message, '⚠'); });
    }
    reload();
    return modal;
  }

  SC.on(document, 'click', '[data-cus-open]', function (ev, node) {
    customerModal(node.dataset.cusOpen);
  });
  /* -------------------------------------------------------- staff accounts ---

     Everything on this pane is an authorisation change, so nothing is decided
     here. The buttons post an intent; the server refuses what must be refused —
     the root Master cannot be disabled or deleted, no operator can disable or
     delete themselves, and a Seller holding orders is disabled instead of
     removed so order history keeps its owner.                                  */
  function staffRow(s, isMaster) {
    var stats = s.order_stats || {};
    return '<div class="lrow">' +
      '<div class="main">' +
        '<b style="font-size:.9rem">' + SC.esc(s.username) + '</b> ' +
        (s.is_root ? '<span class="badge badge-violet">ROOT</span> ' : '') +
        SC.statusBadge(s.is_active ? 'ACTIVE' : 'DISABLED') +
        (s.must_change_password
          ? ' <span class="badge badge-warn">must change password</span>' : '') +
        '<div class="mono faint" style="font-size:.72rem">' + SC.esc(s.code || '') +
          (s.seller_code ? ' · ' + SC.esc(s.seller_code) : '') + '</div>' +
        '<div class="muted" style="font-size:.74rem">' +
          (s.device_lock ? 'device lock ON' : 'device lock OFF') +
          ' · ' + (s.bound_device ? 'bound to ' + SC.esc(s.bound_device) : 'no device bound') +
          (isMaster ? '' : ' · ' + (s.can_verify_payments
            ? 'can verify payments' : 'cannot verify payments')) +
        '</div>' +
        '<div class="faint" style="font-size:.72rem">' +
          (s.last_login_at ? 'last login ' + SC.esc(SC.rel(s.last_login_at)) +
            (s.last_login_ip ? ' from ' + SC.esc(s.last_login_ip) : '') : 'never signed in') +
          (isMaster ? '' : ' · ' + SC.n(stats.total || 0) + ' orders') +
        '</div>' +
        (s.note ? '<div class="faint" style="font-size:.72rem">' + SC.esc(s.note) + '</div>' : '') +
      '</div>' +
      '<div class="acts">' +
        '<button class="btn btn-sm btn-ghost" type="button" data-staff-edit="' + SC.esc(s.id) +
          '">Manage</button>' +
      '</div></div>';
  }

  var staffCache = { masters: [], sellers: [] };

  function loadStaff() {
    var q = (SC.$('#m-staff-q').value || '').trim();
    var mhost = SC.$('#m-mst-list');
    var shost = SC.$('#m-slr-list');
    return Promise.all([
      get('/api/master/masters', { q: q }).catch(function (e) { bad(mhost, e.message); return null; }),
      get('/api/master/sellers', { q: q }).catch(function (e) { bad(shost, e.message); return null; })
    ]).then(function (results) {
      if (results[0]) {
        staffCache.masters = results[0].masters || [];
        if (staffCache.masters.length) mhost.innerHTML = staffCache.masters.map(function (m) {
          return staffRow(m, true);
        }).join('');
        else none(mhost, q ? 'No Master matches that.' : 'No Master account.', '⚿');
      }
      if (results[1]) {
        staffCache.sellers = results[1].sellers || [];
        if (staffCache.sellers.length) shost.innerHTML = staffCache.sellers.map(function (s) {
          return staffRow(s, false);
        }).join('');
        else none(shost, q ? 'No Seller matches that.' : 'No Seller account yet.', '⚿');
      }
    });
  }
  LOADERS.staff = loadStaff;
  SC.$('#m-staff-q').addEventListener('input', SC.debounce(loadStaff, 350));
  function masterForm() {
    formModal('New Master account',
      '<div class="form-grid">' +
        field('Username', 'ms-user', 'type="text" maxlength="64" required autocomplete="off"') +
        field('Password', 'ms-pass', 'type="password" maxlength="256" required autocomplete="new-password"',
          'At least 6 characters. Stored as an Argon2id hash — never in plain text.') +
      '</div>' +
      area('Note (optional)', 'ms-note', 'rows="2" maxlength="500"') +
      check('Bind this account to the first device that signs in', 'ms-lock', true),
      function (m) {
        var username = val(m, 'ms-user');
        var password = val(m, 'ms-pass');
        if (username.length < 3) { SC.err('Username needs at least 3 characters.'); return null; }
        if (password.length < 6) { SC.err('Password needs at least 6 characters.'); return null; }
        return post('/api/master/masters', {
          username: username, password: password,
          device_lock: flag(m, 'ms-lock'), note: val(m, 'ms-note') || null
        }).then(function (data) {
          m.close();
          SC.ok('Master ' + (data.master && data.master.username) + ' created.');
          loadStaff();
        }).catch(SC.fail);
      }, { saveLabel: 'Create Master', wide: false });
  }

  function sellerForm() {
    formModal('New Seller account',
      '<div class="form-grid">' +
        field('Username', 'sl-user', 'type="text" maxlength="64" required autocomplete="off"') +
        field('Password', 'sl-pass', 'type="password" maxlength="256" required autocomplete="new-password"') +
        field('Contact email', 'sl-mail', 'type="email" maxlength="255"',
          'Used for seller-facing notices only — never for customer delivery.') +
      '</div>' +
      area('Note (optional)', 'sl-note', 'rows="2" maxlength="500"') +
      check('Bind this account to the first device that signs in', 'sl-lock', true) +
      check('Allow this Seller to confirm or reject coin payments', 'sl-verify', false),
      function (m) {
        var username = val(m, 'sl-user');
        var password = val(m, 'sl-pass');
        if (username.length < 3) { SC.err('Username needs at least 3 characters.'); return null; }
        if (password.length < 6) { SC.err('Password needs at least 6 characters.'); return null; }
        return post('/api/master/sellers', {
          username: username, password: password,
          contact_email: val(m, 'sl-mail') || null,
          device_lock: flag(m, 'sl-lock'),
          can_verify_payments: flag(m, 'sl-verify'),
          note: val(m, 'sl-note') || null
        }).then(function (data) {
          m.close();
          SC.ok('Seller ' + (data.seller && data.seller.username) + ' created.');
          loadStaff();
          ref.sellers = [];
          loadSellerOptions();
        }).catch(SC.fail);
      }, { saveLabel: 'Create Seller', wide: false });
  }

  SC.$('#m-mst-new').addEventListener('click', masterForm);
  SC.$('#m-slr-new').addEventListener('click', sellerForm);
  function deviceCard(d) {
    return '<div class="lrow"><div class="main">' +
      '<b style="font-size:.82rem">' + SC.esc(d.label || 'device') + '</b> ' +
      SC.statusBadge(d.is_active ? 'ACTIVE' : 'DISABLED') +
      '<div class="muted" style="font-size:.74rem">' + SC.esc(d.platform || 'unknown platform') +
        '</div>' +
      '<div class="faint mono" style="font-size:.7rem">bound ' +
        SC.esc(d.bound_at ? SC.dt(d.bound_at) : '—') +
        ' · seen ' + SC.esc(d.last_seen_at ? SC.rel(d.last_seen_at) : 'never') +
        ' · ' + SC.esc(d.last_ip || d.first_ip || 'no ip') + '</div>' +
      '</div></div>';
  }

  function staffModal(id, isMaster) {
    var modal = SC.modal('<div class="skel" style="height:220px"></div>',
      { title: isMaster ? 'Master account' : 'Seller account', wide: true });

    function paint(s) {
      var stats = s.order_stats || {};
      var self = me && me.id === s.id;
      modal.content.innerHTML =
        '<div class="row" style="gap:.7rem;margin-bottom:.9rem">' +
          '<div><b style="font-size:1rem">' + SC.esc(s.username) + '</b>' +
            (s.is_root ? ' <span class="badge badge-violet">ROOT</span>' : '') +
            (self ? ' <span class="badge badge-info">you</span>' : '') +
            '<div class="mono faint" style="font-size:.74rem">' + SC.esc(s.code || '') +
            (s.seller_code ? ' · ' + SC.esc(s.seller_code) : '') + '</div></div>' +
          '<div class="grow"></div>' + SC.statusBadge(s.is_active ? 'ACTIVE' : 'DISABLED') +
        '</div>' +
        '<dl class="kv">' +
          '<dt>Role</dt><dd>' + SC.esc(s.role) + '</dd>' +
          '<dt>Created</dt><dd>' + SC.esc(SC.dt(s.created_at)) + '</dd>' +
          '<dt>Last login</dt><dd>' + SC.esc(s.last_login_at ? SC.dt(s.last_login_at) : 'never') +
            (s.last_login_ip ? ' · ' + SC.esc(s.last_login_ip) : '') + '</dd>' +
          '<dt>Must change password</dt><dd>' + (s.must_change_password ? 'yes' : 'no') + '</dd>' +
          (isMaster ? '' :
            '<dt>Contact email</dt><dd>' + SC.esc(s.contact_email || '—') + '</dd>' +
            '<dt>Orders</dt><dd>' + SC.n(stats.total || 0) + ' total · ' +
              SC.n(stats.COMPLETED || 0) + ' completed · ' + SC.n(stats.PAID || 0) + ' paid</dd>') +
        '</dl>' +
        '<div class="divider"></div>' +
        '<form onsubmit="return false">' +
          check('Account enabled', 'st-active', !!s.is_active) +
          check('Device lock (one device per account)', 'st-lock', !!s.device_lock) +
          (isMaster ? '' :
            check('Allowed to confirm or reject coin payments', 'st-verify',
              !!s.can_verify_payments)) +
          (isMaster ? '' :
            '<div class="form-grid">' + field('Contact email', 'st-mail',
              'type="email" maxlength="255" value="' + SC.attr(s.contact_email || '') + '"') +
            '</div>') +
          area('Note', 'st-note', 'rows="2" maxlength="1000"') +
        '</form>' +
        '<div class="row-wrap" style="justify-content:flex-end">' +
          '<button class="btn btn-sm btn-primary" type="button" data-st-save>Save changes</button>' +
        '</div>' +
        '<div class="divider"></div>' +
        '<div class="card-title">Bound devices</div>' +
        (s.devices && s.devices.length ? s.devices.map(deviceCard).join('')
          : '<p class="hint">No device bound yet — the next successful login claims the binding.</p>') +
        '<div class="row-wrap" style="justify-content:flex-end;margin-top:.6rem">' +
          '<button class="btn btn-sm" type="button" data-st-unbind>Reset device binding</button>' +
          '<button class="btn btn-sm" type="button" data-st-pass>Reset password</button>' +
          (s.is_root || self ? '' :
            '<button class="btn btn-sm btn-danger" type="button" data-st-del>Delete account</button>') +
        '</div>' +
        (s.is_root
          ? '<p class="hint" style="margin-top:.6rem">The root Master cannot be disabled or ' +
            'deleted — that rule lives in the API, not in this screen.</p>'
          : '');
      modal.content.querySelector('#st-note').value = s.note || '';
      wireStaff(modal, s, isMaster);
    }

    get('/api/master/' + (isMaster ? 'masters' : 'sellers')).then(function (data) {
      var list = (isMaster ? data.masters : data.sellers) || [];
      var found = list.filter(function (x) { return x.id === id; })[0];
      if (!found) { none(modal.content, 'That account no longer exists.', '⚠'); return; }
      paint(found);
    }).catch(function (error) { none(modal.content, error.message, '⚠'); });
    return modal;
  }
  function wireStaff(modal, s, isMaster) {
    var save = modal.content.querySelector('[data-st-save]');
    save.addEventListener('click', function () {
      var payload = {
        is_active: flag(modal, 'st-active'),
        device_lock: flag(modal, 'st-lock'),
        note: val(modal, 'st-note') || null
      };
      if (!isMaster) {
        payload.can_verify_payments = flag(modal, 'st-verify');
        payload.contact_email = val(modal, 'st-mail') || null;
      }
      SC.guard(save, function () {
        return patch('/api/master/accounts/' + encodeURIComponent(s.id), payload)
          .then(function () {
            modal.close();
            SC.ok('Account updated.');
            loadStaff();
          }).catch(SC.fail);
      });
    });

    var unbind = modal.content.querySelector('[data-st-unbind]');
    unbind.addEventListener('click', function () {
      SC.confirm({
        title: 'Reset device binding',
        message: 'Every bound device is released and all sessions for this account are revoked. ' +
          'The next successful login claims the binding.',
        confirmLabel: 'Reset binding',
        danger: true
      }).then(function (answer) {
        if (!answer.ok) return;
        SC.guard(unbind, function () {
          return post('/api/master/accounts/' + encodeURIComponent(s.id) + '/reset-device')
            .then(function (data) { modal.close(); SC.ok(data.message); loadStaff(); })
            .catch(SC.fail);
        });
      });
    });

    modal.content.querySelector('[data-st-pass]').addEventListener('click', function () {
      formModal('Reset password — ' + s.username,
        '<div class="form-grid">' +
          field('New password', 'rp-pass',
            'type="password" maxlength="256" required autocomplete="new-password"',
            'At least 6 characters. Every other session for this account is revoked.') +
        '</div>' +
        check('Force a password change at next sign-in', 'rp-force', true),
        function (m) {
          var pass = val(m, 'rp-pass');
          if (pass.length < 6) { SC.err('Password needs at least 6 characters.'); return null; }
          return post('/api/master/accounts/' + encodeURIComponent(s.id) + '/reset-password', {
            new_password: pass, force_change: flag(m, 'rp-force')
          }).then(function (data) {
            m.close();
            modal.close();
            SC.ok(data.message);
            loadStaff();
          }).catch(SC.fail);
        }, { saveLabel: 'Reset password', wide: false });
    });

    var kill = modal.content.querySelector('[data-st-del]');
    if (kill) {
      kill.addEventListener('click', function () {
        SC.confirm({
          title: 'Delete ' + s.username,
          message: isMaster
            ? 'The account and its sessions go away. Audit entries it wrote stay forever.'
            : 'A Seller holding orders is disabled instead of deleted, so order history keeps ' +
              'its owner.',
          confirmLabel: 'Delete',
          danger: true
        }).then(function (answer) {
          if (!answer.ok) return;
          SC.guard(kill, function () {
            return del('/api/master/accounts/' + encodeURIComponent(s.id))
              .then(function (data) {
                modal.close();
                SC.ok(data.message);
                loadStaff();
                ref.sellers = [];
                loadSellerOptions();
              }).catch(SC.fail);
          });
        });
      });
    }
  }

  SC.on(document, 'click', '[data-staff-edit]', function (ev, node) {
    var id = node.dataset.staffEdit;
    var isMaster = staffCache.masters.some(function (m) { return m.id === id; });
    staffModal(id, isMaster);
  });
  /* ------------------------------------------------------------- settings ---

     The store is typed key/value. Every row keeps its declared type, so this
     form only chooses a widget from that type and posts strings back — the
     server casts and validates. Nothing here is a feature flag for
     authorisation; those are role checks, not settings.                       */
  var GROUP_LABELS = {
    branding: 'Branding & contact',
    seo: 'Search engine',
    payments: 'Payment copy',
    ui: 'Interface',
    general: 'General'
  };
  var setRows = [];

  function setId(key) { return 'set-' + key.replace(/[^a-zA-Z0-9]+/g, '-'); }

  function setWidget(row) {
    var id = setId(row.key);
    var label = row.label || row.key;
    if (row.type === 'bool') {
      return check(label, id, row.typed_value === true) +
        '<div class="hint mono" style="margin:-.6rem 0 .8rem">' + SC.esc(row.key) + '</div>';
    }
    if (row.type === 'int' || row.type === 'float') {
      return field(label, id, 'type="number" ' + (row.type === 'int' ? 'step="1"' : 'step="0.01"') +
        ' value="' + SC.attr(row.value == null ? '' : row.value) + '"', row.key);
    }
    var long = (row.value || '').length > 70 || /instructions|announcement|description|note/.test(row.key);
    if (long) {
      return '<div class="field span-2"><label for="' + id + '">' + SC.esc(label) + '</label>' +
        '<textarea id="' + id + '" rows="3" maxlength="4000"></textarea>' +
        '<div class="hint mono">' + SC.esc(row.key) + '</div></div>';
    }
    return field(label, id, 'type="text" maxlength="500" value="' +
      SC.attr(row.value == null ? '' : row.value) + '"', row.key);
  }

  function loadSettings() {
    var host = SC.$('#m-set-form');
    return get('/api/master/settings').then(function (data) {
      setRows = data.settings || [];
      if (!setRows.length) { none(host, 'The settings store is empty.', '⚙'); return; }
      var groups = {};
      var order = [];
      setRows.forEach(function (row) {
        var g = row.group || 'general';
        if (!groups[g]) { groups[g] = []; order.push(g); }
        groups[g].push(row);
      });
      host.innerHTML = order.map(function (g) {
        return '<div class="card" style="margin-bottom:1.2rem">' +
          '<div class="card-title">' + SC.esc(GROUP_LABELS[g] || g) + '</div>' +
          '<div class="form-grid">' +
            groups[g].filter(function (r) { return r.type !== 'bool'; }).map(setWidget).join('') +
          '</div>' +
          groups[g].filter(function (r) { return r.type === 'bool'; }).map(setWidget).join('') +
        '</div>';
      }).join('');
      /* textareas are filled as properties so no value can break out of markup */
      setRows.forEach(function (row) {
        var node = host.querySelector('#' + setId(row.key));
        if (node && node.tagName === 'TEXTAREA') node.value = row.value == null ? '' : row.value;
      });
    }).catch(function (error) { bad(host, error.message); });
  }
  LOADERS.settings = loadSettings;

  SC.$('#m-set-save').addEventListener('click', function (ev) {
    var host = SC.$('#m-set-form');
    var values = {};
    setRows.forEach(function (row) {
      var node = host.querySelector('#' + setId(row.key));
      if (!node) return;
      values[row.key] = row.type === 'bool'
        ? (node.checked ? 'true' : 'false')
        : String(node.value == null ? '' : node.value);
    });
    if (!Object.keys(values).length) { SC.warn('Nothing to save yet.'); return; }
    SC.guard(ev.currentTarget, function () {
      return put('/api/master/settings', { values: values }).then(function (data) {
        setRows = data.settings || setRows;
        SC.ok(SC.n(data.updated || 0) + ' setting' + (data.updated === 1 ? '' : 's') +
          ' saved. Public pages pick the change up on their next render.');
      }).catch(SC.fail);
    });
  });
  /* ------------------------------------------------------------ audit log ---

     Append-only. There is no edit or delete endpoint for these rows, which is
     the point: every wallet adjustment, payment decision, refund, account change
     and upload lands here with the operator, the IP and the reason.            */
  var audState = { action: '', offset: 0, limit: 50, total: 0 };

  function audRow(a) {
    var meta = a.meta && Object.keys(a.meta).length
      ? '<div class="faint mono" style="font-size:.68rem;white-space:pre-wrap">' +
        SC.esc(JSON.stringify(a.meta)) + '</div>'
      : '';
    return '<div class="audit-row">' +
      '<div class="when mono">' + SC.esc(SC.dt(a.created_at)) + '</div>' +
      '<div class="act mono">' + SC.esc(a.action) + '</div>' +
      '<div class="sum">' + SC.esc(a.summary || '') + meta + '</div>' +
      '<div class="faint mono" style="font-size:.7rem;text-align:right">' +
        SC.esc(a.actor || 'system') +
        (a.actor_role ? ' · ' + SC.esc(a.actor_role) : '') +
        (a.ip ? '<br>' + SC.esc(a.ip) : '') + '</div>' +
    '</div>';
  }

  function loadAudit(append) {
    var host = SC.$('#m-aud-list');
    if (!append) {
      audState.offset = 0;
      host.innerHTML = '<div class="skel"></div>';
    }
    return get('/api/master/audit', {
      action: audState.action, limit: audState.limit, offset: audState.offset
    }).then(function (data) {
      audState.total = data.total || 0;
      var list = data.entries || [];
      var html = list.map(audRow).join('');
      if (append) host.insertAdjacentHTML('beforeend', html);
      else if (!html) {
        none(host, audState.action ? 'No entry matches that action.' : 'The trail is empty.', '≡');
      } else host.innerHTML = html;
      audState.offset += list.length;
      SC.$('#m-aud-total').textContent = SC.n(audState.total) + ' entries';
      SC.$('#m-aud-more').classList.toggle('hidden', audState.offset >= audState.total);
    }).catch(function (error) { bad(host, error.message); });
  }
  LOADERS.audit = function () { return loadAudit(false); };

  SC.$('#m-aud-action').addEventListener('input', SC.debounce(function () {
    audState.action = (SC.$('#m-aud-action').value || '').trim();
    loadAudit(false);
  }, 350));

  SC.$('#m-aud-more').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () { return loadAudit(true); });
  });
  /* ============================================================ live wiring

     The socket is a hint, never a source of truth. Every event below does one
     thing: mark the affected pane stale and re-fetch it from the API. No number
     on screen is ever patched from a socket payload, so a spoofed or replayed
     frame cannot change what an operator sees — the next GET decides.          */
  function currentView() {
    var active = SC.$('#m-nav button.active');
    return active ? active.dataset.view : 'overview';
  }

  function refresh(view) {
    if (!me || !loaded[view]) return;
    if (currentView() !== view && view !== 'overview') return;
    var loader = LOADERS[view];
    if (loader) loader();
  }

  var NOTE_VIEWS = {
    NEW_COIN_PAYMENT: 'payments',
    PAYMENT_CONFIRMED: 'payments',
    PAYMENT_REJECTED: 'payments',
    NEW_PRODUCT_ORDER: 'orders',
    PRODUCT_PURCHASED: 'orders',
    PRODUCT_DELIVERED: 'orders',
    DELIVERY_FAILED: 'orders',
    ORDER_COMPLETED: 'orders',
    ORDER_REFUNDED: 'orders',
    NEW_CUSTOMER: 'customers',
    COINS_ADDED: 'customers'
  };

  SC.ws.on('notification', function (payload) {
    if (!me) return;
    var view = NOTE_VIEWS[payload && payload.kind];
    if (view) refresh(view);
    /* a new top-up must show on the sidebar even while another pane is open */
    if (payload && payload.kind === 'NEW_COIN_PAYMENT') {
      get('/api/master/payments', { status: 'PENDING', limit: 1 }).then(function (data) {
        setPendingPill((data.counts && data.counts.PENDING) || data.total || 0);
      }).catch(function () { /* the pill is cosmetic; a failure changes nothing */ });
    }
    if (loaded.overview) loadOverview();
    if (loaded.audit && currentView() === 'audit') loadAudit(false);
  });

  SC.ws.on('stats_dirty', function () {
    if (me && loaded.overview) loadOverview();
  });

  /* the socket only carries staff topics once the handshake has re-validated the
     session and the device, so a dropped connection means re-checking who we are */
  window.addEventListener('online', function () {
    if (me) boot(false);
  });
  /* ============================================================== bootstrap */
  boot(false);
})();
