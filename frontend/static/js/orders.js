/* ===========================================================================
   STREAM CORPORATION — viewer order history.

   Every row here is server state. The download button does not build a URL: it
   asks the server to mint a fresh expiring grant, which the server ties to the
   order, the caller and a download ceiling.
   ======================================================================== */
(function () {
  'use strict';

  var SC = window.SC;
  var main = SC.$('#orders-main');
  var gate = SC.$('#orders-gate');
  if (!main || !gate) return;

  var state = { status: '', offset: 0, limit: 20, total: 0 };

  gate.querySelector('[data-signin]').addEventListener('click', function () {
    SC.login('Sign in to see your orders.');
  });

  function deliveryCell(order) {
    var d = order.delivery || {};
    if (!d.status) return '<span class="muted">—</span>';
    var extra = d.sent_at ? '<div class="muted" style="font-size:.72rem">' + SC.esc(SC.rel(d.sent_at)) + '</div>' : '';
    return SC.statusBadge(d.status) + extra;
  }

  function row(order) {
    var product = order.product || {};
    var refunded = order.status === 'REFUNDED' || order.status === 'CANCELLED';
    return '<tr>' +
      '<td class="mono">' + SC.esc(order.order_code) + '</td>' +
      '<td><b style="font-size:.88rem">' + SC.esc(product.name || 'Removed product') + '</b>' +
        (product.version ? '<div class="muted" style="font-size:.72rem">v' +
          SC.esc(product.version) + '</div>' : '') + '</td>' +
      '<td class="right"><span class="coin">' + SC.coins(order.coin_total) + '</span></td>' +
      '<td>' + SC.statusBadge(order.status) + '</td>' +
      '<td>' + deliveryCell(order) + '</td>' +
      '<td class="mono" style="white-space:nowrap">' + SC.esc(SC.dt(order.created_at)) + '</td>' +
      '<td class="right"><div class="row-wrap" style="justify-content:flex-end">' +
        '<button class="btn btn-sm btn-ghost" type="button" data-view="' + SC.esc(order.id) + '">Details</button>' +
        (refunded ? '' : '<button class="btn btn-sm btn-primary" type="button" data-dl="' +
          SC.esc(order.id) + '">Download</button>') +
      '</div></td>' +
    '</tr>';
  }

  function load(append) {
    var body = SC.$('#order-body');
    if (!append) {
      state.offset = 0;
      body.innerHTML = '<tr><td colspan="7"><div class="skel"></div></td></tr>';
    }
    return SC.get('/api/orders', {
      status: state.status,
      limit: state.limit,
      offset: state.offset
    }).then(function (data) {
      state.total = data.total || 0;
      var list = data.orders || [];
      var html = list.map(row).join('');
      if (append) body.insertAdjacentHTML('beforeend', html);
      else body.innerHTML = html || '<tr><td colspan="7"><div class="empty">' +
        '<div class="big">▤</div>No order here yet. ' +
        '<a href="/">Browse the marketplace</a>.</div></td></tr>';

      state.offset += list.length;
      SC.$('#order-more').classList.toggle('hidden', state.offset >= state.total);

      var counts = data.counts || {};
      var all = 0;
      Object.keys(counts).forEach(function (key) { all += counts[key]; });
      SC.$$('#order-tabs [data-count]').forEach(function (node) {
        var key = node.dataset.count;
        node.textContent = SC.n(key ? (counts[key] || 0) : all);
      });
    }).catch(function (error) {
      if (error.status === 401) { show(false); return; }
      body.innerHTML = '<tr><td colspan="7"><div class="empty"><div class="big">⚠</div>' +
        SC.esc(error.message) + '</div></td></tr>';
    });
  }
  /* ---------------------------------------------------------- order details */
  function detailModal(id) {
    var modal = SC.modal('<div class="skel" style="height:180px"></div>', { title: 'Order' });
    SC.get('/api/orders/' + encodeURIComponent(id)).then(function (order) {
      var product = order.product || {};
      var delivery = order.delivery || {};
      var refunded = order.status === 'REFUNDED' || order.status === 'CANCELLED';

      modal.content.innerHTML =
        '<div class="row" style="gap:.9rem;margin-bottom:1rem">' +
          (product.thumbnail_url
            ? '<img class="thumb" style="width:76px;height:58px;border-radius:10px;object-fit:cover" src="' +
              SC.esc(product.thumbnail_url) + '" alt="">'
            : '') +
          '<div><b style="font-size:1rem">' + SC.esc(product.name || 'Removed product') + '</b>' +
          '<div class="mono muted" style="font-size:.78rem">' + SC.esc(order.order_code) + '</div></div>' +
        '</div>' +
        '<dl class="kv">' +
          '<dt>Status</dt><dd>' + SC.statusBadge(order.status) + '</dd>' +
          '<dt>Coins paid</dt><dd><span class="coin">' + SC.coins(order.coin_total) + '</span></dd>' +
          '<dt>Seller</dt><dd>' + SC.esc((order.seller && order.seller.label) || 'STREAM CORPORATION') + '</dd>' +
          '<dt>Delivery</dt><dd>' + (delivery.status ? SC.statusBadge(delivery.status) : '—') + '</dd>' +
          '<dt>Sent to</dt><dd>' + SC.esc(delivery.email_to || '—') + '</dd>' +
          '<dt>Sent at</dt><dd>' + SC.esc(delivery.sent_at ? SC.dt(delivery.sent_at) : 'not yet') + '</dd>' +
          '<dt>Ordered</dt><dd>' + SC.esc(SC.dt(order.created_at)) + '</dd>' +
          (order.completed_at ? '<dt>Completed</dt><dd>' + SC.esc(SC.dt(order.completed_at)) + '</dd>' : '') +
          (order.refunded_at ? '<dt>Refunded</dt><dd>' + SC.esc(SC.dt(order.refunded_at)) + '</dd>' : '') +
          (order.refund_reason ? '<dt>Refund reason</dt><dd>' + SC.esc(order.refund_reason) + '</dd>' : '') +
          (order.seller_note ? '<dt>Seller note</dt><dd>' + SC.esc(order.seller_note) + '</dd>' : '') +
        '</dl>' +
        (delivery.error
          ? '<p class="bad" style="font-size:.84rem;margin-top:.7rem">Delivery problem: ' +
            SC.esc(delivery.error) + '</p>'
          : '') +
        (product.delivery_note
          ? '<div class="divider"></div><div class="muted" style="white-space:pre-wrap;font-size:.86rem">' +
            SC.esc(product.delivery_note) + '</div>'
          : '') +
        '<div class="divider"></div>' +
        '<div class="row-wrap" style="justify-content:flex-end">' +
          (product.slug ? '<a class="btn btn-sm btn-ghost" href="/product/' + SC.esc(product.slug) +
            '">Product page</a>' : '') +
          (refunded ? '' :
            '<button class="btn btn-sm" type="button" data-resend>Re-send email</button>' +
            '<button class="btn btn-sm btn-primary" type="button" data-dl="' + SC.esc(order.id) +
            '">Get download link</button>') +
        '</div>';

      var resend = modal.content.querySelector('[data-resend]');
      if (resend) {
        resend.addEventListener('click', function () {
          SC.guard(resend, function () {
            return SC.post('/api/orders/' + encodeURIComponent(order.id) + '/resend-email')
              .then(function (data) { SC.ok(data.message); })
              .catch(SC.fail);
          });
        });
      }
    }).catch(function (error) {
      modal.content.innerHTML = '<div class="empty"><div class="big">⚠</div>' +
        SC.esc(error.message) + '</div>';
    });
  }

  /* -------------------------------------------------------------- downloads */
  function download(button, id) {
    SC.guard(button, function () {
      return SC.post('/api/orders/' + encodeURIComponent(id) + '/download-link')
        .then(function (data) {
          SC.ok('Link ready — it allows ' + data.max_downloads + ' downloads and expires ' +
            SC.dt(data.expires_at) + '.');
          window.location.href = data.download_url;
        })
        .catch(SC.fail);
    });
  }

  SC.on(document, 'click', '[data-dl]', function (ev, node) { download(node, node.dataset.dl); });
  SC.on(document, 'click', '[data-view]', function (ev, node) { detailModal(node.dataset.view); });

  SC.$$('#order-tabs .tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      SC.$$('#order-tabs .tab').forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      state.status = tab.dataset.status || '';
      load(false);
    });
  });

  SC.$('#order-more').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () { return load(true); });
  });

  /* -------------------------------------------------------------- bootstrap */
  function show(authenticated) {
    gate.classList.toggle('hidden', authenticated);
    main.classList.toggle('hidden', !authenticated);
    if (authenticated) load(false);
  }

  /* a delivery finishing or a refund landing arrives as a notification, so
     refresh the list when the server says something changed. */
  SC.ws.on('notification', function (payload) {
    var kind = payload && payload.kind;
    if (kind === 'PRODUCT_DELIVERED' || kind === 'DELIVERY_FAILED' ||
        kind === 'ORDER_COMPLETED' || kind === 'ORDER_REFUNDED') {
      if (!main.classList.contains('hidden')) load(false);
    }
  });

  var applied = false;
  document.addEventListener('sc:session', function (ev) {
    applied = true;
    show(!!(ev.detail && ev.detail.authenticated));
  });
  if (SC.session && SC.session.ready) {
    SC.session.ready.then(function (s) { if (!applied) show(!!s.authenticated); });
  }
})();
