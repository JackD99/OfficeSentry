/* Office Sentry website: docs search, the docs menu on phones and the "On this page" highlight. No dependencies. */
(function () {
  'use strict';

  // The docs menu starts open for laptops; on a phone it starts closed above the page.
  var menu = document.querySelector('.docs-menu');
  if (menu && window.matchMedia('(max-width: 800px)').matches) menu.open = false;

  // The header menu on phones closes when a link in it is followed.
  var headerMenu = document.querySelector('.menu');
  if (headerMenu) headerMenu.addEventListener('click', function (e) {
    if (e.target.closest('a')) headerMenu.open = false;
  });

  // Highlight the heading being read in "On this page".
  var tocLinks = Array.prototype.slice.call(document.querySelectorAll('.toc a'));
  if (tocLinks.length && 'IntersectionObserver' in window) {
    var byId = {};
    tocLinks.forEach(function (a) { byId[decodeURIComponent(a.hash.slice(1))] = a; });
    var visible = {};
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { visible[e.target.id] = e.isIntersecting; });
      var first = Object.keys(byId).find(function (id) { return visible[id]; });
      if (!first) return;
      tocLinks.forEach(function (a) { a.classList.remove('active'); });
      byId[first].classList.add('active');
    }, { rootMargin: '-80px 0px -60% 0px' });
    Object.keys(byId).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) observer.observe(el);
    });
  }

  // Search: the index (every page cut at its headings) loads the first time the box is used.
  var box = document.querySelector('[data-search]');
  if (!box) return;
  var input = box.querySelector('input');
  var list = box.querySelector('.search-results');
  var index = null, loading = null, active = -1;

  function load() {
    if (!loading) {
      loading = fetch('/assets/search.json').then(function (r) { return r.json(); })
        .then(function (data) {
          index = data.map(function (e) {
            return { e: e, t: e.t.toLowerCase(), h: e.h.toLowerCase(), x: e.x.toLowerCase() };
          });
        });
    }
    return loading;
  }

  function esc(s) {
    return s.replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  }

  function mark(text, words) {
    var out = esc(text);
    words.forEach(function (w) {
      out = out.replace(new RegExp('(' + w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi'), '<mark>$1</mark>');
    });
    return out;
  }

  function snippet(text, words) {
    var lower = text.toLowerCase(), at = lower.indexOf(words[0]);
    var start = Math.max(0, at - 40);
    var cut = text.slice(start, start + 140);
    return (start > 0 ? '…' : '') + cut + (start + 140 < text.length ? '…' : '');
  }

  function search(query) {
    var words = query.toLowerCase().split(/\s+/).filter(function (w) { return w.length > 1; });
    if (!words.length) return [];
    var hits = [];
    index.forEach(function (item) {
      var score = 0;
      for (var i = 0; i < words.length; i++) {
        var w = words[i], s = 0;
        if (item.h.indexOf(w) !== -1) s += 10;
        if (item.t.indexOf(w) !== -1) s += 4;
        if (item.x.indexOf(w) !== -1) s += 1 + Math.min(3, item.x.split(w).length - 2);
        if (!s) return;
        score += s;
      }
      hits.push({ item: item.e, score: score });
    });
    hits.sort(function (a, b) { return b.score - a.score; });
    return hits.slice(0, 12).map(function (h) { return { e: h.item, words: words }; });
  }

  function show() {
    var q = input.value.trim();
    if (!q) { list.hidden = true; return; }
    var results = search(q);
    active = -1;
    list.innerHTML = results.length ? results.map(function (r) {
      var e = r.e;
      return '<li><a href="' + esc(e.u) + '">' + (e.h ? mark(e.h, r.words) + '<small>' + esc(e.t) + '</small>' : mark(e.t, r.words)) +
        (e.x ? '<span class="snippet">' + mark(snippet(e.x, r.words), r.words) + '</span>' : '') + '</a></li>';
    }).join('') : '<li class="none">Nothing found for “' + esc(q) + '”.</li>';
    list.hidden = false;
  }

  input.addEventListener('focus', load);
  input.addEventListener('input', function () { load().then(show); });
  input.addEventListener('keydown', function (e) {
    var links = list.querySelectorAll('a');
    if (e.key === 'Escape') { list.hidden = true; input.blur(); return; }
    if (!links.length || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Enter')) return;
    e.preventDefault();
    if (e.key === 'Enter') { (links[Math.max(active, 0)]).click(); return; }
    active = (active + (e.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length;
    links.forEach(function (a, i) { a.classList.toggle('active', i === active); });
    links[active].scrollIntoView({ block: 'nearest' });
  });
  document.addEventListener('click', function (e) { if (!box.contains(e.target)) list.hidden = true; });
  // "/" jumps to the search box, as on GitHub.
  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && document.activeElement === document.body) { e.preventDefault(); input.focus(); }
  });
}());
