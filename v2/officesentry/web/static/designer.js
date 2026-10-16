/* The report design editor (web/designs.py, design_editor.html). No dependencies.
 * The document lives in `doc`; every change redraws the layout list, saves after a short pause (JSON,
 * with the CSRF token) and reloads the preview on a real tenant. Blocks are dragged in from the palette
 * and reordered by drag, with buttons for the same (keyboards, phones). Every change can be undone
 * (the Undo and Redo buttons, Ctrl+Z and Ctrl+Shift+Z outside a text box). The preview is the report page
 * in an iframe (scaled to fit by app.js); clicking a block there opens its settings here. */
(function () {
  var root = document.getElementById('designer');
  if (!root) return;
  var data = JSON.parse(document.getElementById('designer-data').textContent);
  var doc = data.doc, widgets = {}, MAX_BLOCKS = data.max_blocks;
  data.palette.forEach(function (g) { g.widgets.forEach(function (w) { widgets[w.key] = w; }); });
  var icons = {};
  var iconTemplate = document.getElementById('designer-icons');
  if (iconTemplate) iconTemplate.content.querySelectorAll('[data-icon]').forEach(function (s) { icons[s.dataset.icon] = s.innerHTML; });
  var token = root.querySelector('input[name=csrf_token]');
  var list = document.getElementById('layout-list'), empty = document.getElementById('layout-empty');
  var status = document.getElementById('designer-status'), preview = document.getElementById('preview');
  var countEl = document.getElementById('block-count');
  var undoBtn = document.getElementById('undo'), redoBtn = document.getElementById('redo');
  var selected = null;
  var saveTimer = null, saving = false, dirty = false, seq = 0;
  var notice = '';  // said once the change is saved ("Removed Key figures. Undo brings it back.")

  function uid() { seq += 1; return (Date.now().toString(36) + seq.toString(36)).slice(-10); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function blockAt(id) { for (var i = 0; i < doc.blocks.length; i++) if (doc.blocks[i].id === id) return i; return -1; }
  function defaults(w) {
    var s = {};
    w.fields.forEach(function (f) { s[f.key] = Array.isArray(f.default) ? f.default.slice() : f.default; });
    return s;
  }
  // A number box's value within its field's range. 0 is kept (several fields say what 0 does); only an empty
  // or unreadable box falls back to the default.
  function inRange(f, text) {
    var n = parseInt(text, 10);
    if (isNaN(n)) n = f.default == null ? f.min : f.default;
    return Math.max(f.min, Math.min(f.max, n));
  }
  function summary(b) {
    var w = widgets[b.kind], parts = [];
    if (b.settings.heading && w.fields.some(function (f) { return f.key === 'heading'; })) parts.push(b.settings.heading);
    w.fields.forEach(function (f) {
      var v = b.settings[f.key];
      if (f.key === 'heading' || f.type === 'textarea' || f.type === 'checkbox') return;
      if (f.type === 'select') { var o = f.options.filter(function (x) { return String(x[0]) === String(v); })[0]; if (o && f.key !== 'style') parts.push(o[1]); }
      else if (f.type === 'multiselect' && Array.isArray(v) && v.length && v.length < f.options.length) {
        parts.push(f.options.filter(function (x) { return v.indexOf(x[0]) !== -1; }).map(function (x) { return x[1]; }).join(', '));
      }
    });
    return parts.join(' · ');
  }

  // -- Undo and redo ---------------------------------------------------------------------------------
  // A snapshot of the document is kept before each change. Typing in one box is one step, not a letter each.
  var past = [], future = [], lastKey = null, lastAt = 0, LIMIT = 100;
  function snapshot() { return JSON.stringify({ settings: doc.settings, blocks: doc.blocks }); }
  function remember(key) {
    var now = Date.now();
    if (key && key === lastKey && now - lastAt < 1500) { lastAt = now; return; }
    lastKey = key || null; lastAt = now;
    past.push(snapshot());
    if (past.length > LIMIT) past.shift();
    future = [];
    historyButtons();
  }
  function historyButtons() {
    if (undoBtn) undoBtn.disabled = !past.length;
    if (redoBtn) redoBtn.disabled = !future.length;
  }
  function travel(from, to) {
    if (!from.length) return;
    to.push(snapshot());
    var s = JSON.parse(from.pop());
    doc.settings = s.settings; doc.blocks = s.blocks;
    lastKey = null;
    if (selected !== null && blockAt(selected) === -1) selected = null;
    syncCover();
    changed(selected, true);
    historyButtons();
  }
  function undo() { travel(past, future); }
  function redo() { travel(future, past); }
  if (undoBtn) undoBtn.addEventListener('click', undo);
  if (redoBtn) redoBtn.addEventListener('click', redo);
  document.addEventListener('keydown', function (e) {
    if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
    var k = e.key.toLowerCase();
    if (k !== 'z' && k !== 'y') return;
    var t = e.target;
    // A text box keeps its own undo for what's being typed.
    if (t && (t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && /^(text|search|number)$/.test(t.type)))) return;
    e.preventDefault();
    if (k === 'y' || e.shiftKey) redo(); else undo();
  });

  // -- Drawing the layout list ---------------------------------------------------------------------
  // A choice of a few values (Per row: 2, 3 or 4; Width) is a row of buttons rather than a box to type in.
  function segmented(name, label, options, value, onChange, help) {
    var wrap = el('fieldset', 'seg-field');
    wrap.appendChild(el('legend', '', label));
    var seg = el('span', 'seg sm');
    options.forEach(function (o) {
      var lab = el('label'), r = el('input');
      r.type = 'radio'; r.name = name; r.value = o[0]; r.id = name + '-' + o[0]; r.checked = String(o[0]) === String(value);
      r.addEventListener('change', function () { if (r.checked) onChange(o[0], r.id); });
      lab.appendChild(r); lab.appendChild(el('span', '', o[1])); seg.appendChild(lab);
    });
    wrap.appendChild(seg);
    if (help) wrap.appendChild(el('span', 'hint', help));
    return wrap;
  }

  function field(b, f) {
    var wrap = el('label'), id = 'f-' + b.id + '-' + f.key, v = b.settings[f.key];
    if (f.type === 'checkbox') {
      wrap.className = 'check';
      var c = el('input'); c.type = 'checkbox'; c.id = id; c.checked = !!v;
      c.addEventListener('change', function () { remember(); b.settings[f.key] = c.checked; changed(b.id, false); });
      wrap.appendChild(c); wrap.appendChild(document.createTextNode(' ' + f.label));
      return wrap;
    }
    if (f.type === 'number' && f.max - f.min <= 4) {
      var values = [];
      for (var n = f.min; n <= f.max; n++) values.push([n, String(n)]);
      return segmented(id, f.label, values, v, function (value, focusId) {
        remember(); b.settings[f.key] = value; changed(b.id, false);
      }, f.help);
    }
    wrap.appendChild(document.createTextNode(f.label));
    var input;
    if (f.type === 'multiselect') {
      wrap = el('fieldset', 'multi');
      wrap.appendChild(el('legend', '', f.label));
      f.options.forEach(function (o) {
        var lab = el('label', 'check'), c = el('input');
        c.type = 'checkbox'; c.value = o[0]; c.checked = (v || []).indexOf(o[0]) !== -1;
        c.addEventListener('change', function () {
          var chosen = [];
          wrap.querySelectorAll('input:checked').forEach(function (x) { chosen.push(x.value); });
          remember(); b.settings[f.key] = chosen; changed(b.id, false);
        });
        lab.appendChild(c); lab.appendChild(document.createTextNode(' ' + o[1])); wrap.appendChild(lab);
      });
    } else if (f.type === 'select') {
      input = el('select');
      f.options.forEach(function (o) { var op = el('option', '', o[1]); op.value = o[0]; if (String(o[0]) === String(v)) op.selected = true; input.appendChild(op); });
    } else if (f.type === 'textarea') {
      input = el('textarea'); input.rows = 5; input.value = v || '';
    } else {
      input = el('input'); input.type = f.type === 'number' ? 'number' : 'text'; input.value = v == null ? '' : v;
      if (f.type === 'number') { input.min = f.min; input.max = f.max; } else input.maxLength = f.type === 'text' ? 200 : 4000;
    }
    if (input) {
      input.id = id;
      input.addEventListener(input.tagName === 'SELECT' ? 'change' : 'input', function () {
        remember(input.tagName === 'SELECT' ? null : id);
        b.settings[f.key] = f.type === 'number' ? inRange(f, input.value) : input.value;
        changed(b.id, false);
      });
      // Leaving a number box shows what was kept (a number past its range is saved as the nearest end).
      if (f.type === 'number') input.addEventListener('change', function () { input.value = b.settings[f.key]; });
      wrap.appendChild(input);
    }
    if (f.help) wrap.appendChild(el('span', 'hint', f.help));
    return wrap;
  }

  function iconButton(icon, title, label, fn, disabled) {
    var btn = el('button', 'sm ghost icon-btn'); btn.type = 'button'; btn.title = title; btn.setAttribute('aria-label', label);
    btn.innerHTML = icons[icon] || '';
    if (disabled) btn.disabled = true;
    btn.addEventListener('click', function (e) { e.stopPropagation(); fn(); });
    return btn;
  }

  function setWidth(b, width, focusId) {
    if (b.width === width) return;
    remember(); b.width = width; changed(b.id, true);
    if (focusId) { var r = document.getElementById(focusId); if (r) r.focus(); }
  }

  function card(b, i) {
    var w = widgets[b.kind], half = b.width === 'half';
    var li = el('li', 'layout-card' + (b.id === selected ? ' selected' : '') + (half ? ' half' : ''));
    li.dataset.id = b.id; li.draggable = true;
    var head = el('div', 'layout-head');
    var grip = el('span', 'layout-grip'); grip.setAttribute('aria-hidden', 'true'); grip.innerHTML = gripSvg; head.appendChild(grip);
    var open = el('button', 'layout-open'); open.type = 'button'; open.setAttribute('aria-expanded', b.id === selected ? 'true' : 'false');
    open.appendChild(el('span', 'layout-title', w.title));
    var sub = summary(b); if (sub) open.appendChild(el('span', 'layout-sub', sub));
    if (half) open.appendChild(el('span', 'layout-width', 'Half width'));
    open.addEventListener('click', function () { select(b.id === selected ? null : b.id); });
    head.appendChild(open);
    var actions = el('div', 'layout-actions'), name = ': ' + w.title;
    actions.appendChild(iconButton('up', 'Move up', 'Move up' + name, function () { move(i, i - 1); }, i === 0));
    actions.appendChild(iconButton('down', 'Move down', 'Move down' + name, function () { move(i, i + 1); }, i === doc.blocks.length - 1));
    if (w.halfable) {
      var widthBtn = iconButton('half', half ? 'Half width: press for full width' : 'Make it half width, beside the next block',
                                'Half width' + name, function () { setWidth(b, half ? 'full' : 'half'); });
      widthBtn.setAttribute('aria-pressed', half ? 'true' : 'false');
      actions.appendChild(widthBtn);
    }
    actions.appendChild(iconButton('copy', 'Duplicate', 'Duplicate' + name, function () {
      if (full()) return;
      remember();
      var copy = JSON.parse(JSON.stringify(b)); copy.id = uid(); doc.blocks.splice(i + 1, 0, copy); selected = copy.id; changed(copy.id, true);
    }));
    actions.appendChild(iconButton('close', 'Remove (Undo brings it back)', 'Remove' + name, function () {
      remember(); doc.blocks.splice(i, 1); if (selected === b.id) selected = null; changed(null, true);
      notice = 'Removed ' + w.title + '. Undo brings it back.';
    }));
    head.appendChild(actions);
    li.appendChild(head);
    if (b.id === selected) {
      var settings = el('div', 'layout-settings');
      if (!w.fields.length && !w.halfable) settings.appendChild(el('p', 'hint m0', w.description));
      w.fields.forEach(function (f) { settings.appendChild(field(b, f)); });
      if (w.halfable) {
        settings.appendChild(segmented('w-' + b.id, 'Width', [['full', 'Full width'], ['half', 'Half width']], b.width || 'full',
          function (value, focusId) { setWidth(b, value, focusId); }, 'Two half-width blocks in a row sit side by side.'));
      }
      li.appendChild(settings);
    }
    return li;
  }

  function draw() {
    list.innerHTML = '';
    doc.blocks.forEach(function (b, i) { if (widgets[b.kind]) list.appendChild(card(b, i)); });
    empty.hidden = doc.blocks.length > 0;
    countEl.textContent = doc.blocks.length ? doc.blocks.length + (doc.blocks.length === 1 ? ' block' : ' blocks') +
      (doc.blocks.length >= MAX_BLOCKS ? ', the most a design holds' : '') : '';
    coverSub();
    markPreview();
  }

  function select(id, fromPreview) {
    selected = id;
    draw();
    if (id === null) return;
    var card = list.querySelector('[data-id="' + id + '"]');
    if (card) {
      card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      if (fromPreview) { var open = card.querySelector('.layout-open'); if (open) open.focus({ preventScroll: true }); }
    }
    if (!fromPreview) scrollPreviewTo(id);
  }

  function move(from, to) {
    if (to < 0 || to >= doc.blocks.length) return;
    remember();
    var b = doc.blocks.splice(from, 1)[0];
    doc.blocks.splice(to, 0, b);
    changed(b.id, true);
  }
  // A design holds MAX_BLOCKS blocks (designs.MAX_BLOCKS): another is refused with a word, never dropped later.
  function full() {
    if (doc.blocks.length < MAX_BLOCKS) return false;
    say('A design holds ' + MAX_BLOCKS + ' blocks at most. Remove one to add another.', 'failed');
    return true;
  }
  function add(kind, at) {
    var w = widgets[kind]; if (!w || full()) return;
    remember();
    var b = { id: uid(), kind: kind, width: 'full', settings: defaults(w) };
    if (at === undefined) { var s = blockAt(selected); at = s === -1 ? doc.blocks.length : s + 1; }
    doc.blocks.splice(at, 0, b);
    selected = b.id;
    changed(b.id, true);
    var li = list.querySelector('[data-id="' + b.id + '"] .layout-open'); if (li) li.focus();
  }

  // -- Saving and the preview ----------------------------------------------------------------------
  function say(text, cls) { status.textContent = text; status.className = 'designer-status' + (cls ? ' ' + cls : ''); }
  function coverSub() {
    var title = doc.settings.title || document.getElementById('design-name').value;
    document.getElementById('cover-sub').textContent = title + (doc.settings.cover === 'compact' ? ' · band on page one' : '');
  }
  function changed(focusId, redraw) {
    dirty = true; notice = '';
    if (redraw !== false) draw(); else coverSub();
    if (redraw !== false && focusId) { var open = list.querySelector('[data-id="' + focusId + '"] .layout-open'); if (open) open.setAttribute('aria-expanded', 'true'); }
    say('Unsaved changes', 'pending');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 700);
  }
  function save() {
    if (saving) { dirty = true; return; }
    saving = true; dirty = false;
    say('Saving…', 'pending');
    var body = { settings: doc.settings, blocks: doc.blocks, name: document.getElementById('design-name').value,
                 description: document.getElementById('design-description').value };
    fetch(root.dataset.save, { method: 'POST', credentials: 'same-origin', body: JSON.stringify(body),
                               headers: { 'Content-Type': 'application/json', 'Accept': 'application/json',
                                          'X-CSRFToken': token ? token.value : '' } })
      .then(function (r) {
        if (r.ok) return r.json();
        // A save the portal refuses (its answer says why) isn't tried again; anything else is.
        return r.json().catch(function () { return {}; }).then(function (answer) {
          var e = new Error(r.status); e.refused = answer.error; throw e;
        });
      })
      .then(function () {
        saving = false;
        if (dirty) { save(); return; }
        say(notice || 'Saved');
        reloadPreview();
      })
      .catch(function (e) {
        saving = false;
        if (e && e.refused) { say(e.refused, 'failed'); return; }
        say("Couldn't save. Trying again…", 'failed');
        saveTimer = setTimeout(save, 4000);
      });
  }

  // The client picked to preview, as a number: it only ever goes into a URL as one.
  function tenantId() {
    var pick = document.getElementById('preview-tenant');
    return pick ? parseInt(pick.value, 10) || 0 : 0;
  }

  // The preview reloads into a second frame that replaces the first once it has loaded at the same
  // scroll position, so the page doesn't flash white and jump back to the top on every change.
  function reloadPreview() {
    if (!preview || !data.tenant) return;
    var y = 0;
    try { y = preview.contentWindow.scrollY; } catch (e) { y = 0; }
    var next = preview.cloneNode(false);
    next.removeAttribute('id');
    next.classList.add('loading');
    next.src = root.dataset.preview + '?tenant=' + tenantId() + '&v=' + Date.now();
    next.addEventListener('load', function () {
      try { next.contentWindow.scrollTo(0, y); } catch (e) {}
      if (!next.parentNode) return;
      var old = preview;
      next.id = 'preview'; next.classList.remove('loading');
      preview = next;
      if (old.parentNode) old.parentNode.removeChild(old);
      wirePreview();
    }, { once: true });
    preview.parentNode.appendChild(next);
  }
  function previewDoc() { try { return preview.contentDocument; } catch (e) { return null; } }
  function scrollPreviewTo(id) {
    var d = previewDoc(); if (!d || !id) return;
    var target = d.getElementById('b' + id);
    if (target) preview.contentWindow.scrollTo({ top: target.getBoundingClientRect().top + preview.contentWindow.scrollY - 16, behavior: 'smooth' });
  }
  function markPreview() {
    var d = previewDoc(); if (!d || !d.body) return;
    d.querySelectorAll('.is-selected').forEach(function (x) { x.classList.remove('is-selected'); });
    var target = selected && d.getElementById('b' + selected);
    if (target) target.classList.add('is-selected');
  }
  // Clicking the preview picks the block under the pointer (or the cover); its links don't navigate.
  function wirePreview() {
    var d = previewDoc(); if (!d || !d.body || d.body.dataset.wired || d.location.href === 'about:blank') return;
    d.body.dataset.wired = '1';
    d.body.classList.add('editable');
    d.addEventListener('click', function (e) {
      if (e.target.closest('a')) e.preventDefault();
      var hit = e.target.closest('.block[id], .block-note[id]');
      if (hit && hit.id.charAt(0) === 'b' && blockAt(hit.id.slice(1)) !== -1) { select(hit.id.slice(1), true); return; }
      if (e.target.closest('.cover')) openCover(true);
    });
    markPreview();
  }
  if (preview) {
    preview.addEventListener('load', wirePreview, { once: true });
    if (previewDoc() && previewDoc().readyState === 'complete') wirePreview();
  }

  // -- Drag and drop ---------------------------------------------------------------------------------
  var placeholder = el('li', 'layout-placeholder'), dragging = null;
  function indexFromPointer(y) {
    var cards = Array.prototype.slice.call(list.querySelectorAll('.layout-card'));
    for (var i = 0; i < cards.length; i++) {
      var r = cards[i].getBoundingClientRect();
      if (y < r.top + r.height / 2) return i;
    }
    return cards.length;
  }
  document.querySelectorAll('.palette-item').forEach(function (item) {
    item.addEventListener('dragstart', function (e) {
      dragging = { kind: item.dataset.kind };
      e.dataTransfer.setData('text/plain', 'new:' + item.dataset.kind);
      e.dataTransfer.effectAllowed = 'copy';
    });
    item.addEventListener('dragend', function () { dragging = null; placeholder.remove(); });
  });
  document.querySelectorAll('.palette-add').forEach(function (btn) {
    btn.addEventListener('click', function () { add(btn.dataset.add); });
  });
  list.addEventListener('dragstart', function (e) {
    var li = e.target.closest && e.target.closest('.layout-card');
    if (!li) return;
    dragging = { id: li.dataset.id };
    e.dataTransfer.setData('text/plain', 'move:' + li.dataset.id);
    e.dataTransfer.effectAllowed = 'move';
    li.classList.add('dragging');
  });
  list.addEventListener('dragend', function (e) {
    var li = e.target.closest && e.target.closest('.layout-card');
    if (li) li.classList.remove('dragging');
    dragging = null; placeholder.remove();
  });
  function over(e) {
    if (!dragging) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = dragging.kind ? 'copy' : 'move';
    var i = indexFromPointer(e.clientY), cards = list.querySelectorAll('.layout-card');
    if (i >= cards.length) list.appendChild(placeholder); else list.insertBefore(placeholder, cards[i]);
    empty.hidden = true;
  }
  function drop(e) {
    if (!dragging) return;
    e.preventDefault();
    var i = indexFromPointer(e.clientY);
    placeholder.remove();
    if (dragging.kind) { add(dragging.kind, i); }
    else {
      var from = blockAt(dragging.id);
      if (from !== -1) { if (i > from) i -= 1; if (i !== from) move(from, i); else draw(); }
    }
    dragging = null;
  }
  var layout = document.querySelector('.designer-layout');
  layout.addEventListener('dragover', over);
  layout.addEventListener('drop', drop);
  layout.addEventListener('dragleave', function (e) { if (!layout.contains(e.relatedTarget)) { placeholder.remove(); empty.hidden = doc.blocks.length > 0; } });

  // -- Finding a block -------------------------------------------------------------------------------
  var search = document.getElementById('palette-search');
  if (search) search.addEventListener('input', function () {
    var words = search.value.toLowerCase().split(/\s+/).filter(Boolean), any = false;
    document.querySelectorAll('.palette-group').forEach(function (g) {
      var shown = 0;
      g.querySelectorAll('.palette-item').forEach(function (item) {
        var text = item.textContent.toLowerCase();
        var hit = words.every(function (w) { return text.indexOf(w) !== -1; });
        item.hidden = !hit; if (hit) shown += 1;
      });
      g.hidden = !shown; if (shown) any = true;
    });
    document.getElementById('palette-none').hidden = any;
  });

  // -- The cover, the options and the name ------------------------------------------------------------
  var coverToggle = document.getElementById('cover-toggle'), coverSettings = document.getElementById('cover-settings');
  function openCover(open) {
    coverSettings.hidden = !open; coverToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) document.getElementById('cover-card').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  coverToggle.addEventListener('click', function () { openCover(coverSettings.hidden); });
  var bound = [];
  function bindCover(id, key, isCheck) {
    var input = document.getElementById(id);
    if (isCheck) input.addEventListener('change', function () { remember(); doc.settings[key] = input.checked; changed(null, false); });
    else input.addEventListener('input', function () { remember('cover-' + key); doc.settings[key] = input.value; changed(null, false); });
    bound.push(function () { if (isCheck) input.checked = !!doc.settings[key]; else input.value = doc.settings[key] || ''; });
  }
  bindCover('cover-title', 'title'); bindCover('cover-subtitle', 'subtitle');
  bindCover('cover-verdict', 'cover_verdict', true); bindCover('cover-contents', 'contents', true);
  bindCover('cover-notes', 'notes', true);
  var styles = document.querySelectorAll('input[name=cover-style]');
  styles.forEach(function (r) {
    r.addEventListener('change', function () {
      if (!r.checked) return;
      remember(); doc.settings.cover = r.value; syncCover(); changed(null, false);
    });
  });
  function syncCover() {
    bound.forEach(function (fn) { fn(); });
    var style = doc.settings.cover || 'full';
    styles.forEach(function (r) { r.checked = r.value === style; });
    // The contents list is on a cover page of its own; a band has no room for it.
    document.getElementById('cover-contents-label').hidden = style === 'compact';
  }
  syncCover();
  document.getElementById('design-name').addEventListener('input', function () { changed(null, false); });
  document.getElementById('design-description').addEventListener('input', function () { changed(null, false); });
  var tenantPick = document.getElementById('preview-tenant');
  if (tenantPick) tenantPick.addEventListener('change', function () {
    data.tenant = tenantId();
    ['open-pdf', 'open-page'].forEach(function (id) {
      var a = document.getElementById(id);
      if (a) a.href = a.dataset.pattern.replace('/tenants/0/', '/tenants/' + tenantId() + '/');
    });
    history.replaceState(null, '', location.pathname + '?tenant=' + tenantId());
    reloadPreview();
  });
  window.addEventListener('beforeunload', function (e) { if (dirty || saving) { e.preventDefault(); e.returnValue = ''; } });

  var gripSvg = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>';
  draw();
})();
