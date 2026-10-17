/* The jump box (web/jump.py, templates/_jump.html). "/" or Ctrl+K (⌘K on a Mac) opens it over the page, and so
 * does the menu's search box. Type a few letters to land on a client, a report for a client ("north mfa"), a
 * person, a page or a setting: rows come from GET /jump as JSON, ranked; ↑ and ↓ move, Enter opens the chosen
 * row (or, with none, searches everything), Escape closes. The last few places opened are kept in this browser
 * for this account and shown while the box is empty. Without this script the menu's box submits to /search. */
(function () {
  var dialog = document.getElementById('jump');
  if (!dialog || typeof dialog.showModal !== 'function' || !window.fetch) return;
  var input = dialog.querySelector('input');
  var list = dialog.querySelector('[role=listbox]');
  var status = dialog.querySelector('.jump-status');
  var hint = dialog.querySelector('[data-jump-hint]');
  var src = dialog.getAttribute('data-src');
  var tenant = dialog.getAttribute('data-tenant');
  var store = 'jump:' + (dialog.getAttribute('data-who') || '');
  var DEBOUNCE = 120, RECENT = 6;
  var icons = {};
  Array.prototype.forEach.call(dialog.querySelectorAll('template[data-icon]'), function (t) {
    icons[t.getAttribute('data-icon')] = t.content.firstElementChild;
  });
  var rows = [], selected = -1, latest = 0, timer = null, controller = null;
  var wanted = false;  // Enter pressed while the answer for what was typed is still on its way

  /* Recent jumps: a few rows, newest first, in localStorage for this account. */
  function recent() {
    try { return JSON.parse(localStorage.getItem(store) || '[]').filter(function (r) { return r && r.href && r.title; }); }
    catch (e) { return []; }
  }
  function remember(row) {
    if (row.kind === 'more') return;
    var kept = recent().filter(function (r) { return r.href !== row.href; });
    kept.unshift({ kind: row.kind, title: row.title, where: row.where || '', detail: row.detail || '', href: row.href });
    try { localStorage.setItem(store, JSON.stringify(kept.slice(0, RECENT))); } catch (e) { /* not kept */ }
  }

  function tokens() { return input.value.toLowerCase().split(/\s+/).filter(Boolean); }
  /* The text as nodes, with the parts a typed word starts in bold (never parsed as HTML). */
  function marked(text, toks) {
    var frag = document.createDocumentFragment();
    var low = text.toLowerCase(), hit = [], i, j, t;
    for (i = 0; i < low.length; i++) hit.push(false);
    for (t = 0; t < toks.length; t++) {
      for (i = 0; i < low.length; i++) {
        if (i > 0 && /[a-z0-9]/.test(low[i - 1])) continue;  // only at the start of a word
        if (low.substr(i, toks[t].length) !== toks[t]) continue;
        for (j = i; j < i + toks[t].length && j < low.length; j++) hit[j] = true;
      }
    }
    var run = '', on = false;
    function flush() {
      if (!run) return;
      if (on) { var m = document.createElement('mark'); m.textContent = run; frag.appendChild(m); }
      else frag.appendChild(document.createTextNode(run));
      run = '';
    }
    for (i = 0; i < text.length; i++) {
      if (hit[i] !== on) { flush(); on = hit[i]; }
      run += text[i];
    }
    flush();
    return frag;
  }

  function option(row, index, toks) {
    var a = document.createElement('a');
    a.className = 'jump-row';
    a.id = 'jump-opt-' + index;
    a.href = row.href;
    a.setAttribute('role', 'option');
    a.setAttribute('aria-selected', 'false');
    var ico = document.createElement('span');
    ico.className = 'jump-ico';
    var svg = icons[row.kind] || icons.page;
    if (svg) ico.appendChild(svg.cloneNode(true));
    var text = document.createElement('span');
    text.className = 'jump-text';
    var title = document.createElement('span');
    title.className = 'jump-title';
    title.appendChild(marked(row.title, toks));
    if (row.where) {
      var where = document.createElement('span');
      where.className = 'jump-where';
      where.appendChild(document.createTextNode(' · '));
      where.appendChild(marked(row.where, toks));
      title.appendChild(where);
    }
    text.appendChild(title);
    if (row.detail) {
      var detail = document.createElement('span');
      detail.className = 'jump-detail';
      detail.textContent = row.detail;
      text.appendChild(detail);
    }
    var key = document.createElement('kbd');
    key.className = 'jump-enter';
    key.textContent = '↵';
    key.setAttribute('aria-hidden', 'true');
    a.appendChild(ico);
    a.appendChild(text);
    a.appendChild(key);
    a.addEventListener('mousemove', function () { if (selected !== index) select(index); });
    a.addEventListener('click', function () { remember(row); });
    return a;
  }
  function label(text) {
    var div = document.createElement('div');
    div.className = 'jump-label';
    div.setAttribute('role', 'presentation');
    div.textContent = text;
    return div;
  }
  function select(index) {
    var options = list.querySelectorAll('[role=option]');
    selected = index;
    Array.prototype.forEach.call(options, function (el, i) {
      var on = i === index;
      el.classList.toggle('sel', on);
      el.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on) {
        input.setAttribute('aria-activedescendant', el.id);
        if (el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
      }
    });
    if (index < 0) input.removeAttribute('aria-activedescendant');
  }
  function render(items, heading, toks) {
    rows = items;
    list.textContent = '';
    if (heading && items.length) list.appendChild(label(heading));
    items.forEach(function (row, i) { list.appendChild(option(row, i, toks || [])); });
    input.setAttribute('aria-expanded', items.length ? 'true' : 'false');
    select(items.length ? 0 : -1);
  }
  function count(n) { return n === 1 ? '1 match' : n + ' matches'; }

  function showRecent() {
    latest++;
    wanted = false;
    if (controller) controller.abort();
    list.removeAttribute('aria-busy');
    var kept = recent();
    hint.hidden = kept.length > 0;
    render(kept, 'Recent');
    status.textContent = '';
  }
  function ask() {
    var q = input.value.trim();
    if (!q) { showRecent(); return; }
    var seq = ++latest;
    if (controller) controller.abort();
    controller = window.AbortController ? new AbortController() : null;
    list.setAttribute('aria-busy', 'true');
    status.textContent = '';
    var url = src + '?q=' + encodeURIComponent(q) + (tenant ? '&tenant=' + encodeURIComponent(tenant) : '');
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: controller ? controller.signal : undefined })
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(function (data) {
        if (seq !== latest) return;
        hint.hidden = true;
        var items = data.rows.slice();
        if (data.more) {
          items.push({
            kind: 'more',
            title: 'Search everything for “' + data.q + '”',
            detail: data.partial
              ? 'Some clients weren’t checked yet; the full search reads them all'
              : 'People, mailboxes, forwarding addresses, apps, devices and more, in every client',
            href: data.more
          });
        }
        render(items, '', tokens());
        list.removeAttribute('aria-busy');
        status.textContent = data.rows.length ? count(data.rows.length) : 'Nothing matches “' + data.q + '”';
        if (wanted) go();
      })
      .catch(function (err) {
        if ((err && err.name === 'AbortError') || seq !== latest) return;
        wanted = false;
        render([]);
        list.removeAttribute('aria-busy');
        status.textContent = 'Couldn’t search just now';
      });
  }
  function go() {
    wanted = false;
    var row = rows[selected];
    if (!row) return;
    remember(row);
    status.textContent = 'Opening…';
    window.location.assign(row.href);
  }
  function pending() { return timer !== null || list.getAttribute('aria-busy') === 'true'; }

  input.addEventListener('input', function () {
    wanted = false;
    clearTimeout(timer);
    timer = setTimeout(function () { timer = null; ask(); }, DEBOUNCE);
  });
  input.addEventListener('keydown', function (e) {
    if (e.isComposing) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (rows.length) select((selected + 1) % rows.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (rows.length) select((selected - 1 + rows.length) % rows.length);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (pending()) {  // open the best match for what was typed, once it's known
        wanted = true;
        if (timer !== null) { clearTimeout(timer); timer = null; ask(); }
      } else go();
    }
  });
  dialog.addEventListener('close', function () {
    // The event comes a task after the box closed: if it was opened again since (Escape, then Ctrl+K straight
    // away), the search waiting for what was typed there is the new box's, so leave it.
    if (dialog.open) return;
    clearTimeout(timer);
    timer = null;
    wanted = false;
    if (controller) controller.abort();
    list.removeAttribute('aria-busy');
    latest++;
  });
  dialog.addEventListener('click', function (e) {
    if (e.target === dialog) dialog.close();  // the backdrop: the box itself has no padding of its own
  });

  function open() {
    var menu = document.querySelector('.mobile-bar');
    if (menu && menu.open) menu.open = false;
    if (!dialog.open) {
      input.value = '';
      dialog.showModal();
      showRecent();
    }
    input.focus();
    input.select();
  }
  /* The menu's search box becomes the button that opens the box; the form and its link are for no script. */
  Array.prototype.forEach.call(document.querySelectorAll('.nav-search, .nav-search-link'), function (el) { el.hidden = true; });
  Array.prototype.forEach.call(document.querySelectorAll('[data-jump-open]'), function (el) {
    el.hidden = false;
    el.addEventListener('click', function (e) { e.preventDefault(); open(); });
  });
  /* "/" (not while typing in a field) or Ctrl+K, ⌘K anywhere. */
  function typing(el) {
    return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  }
  document.addEventListener('keydown', function (e) {
    var slash = e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey;
    var k = (e.key === 'k' || e.key === 'K') && (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey;
    if (e.defaultPrevented || !(slash || k) || (slash && typing(document.activeElement))) return;
    e.preventDefault();
    open();
  });
})();
