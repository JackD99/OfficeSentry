/* Tables, report search and in-place views. No dependencies.
 * Sorting: <table class="sortable"> with <button class="sort"> in each <th>; cells may carry data-sort.
 * Searching rows: <input data-filter="table-id"> matches each row's data-name.
 * Report pickers: <form data-autosubmit> submits when its <select> changes.
 * Report search: <input data-report-search="list-id"> filters the list's [data-name] items, hiding empty
 *   .report-group blocks; a group marked data-search-only shows only while searching.
 * Row groups: <button data-group> in a <tbody class="group"> heading folds that group's rows;
 *   <button data-groups="table-id"> folds or opens every group in the table.
 * Views: <a data-view> links (filters, columns, sort on detail reports) load the next page in place,
 *   keeping the scroll position, open menus and anything typed in a row search. Plain links without JS. */
(function () {
  /* Every page loads this file (base.html); a page that also loads it gets one copy.
   * Long tables: a stacked table (table.stack-sm) shows its first rows, with "Show all" under it (capRows). */
  if (window.officeSentryTables) return;
  window.officeSentryTables = true;
  var CAP = 20, FEW = 5;
  var phone = window.matchMedia ? window.matchMedia('(max-width: 640px)') : { matches: false };
  function value(cell) {
    if (!cell) return null;
    var raw = cell.hasAttribute('data-sort') ? cell.getAttribute('data-sort') : cell.textContent.trim();
    if (raw === '' || raw === '–') return null;
    var n = Number(raw);
    return isNaN(n) ? raw.toLowerCase() : n;
  }
  function cellAt(row, index) {
    // Rows whose data cells are merged (no data for the report) sort last.
    var cells = row.cells;
    if (cells.length !== row.parentNode.parentNode.tHead.rows[0].cells.length) return index === 0 ? cells[0] : null;
    return cells[index];
  }
  function sortable(root) {
    root.querySelectorAll('table.sortable').forEach(function (table) {
      var headers = table.tHead.rows[0].cells;
      Array.prototype.forEach.call(headers, function (th, index) {
        var button = th.querySelector('button.sort');
        if (!button) return;
        button.addEventListener('click', function () {
          // First click: text A to Z, numbers (and the flag column, by severity) largest or worst first.
          var first = null;
          Array.prototype.some.call(table.tBodies[0].rows, function (r) { first = value(cellAt(r, index)); return first !== null; });
          var asc = th.getAttribute('aria-sort') !== 'ascending' && th.getAttribute('aria-sort') !== 'descending'
            ? (index === 0 || typeof first === 'string' || th.classList.contains('flagcol'))
            : th.getAttribute('aria-sort') === 'descending';
          Array.prototype.forEach.call(headers, function (h) { h.setAttribute('aria-sort', 'none'); });
          th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
          var body = table.tBodies[0];
          var rows = Array.prototype.slice.call(body.rows);
          rows.sort(function (a, b) {
            var x = value(cellAt(a, index)), y = value(cellAt(b, index));
            if (x === null && y === null) return 0;
            if (x === null) return 1;
            if (y === null) return -1;
            if (typeof x !== typeof y) { x = String(x); y = String(y); }
            return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1);
          });
          rows.forEach(function (r) { body.appendChild(r); });
          markCap(table);
        });
      });
    });
  }
  function rowSearch(root) {
    root.querySelectorAll('input[data-filter]').forEach(function (input) {
      var table = document.getElementById(input.getAttribute('data-filter'));
      if (!table) return;
      input.addEventListener('input', function () {
        var q = input.value.trim().toLowerCase();
        table.classList.toggle('searching', !!q);
        table.querySelectorAll('tbody tr').forEach(function (tr) {
          // A row's note line (tr.row-note) goes with the row above it.
          if (tr.classList.contains('row-note')) { tr.hidden = !!(tr.previousElementSibling && tr.previousElementSibling.hidden); return; }
          tr.hidden = q && (tr.getAttribute('data-name') || '').indexOf(q) === -1;
        });
      });
    });
  }
  function reportSearch(root) {
    root.querySelectorAll('input[data-report-search]').forEach(function (input) {
      var list = document.getElementById(input.getAttribute('data-report-search'));
      if (!list) return;
      var none = list.parentNode.querySelector('.no-match');
      function run() {
        var q = input.value.trim().toLowerCase(), shown = 0;
        var words = q.split(/\s+/).filter(Boolean);
        list.querySelectorAll('.report-group').forEach(function (group) {
          var searchOnly = group.hasAttribute('data-search-only'), visible = 0;
          group.querySelectorAll('[data-name]').forEach(function (item) {
            var name = item.getAttribute('data-name');
            item.hidden = (searchOnly && !q) || !words.every(function (w) { return name.indexOf(w) !== -1; });
            if (!item.hidden) visible++;
          });
          group.hidden = !visible;
          shown += visible;
        });
        if (none) none.hidden = shown > 0;
      }
      input.addEventListener('input', run);
      input.addEventListener('keydown', function (e) {  // Enter opens the first match
        if (e.key !== 'Enter') return;
        var first = Array.prototype.find.call(list.querySelectorAll('[data-name]'), function (el) { return !el.hidden && el.offsetParent; });
        var a = first && (first.tagName === 'A' ? first : first.querySelector('a'));
        if (a) { e.preventDefault(); a.click(); }
      });
      if (input.value) run();
    });
  }
  function autosubmit(root) {
    root.querySelectorAll('form[data-autosubmit]').forEach(function (form) {
      form.querySelector('select').addEventListener('change', function () { form.submit(); });
    });
  }
  function foldOnPhones(root) {
    // A long grouped table opens folded on a phone, as a list of headings; a linked group stays open.
    if (!window.matchMedia || !window.matchMedia('(max-width: 640px)').matches) return;
    root.querySelectorAll('table.grouped').forEach(function (table) {
      if (table.querySelectorAll('tbody.group tr:not(.group-head)').length <= 20) return;
      table.querySelectorAll('tbody.group').forEach(function (body) {
        if ('#' + body.id !== window.location.hash) fold(body, true);
      });
      var all = document.querySelector('button[data-groups="' + table.id + '"]');
      if (all) { all.setAttribute('aria-expanded', 'false'); all.querySelector('span').textContent = 'Expand all'; }
    });
  }
  /* Wide tables (.table-scroll). A table first tries to fit its card with its text wrapped, then with its
   * columns a little narrower (.snug). Still too wide, it scrolls sideways (.wide); far too wide, its columns
   * also take the room they need (.natural), since squeezing them saves no scrolling. .scrolled once it has
   * moved sideways, so the pinned first column shows its edge and nothing seems to slide under it unseen. */
  var NATURAL = 0.15;  // too wide by more than this share of the card
  function edges(box) {
    function over() { return box.scrollWidth - box.clientWidth; }
    box.classList.remove('wide', 'snug', 'natural');
    if (over() > 1) box.classList.add('snug');
    var left = over();
    if (left > 1) box.classList.add('wide');
    if (left > box.clientWidth * NATURAL) box.classList.replace('snug', 'natural');
    box.classList.toggle('scrolled', box.scrollLeft > 0);
  }
  function wide(root) { root.querySelectorAll('.table-scroll').forEach(edges); }
  document.addEventListener('scroll', function (e) {
    var box = e.target;
    if (box.classList && box.classList.contains('table-scroll')) box.classList.toggle('scrolled', box.scrollLeft > 0);
  }, true);
  document.addEventListener('toggle', function (e) {  // a list opened in a cell can widen its table
    var box = e.target.closest && e.target.closest('.table-scroll');
    if (box) edges(box);
  }, true);
  window.addEventListener('resize', function () { wide(document); });
  // The page first draws in a fallback font (font-display: swap) and Inter's text is a little wider, so a
  // table that fitted then may not now: measure again once the fonts are in.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { wide(document); });

  function init(root) { sortable(root); rowSearch(root); reportSearch(root); autosubmit(root); foldOnPhones(root); wide(root); }
  init(document);

  /* Row groups fold to their heading. Delegated, so groups loaded in place by a view work too. */
  function fold(body, folded) {
    body.classList.toggle('folded', folded);
    body.querySelector('[data-group]').setAttribute('aria-expanded', folded ? 'false' : 'true');
  }
  document.addEventListener('click', function (e) {
    var one = e.target.closest && e.target.closest('button[data-group]');
    if (one) { var body = one.closest('tbody'); fold(body, !body.classList.contains('folded')); return; }
    var all = e.target.closest && e.target.closest('button[data-groups]');
    if (!all) return;
    var table = document.getElementById(all.getAttribute('data-groups'));
    var folded = all.getAttribute('aria-expanded') === 'true';
    if (table) table.querySelectorAll('tbody.group').forEach(function (b) { fold(b, folded); });
    all.setAttribute('aria-expanded', folded ? 'false' : 'true');
    all.querySelector('span').textContent = folded ? 'Expand all' : 'Collapse all';
  });

  /* The All reports menu: its search box takes the typing as it opens. */
  document.addEventListener('toggle', function (e) {
    var d = e.target;
    if (d.id === 'report-switch' && d.open) { var f = d.querySelector('input[type=search]'); if (f) f.focus(); }
  }, true);
  /* Menus (<details class="menu">) close on Escape or a click outside. */
  document.addEventListener('click', function (e) {
    document.querySelectorAll('details.menu[open]').forEach(function (d) { if (!d.contains(e.target)) d.open = false; });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('details.menu[open]').forEach(function (d) { d.open = false; d.querySelector('summary').focus(); });
  });

  /* Long tables: the first rows of a stacked table, then a "Show all" button. */
  function rows(table) {
    return Array.prototype.filter.call(table.tBodies[0].rows, function (r) { return !r.classList.contains('group') && !r.classList.contains('row-note'); });
  }
  function markCap(table) {
    if (!table.classList.contains('capped')) return;
    var cap = Number(table.getAttribute('data-cap')) || CAP;
    var shown = 0;  // a group heading goes with the rows under it, a note line with the row above it
    Array.prototype.forEach.call(table.tBodies[0].rows, function (r) {
      var note = r.classList.contains('row-note');
      r.classList.toggle('over-cap', note ? shown > cap : shown >= cap);
      if (!r.classList.contains('group') && !note) shown++;
    });
  }
  function capRows(root) {
    // Grouped tables (several bodies) fold their groups instead.
    var tables = Array.prototype.filter.call(root.querySelectorAll('table.stack-sm'), function (table) {
      return table.tBodies.length === 1 && !table.classList.contains('grouped') && !table.hasAttribute('data-cap');
    });
    // A page of several long tables shows the first few rows of each, so the page reads as a summary
    // of its tables rather than one endless list: 5 on a phone, 10 on a laptop. One long table on its
    // own keeps 20 rows on a phone and all of them on a laptop.
    var long = tables.filter(function (table) { return rows(table).length > (phone.matches ? 8 : 15); });
    var several = long.length > 1;
    var cap = phone.matches ? (several ? FEW : CAP) : (several ? 10 : 0);
    if (!cap) return;
    long.forEach(function (table) {
      var n = rows(table).length;
      if (n <= cap + Math.max(3, cap / 4)) return;  // hiding a handful saves nothing
      table.setAttribute('data-cap', cap);
      table.classList.add('capped');
      markCap(table);
      // A few rows need no scroll box of their own; "Show all" gives the long table its box back.
      var box = table.closest('.table-scroll.tall');
      if (box) box.classList.remove('tall');
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'show-all';
      button.textContent = 'Show all ' + n + ' rows';
      button.addEventListener('click', function () {
        table.classList.remove('capped');
        rows(table).forEach(function (r) { r.classList.remove('over-cap'); });
        if (box) box.classList.add('tall');
        button.remove();
      });
      var after = table.closest('.table-scroll, .scroll-x') || table;
      after.parentNode.insertBefore(button, after.nextSibling);
    });
  }
  capRows(document);

  /* In-place views: fetch the linked page and swap <main>, keeping where the person was. */
  var main = document.querySelector('main');
  if (!main || !window.fetch || !window.DOMParser || !history.pushState) return;
  function state() {
    var s = { scroll: window.scrollY, menus: [], searches: {}, inner: {} };
    main.querySelectorAll('details.menu[open][id]').forEach(function (d) { s.menus.push(d.id); });
    main.querySelectorAll('input[data-filter]').forEach(function (i) { if (i.value) s.searches[i.getAttribute('data-filter')] = i.value; });
    main.querySelectorAll('.table-scroll[data-key]').forEach(function (el) { s.inner[el.getAttribute('data-key')] = el.scrollTop; });
    return s;
  }
  function restore(s) {
    s.menus.forEach(function (id) { var d = document.getElementById(id); if (d) d.open = true; });
    Object.keys(s.searches).forEach(function (id) {
      var input = main.querySelector('input[data-filter="' + id + '"]');
      if (input) { input.value = s.searches[id]; input.dispatchEvent(new Event('input')); }
    });
    Object.keys(s.inner).forEach(function (key) {
      var el = main.querySelector('.table-scroll[data-key="' + key + '"]');
      if (el) el.scrollTop = s.inner[key];
    });
    window.scrollTo(0, s.scroll);
  }
  function load(href, push, keep) {
    var before = keep ? state() : null;
    main.setAttribute('aria-busy', 'true');
    return fetch(href, { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) {
        if (!r.ok || r.redirected) throw new Error('reload');
        return r.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var next = doc.querySelector('main');
        if (!next) throw new Error('reload');
        main.innerHTML = next.innerHTML;
        document.title = doc.title;
        if (push) history.pushState({ view: true }, '', href);
        init(main);
        capRows(main);
        if (window.officeSentryHead) window.officeSentryHead();
        if (before) restore(before);
        main.removeAttribute('aria-busy');
      })
      .catch(function () { window.location.href = href; });
  }
  main.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[data-view]');
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var url = new URL(a.href, window.location.href);
    if (url.origin !== window.location.origin) return;
    e.preventDefault();
    // A headline figure jumps to its table; everything else stays where it is.
    var jump = a.classList.contains('tile') && url.hash;
    load(url.href, true, !jump).then(function () {
      if (jump) { var target = document.getElementById(url.hash.slice(1)); if (target) target.scrollIntoView(); }
    });
  });
  window.addEventListener('popstate', function () { load(window.location.href, false, false); });
})();
