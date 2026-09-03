/* ===========================================================================
   STREAM CORPORATION — Seller Console.

   The seller surface is deliberately narrower than the Master one, and the
   narrowing is enforced on the server, not here:

     • every order query is scoped to `seller_id == principal.user.id`, so this
       file cannot ask for another seller's work even if it tried;
     • MARK COMPLETED re-checks the assignment inside the transaction;
     • the payment pane only exists when /api/seller/overview reports
       `can_verify_payments`, and the four payment endpoints refuse the call
       anyway when a Master has not granted it;
     • refunds, wallet adjustments, catalogue edits and account management have
       no endpoint on this router at all.

   As on /master, nothing on screen is computed in the browser. Coins, counts and
   statuses are rendered from the server response.
   ======================================================================== */
(function () {
  'use strict';

  var SC = window.SC;
  var gate = SC.$('#s-gate');
  var shell = SC.$('#s-console');
  if (!gate || !shell) return;

  var me = null;
  var canVerify = false;
  var loaded = {};

  function bad(node, message) {
    node.innerHTML = '<div class="empty"><div class="big">⚠</div>' + SC.esc(message) + '</div>';
  }
  function none(node, message, icon) {
    node.innerHTML = '<div class="empty"><div class="big">' + SC.esc(icon || '◌') + '</div>' +
      SC.esc(message) + '</div>';
  }

  /* A 401/403 anywhere means the session, the device binding or the account went
     away — drop straight back to the gate with the server's own words. */
  function req(path, options) {
    return SC.api(path, options).catch(function (error) {
      if (error && (error.status === 401 || error.status === 403) && me) lock(error.message);
      throw error;
    });
  }
  function get(path, query) { return req(path, { query: query }); }
  function post(path, json) { return req(path, { method: 'POST', json: json || {} }); }
  function field(label, id, attrs, hint) {
    return '<div class="field"><label for="' + id + '">' + SC.esc(label) + '</label>' +
      '<input id="' + id + '" ' + (attrs || '') + '>' +
      (hint ? '<div class="hint">' + SC.esc(hint) + '</div>' : '') + '</div>';
  }
  function val(modal, id) { return (modal.content.querySelector('#' + id).value || '').trim(); }

  function formModal(title, body, onSave, opts) {
    var o = opts || {};
    var modal = SC.modal(
      '<form onsubmit="return false">' + body + '</form>' +
      '<div class="divider"></div>' +
      '<div class="row-wrap" style="justify-content:flex-end">' +
        (o.hideCancel ? '' : '<button class="btn btn-ghost" type="button" data-cancel>Cancel</button>') +
        '<button class="btn btn-primary" type="button" data-save>' +
        SC.esc(o.saveLabel || 'Save') + '</button>' +
      '</div>',
      { title: title, wide: !!o.wide, sticky: !!o.hideCancel }
    );
    var cancel = modal.content.querySelector('[data-cancel]');
    if (cancel) cancel.addEventListener('click', function () { modal.close(); });
    var save = modal.content.querySelector('[data-save]');
    save.addEventListener('click', function () {
      SC.guard(save, function () { return onSave(modal, save); });
    });
    return modal;
  }
  /* ============================================================== auth gate */
  var loginForm = SC.$('#s-login');
  var loginErr = SC.$('#s-login-err');

  function lock(message) {
    me = null;
    canVerify = false;
    loaded = {};
    shell.classList.add('hidden');
    gate.classList.remove('hidden');
    var bell = SC.$('#sc-bell');
    if (bell) bell.classList.add('hidden');
    SC.$('#sc-user').innerHTML = '';
    SC.$('#s-nav-payments').classList.add('hidden');
    if (message) {
      loginErr.textContent = message;
      loginErr.classList.remove('hidden');
    }
  }

  loginForm.addEventListener('submit', function (ev) {
    ev.preventDefault();
    loginErr.classList.add('hidden');
    var go = SC.$('#s-login-go');
    SC.guard(go, function () {
      return SC.post('/api/auth/seller/login', {
        username: SC.$('#s-user').value.trim(),
        password: SC.$('#s-pass').value,
        device_id: SC.deviceId()
      }).then(function (data) {
        SC.$('#s-pass').value = '';
        return boot(data && data.must_change_password);
      }).catch(function (error) {
        loginErr.textContent = error.message;
        loginErr.classList.remove('hidden');
      });
    });
  });

  SC.$('#s-logout').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () {
      return post('/api/auth/staff/logout')
        .then(function () { lock('Signed out.'); })
        .catch(function () { lock('Signed out.'); });
    });
  });
  function passwordModal(forced) {
    var body =
      (forced
        ? '<p class="warn">This account still uses the password a Master created it with. ' +
          'Choose a new one before working on orders.</p>'
        : '<p>Changing your password signs out every other session on this account.</p>') +
      field('Current password', 'pw-old',
        'type="password" maxlength="256" autocomplete="current-password"') +
      field('New password', 'pw-new', 'type="password" maxlength="256" autocomplete="new-password"',
        'At least 6 characters.') +
      field('Repeat new password', 'pw-rep',
        'type="password" maxlength="256" autocomplete="new-password"');

    var modal = formModal('Change password', body, function (m) {
      var newPw = val(m, 'pw-new');
      if (newPw !== val(m, 'pw-rep')) { SC.err('The two new passwords do not match.'); return null; }
      if (newPw.length < 6) { SC.err('The new password must be at least 6 characters.'); return null; }
      return post('/api/auth/staff/change-password', {
        current_password: val(m, 'pw-old'), new_password: newPw
      }).then(function (data) {
        m.close();
        SC.ok(data.message || 'Password updated.');
        if (forced) { me.must_change_password = false; openView('overview'); }
      }).catch(SC.fail);
    }, { saveLabel: 'Update password', hideCancel: !!forced });

    if (forced) {
      modal.host.addEventListener('click', function (ev) {
        if (ev.target.closest('.modal-back')) ev.stopPropagation();
      }, true);
    }
    return modal;
  }

  SC.$('#s-password').addEventListener('click', function () { passwordModal(false); });

  function paintWho() {
    SC.$('#s-who').textContent = me.username || me.name || 'Seller';
    SC.$('#s-who-code').textContent = me.code || 'SELLER';
    var bell = SC.$('#sc-bell');
    if (bell) bell.classList.remove('hidden');
    SC.$('#sc-user').innerHTML =
      '<span class="badge badge-neon" title="Signed in as ' + SC.attr(me.username || '') + '">' +
      SC.esc(me.username || 'SELLER') + '</span>';
  }
  /* ================================================================== views */
  var LOADERS = {};

  function openView(name) {
    if (name === 'payments' && !canVerify) name = 'overview';
    SC.$$('#s-nav button').forEach(function (button) {
      button.classList.toggle('active', button.dataset.view === name);
    });
    SC.$$('#s-views .view').forEach(function (pane) {
      pane.classList.toggle('active', pane.dataset.pane === name);
    });
    try { window.history.replaceState({}, '', '/seller#' + name); } catch (e) { /* ignore */ }
    var loader = LOADERS[name];
    if (loader && !loaded[name]) { loaded[name] = true; loader(); }
  }

  SC.on(SC.$('#s-nav'), 'click', 'button', function (ev, node) { openView(node.dataset.view); });

  function currentView() {
    var active = SC.$('#s-nav button.active');
    return active ? active.dataset.view : 'overview';
  }
  /* ------------------------------------------------------------- bootstrap */
  function boot(forcePassword) {
    return get('/api/auth/staff/me').then(function (data) {
      me = (data && data.user) || null;
      if (!me || me.role !== 'SELLER') {
        lock(me ? 'This surface needs a Seller account. Masters use /master.' : '');
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
  var STAT_CARDS = [
    { key: 'my_orders', label: 'My orders', sub: 'assigned to this account' },
    { key: 'paid', label: 'Awaiting work', sub: 'paid, not started' },
    { key: 'processing', label: 'In progress', sub: 'being fulfilled' },
    { key: 'completed', label: 'Completed', sub: 'delivered and closed' },
    { key: 'cancelled', label: 'Cancelled', sub: 'closed without delivery' },
    { key: 'refunded', label: 'Refunded', sub: 'coins returned by a Master' },
    { key: 'my_products', label: 'My products', sub: 'assigned catalogue' },
    { key: 'coins_handled', label: 'Coins handled', sub: 'total order value', coin: true }
  ];

  function paintStats(stats) {
    SC.$('#s-stats').innerHTML = STAT_CARDS.map(function (card) {
      var raw = stats[card.key];
      return '<div class="card stat">' +
        '<div class="lbl">' + SC.esc(card.label) + '</div>' +
        '<div class="val">' + (card.coin ? SC.coins(raw) : SC.n(raw || 0)) + '</div>' +
        '<div class="sub">' + SC.esc(card.sub) + '</div></div>';
    }).join('');
  }

  function miniOrder(o) {
    var p = o.product || {};
    var c = o.customer || {};
    return '<div class="lrow"><div class="main">' +
      '<b style="font-size:.84rem">' + SC.esc(p.name || 'Removed product') + '</b> ' +
      SC.statusBadge(o.status) +
      '<div class="mono faint" style="font-size:.7rem">' + SC.esc(o.order_code) + '</div>' +
      '<div class="muted" style="font-size:.74rem">' + SC.esc(c.email || c.label || '—') +
        ' · ' + SC.esc(SC.rel(o.created_at)) + '</div></div>' +
      '<div class="acts"><span class="coin">' + SC.coins(o.coin_total) + '</span>' +
      '<button class="btn btn-sm btn-ghost" type="button" data-ord-open="' + SC.esc(o.id) +
        '">Open</button></div></div>';
  }

  function miniPayment(r) {
    var c = r.customer || {};
    return '<div class="lrow"><div class="main">' +
      '<b style="font-size:.84rem">' + SC.esc(c.label || c.email || 'Viewer') + '</b> ' +
      SC.statusBadge(r.status) +
      '<div class="mono faint" style="font-size:.7rem">' + SC.esc(r.code) + '</div>' +
      '<div class="muted" style="font-size:.74rem">' + SC.esc(r.method_name || 'unknown method') +
        ' · ' + SC.esc(SC.rel(r.created_at)) + '</div></div>' +
      '<div class="acts"><div style="text-align:right">' +
        '<b style="font-size:.84rem">' + SC.esc(SC.bdt(r.amount_bdt)) + '</b>' +
        '<div class="faint" style="font-size:.7rem">' + SC.n(r.total_coins) + ' coins</div></div>' +
      '</div></div>';
  }

  function setPendingPill(count) {
    var pill = SC.$('#s-pending-pill');
    pill.textContent = SC.n(count || 0);
    pill.classList.toggle('hidden', !count);
  }

  function loadOverview() {
    var ordersHost = SC.$('#s-ov-orders');
    var payHost = SC.$('#s-ov-pending');
    return get('/api/seller/overview').then(function (data) {
      paintStats(data.stats || {});

      /* the permission is a server fact; the nav item only mirrors it */
      canVerify = !!data.can_verify_payments;
      SC.$('#s-nav-payments').classList.toggle('hidden', !canVerify);

      var recent = data.recent_orders || [];
      if (recent.length) ordersHost.innerHTML = recent.map(miniOrder).join('');
      else none(ordersHost, 'No order has been assigned to you yet.', '▤');

      if (!canVerify) {
        payHost.innerHTML = '<p class="hint">A Master has not granted this account permission ' +
          'to verify coin payments, so top-up requests are not shown here.</p>';
        setPendingPill(0);
        return;
      }
      var pending = data.pending_payments || [];
      setPendingPill(data.pending_payment_total || pending.length);
      if (pending.length) payHost.innerHTML = pending.map(miniPayment).join('');
      else none(payHost, 'Nothing is waiting for verification.', '✓');
    }).catch(function (error) {
      bad(ordersHost, error.message);
      bad(payHost, error.message);
    });
  }
  LOADERS.overview = loadOverview;
  SC.$('#s-refresh').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () { return loadOverview(); });
  });
  /* ================================================================= orders */
  var ordState = { status: '', q: '', offset: 0, limit: 25, total: 0 };

  function ordRow(o) {
    var p = o.product || {};
    var c = o.customer || {};
    var d = o.delivery || {};
    var closed = o.status === 'COMPLETED' || o.status === 'CANCELLED' || o.status === 'REFUNDED';
    return '<tr>' +
      '<td class="mono">' + SC.esc(o.order_code) + '</td>' +
      '<td><b style="font-size:.84rem">' + SC.esc(c.label || 'Viewer') + '</b>' +
        '<div class="mono muted" style="font-size:.72rem">' + SC.esc(c.email || '—') +
        '</div></td>' +
      '<td>' + SC.esc(p.name || 'Removed product') +
        (p.version ? '<div class="faint" style="font-size:.72rem">v' + SC.esc(p.version) +
          '</div>' : '') + '</td>' +
      '<td class="right"><span class="coin">' + SC.coins(o.coin_total) + '</span></td>' +
      '<td>' + SC.statusBadge(o.status) + '</td>' +
      '<td>' + (d.status ? SC.statusBadge(d.status) : '<span class="muted">—</span>') + '</td>' +
      '<td class="mono" style="white-space:nowrap;font-size:.74rem">' +
        SC.esc(SC.dt(o.created_at)) + '</td>' +
      '<td class="right"><div class="row-wrap" style="justify-content:flex-end">' +
        '<button class="btn btn-sm btn-ghost" type="button" data-ord-open="' + SC.esc(o.id) +
          '">Open</button>' +
        (closed ? '' : '<button class="btn btn-sm btn-ok" type="button" data-ord-done="' +
          SC.esc(o.id) + '">Complete</button>') +
      '</div></td>' +
    '</tr>';
  }

  function loadOrders(append) {
    var body = SC.$('#s-ord-body');
    if (!append) {
      ordState.offset = 0;
      body.innerHTML = '<tr><td colspan="8"><div class="skel"></div></td></tr>';
    }
    return get('/api/seller/orders', {
      status: ordState.status, q: ordState.q,
      limit: ordState.limit, offset: ordState.offset
    }).then(function (data) {
      ordState.total = data.total || 0;
      var list = data.orders || [];
      var html = list.map(ordRow).join('');
      if (append) body.insertAdjacentHTML('beforeend', html);
      else {
        body.innerHTML = html ||
          '<tr><td colspan="8"><div class="empty"><div class="big">▤</div>' +
          (ordState.q || ordState.status
            ? 'No order matches this filter.'
            : 'No order has been assigned to you yet.') + '</div></td></tr>';
      }
      ordState.offset += list.length;
      SC.$('#s-ord-more').classList.toggle('hidden', ordState.offset >= ordState.total);

      var counts = data.counts || {};
      SC.$$('#s-ord-tabs [data-count]').forEach(function (node) {
        node.textContent = SC.n(counts[node.dataset.count] || 0);
      });
    }).catch(function (error) {
      body.innerHTML = '<tr><td colspan="8"><div class="empty"><div class="big">⚠</div>' +
        SC.esc(error.message) + '</div></td></tr>';
    });
  }
  LOADERS.orders = function () { return loadOrders(false); };

  SC.$$('#s-ord-tabs .tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      SC.$$('#s-ord-tabs .tab').forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      ordState.status = tab.dataset.status || '';
      loadOrders(false);
    });
  });

  SC.$('#s-ord-q').addEventListener('input', SC.debounce(function () {
    ordState.q = (SC.$('#s-ord-q').value || '').trim();
    loadOrders(false);
  }, 350));

  SC.$('#s-ord-more').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () { return loadOrders(true); });
  });
  function afterOrderChange() {
    if (loaded.orders) loadOrders(false);
    if (loaded.overview) loadOverview();
  }

  /* MARK COMPLETED. The note is mandatory here because the customer reads it, and
     the server re-checks the assignment inside the transaction — this button is a
     request, not a decision. */
  function completeOrder(button, id) {
    return SC.confirm({
      title: 'Mark completed',
      message: 'Confirm the software has been delivered. The note below is stored with the ' +
        'order and shown to the customer.',
      confirmLabel: 'Mark completed',
      reason: true,
      reasonLabel: 'Note to the customer (required)',
      reasonPlaceholder: 'Licence key emailed, activation verified.'
    }).then(function (answer) {
      if (!answer.ok) return null;
      var note = (answer.reason || '').trim();
      if (!note) { SC.err('A note is required.'); return null; }
      return new Promise(function (resolve) {
        SC.guard(button, function () {
          return post('/api/seller/orders/' + encodeURIComponent(id) + '/complete', { note: note })
            .then(function (data) {
              SC.ok(data.message);
              afterOrderChange();
              resolve(data);
            })
            .catch(function (error) { SC.fail(error); resolve(null); });
        });
      });
    });
  }

  function orderModal(id) {
    var modal = SC.modal('<div class="skel" style="height:200px"></div>',
      { title: 'Order', wide: true });
    get('/api/seller/orders/' + encodeURIComponent(id)).then(function (data) {
      var o = data.order || {};
      var p = o.product || {};
      var c = o.customer || {};
      var d = o.delivery || {};
      var closed = o.status === 'COMPLETED' || o.status === 'CANCELLED' || o.status === 'REFUNDED';

      modal.content.innerHTML =
        '<div class="row" style="gap:.7rem;margin-bottom:.9rem">' +
          '<div><b style="font-size:1rem">' + SC.esc(p.name || 'Removed product') + '</b>' +
            '<div class="mono muted" style="font-size:.78rem">' + SC.esc(o.order_code) +
            '</div></div>' +
          '<div class="grow"></div>' + SC.statusBadge(o.status) +
        '</div>' +
        '<dl class="kv">' +
          '<dt>Customer</dt><dd>' + SC.esc(c.label || 'Viewer') + '</dd>' +
          '<dt>Gmail</dt><dd class="mono">' + SC.esc(c.email || '—') + '</dd>' +
          '<dt>Customer ID</dt><dd class="mono">' + SC.esc(c.code || '—') + '</dd>' +
          '<dt>Coins paid</dt><dd><span class="coin">' + SC.coins(o.coin_total) + '</span></dd>' +
          '<dt>Delivery</dt><dd>' + (d.status ? SC.statusBadge(d.status) : '—') + '</dd>' +
          '<dt>Sent to</dt><dd class="mono">' + SC.esc(d.email_to || '—') + '</dd>' +
          '<dt>Sent at</dt><dd>' + SC.esc(d.sent_at ? SC.dt(d.sent_at) : 'not yet') + '</dd>' +
          '<dt>Ordered</dt><dd>' + SC.esc(SC.dt(o.created_at)) + '</dd>' +
          (o.completed_at ? '<dt>Completed</dt><dd>' + SC.esc(SC.dt(o.completed_at)) +
            '</dd>' : '') +
          (o.refunded_at ? '<dt>Refunded</dt><dd>' + SC.esc(SC.dt(o.refunded_at)) + '</dd>' : '') +
          (o.refund_reason ? '<dt>Refund reason</dt><dd>' + SC.esc(o.refund_reason) +
            '</dd>' : '') +
          (o.seller_note ? '<dt>My note</dt><dd>' + SC.esc(o.seller_note) + '</dd>' : '') +
        '</dl>' +
        (d.error ? '<p class="bad" style="font-size:.84rem;margin-top:.7rem">Delivery problem: ' +
          SC.esc(d.error) + '</p>' : '') +
        '<div class="divider"></div>' +
        (closed
          ? '<p class="hint">This order is closed. Refunds and re-delivery stay with a Master.</p>'
          : '<div class="row-wrap" style="justify-content:flex-end">' +
            '<button class="btn btn-sm btn-ok" type="button" data-done>Mark completed</button>' +
            '</div>');

      var done = modal.content.querySelector('[data-done]');
      if (done) {
        done.addEventListener('click', function () {
          completeOrder(done, o.id).then(function (result) { if (result) modal.close(); });
        });
      }
    }).catch(function (error) { none(modal.content, error.message, '⚠'); });
    return modal;
  }

  SC.on(document, 'click', '[data-ord-open]', function (ev, node) {
    orderModal(node.dataset.ordOpen);
  });
  SC.on(document, 'click', '[data-ord-done]', function (ev, node) {
    completeOrder(node, node.dataset.ordDone);
  });
  /* =============================================================== products

     Read-only on purpose: a seller has no catalogue endpoint that writes. Pricing,
     media and the deliverable file belong to a Master.                          */
  function prodRow(p) {
    var flags = [];
    if (!p.is_active) flags.push('<span class="badge badge-bad">hidden</span>');
    if (p.is_featured) flags.push('<span class="badge badge-violet">featured</span>');
    if (p.file) flags.push('<span class="badge badge-ok">file attached</span>');
    else if (p.external_download_url) flags.push('<span class="badge badge-info">external link</span>');
    else flags.push('<span class="badge badge-warn">no deliverable</span>');
    if (!p.in_stock) flags.push('<span class="badge badge-bad">out of stock</span>');
    return '<div class="lrow">' +
      (p.thumbnail_url
        ? '<img class="thumb" src="' + SC.esc(p.thumbnail_url) + '" alt="">'
        : '<div class="thumb"></div>') +
      '<div class="main"><b style="font-size:.9rem">' + SC.esc(p.name) + '</b>' +
        '<div class="muted" style="font-size:.74rem">' +
          SC.esc((p.category && p.category.name) || 'Uncategorised') +
          (p.version ? ' · v' + SC.esc(p.version) : '') +
          ' · ' + SC.n(p.sold_count || 0) + ' sold' +
          ' · ' + SC.n(p.view_count || 0) + ' views</div>' +
        '<div class="row-wrap" style="gap:.3rem;margin-top:.25rem">' + flags.join('') + '</div>' +
      '</div>' +
      '<div class="acts"><span class="coin">' + SC.coins(p.coin_price) + '</span>' +
        (p.slug ? '<a class="btn btn-sm btn-ghost" href="/product/' + SC.esc(p.slug) +
          '" target="_blank" rel="noopener">View page</a>' : '') +
      '</div></div>';
  }

  function loadProducts() {
    var host = SC.$('#s-prod-list');
    return get('/api/seller/products', { limit: 100 }).then(function (data) {
      var list = data.products || [];
      SC.$('#s-prod-total').textContent = SC.n(data.total || 0) + ' total';
      if (!list.length) {
        none(host, 'No product is assigned to this seller account yet.', '◱');
        return;
      }
      host.innerHTML = '<div class="card">' + list.map(prodRow).join('') + '</div>';
    }).catch(function (error) { bad(host, error.message); });
  }
  LOADERS.products = loadProducts;
  /* =============================================================== payments

     Identical mechanics to the Master pane, and identically powerless: CONFIRM
     posts an intent and the server performs a conditional state transition. If a
     Master confirmed the same request a moment earlier the response comes back
     with `already_processed` — a success, and the reason a double-click or two
     operators racing can never credit coins twice.                             */
  var payState = { status: 'PENDING', offset: 0, limit: 20, total: 0 };

  function reviewCard(r) {
    var c = r.customer || {};
    var pending = r.status === 'PENDING';
    var shot = (r.screenshots || [])[0];
    return '<div class="card review">' +
      '<div class="review-top">' +
        '<div class="who"><b>' + SC.esc(c.label || 'Viewer') + '</b>' +
          '<div class="mono muted" style="font-size:.76rem">' + SC.esc(c.email || '—') + '</div>' +
          '<div class="mono faint" style="font-size:.7rem">' + SC.esc(c.code || '') + '</div>' +
        '</div>' +
        '<div class="review-amt"><div class="bdt">' + SC.esc(SC.bdt(r.amount_bdt)) + '</div>' +
          '<div class="cn">' + SC.n(r.total_coins) + ' coins</div>' +
          SC.statusBadge(r.status) + '</div>' +
      '</div>' +
      '<div class="review-meta">' +
        '<div><span>request</span><b class="mono">' + SC.esc(r.code) + '</b></div>' +
        '<div><span>package</span>' + SC.esc(r.package_name || '—') + '</div>' +
        '<div><span>method</span>' + SC.esc(r.method_name || '—') +
          (r.method_number ? ' · ' + SC.esc(r.method_number) : '') + '</div>' +
        '<div><span>sender</span><b class="mono">' + SC.esc(r.sender_number || '—') + '</b></div>' +
        '<div><span>transaction ref</span><b class="mono">' +
          SC.esc(r.transaction_ref || '—') + '</b></div>' +
        '<div><span>submitted</span>' + SC.esc(SC.dt(r.created_at)) + '</div>' +
        '<div><span>coins</span>' + SC.n(r.coins) +
          (r.bonus_coins ? ' + ' + SC.n(r.bonus_coins) + ' bonus' : '') + '</div>' +
        (r.reviewed_by ? '<div><span>reviewed by</span>' + SC.esc(r.reviewed_by) +
          (r.reviewed_at ? ' · ' + SC.esc(SC.dt(r.reviewed_at)) : '') + '</div>' : '') +
        (r.reject_reason ? '<div style="grid-column:1/-1"><span>reject reason</span>' +
          SC.esc(r.reject_reason) + '</div>' : '') +
        (r.note ? '<div style="grid-column:1/-1"><span>customer note</span>' +
          SC.esc(r.note) + '</div>' : '') +
      '</div>' +
      '<div class="review-acts">' +
        (shot
          ? '<button class="btn btn-sm" type="button" data-shot="' + SC.esc(shot.id) +
            '" data-shot-code="' + SC.attr(r.code) + '">View screenshot</button>'
          : '<span class="badge badge-warn">No screenshot</span>') +
        '<div class="grow"></div>' +
        (pending
          ? '<button class="btn btn-sm btn-danger" type="button" data-reject="' + SC.esc(r.id) +
              '">Reject</button>' +
            '<button class="btn btn-sm btn-ok" type="button" data-confirm="' + SC.esc(r.id) +
              '">Confirm &amp; credit coins</button>'
          : '<span class="hint">Already reviewed — coins can no longer change here.</span>') +
      '</div></div>';
  }

  function loadPayments(append) {
    var host = SC.$('#s-pay-list');
    if (!canVerify) {
      host.innerHTML = '<div class="card"><p class="hint">Your account is not allowed to verify ' +
        'coin payments. Ask a Master to enable it.</p></div>';
      return Promise.resolve();
    }
    if (!append) {
      payState.offset = 0;
      host.innerHTML = '<div class="skel" style="height:120px"></div>';
    }
    return get('/api/seller/payments', {
      status: payState.status, limit: payState.limit, offset: payState.offset
    }).then(function (data) {
      payState.total = data.total || 0;
      var list = data.requests || [];
      var html = list.map(reviewCard).join('');
      if (append) host.insertAdjacentHTML('beforeend', html);
      else if (!html) none(host, 'Nothing here.', '✓');
      else host.innerHTML = html;

      payState.offset += list.length;
      SC.$('#s-pay-more').classList.toggle('hidden', payState.offset >= payState.total);

      var counts = data.counts || {};
      SC.$$('#s-pay-tabs [data-count]').forEach(function (node) {
        node.textContent = SC.n(counts[node.dataset.count] || 0);
      });
      setPendingPill(counts.PENDING || 0);
    }).catch(function (error) { bad(host, error.message); });
  }
  LOADERS.payments = function () { return loadPayments(false); };

  SC.$$('#s-pay-tabs .tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      SC.$$('#s-pay-tabs .tab').forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      payState.status = tab.dataset.status || '';
      loadPayments(false);
    });
  });

  SC.$('#s-pay-more').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () { return loadPayments(true); });
  });
  /* ------------------------------------------------------- verification acts
     Confirm and reject both report ``already_processed`` when the state machine
     refused a second transition — that is a success, not an error, and it is the
     reason a double-click can never credit coins twice. The number of coins is
     never sent from here; the server reads it from the stored request row.     */
  SC.on(document, 'click', '[data-shot]', function (ev, node) {
    SC.modal(
      '<div class="shot-frame"><img src="/api/payments/screenshot/' +
      encodeURIComponent(node.dataset.shot) + '" alt="Payment screenshot"></div>' +
      '<p class="hint">Check the amount, the sender number and the transaction reference ' +
      'against the request before confirming. A screenshot on its own credits nothing.</p>',
      { title: 'Screenshot · ' + (node.dataset.shotCode || ''), wide: true }
    );
  });

  function afterReview() {
    loadPayments(false);
    if (loaded.overview) loadOverview();
  }

  SC.on(document, 'click', '[data-confirm]', function (ev, node) {
    var id = node.dataset.confirm;
    SC.confirm({
      title: 'Confirm this top-up?',
      message: 'The coins recorded on the stored request are credited to the customer wallet ' +
        'as one ledger entry and the customer is notified. A seller cannot undo this.',
      confirmLabel: 'Confirm & credit'
    }).then(function (answer) {
      if (!answer.ok) return;
      SC.guard(node, function () {
        return post('/api/seller/payments/' + encodeURIComponent(id) + '/confirm')
          .then(function (data) {
            if (data.already_processed) SC.warn(data.message);
            else SC.ok(data.message);
            afterReview();
          })
          .catch(SC.fail);
      });
    });
  });

  SC.on(document, 'click', '[data-reject]', function (ev, node) {
    var id = node.dataset.reject;
    SC.confirm({
      title: 'Reject this top-up?',
      message: 'No coins are added. The customer is notified with the reason you give, so ' +
        'write something they can act on.',
      confirmLabel: 'Reject request',
      danger: true,
      reason: true,
      reasonLabel: 'Reason (required — the customer sees this)',
      reasonPlaceholder: 'e.g. the screenshot amount does not match the package price.'
    }).then(function (answer) {
      if (!answer.ok) return;
      SC.guard(node, function () {
        return post('/api/seller/payments/' + encodeURIComponent(id) + '/reject',
          { reason: answer.reason })
          .then(function (data) {
            if (data.already_processed) SC.warn(data.message);
            else SC.ok(data.message);
            afterReview();
          })
          .catch(SC.fail);
      });
    });
  });

  /* ============================================================ live wiring
     Same contract as the Master console: a socket frame is only a hint that
     something changed. It marks the affected pane stale and lets the pane
     re-fetch from an endpoint that re-authorises this seller. No coin count,
     order status or pending total on this screen is ever patched from a socket
     payload, so a spoofed or replayed frame cannot change what is shown.       */
  var NOTE_VIEWS = {
    NEW_COIN_PAYMENT: 'payments',
    PAYMENT_CONFIRMED: 'payments',
    PAYMENT_REJECTED: 'payments',
    NEW_PRODUCT_ORDER: 'orders',
    PRODUCT_PURCHASED: 'orders',
    PRODUCT_DELIVERED: 'orders',
    DELIVERY_FAILED: 'orders',
    ORDER_COMPLETED: 'orders',
    ORDER_REFUNDED: 'orders'
  };

  function refresh(view) {
    if (!me || !loaded[view]) return;
    if (view === 'payments' && !canVerify) return;
    if (currentView() !== view && view !== 'overview') return;
    var loader = LOADERS[view];
    if (loader) loader();
  }

  SC.ws.on('notification', function (payload) {
    if (!me) return;
    var view = NOTE_VIEWS[payload && payload.kind];
    if (view) refresh(view);
    /* a new top-up has to reach the sidebar even while another pane is open —
       and only a seller a Master trusts with verification may ask for it */
    if (canVerify && payload && payload.kind === 'NEW_COIN_PAYMENT') {
      get('/api/seller/payments', { status: 'PENDING', limit: 1 }).then(function (data) {
        setPendingPill((data.counts && data.counts.PENDING) || data.total || 0);
      }).catch(function () { /* the pill is cosmetic; a failure changes nothing */ });
    }
    if (loaded.overview) loadOverview();
  });

  SC.ws.on('stats_dirty', function () {
    if (me && loaded.overview) loadOverview();
  });

  /* the handshake re-validates the session and the device binding, so a dropped
     connection is a reason to re-check who this browser still is */
  window.addEventListener('online', function () {
    if (me) boot(false);
  });
  /* ============================================================== bootstrap */
  boot(false);
})();
