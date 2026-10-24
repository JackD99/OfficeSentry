/* Keyboard shortcuts on staff pages, listed in the box "?" opens (templates/_keys.html).
 * j and k move a highlight down and up the rows of the page's tables; Enter or o opens the highlighted row (its
 * sheet on Findings, otherwise the row's first link). g then a letter opens an entry of the menu: the menu's links
 * carry their letter (data-go, base.html), so the box lists only what this person's menu has.
 * Keys typed into a field, or held with Ctrl, Alt or ⌘, are left alone; "/" and Ctrl+K are the jump box's (jump.js). */
(function () {
  var main = document.querySelector('main');
  var box = document.getElementById('keys');
  if (!main || !box || typeof box.showModal !== 'function') return;
  var cursor = null, waiting = null;

  function typing(el) {
    return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  }
  function control(el) {  // Enter on a link or a button is that link's or button's
    return el && el !== main && el.closest && el.closest('a, button, summary, [role=button]');
  }

  /* Rows: a table's data rows that are on the page now, not group headings, note lines or rows hidden by a
   * search, a fold or "Show all". */
  function rows() {
    return Array.prototype.filter.call(main.querySelectorAll('table tbody tr'), function (tr) {
      return !tr.classList.contains('group-head') && !tr.classList.contains('row-note') && tr.getClientRects().length > 0
        && !tr.closest('.sheet, .menu-panel');
    });
  }
  function place(tr) {
    if (cursor) cursor.classList.remove('row-cursor');
    cursor = tr;
    tr.classList.add('row-cursor');
    tr.scrollIntoView({ block: 'nearest' });
  }
  function move(step) {
    var list = rows();
    if (!list.length) return;
    var at = list.indexOf(cursor);
    if (at === -1) {
      // The first move starts from the first row in view, so it never jumps back to the top of a long page.
      var first = list.findIndex(function (tr) { return tr.getBoundingClientRect().top >= 0; });
      at = first === -1 ? list.length - 1 : first;
      place(list[at]);
      return;
    }
    place(list[Math.max(0, Math.min(list.length - 1, at + step))]);
  }
  function open() {
    if (!cursor || !cursor.isConnected) return false;
    var link = cursor.querySelector('a.sheet-link') || cursor.querySelector('a[href]:not([target])') || cursor.querySelector('a[href]');
    if (!link) return false;
    link.click();
    return true;
  }

  /* The list of shortcuts: its "Go to" part is the menu's own entries. */
  var go = box.querySelector('[data-keys-go]');
  document.querySelectorAll('.sidebar a[data-go]').forEach(function (a) {
    var label = a.querySelector('span');
    var dt = document.createElement('dt'), dd = document.createElement('dd');
    ['g', a.getAttribute('data-go')].forEach(function (k) {
      var kbd = document.createElement('kbd');
      kbd.textContent = k;
      dt.appendChild(kbd);
    });
    dd.textContent = label ? label.textContent : a.textContent;
    go.appendChild(dt);
    go.appendChild(dd);
  });
  if (!go.children.length) go.previousElementSibling.hidden = go.hidden = true;
  box.addEventListener('click', function (e) { if (e.target === box) box.close(); });  // a click on the backdrop

  document.addEventListener('keydown', function (e) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || typing(document.activeElement)) return;
    if (e.key === '?') { e.preventDefault(); if (box.open) box.close(); else box.showModal(); return; }
    if (document.querySelector('dialog[open]')) return;
    if (waiting) {  // g, then a letter
      clearTimeout(waiting);
      waiting = null;
      var target = /^[a-z]$/.test(e.key) && document.querySelector('.sidebar a[data-go="' + e.key + '"]');
      if (target) { e.preventDefault(); target.click(); }
      return;
    }
    if (e.key === 'g') { waiting = setTimeout(function () { waiting = null; }, 1500); return; }
    if (e.key === 'j' || e.key === 'k') { e.preventDefault(); move(e.key === 'j' ? 1 : -1); return; }
    if ((e.key === 'o' || (e.key === 'Enter' && !control(document.activeElement))) && open()) { e.preventDefault(); return; }
    if (e.key === 'Escape' && cursor) { cursor.classList.remove('row-cursor'); cursor = null; }
  });
})();
