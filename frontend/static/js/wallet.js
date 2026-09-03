/* ===========================================================================
   STREAM CORPORATION — viewer wallet.

   Three things this file deliberately does NOT do:
     1. it never computes a balance — the number comes from /api/wallet and from
        the server-pushed `wallet` websocket event;
     2. it never credits coins — uploading a screenshot only opens a PENDING
        request, exactly as the spec demands;
     3. it never decides a price — the package rows are server data and the
        payment request is validated again server-side.
   ======================================================================== */
(function () {
  'use strict';

  var SC = window.SC;
  var main = SC.$('#wallet-main');
  var gate = SC.$('#wallet-gate');
  if (!main || !gate) return;

  var cfg = { packages: [], methods: [], instructions: '', note: '', maxMb: 5 };
  var txn = { offset: 0, limit: 25, type: '', done: false };

  gate.querySelector('[data-signin]').addEventListener('click', function () {
    SC.login('Sign in to open your wallet.');
  });

  /* ------------------------------------------------------------------ balance */
  function paintWallet(data) {
    SC.$$('[data-coin-balance]').forEach(function (n) { n.textContent = SC.coins(data.balance); });
    SC.$('#w-credited').textContent = SC.coins(data.lifetime_credited);
    SC.$('#w-spent').textContent = SC.coins(data.lifetime_spent);

    var pending = data.pending_requests || [];
    SC.$('#w-pending').textContent = SC.n(pending.length);
    SC.$('#w-frozen-note').innerHTML = data.is_frozen
      ? '<span class="bad">This wallet is frozen — spending is disabled. Contact support.</span>'
      : 'Every coin movement below is a ledger row. Balances are never edited directly.';

    SC.$('#pending-list').innerHTML = pending.length
      ? pending.map(pendingRow).join('')
      : '<p class="muted" style="font-size:.88rem;margin:0">No top-up is waiting for review.</p>';
  }

  function pendingRow(r) {
    return '<div class="lrow">' +
      '<div><b class="mono">' + SC.esc(r.code) + '</b>' +
        '<div class="muted" style="font-size:.78rem">' + SC.esc(r.package_name || '') +
        ' · ' + SC.esc(r.method_name || '—') + ' · ' + SC.dt(r.created_at) + '</div></div>' +
      '<div class="right"><span class="coin">' + SC.coins(r.total_coins) + '</span>' +
        '<div class="muted mono" style="font-size:.78rem">' + SC.bdt(r.amount_bdt) + '</div></div>' +
      '<div>' + SC.statusBadge(r.status === 'PENDING' ? 'PENDING_VERIFICATION' : r.status) + '</div>' +
    '</div>';
  }

  /* ----------------------------------------------------------------- packages */
  function paintPackages() {
    var grid = SC.$('#pkg-grid');
    if (!cfg.packages.length) {
      grid.innerHTML = '<div class="empty" style="grid-column:1/-1"><div class="big">🪙</div>' +
        'No coin package is on sale right now.</div>';
      return;
    }
    grid.innerHTML = cfg.packages.map(function (p) {
      return '<div class="card corner" style="display:flex;flex-direction:column;gap:.5rem">' +
        (p.badge ? '<span class="badge badge-violet" style="align-self:flex-start">' +
          SC.esc(p.badge) + '</span>' : '') +
        '<b style="font-size:1rem">' + SC.esc(p.name) + '</b>' +
        '<span class="coin coin-lg">' + SC.coins(p.total_coins) + '</span>' +
        (p.bonus_coins > 0
          ? '<span class="muted" style="font-size:.78rem">' + SC.coins(p.coins) + ' + ' +
            SC.coins(p.bonus_coins) + ' bonus</span>'
          : '<span class="muted" style="font-size:.78rem">no bonus</span>') +
        '<div class="spread" style="margin-top:auto;padding-top:.6rem">' +
          '<b class="mono">' + SC.bdt(p.price_bdt) + '</b>' +
          '<button class="btn btn-primary btn-sm" type="button" data-buy-pkg="' +
            SC.esc(p.id) + '">Buy</button>' +
        '</div></div>';
    }).join('');
    SC.$('#pay-instructions').textContent = cfg.instructions || '';
  }

  SC.on(document, 'click', '[data-buy-pkg]', function (ev, node) {
    var id = node.dataset.buyPkg;
    var pkg = cfg.packages.filter(function (p) { return p.id === id; })[0];
    if (pkg) buyModal(pkg);
  });
  /* -------------------------------------------------------- buy coins popup */
  /* The popup shows the Master-configured method, number and amount. It sends a
     multipart request to /api/wallet/payment-request, which opens a PENDING row
     — no coin is credited here or anywhere else on the client. */
  function buyModal(pkg) {
    if (!cfg.methods.length) {
      SC.err('No payment method is configured yet. Please contact support.');
      return;
    }

    var options = cfg.methods.map(function (m, i) {
      return '<option value="' + SC.esc(m.id) + '"' + (i === 0 ? ' selected' : '') + '>' +
        SC.esc(m.name) + (m.account_type ? ' · ' + SC.esc(m.account_type) : '') + '</option>';
    }).join('');

    var html =
      '<dl class="kv">' +
        '<dt>Package</dt><dd>' + SC.esc(pkg.name) + '</dd>' +
        '<dt>You receive</dt><dd><span class="coin">' + SC.coins(pkg.total_coins) + '</span>' +
          (pkg.bonus_coins > 0 ? ' <span class="muted">(incl. ' + SC.coins(pkg.bonus_coins) +
            ' bonus)</span>' : '') + '</dd>' +
        '<dt>Amount to send</dt><dd><b class="mono">' + SC.bdt(pkg.price_bdt) + '</b></dd>' +
      '</dl>' +
      '<div class="divider"></div>' +
      '<div class="field"><label for="pay-method">Payment method</label>' +
        '<select id="pay-method">' + options + '</select></div>' +
      '<div class="card" id="method-box" style="padding:.9rem"></div>' +
      '<div class="field" style="margin-top:.95rem"><label for="pay-sender">Your sending number</label>' +
        '<input id="pay-sender" type="text" maxlength="80" inputmode="tel" ' +
        'placeholder="01XXXXXXXXX" autocomplete="tel"></div>' +
      '<div class="field"><label for="pay-ref">Transaction ID / reference</label>' +
        '<input id="pay-ref" type="text" maxlength="120" placeholder="e.g. 8FA2K19QLM"></div>' +
      '<div class="field"><label for="pay-note">Note (optional)</label>' +
        '<input id="pay-note" type="text" maxlength="1000" placeholder="Anything the reviewer should know"></div>' +
      '<div class="field"><label>Payment screenshot <span class="bad">*</span></label>' +
        '<label class="drop" id="pay-drop" for="pay-shot">' +
          '<div class="big">🖼</div>' +
          '<b id="pay-drop-label">Tap to choose your screenshot</b>' +
          '<span class="muted" style="font-size:.78rem">PNG, JPG or WEBP · up to ' +
            cfg.maxMb + ' MB</span>' +
          '<input id="pay-shot" type="file" accept="image/png,image/jpeg,image/webp">' +
        '</label>' +
        '<div class="shot-preview hidden" id="pay-preview"><img alt="Screenshot preview"></div></div>' +
      (cfg.note ? '<p class="hint">' + SC.esc(cfg.note) + '</p>' : '') +
      '<div class="divider"></div>' +
      '<p class="hint">Your coins are credited only after a Master or authorised Seller ' +
        'verifies this payment. Submitting never adds coins by itself.</p>' +
      '<button class="btn btn-primary btn-block btn-lg" type="button" data-submit style="margin-top:.7rem">' +
        'Submit for verification</button>';

    var modal = SC.modal(html, { title: 'Buy ' + SC.coins(pkg.total_coins) + ' coins', wide: true });
    var box = modal.content.querySelector('#method-box');
    var select = modal.content.querySelector('#pay-method');
    var file = modal.content.querySelector('#pay-shot');
    var drop = modal.content.querySelector('#pay-drop');
    var preview = modal.content.querySelector('#pay-preview');
    var dropLabel = modal.content.querySelector('#pay-drop-label');

    function showMethod() {
      var m = cfg.methods.filter(function (x) { return x.id === select.value; })[0] || {};
      box.innerHTML =
        '<div class="eyebrow" style="margin:0">SEND ' + SC.esc(SC.bdt(pkg.price_bdt)) + ' TO</div>' +
        '<div class="copy-row" style="margin-top:.35rem">' +
          '<span class="val" id="m-number">' + SC.esc(m.account_number || '—') + '</span>' +
          '<button class="btn btn-sm" type="button" data-copy="' + SC.esc(m.account_number || '') +
            '">Copy</button>' +
        '</div>' +
        (m.account_name ? '<div class="muted" style="font-size:.8rem;margin-top:.3rem">Account name: ' +
          SC.esc(m.account_name) + '</div>' : '') +
        (m.instructions ? '<p class="hint" style="margin-top:.5rem">' + SC.esc(m.instructions) +
          '</p>' : '');
    }
    select.addEventListener('change', showMethod);
    showMethod();

    box.addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-copy]');
      if (btn && btn.dataset.copy) SC.copy(btn.dataset.copy);
    });

    ['dragenter', 'dragover'].forEach(function (name) {
      drop.addEventListener(name, function (ev) { ev.preventDefault(); drop.classList.add('over'); });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      drop.addEventListener(name, function () { drop.classList.remove('over'); });
    });
    drop.addEventListener('drop', function (ev) {
      ev.preventDefault();
      if (ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0]) {
        file.files = ev.dataTransfer.files;
        onPick();
      }
    });

    function onPick() {
      var chosen = file.files && file.files[0];
      if (!chosen) return;
      /* a client-side ceiling only saves a doomed upload — the server enforces
         the real limit, the extension allowlist and the magic bytes. */
      if (chosen.size > cfg.maxMb * 1024 * 1024) {
        SC.err('That screenshot is ' + SC.bytes(chosen.size) + ' — the limit is ' + cfg.maxMb + ' MB.');
        file.value = '';
        return;
      }
      dropLabel.textContent = chosen.name + ' · ' + SC.bytes(chosen.size);
      preview.classList.remove('hidden');
      preview.querySelector('img').src = URL.createObjectURL(chosen);
    }
    file.addEventListener('change', onPick);

    modal.content.querySelector('[data-submit]').addEventListener('click', function (ev) {
      var button = ev.currentTarget;
      var chosen = file.files && file.files[0];
      if (!chosen) { SC.err('Attach the payment screenshot first.'); return; }

      var form = new FormData();
      form.append('package_id', pkg.id);
      form.append('method_id', select.value);
      form.append('sender_number', modal.content.querySelector('#pay-sender').value.trim());
      form.append('transaction_ref', modal.content.querySelector('#pay-ref').value.trim());
      form.append('note', modal.content.querySelector('#pay-note').value.trim());
      form.append('screenshot', chosen);

      SC.guard(button, function () {
        return SC.api('/api/wallet/payment-request', { method: 'POST', form: form })
          .then(function (data) {
            modal.close();
            submitted(data.request || {}, data.message);
            loadWallet();
          })
          .catch(SC.fail);
      });
    });
  }

  function submitted(request, message) {
    SC.modal(
      '<div class="center" style="margin-bottom:1rem">' +
        '<div style="font-size:2.4rem">⏳</div>' +
        '<b style="font-size:1.05rem">Payment submitted</b>' +
        '<p style="font-size:.88rem;margin-top:.4rem">' + SC.esc(message || '') + '</p></div>' +
      '<dl class="kv">' +
        '<dt>Request ID</dt><dd>' + SC.esc(request.code || '—') + '</dd>' +
        '<dt>Status</dt><dd>' + SC.statusBadge('PENDING_VERIFICATION') + '</dd>' +
        '<dt>Coins on approval</dt><dd><span class="coin">' + SC.coins(request.total_coins) + '</span></dd>' +
        '<dt>Amount</dt><dd class="mono">' + SC.bdt(request.amount_bdt) + '</dd>' +
      '</dl>' +
      '<p class="hint" style="margin-top:.8rem">A Master or authorised Seller has been notified. ' +
        'You will get a notification here the moment it is verified.</p>',
      { title: 'PENDING VERIFICATION' }
    );
  }
  /* ------------------------------------------------------------ ledger table */
  var TYPE_LABEL = {
    COIN_PURCHASE: 'Coin purchase',
    COIN_SPENT: 'Coin spent',
    COIN_REFUND: 'Coin refund',
    BONUS_COIN: 'Bonus coin',
    ADMIN_CREDIT: 'Admin credit',
    ADMIN_DEBIT: 'Admin debit'
  };

  function txnRow(t) {
    var amount = Number(t.amount || 0);
    var cls = amount < 0 ? 'bad' : 'ok';
    return '<tr>' +
      '<td class="mono">' + SC.esc(t.reference_code || t.id) + '</td>' +
      '<td class="mono" style="white-space:nowrap">' + SC.esc(SC.dt(t.created_at)) + '</td>' +
      '<td>' + SC.esc(TYPE_LABEL[t.type] || t.type) + '</td>' +
      '<td class="right ' + cls + '"><b class="mono">' + (amount > 0 ? '+' : '') +
        SC.n(amount) + '</b></td>' +
      '<td class="right mono">' + (t.bdt_amount === null || t.bdt_amount === undefined
        ? '—' : SC.esc(SC.bdt(t.bdt_amount))) + '</td>' +
      '<td>' + SC.esc(t.payment_method || '—') + '</td>' +
      '<td>' + SC.statusBadge(t.status) + '</td>' +
      '<td>' + SC.esc(t.approved_by || 'system') + '</td>' +
      '<td class="right mono">' + SC.n(t.balance_after) + '</td>' +
    '</tr>';
  }

  function loadTxns(append) {
    var body = SC.$('#txn-body');
    if (!append) {
      txn.offset = 0;
      txn.done = false;
      body.innerHTML = '<tr><td colspan="9"><div class="skel"></div></td></tr>';
    }
    return SC.get('/api/wallet/transactions', { limit: txn.limit, offset: txn.offset })
      .then(function (data) {
        var rows = (data.transactions || []).filter(function (t) {
          return !txn.type || t.type === txn.type;
        });
        var html = rows.map(txnRow).join('');
        if (append) body.insertAdjacentHTML('beforeend', html);
        else body.innerHTML = html || '<tr><td colspan="9"><div class="empty">' +
          '<div class="big">▤</div>No transaction yet.</div></td></tr>';

        var fetched = (data.transactions || []).length;
        txn.offset += fetched;
        txn.done = fetched < txn.limit;
        SC.$('#txn-more').classList.toggle('hidden', txn.done);
      })
      .catch(function (error) {
        body.innerHTML = '<tr><td colspan="9"><div class="empty"><div class="big">⚠</div>' +
          SC.esc(error.message) + '</div></td></tr>';
      });
  }

  SC.$('#txn-filter').addEventListener('change', function (ev) {
    txn.type = ev.currentTarget.value;
    loadTxns(false);
  });
  SC.$('#txn-more').addEventListener('click', function (ev) {
    SC.guard(ev.currentTarget, function () { return loadTxns(true); });
  });

  /* -------------------------------------------------------------- bootstrap */
  function loadWallet() {
    return SC.get('/api/wallet').then(paintWallet).catch(function (error) {
      if (error.status !== 401) SC.fail(error);
    });
  }

  function loadPackages() {
    return SC.get('/api/wallet/packages').then(function (data) {
      cfg.packages = data.packages || [];
      cfg.methods = data.methods || [];
      cfg.instructions = data.instructions || '';
      cfg.note = data.screenshot_note || '';
      cfg.maxMb = Number(data.max_screenshot_mb || 5);
      paintPackages();
    }).catch(function () {
      SC.$('#pkg-grid').innerHTML =
        '<div class="empty" style="grid-column:1/-1"><div class="big">⚠</div>' +
        'Coin packages could not be loaded.</div>';
    });
  }

  function show(authenticated) {
    gate.classList.toggle('hidden', authenticated);
    main.classList.toggle('hidden', !authenticated);
    if (!authenticated) return;
    loadWallet();
    loadPackages();
    loadTxns(false);
  }

  /* the server pushes every confirmed top-up, so a verification landing while
     this tab is open repaints the balance and the ledger without a reload. */
  document.addEventListener('sc:wallet', function () { loadWallet(); loadTxns(false); });

  var applied = false;
  document.addEventListener('sc:session', function (ev) {
    applied = true;
    show(!!(ev.detail && ev.detail.authenticated));
  });
  if (SC.session && SC.session.ready) {
    SC.session.ready.then(function (s) { if (!applied) show(!!s.authenticated); });
  }
})();
