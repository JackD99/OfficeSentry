/* Every portal page. No dependencies.
 * Wide tables: a box that scrolls sideways can be focused, so keyboard users can scroll it too (WCAG 2.1.1).
 * PDF links (any link to a .pdf): a PDF can take a few seconds to build, so the link shows
 *   "Preparing your PDF…" until the file arrives, then saves it. A page instead of a PDF (no WeasyPrint:
 *   the print-ready page) or any error falls back to opening the link normally. */
(function () {
  if (!window.fetch || !window.URL || !URL.createObjectURL) return;
  var status = null;
  function say(text, done) {
    if (!status) {
      status = document.createElement('div');
      status.className = 'pdf-status';
      status.setAttribute('role', 'status');
      document.body.appendChild(status);
    }
    status.textContent = text;
    status.classList.toggle('done', !!done);
    status.hidden = !text;
  }
  function filename(response, href) {
    var m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(response.headers.get('Content-Disposition') || '');
    return m ? decodeURIComponent(m[1]) : href.split('/').pop().split('?')[0];
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var url = new URL(a.href, window.location.href);
    if (url.origin !== window.location.origin || !/\.pdf$/i.test(url.pathname) || a.hasAttribute('download')) return;
    e.preventDefault();
    if (a.getAttribute('aria-busy') === 'true') return;
    a.setAttribute('aria-busy', 'true');
    a.classList.add('preparing');
    say('Preparing your PDF…');
    fetch(url.href, { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok || r.redirected || (r.headers.get('Content-Type') || '').indexOf('application/pdf') !== 0) throw new Error('open');
        var name = filename(r, url.pathname);
        return r.blob().then(function (blob) {
          var link = document.createElement('a');
          link.href = URL.createObjectURL(blob);
          link.download = name;
          document.body.appendChild(link);
          link.click();
          link.remove();
          setTimeout(function () { URL.revokeObjectURL(link.href); }, 60000);
          say('Your PDF is ready: ' + name, true);
          setTimeout(function () { if (status && status.classList.contains('done')) say(''); }, 4000);
        });
      })
      .catch(function () { say(''); window.location.href = url.href; })
      .then(function () { a.removeAttribute('aria-busy'); a.classList.remove('preparing'); });
  });
})();

(function () {
  function focusable() {
    document.querySelectorAll('.scroll-x, .table-scroll').forEach(function (box) {
      if (box.scrollWidth > box.clientWidth + 1) {
        if (!box.hasAttribute('tabindex')) box.setAttribute('tabindex', '0');
        if (!box.hasAttribute('role')) box.setAttribute('role', 'region');
        if (!box.hasAttribute('aria-label')) {
          var card = box.closest('section, .card');
          var heading = card && card.querySelector('h1, h2, h3');
          box.setAttribute('aria-label', heading ? heading.textContent.trim() + ' (scrolls sideways)' : 'Table (scrolls sideways)');
        }
      }
    });
  }
  var timer;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', focusable);
  else focusable();
  window.addEventListener('resize', function () { clearTimeout(timer); timer = setTimeout(focusable, 150); });
})();

/* Tenant pages on laptops: the header's tab bar stays pinned to the top of the window as the page
 * scrolls, with the tenant's name in it (.tenant-head[data-pin], app.css). Pure CSS position: sticky with
 * a negative top, so nothing changes height and the page never jumps; this only measures. The Reports tab's
 * picker scrolls to the open report. window.officeSentryHead runs again after a view loads in place. */
