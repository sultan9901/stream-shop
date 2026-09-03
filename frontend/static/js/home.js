/* ===========================================================================
   STREAM CORPORATION — marketplace home.
   Category filter, search, sort and paging against /api/products.
   ======================================================================== */
(function () {
  'use strict';

  var SC = window.SC;
  var grid = SC.$('#product-grid');
  if (!grid) return;

  var more = SC.$('#load-more');
  var state = { category: '', q: '', sort: 'featured', offset: 0, limit: 12, total: 0 };

  function card(p) {
    var media = p.thumbnail_url
      ? '<img data-src="' + SC.esc(p.thumbnail_url) + '" alt="' + SC.esc(p.name) +
        '" width="480" height="300">'
      : '<div class="ph">▣</div>';

    var flags = '';
    if (p.owned) flags += '<span class="badge badge-ok">Owned</span>';
    else if (p.is_featured) flags += '<span class="badge badge-violet">Featured</span>';
    if (!p.in_stock) flags += '<span class="badge badge-bad">Sold out</span>';

    return '<a class="card pcard" href="/product/' + SC.esc(p.slug) + '">' +
      '<div class="pcard-media">' + media +
        '<div class="pcard-badges">' + flags + '</div>' +
      '</div>' +
      '<div class="pcard-body">' +
        '<h3>' + SC.esc(p.name) + '</h3>' +
        '<p class="tag">' + SC.esc(p.tagline || '') + '</p>' +
        '<div class="pcard-foot">' +
          '<span class="coin">' + SC.coins(p.coin_price) + '</span>' +
          '<span class="badge">' + SC.esc(p.category ? p.category.name : 'General') + '</span>' +
        '</div>' +
      '</div></a>';
  }

  function load(append) {
    if (!append) { state.offset = 0; grid.innerHTML = '<div class="skel" style="height:250px"></div>'; }
    return SC.get('/api/products', {
      category: state.category,
      q: state.q,
      sort: state.sort,
      limit: state.limit,
      offset: state.offset
    }).then(function (data) {
      state.total = data.total || 0;
      var html = (data.products || []).map(card).join('');
      if (append) grid.insertAdjacentHTML('beforeend', html);
      else grid.innerHTML = html || '<div class="empty" style="grid-column:1/-1">' +
        '<div class="big">▤</div>No software matches that filter yet.</div>';

      state.offset += (data.products || []).length;
      if (more) more.classList.toggle('hidden', state.offset >= state.total);

      var counter = SC.$('#stat-products');
      if (counter && !state.category && !state.q) counter.textContent = SC.n(state.total);
      SC.lazy(grid);
    }).catch(function (error) {
      grid.innerHTML = '<div class="empty" style="grid-column:1/-1"><div class="big">⚠</div>' +
        SC.esc(error.message) + '</div>';
    });
  }

  SC.$$('#cat-strip .cat-chip').forEach(function (chip) {
    chip.addEventListener('click', function () {
      SC.$$('#cat-strip .cat-chip').forEach(function (c) { c.classList.remove('active'); });
      chip.classList.add('active');
      state.category = chip.dataset.cat || '';
      load(false);
    });
  });

  var search = SC.$('#q');
  if (search) {
    search.addEventListener('input', SC.debounce(function () {
      state.q = search.value.trim();
      load(false);
    }, 320));
  }

  var sort = SC.$('#sort');
  if (sort) {
    sort.addEventListener('change', function () {
      state.sort = sort.value;
      load(false);
    });
  }

  if (more) more.addEventListener('click', function () { SC.guard(more, function () { return load(true); }); });

  /* a fresh sign-in changes `owned`, so repaint once — but skip the very first
     session resolution, which races the initial load below. */
  var firstSession = true;
  document.addEventListener('sc:session', function () {
    if (firstSession) { firstSession = false; return; }
    load(false);
  });
  load(false);
})();

