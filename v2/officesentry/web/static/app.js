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

/* Forms. A form or submit button with data-confirm="What happens?" asks first, in the portal's own dialog:
 * the first question is the heading and the rest the words under it (data-confirm-title names the heading
 * instead), the button says what the submit button says (or data-confirm-action), in red when that button
 * is .danger or the form or button has data-danger. The cursor starts on Cancel.
 * Then every form that posts shows its button busy (a spinner, or the form's data-busy words: "Sending…")
 * and ignores further presses until the next page arrives; the browser's Back button finds it as it was.
 * data-no-busy leaves a form out (one that downloads a file and stays on the page). */
(function () {
  var dialog = null, pending = null;
  function build() {
    dialog = document.createElement('dialog');
    dialog.className = 'confirm-dialog';
    dialog.setAttribute('aria-labelledby', 'confirm-title');
    dialog.setAttribute('aria-describedby', 'confirm-text');
    dialog.innerHTML = '<form method="dialog"><h2 id="confirm-title"></h2><p id="confirm-text"></p>' +
      '<div class="confirm-actions"><button value="cancel" class="confirm-cancel">Cancel</button><button value="ok" class="confirm-ok"></button></div></form>';
    dialog.addEventListener('close', function () {
      var go = dialog.returnValue === 'ok' && pending;
      var form = pending && pending.form, submitter = pending && pending.submitter;
      pending = null;
      if (go) send(form, submitter);
    });
    document.body.appendChild(dialog);
  }
  function send(form, submitter) {
    form.dataset.confirmed = '1';
    if (form.requestSubmit) form.requestSubmit(submitter || undefined);
    else if (submitter) submitter.click();
    else form.submit();
  }
  function submitButton(form) {
    return form.querySelector('button:not([type=button]), input[type=submit]');
  }
  function ask(form, submitter, holder) {
    if (!dialog) build();
    var words = holder.getAttribute('data-confirm');
    var title = holder.getAttribute('data-confirm-title') || form.getAttribute('data-confirm-title');
    var text = words;
    if (!title) {  // "Delete Gold? Its clients are kept." is a heading and a line under it
      var cut = words.indexOf('? ');
      title = cut === -1 ? words : words.slice(0, cut + 1);
      text = cut === -1 ? '' : words.slice(cut + 2);
    }
    var button = submitter || submitButton(form);
    var label = holder.getAttribute('data-confirm-action') || form.getAttribute('data-confirm-action') ||
      (button && (button.tagName === 'INPUT' ? button.value : button.textContent.trim())) || 'Continue';
    var danger = (button && button.classList.contains('danger')) || holder.hasAttribute('data-danger') || form.hasAttribute('data-danger');
    dialog.querySelector('h2').textContent = title;
    var p = dialog.querySelector('p');
    p.textContent = text;
    p.hidden = !text;
    var ok = dialog.querySelector('.confirm-ok');
    ok.textContent = label;
    ok.className = 'confirm-ok ' + (danger ? 'danger' : 'primary');
    pending = { form: form, submitter: submitter };
    dialog.returnValue = '';
    dialog.showModal();
    dialog.querySelector('.confirm-cancel').focus();
  }
  // In the capture phase, so the question comes before any page's own handling of the submit.
  document.addEventListener('submit', function (e) {
    var form = e.target, submitter = e.submitter || null;
    if (form.method === 'dialog') return;
    var holder = submitter && submitter.hasAttribute('data-confirm') ? submitter : form.hasAttribute('data-confirm') ? form : null;
    if (holder && form.dataset.confirmed !== '1') {
      e.preventDefault();
      e.stopImmediatePropagation();
      if (typeof HTMLDialogElement === 'function') ask(form, submitter, holder);
      else if (window.confirm(holder.getAttribute('data-confirm'))) send(form, submitter);
      return;
    }
    delete form.dataset.confirmed;
    if (form.dataset.sending === '1') { e.preventDefault(); e.stopImmediatePropagation(); return; }
    if (form.method !== 'post' || form.hasAttribute('data-no-busy') || form.target) return;
    // Once every handler has had its say: a page that sends the form itself (fetch) has cancelled it.
    setTimeout(function () {
      if (e.defaultPrevented) return;
      form.dataset.sending = '1';
      var b = submitter || submitButton(form);
      if (!b) return;
      b.setAttribute('aria-busy', 'true');
      b.setAttribute('aria-disabled', 'true');
      if (form.dataset.busy && b.tagName === 'BUTTON') { b.dataset.label = b.innerHTML; b.textContent = form.dataset.busy; }
    }, 0);
  }, true);
  // Back to a page the browser kept: its buttons work again.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    document.querySelectorAll('form[data-sending]').forEach(function (f) { delete f.dataset.sending; });
    document.querySelectorAll('button[aria-busy="true"], input[aria-busy="true"]').forEach(function (b) {
      if (b.dataset.label !== undefined) { b.innerHTML = b.dataset.label; delete b.dataset.label; }
      b.removeAttribute('aria-busy');
      b.removeAttribute('aria-disabled');
    });
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

/* The menu folds to its icons on laptops ([data-fold], .menu-folded on <html>), giving wide reports the
 * room, and stays folded from page to page (localStorage; base.html applies it before the page draws).
 * Folded, each icon's name shows as its tooltip. */
(function () {
  var button = document.querySelector('[data-fold]');
  if (!button) return;
  var root = document.documentElement;
  var named = button.closest('.sidebar').querySelectorAll('.nav a, .account .links a, .account button.link, [data-fold]');
  function show(folded) {
    root.classList.toggle('menu-folded', folded);
    button.querySelector('span').textContent = folded ? 'Unfold the menu' : 'Fold the menu';
    named.forEach(function (el) {
      if (folded) el.title = el.textContent.trim();
      else el.removeAttribute('title');
    });
  }
  show(root.classList.contains('menu-folded'));
  button.addEventListener('click', function () {
    var folded = !root.classList.contains('menu-folded');
    show(folded);
    try { localStorage.setItem('menu', folded ? 'folded' : 'open'); } catch (e) { /* kept for this page only */ }
    window.dispatchEvent(new Event('resize'));  // tables and the pinned header measure again
  });
})();

/* Copy buttons: data-copy="#id" copies that element's text (a textarea's value) to the clipboard and says so;
 * data-copy-text="..." copies the attribute's own text (a link). */
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
    var button = e.target.closest && e.target.closest('[data-copy], [data-copy-text]');
    if (!button) return;
    var text = button.getAttribute('data-copy-text');
    if (text === null) {
      var source = document.querySelector(button.getAttribute('data-copy'));
      if (!source) return;
      text = source.value !== undefined ? source.value : source.textContent;
    }
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
        text.textContent = d.status === 'working' ? (box.dataset.working || 'Signed in. Creating the app…')
          : (d.note || 'Waiting for you to sign in. This page moves on by itself.');
        if (left) { left.textContent = minutes(d.seconds_left); }
        setTimeout(check, Math.max(d.wait || 5, 2) * 1000);
      })
      .catch(function () { setTimeout(check, 10000); });
  }
  setTimeout(check, Math.max(parseInt(box.dataset.setupWait, 10) || 5, 2) * 1000);
})();