(function () {
  var wide = window.matchMedia ? window.matchMedia('(min-width: 961px)') : { matches: true };
  var GAP = 10;  // px of the header kept above the tabs while pinned
  var head = null, top = 0;
  function measure() {
    var root = document.documentElement;
    if (!head || !wide.matches) {
      if (head) head.classList.remove('pinned', 'stuck');
      root.style.removeProperty('--pin-h');
      return;
    }
    head.classList.add('pinned');
    var tabs = head.querySelector('.tabs');
    var keep = tabs.offsetHeight + GAP;
    top = keep - head.offsetHeight;
    head.style.setProperty('--head-top', top + 'px');
    root.style.setProperty('--pin-h', keep + 'px');
    check();
  }
  function check() {
    if (head && head.classList.contains('pinned')) head.classList.toggle('stuck', head.getBoundingClientRect().top <= top + 0.5);
  }
  function picker() {
    var list = document.querySelector('.picker-list');
    var open = list && list.querySelector('a[aria-current]');
    if (open && list.scrollHeight > list.clientHeight) {
      var below = Math.round(open.offsetTop - list.offsetTop - list.clientHeight / 3);
      if (below > 0) list.scrollTop = below;
    }
  }
  function start() {
    head = document.querySelector('.tenant-head[data-pin]');
    measure();
    picker();
    // Web fonts change line heights, so measure again once they're in: the open report's place in the
    // picker (and the pinned bar's height) then come out the same on every load.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { measure(); picker(); });
  }
  window.officeSentryHead = start;
  var ticking = false;
  window.addEventListener('scroll', function () {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(function () { ticking = false; check(); });
  }, { passive: true });
  var timer;
  window.addEventListener('resize', function () { clearTimeout(timer); timer = setTimeout(measure, 100); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();

/* Copy buttons: data-copy="#id" copies that element's text (a textarea's value) to the clipboard and says so. */
(function () {
  function fallback(text) {
    var box = document.createElement('textarea');
    box.value = text;
    box.setAttribute('readonly', '');
    box.style.position = 'fixed';
    box.style.opacity = '0';
    document.body.appendChild(box);
    box.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    box.remove();
    return ok ? Promise.resolve() : Promise.reject(new Error('copy'));
  }
  // Each button's own label (its icon and words) as nodes, put back as they were after "Copied", so nothing
  // read from the page is ever parsed as HTML.
  var labels = new WeakMap();
  document.addEventListener('click', function (e) {
    var button = e.target.closest && e.target.closest('[data-copy]');
    if (!button) return;
    var source = document.querySelector(button.getAttribute('data-copy'));
    if (!source) return;
    var text = source.value !== undefined ? source.value : source.textContent;
    var done = navigator.clipboard && window.isSecureContext ? navigator.clipboard.writeText(text) : fallback(text);
    if (!labels.has(button)) labels.set(button, Array.prototype.slice.call(button.childNodes));
    var label = labels.get(button);
    done.then(function () {
      button.classList.add('copied');
      button.textContent = button.getAttribute('data-copied') || 'Copied';
    }, function () {
      button.textContent = 'Select and copy';
    }).then(function () {
      setTimeout(function () { button.replaceChildren.apply(button, label); button.classList.remove('copied'); }, 2000);
    });
  });
})();

/* Report previews ([data-paper]: a box holding the iframe of a report page, in the report designer and the
 * month-end review). The page is drawn as wide as the printed page's text, so it lays out as the PDF does (four
 * figures to a row, half-width blocks side by side) rather than as a phone would, then shrunk to fit the box.
 * The box's height stays put; the page scrolls inside it. */
(function () {
  var PAGE = 760;
  var boxes = document.querySelectorAll('[data-paper]');
  if (!boxes.length) return;
  function fit(box) {
    var frame = box.querySelector('iframe');
    if (!frame || !box.clientWidth) return;
    var scale = Math.min(1, box.clientWidth / PAGE);
    frame.style.width = PAGE + 'px';
    frame.style.height = Math.ceil(box.clientHeight / scale) + 'px';
    frame.style.transform = 'scale(' + scale + ')';
    box.classList.toggle('shrunk', scale < 1);
  }
  boxes.forEach(function (box) {
    box.classList.add('fitted');
    fit(box);
    if (window.ResizeObserver) new ResizeObserver(function () { fit(box); }).observe(box);
    else window.addEventListener('resize', function () { fit(box); });
  });
})();

/* Account page parts (<details class="part">): open on laptops, folded to their heading and summary
 * on phones so the page reads as a list of what's there; a link to a part opens it. */
(function () {
  var parts = document.querySelectorAll('details.part[id]');
  if (!parts.length) return;
  function reveal(hash) {
    var target = hash && hash.length > 1 && document.getElementById(hash.slice(1));
    if (target && target.tagName === 'DETAILS') target.open = true;
  }
  if (window.matchMedia && window.matchMedia('(max-width: 640px)').matches) {
    parts.forEach(function (d) { if ('#' + d.id !== window.location.hash) d.open = false; });
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href^="#"]');
    if (a) reveal(a.getAttribute('href'));
  });
  window.addEventListener('hashchange', function () { reveal(window.location.hash); });
})();

/* The setup wizards (App connection, Staff sign-in). Copy the sign-in code; fill a field from a button
 * ([data-fill]); and while Office Sentry waits for the Microsoft sign-in ([data-setup-wait]), check with it
 * every few seconds (it asks Microsoft) and move on by itself once the app is created or something went wrong. */
(function () {
  document.querySelectorAll('[data-copy]').forEach(function (button) {
    button.addEventListener('click', function () {
      var text = document.querySelector(button.dataset.copy).textContent.trim();
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function () { button.textContent = 'Copied'; });
      }
    });
  });
  document.querySelectorAll('[data-fill]').forEach(function (button) {
    button.addEventListener('click', function () {
      var input = document.querySelector(button.dataset.fill);
      input.value = button.dataset.value;
      input.focus();
    });
  });
  var box = document.querySelector('[data-setup-wait]');
  if (!box || !window.fetch) { return; }
  var token = document.querySelector('input[name=csrf_token]');
  var text = box.querySelector('[data-setup-text]'), left = box.querySelector('[data-setup-left]');
  function minutes(seconds) {
    var m = Math.ceil(seconds / 60);
    return m <= 1 ? 'less than a minute' : m + ' more minutes';
  }
  function check() {
    fetch(box.dataset.check, {method: 'POST', credentials: 'same-origin',
                              headers: {'Accept': 'application/json', 'X-CSRFToken': token ? token.value : ''}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) { setTimeout(check, 10000); return; }
        if (d.status !== 'waiting' && d.status !== 'working') { location.reload(); return; }
        text.textContent = d.status === 'working' ? 'Signed in. Creating the app…'
          : (d.note || 'Waiting for you to sign in. This page moves on by itself.');
        if (left) { left.textContent = minutes(d.seconds_left); }
        setTimeout(check, Math.max(d.wait || 5, 2) * 1000);
      })
      .catch(function () { setTimeout(check, 10000); });
  }
  setTimeout(check, Math.max(parseInt(box.dataset.setupWait, 10) || 5, 2) * 1000);
})();
