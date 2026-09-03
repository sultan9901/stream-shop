/* ===========================================================================
   STREAM CORPORATION — viewer shell.
   Resolves the signed-in viewer, paints the header chip / avatar / bell, and
   owns the Google sign-in modal. Loaded on every public page.
   ======================================================================== */
(function () {
  'use strict';

  var SC = window.SC;
  var CFG = window.SC_CONFIG || {};

  var session = (SC.session = {
    authenticated: false,
    user: null,
    ready: null
  });

  /* ---------------------------------------------------------- sign-in modal */
  function loginModal(message) {
    var google =
      '<a class="btn btn-primary btn-block btn-lg" href="/auth/google/start?next=' +
      encodeURIComponent(window.location.pathname) + '">' +
      '<span style="font-weight:800">G</span> Continue with Google</a>';

    var stub =
      '<div class="divider"></div>' +
      '<div class="eyebrow">DEVELOPMENT SIGN-IN</div>' +
      '<p style="font-size:.84rem">Google keys are not configured, so this local stub creates a ' +
      'real viewer account from any email address.</p>' +
      '<div class="field"><label for="dev-email">Email address</label>' +
      '<input id="dev-email" type="email" placeholder="you@example.com" autocomplete="email"></div>' +
      '<div class="field"><label for="dev-name">Display name (optional)</label>' +
      '<input id="dev-name" type="text" maxlength="160" placeholder="Your name"></div>' +
      '<button class="btn btn-block" type="button" data-dev-login>Sign in locally</button>';

    var html =
      (message ? '<p class="warn">' + SC.esc(message) + '</p>' : '') +
      '<p>Sign in with your Google account. Your purchases are delivered to that ' +
      'Gmail address, so it must be the inbox you actually use.</p>' +
      (CFG.googleEnabled ? google : '') +
      (!CFG.googleEnabled && !CFG.devStub
        ? '<p class="bad">Google sign-in is not configured on this server yet.</p>' : '') +
      (CFG.devStub ? stub : '');

    var modal = SC.modal(html, { title: 'Sign in' });
    var devBtn = modal.content.querySelector('[data-dev-login]');
    if (devBtn) {
      devBtn.addEventListener('click', function () {
        var email = (modal.content.querySelector('#dev-email').value || '').trim();
        var name = (modal.content.querySelector('#dev-name').value || '').trim();
        if (email.indexOf('@') === -1) { SC.err('Enter a valid email address.'); return; }
        SC.guard(devBtn, function () {
          return SC.post('/auth/google/dev-login', { email: email, name: name || null })
            .then(function () { window.location.reload(); })
            .catch(SC.fail);
        });
      });
    }
    return modal;
  }

  SC.login = loginModal;

  /* Ask for a session before a protected action; returns true when signed in. */
  SC.requireLogin = function (message) {
    if (session.authenticated) return true;
    loginModal(message || 'You need to sign in first.');
    return false;
  };

  /* ------------------------------------------------------------- header paint */
  function paint() {
    var host = SC.$('#sc-user');
    var chip = SC.$('#sc-coin-chip');
    var bell = SC.$('#sc-bell');
    if (!host) return;

    if (!session.authenticated) {
      if (chip) chip.classList.add('hidden');
      if (bell) bell.classList.add('hidden');
      host.innerHTML = '<button class="btn btn-primary btn-sm" type="button" data-login>Sign in</button>';
      host.querySelector('[data-login]').addEventListener('click', function () { loginModal(); });
      return;
    }

    var user = session.user || {};
    if (chip) {
      chip.classList.remove('hidden');
      chip.querySelector('[data-coin-balance]').textContent = SC.coins(user.coin_balance || 0);
    }
    if (bell) bell.classList.remove('hidden');

    var avatar = user.avatar
      ? '<img class="avatar" src="' + SC.esc(user.avatar) + '" alt="" referrerpolicy="no-referrer">'
      : '<span class="avatar" style="display:grid;place-items:center;font-weight:800">' +
        SC.esc((user.name || user.email || '?').slice(0, 1).toUpperCase()) + '</span>';

    host.innerHTML =
      '<button class="btn btn-ghost btn-sm" type="button" data-profile style="gap:.5rem">' +
      avatar + '</button>';
    host.querySelector('[data-profile]').addEventListener('click', profileModal);
  }

  /* ------------------------------------------------------------- profile card */
  function profileModal() {
    var user = session.user || {};
    var html =
      '<div class="row" style="gap:.9rem;margin-bottom:1rem">' +
        (user.avatar
          ? '<img class="avatar" style="width:56px;height:56px" src="' + SC.esc(user.avatar) +
            '" alt="" referrerpolicy="no-referrer">'
          : '') +
        '<div><b style="font-size:1.05rem">' + SC.esc(user.name || 'Viewer') + '</b>' +
        '<div class="mono muted" style="font-size:.8rem">' + SC.esc(user.email || '') + '</div></div>' +
      '</div>' +
      '<dl class="kv">' +
        '<dt>Customer ID</dt><dd>' + SC.esc(user.code || '—') + '</dd>' +
        '<dt>User ID</dt><dd>' + SC.esc(user.id || '—') + '</dd>' +
        '<dt>Coin balance</dt><dd><span class="coin">' + SC.coins(user.coin_balance || 0) + '</span></dd>' +
        '<dt>Member since</dt><dd>' + SC.esc(SC.dt(user.created_at)) + '</dd>' +
        '<dt>Last sign-in</dt><dd>' + SC.esc(SC.dt(user.last_login_at)) + '</dd>' +
      '</dl>' +
      '<div class="divider"></div>' +
      '<div class="row-wrap" style="justify-content:space-between">' +
        '<a class="btn btn-sm" href="/wallet">Wallet</a>' +
        '<a class="btn btn-sm" href="/orders">My orders</a>' +
        '<button class="btn btn-sm btn-danger" type="button" data-logout>Sign out</button>' +
      '</div>';

    var modal = SC.modal(html, { title: 'My account' });
    modal.content.querySelector('[data-logout]').addEventListener('click', function (ev) {
      SC.guard(ev.currentTarget, function () {
        return SC.post('/api/auth/viewer/logout')
          .then(function () { window.location.href = '/'; })
          .catch(SC.fail);
      });
    });
  }

  /* --------------------------------------------------------------- bootstrap */
  function refresh() {
    session.ready = SC.get('/api/auth/viewer/me').then(function (data) {
      session.authenticated = !!(data && data.authenticated);
      session.user = (data && data.user) || null;
      paint();
      document.dispatchEvent(new CustomEvent('sc:session', { detail: session }));
      return session;
    }).catch(function () {
      session.authenticated = false;
      session.user = null;
      paint();
      document.dispatchEvent(new CustomEvent('sc:session', { detail: session }));
      return session;
    });
    return session.ready;
  }

  session.refresh = refresh;

  /* the balance chip follows the server-pushed wallet events */
  document.addEventListener('sc:wallet', function (ev) {
    if (session.user && ev.detail) session.user.coin_balance = ev.detail.balance;
  });

  refresh();

  /* ?dev_login=1 comes back from /auth/google/start when no keys are set */
  if (new URLSearchParams(window.location.search).get('dev_login')) {
    history.replaceState({}, '', window.location.pathname);
    setTimeout(function () { loginModal('Google is not configured — use the local sign-in below.'); }, 250);
  }
})();
