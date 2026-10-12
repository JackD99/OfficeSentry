/* The month-end review screen (reporting_review.html). "Leave out", "Put back" and the ticks under "What's in
 * the report" are sent with fetch, so the page stays where it is: every item on the page (data-item) is greyed
 * out or not, its button and tick follow, the count at the top changes and the preview of the client's copy is
 * drawn again at the same place. Without script the forms post and come back to the same spot. */
(function () {
  if (!window.fetch || !window.FormData) return;
  var frame = document.getElementById('review-preview');
  var pick = document.getElementById('preview-report');
  var open = document.getElementById('preview-open');
  var banner = document.getElementById('left-out');

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  // The preview's address for one of the plan's reports (a key like "review" or "design:3").
  function previewUrl(report) { return frame.dataset.base + '?report=' + encodeURIComponent(report); }

  // A second frame loads behind the first and takes its place once drawn, at the same scroll position.
  // Without a report it draws the one showing again.
  function redraw(report) {
    if (!frame) return;
    var y = 0;
    try { if (!report) y = frame.contentWindow.scrollY; } catch (e) { y = 0; }
    var next = frame.cloneNode(false);
    next.removeAttribute('id');
    next.removeAttribute('loading');
    next.classList.add('loading');
    next.dataset.report = report || frame.dataset.report;
    next.src = previewUrl(next.dataset.report) + '&v=' + Date.now();
    next.addEventListener('load', function () {
      try { next.contentWindow.scrollTo(0, y); } catch (e) { /* nothing to restore */ }
      if (!next.parentNode) return;
      var old = frame;
      next.id = 'review-preview';
      next.classList.remove('loading');
      frame = next;
      if (old.parentNode) old.parentNode.removeChild(old);
    }, { once: true });
    frame.parentNode.appendChild(next);
  }

  function show(leftOut) {
    var out = {};
    leftOut.forEach(function (item) { out[item] = true; });
    document.querySelectorAll('[data-item]').forEach(function (el) {
      el.classList.toggle('left-out', !!out[el.getAttribute('data-item')]);
    });
    document.querySelectorAll('form.leave-form').forEach(function (form) {
      var item = form.querySelector('input[name=item]').value;
      var button = form.querySelector('button[name=op]');
      var what = button.getAttribute('data-what') || '';
      button.value = out[item] ? 'back' : 'out';
      button.textContent = out[item] ? 'Put back' : 'Leave out';
      button.setAttribute('aria-label', (out[item] ? 'Put back: ' : 'Leave out: ') + what);
      button.title = out[item] ? "Put it back in this month's report" : "Leave it out of this month's report. Nothing else changes.";
    });
    document.querySelectorAll('input[type=checkbox][name=shown]').forEach(function (box) { box.checked = !out[box.value]; });
    if (banner) {
      banner.hidden = !leftOut.length;
      var count = banner.querySelector('[data-left-out-count]');
      if (count) count.textContent = plural(leftOut.length, 'thing');
    }
  }

  function send(form, submitter) {
    var body = new FormData(form);
    if (submitter && submitter.name) body.set(submitter.name, submitter.value);
    form.setAttribute('aria-busy', 'true');
    return fetch(form.action, { method: 'POST', body: body, credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error('status ' + r.status);
        return r.json();
      })
      .then(function (answer) { show(answer.left_out || []); redraw(); })
      .catch(function () {  // the page's own way: post and come back here
        if (submitter && submitter.name) {
          var op = document.createElement('input');
          op.type = 'hidden'; op.name = submitter.name; op.value = submitter.value;
          form.appendChild(op);
        }
        form.submit();
      })
      .then(function () { form.removeAttribute('aria-busy'); });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target.closest && e.target.closest('form[data-leave-out]');
    if (!form) return;
    e.preventDefault();
    if (form.getAttribute('aria-busy') === 'true') return;
    send(form, e.submitter);
  });
  document.querySelectorAll('form[data-autosave]').forEach(function (form) {
    form.querySelectorAll('[data-save]').forEach(function (button) { button.hidden = true; });
    form.addEventListener('change', function () { send(form, null); });
  });

  if (pick) {
    pick.addEventListener('change', function () {
      if (open && frame) open.href = previewUrl(pick.value);
      redraw(pick.value);
    });
  }
})();
