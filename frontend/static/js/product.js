/* ===========================================================================
   STREAM CORPORATION — product detail + purchase.

   The button state comes from the server (`affordability` on
   /api/products/{slug}); this file never computes a balance or a price. The
   purchase POST carries an idempotency key so a double-click, a retry or a
   flaky connection can only ever create one order.
   ======================================================================== */
(function () {
  'use strict';

  var SC = window.SC;
  var zone = SC.$('#buy-zone');
  if (!zone) return;

  var seed = {};
  try { seed = JSON.parse(SC.$('#product-data').textContent) || {}; } catch (e) { seed = {}; }

  var product = seed;
  var idemKey = null;

  SC.$$('[data-date]').forEach(function (node) {
    node.textContent = SC.dt(node.dataset.date);
  });

  /* image lightbox for the gallery */
  SC.on(document, 'click', '[data-shot]', function (ev, node) {
    SC.modal('<div class="shot-frame"><img src="' + SC.esc(node.dataset.shot) + '" alt=""></div>',
      { title: product.name || 'Preview', wide: true });
  });

  function newKey() {
    var bytes = new Uint8Array(16);
    (window.crypto || window.msCrypto).getRandomValues(bytes);
    return 'buy-' + Array.prototype.map.call(bytes, function (b) {
      return ('0' + b.toString(16)).slice(-2);
    }).join('');
  }

  /* ------------------------------------------------------------- rendering */
  function paint(info) {
    var afford = info.affordability || {};
    if (info.owned && !info.allow_repurchase) {
      zone.innerHTML =
        '<div class="badge badge-ok" style="width:100%;justify-content:center;padding:.6rem">' +
        'You already own this</div>' +
        '<a class="btn btn-block" style="margin-top:.6rem" href="/orders">Open my orders</a>';
      return;
    }
    if (!info.in_stock) {
      zone.innerHTML = '<button class="btn btn-block btn-lg" disabled>Sold out</button>';
      return;
    }
    if (!afford.authenticated) {
      zone.innerHTML =
        '<button class="btn btn-primary btn-block btn-lg" type="button" data-signin>Sign in to buy</button>' +
        '<p class="hint center" style="margin-top:.5rem">Delivered to your Gmail address.</p>';
      zone.querySelector('[data-signin]').addEventListener('click', function () {
        SC.login('Sign in to buy ' + (info.name || 'this product') + '.');
      });
      return;
    }

    var balance = Number(afford.balance || 0);
    var required = Number(afford.required || info.coin_price || 0);
    var shortfall = Math.max(required - balance, 0);

    zone.innerHTML =
      '<div class="spread" style="font-size:.84rem;margin-bottom:.6rem">' +
        '<span class="muted">Your balance</span>' +
        '<span class="coin" data-coin-balance>' + SC.coins(balance) + '</span>' +
      '</div>' +
      (shortfall > 0
        ? '<div class="badge badge-warn" style="width:100%;justify-content:center;padding:.55rem;margin-bottom:.6rem">' +
          'You need ' + SC.coins(shortfall) + ' more coins</div>' +
          '<a class="btn btn-primary btn-block btn-lg" href="/wallet">Buy coins</a>'
        : '<button class="btn btn-primary btn-block btn-lg" type="button" data-buy>' +
          'Buy for ' + SC.coins(required) + ' coins</button>') +
      '<p class="hint center" style="margin-top:.5rem">Coins are deducted server-side. ' +
      'One click = one order.</p>';

    var buy = zone.querySelector('[data-buy]');
    if (buy) buy.addEventListener('click', function () { purchase(buy, info); });
  }

  /* ------------------------------------------------------------- purchasing */
  function purchase(button, info) {
    if (!idemKey) idemKey = newKey();
    SC.guard(button, function () {
      return SC.post('/api/orders/purchase', {
        product_id: info.id,
        idempotency_key: idemKey
      }).then(function (data) {
        idemKey = null;
        SC.$$('[data-coin-balance]').forEach(function (n) { n.textContent = SC.coins(data.balance); });
        success(data.order, data.message);
        refresh();
      }).catch(function (error) {
        if (error.status === 402) { shortfallScreen(error.detail || {}); return; }
        if (error.status === 409 && error.detail && error.detail.order_code) {
          SC.warn(error.message + ' (' + error.detail.order_code + ')');
          refresh();
          return;
        }
        SC.fail(error);
      });
    });
  }

  function success(order, message) {
    order = order || {};
    var html =
      '<div class="center" style="margin-bottom:1rem">' +
        '<div style="font-size:2.4rem">✅</div>' +
        '<b style="font-size:1.05rem">Purchase successful</b>' +
        '<p style="font-size:.88rem;margin-top:.4rem">' + SC.esc(message || '') + '</p>' +
      '</div>' +
      '<dl class="kv">' +
        '<dt>Order ID</dt><dd>' + SC.esc(order.order_code || '—') + '</dd>' +
        '<dt>Status</dt><dd>' + SC.statusBadge(order.status) + '</dd>' +
        '<dt>Coins spent</dt><dd><span class="coin">' + SC.coins(order.coin_total) + '</span></dd>' +
        '<dt>Sent to</dt><dd>' + SC.esc((order.delivery && order.delivery.email_to) || 'your Gmail') + '</dd>' +
      '</dl>' +
      '<div class="divider"></div>' +
      '<div class="row-wrap" style="justify-content:flex-end">' +
        '<a class="btn btn-sm" href="/orders">My orders</a>' +
        '<button class="btn btn-sm btn-primary" type="button" data-link>Get download link</button>' +
      '</div>';

    var modal = SC.modal(html, { title: 'Order created' });
    modal.content.querySelector('[data-link]').addEventListener('click', function (ev) {
      var button = ev.currentTarget;
      SC.guard(button, function () {
        return SC.post('/api/orders/' + encodeURIComponent(order.id) + '/download-link')
          .then(function (data) {
            window.location.href = data.download_url;
            SC.ok('Download starting — the link expires ' + SC.rel(data.expires_at).replace(' ago', '') + ' from now.');
          })
          .catch(SC.fail);
      });
    });
  }

  function shortfallScreen(detail) {
    var html =
      '<div class="center" style="margin-bottom:1rem">' +
        '<div style="font-size:2.4rem">🪙</div>' +
        '<b style="font-size:1.05rem">Not enough coins</b></div>' +
      '<dl class="kv">' +
        '<dt>Required</dt><dd><span class="coin">' + SC.coins(detail.required) + '</span></dd>' +
        '<dt>Your balance</dt><dd><span class="coin">' + SC.coins(detail.balance) + '</span></dd>' +
        (detail.shortfall
          ? '<dt>Still needed</dt><dd><span class="coin">' + SC.coins(detail.shortfall) + '</span></dd>'
          : '') +
      '</dl>' +
      '<p class="hint" style="margin-top:.8rem">' +
        SC.esc(detail.message || 'Top up your wallet, then come back — the price is locked server-side.') +
      '</p>' +
      '<div class="divider"></div>' +
      '<a class="btn btn-primary btn-block btn-lg" href="/wallet">Buy coins</a>';

    SC.modal(html, { title: 'Not enough coins' });
  }

  /* ---------------------------------------------------------------- refresh */
  /* `owned`, `in_stock` and `affordability` are all server truths, so after any
     state change we re-read the product instead of patching the DOM by hand. */
  function refresh() {
    var ident = product.slug || product.id || seed.slug;
    return SC.get('/api/products/' + encodeURIComponent(ident))
      .then(function (data) {
        product = data || product;
        paint(product);
        return product;
      })
      .catch(function (error) {
        zone.innerHTML =
          '<div class="badge badge-bad" style="width:100%;justify-content:center;padding:.6rem">' +
          SC.esc(error.message) + '</div>';
      });
  }

  /* a sign-in or sign-out changes the balance and `owned`, so repaint — but the
     first session event races the initial refresh below, so skip it. */
  var firstSession = true;
  document.addEventListener('sc:session', function () {
    if (firstSession) { firstSession = false; return; }
    refresh();
  });

  /* the server pushes wallet movements; a top-up can turn "Buy coins" into
     "Buy for N coins" without a reload. */
  document.addEventListener('sc:wallet', function () { refresh(); });

  refresh();
})();
