/* The finding sheet (web/finding.py, _finding_sheet.html). Each row's Open link is the page that shows the
 * table with the sheet beside it (first, on a phone), its columns narrowed to leave the sheet room; a phone,
 * a middle click or no script just goes there. On a laptop, the rest of the row opens it too, and once a
 * sheet is open the next row's sheet is swapped in without reloading the page: fetched alone, put in place,
 * the address kept in step so Back and a copied link work (tables.js reloads the page in place on
 * popstate), and the open row marked. Escape or the sheet's close button go back to the table alone. */
(function () {
  var laptop = window.matchMedia('(min-width: 961px)');
  function sheetUrl(href) {  // /tenants/1/findings/5?f=... -> /findings/5/sheet
    var found = href.match(/\/findings\/(\d+)(?:[?#]|$)/);
    return found ? '/findings/' + found[1] + '/sheet' : null;
  }
  function layout() { return document.querySelector('.findings-layout'); }
  function mark(id) {
    document.querySelectorAll('a.sheet-link').forEach(function (a) {
      if (id !== null && a.getAttribute('data-finding') === String(id)) a.setAttribute('aria-current', 'true');
      else a.removeAttribute('aria-current');
    });
  }
  function refit() { window.dispatchEvent(new Event('resize')); }  // tables fit the room they have left
  function open(href) {
    var url = sheetUrl(href), box = layout();
    if (!url || !box) return false;
    if (!document.getElementById('finding-sheet')) { window.location.href = href; return true; }  // narrows the table
    box.setAttribute('aria-busy', 'true');
    fetch(url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) {
        if (!r.ok || r.redirected) throw new Error('reload');
        return r.text();
      })
      .then(function (html) {
        var next = new DOMParser().parseFromString(html, 'text/html').querySelector('#finding-sheet');
        if (!next) throw new Error('reload');
        var current = document.getElementById('finding-sheet');
        if (current) current.replaceWith(next); else box.appendChild(next);
        box.classList.add('with-sheet');
        box.removeAttribute('aria-busy');
        history.pushState({ view: true }, '', href);
        mark(next.getAttribute('data-finding'));
        refit();
        var title = next.querySelector('#sheet-title');
        if (title) title.focus({ preventScroll: true });
      })
      .catch(function () { window.location.href = href; });
    return true;
  }
  function close() {  // the table alone, with its columns back
    var current = document.getElementById('finding-sheet');
    var back = current && current.querySelector('.sheet-close');
    if (back) window.location.href = back.getAttribute('href');
  }
  document.addEventListener('click', function (e) {
    if (!laptop.matches || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var closer = e.target.closest && e.target.closest('.sheet-close');
    if (closer) { e.preventDefault(); close(); return; }
    var link = e.target.closest && e.target.closest('a.sheet-link');
    if (!link) {  // the rest of the row opens it too, except its own links and controls
      var row = e.target.closest && e.target.closest('tr');
      if (!row || e.target.closest('a, button, input, select, textarea, summary, label, form, details')) return;
      link = row.querySelector('a.sheet-link');
      if (!link) return;
    }
    e.preventDefault();
    open(link.href);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !laptop.matches || !document.getElementById('finding-sheet')) return;
    if (e.target.closest && e.target.closest('details[open], dialog')) return;  // that closes first
    close();
  });
})();