/* Switch client (_macros.tenant_header): the clients this person opened last come first, under Recent. Kept in this
 * browser for this account, as the jump box keeps its recent places; every client still lists below. */
(function () {
  var menu = document.querySelector('[data-recent]');
  if (!menu) return;
  var store = 'recent-clients:' + menu.getAttribute('data-recent');
  var here = menu.getAttribute('data-here');
  var KEEP = 5;
  var seen;
  try { seen = JSON.parse(localStorage.getItem(store) || '[]'); } catch (e) { seen = []; }
  if (!Array.isArray(seen)) seen = [];
  var group = menu.querySelector('[data-recent-group]');
  var list = group && group.querySelector('ul');
  if (list) {
    seen.forEach(function (id) {
      var item = id !== here && menu.querySelector('li[data-tenant="' + String(id).replace(/[^0-9]/g, '') + '"]');
      if (item && list.children.length < KEEP) list.appendChild(item);
    });
    // The client being looked at heads the group, so the menu opens at the top rather than scrolled to it.
    var current = list.children.length && menu.querySelector('li[data-tenant="' + here + '"]');
    if (current) list.insertBefore(current, list.firstChild);
    group.hidden = !list.children.length;
    var rest = menu.querySelector('[data-recent-label]');
    if (rest) rest.hidden = group.hidden;
  }
  seen = [here].concat(seen.filter(function (id) { return id !== here; })).slice(0, KEEP + 1);
  try { localStorage.setItem(store, JSON.stringify(seen)); } catch (e) { /* not kept */ }
})();
