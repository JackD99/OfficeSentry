/* The report design editor (web/designs.py, design_editor.html). No dependencies.
 * The document lives in `doc`; every change redraws the layout list, saves after a short pause (JSON,
 * with the CSRF token) and reloads the preview on a real tenant. Blocks are dragged in from the palette
 * and reordered by drag, with buttons for the same (keyboards, phones). */
(function () {
  var root = document.getElementById('designer');
  if (!root) return;
  var data = JSON.parse(document.getElementById('designer-data').textContent);
  var doc = data.doc, widgets = {}, order = [];
  data.palette.forEach(function (g) { g.widgets.forEach(function (w) { widgets[w.key] = w; order.push(w.key); }); });
  var token = root.querySelector('input[name=csrf_token]');
  var list = document.getElementById('layout-list'), empty = document.getElementById('layout-empty');
  var status = document.getElementById('designer-status'), preview = document.getElementById('preview');
  var countEl = document.getElementById('block-count');
  var selected = null;
  var saveTimer = null, saving = false, dirty = false, seq = 0;

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

  // -- Drawing the layout list ---------------------------------------------------------------------
  function field(b, f) {
    var wrap = el('label'), id = 'f-' + b.id + '-' + f.key, v = b.settings[f.key];
    if (f.type === 'checkbox') {
      wrap.className = 'check';
      var c = el('input'); c.type = 'checkbox'; c.id = id; c.checked = !!v;
      c.addEventListener('change', function () { b.settings[f.key] = c.checked; changed(b.id, false); });
      wrap.appendChild(c); wrap.appendChild(document.createTextNode(' ' + f.label));
      return wrap;
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
          b.settings[f.key] = chosen; changed(b.id, false);
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
      input.addEventListener('input', function () {
        b.settings[f.key] = f.type === 'number' ? Math.max(f.min, Math.min(f.max, parseInt(input.value, 10) || f.default || f.min)) : input.value;
        changed(b.id, false);
      });
      wrap.appendChild(input);
    }
    if (f.help) wrap.appendChild(el('span', 'hint', f.help));
    return wrap;
  }

  function card(b, i) {
    var w = widgets[b.kind], li = el('li', 'layout-card' + (b.id === selected ? ' selected' : '') + (b.width === 'half' ? ' half' : ''));
    li.dataset.id = b.id; li.draggable = true;
    var head = el('div', 'layout-head');
    var grip = el('span', 'layout-grip'); grip.setAttribute('aria-hidden', 'true'); grip.innerHTML = gripSvg; head.appendChild(grip);
    var open = el('button', 'layout-open'); open.type = 'button'; open.setAttribute('aria-expanded', b.id === selected ? 'true' : 'false');
    open.appendChild(el('span', 'layout-title', w.title));
    var sub = summary(b); if (sub) open.appendChild(el('span', 'layout-sub', sub));
    if (b.width === 'half') open.appendChild(el('span', 'layout-width', 'Half width'));
    open.addEventListener('click', function () { selected = b.id === selected ? null : b.id; draw(); scrollPreviewTo(b.id); });
    head.appendChild(open);
    var actions = el('div', 'layout-actions');
    function action(label, title, fn, disabled) {
      var btn = el('button', 'sm ghost', label); btn.type = 'button'; btn.title = title; btn.setAttribute('aria-label', title + ': ' + w.title);
      if (disabled) btn.disabled = true;
      btn.addEventListener('click', function (e) { e.stopPropagation(); fn(); });
      actions.appendChild(btn);
    }
    action('↑', 'Move up', function () { move(i, i - 1); }, i === 0);
    action('↓', 'Move down', function () { move(i, i + 1); }, i === doc.blocks.length - 1);
    if (w.halfable) action(b.width === 'half' ? '⬜' : '◧', b.width === 'half' ? 'Make full width' : 'Make half width', function () { b.width = b.width === 'half' ? 'full' : 'half'; changed(b.id, true); });
    action('⧉', 'Duplicate', function () { var copy = JSON.parse(JSON.stringify(b)); copy.id = uid(); doc.blocks.splice(i + 1, 0, copy); selected = copy.id; changed(copy.id, true); });
    action('×', 'Remove', function () { doc.blocks.splice(i, 1); if (selected === b.id) selected = null; changed(null, true); });
    head.appendChild(actions);
    li.appendChild(head);
    if (b.id === selected) {
      var settings = el('div', 'layout-settings');
      if (!w.fields.length) settings.appendChild(el('p', 'hint m0', w.description));
      w.fields.forEach(function (f) { settings.appendChild(field(b, f)); });
      li.appendChild(settings);
    }
    return li;
  }

  function draw() {
    list.innerHTML = '';
    doc.blocks.forEach(function (b, i) { if (widgets[b.kind]) list.appendChild(card(b, i)); });
    empty.hidden = doc.blocks.length > 0;
    countEl.textContent = doc.blocks.length ? doc.blocks.length + (doc.blocks.length === 1 ? ' block' : ' blocks') : '';
    document.getElementById('cover-sub').textContent = doc.settings.title || document.getElementById('design-name').value;
  }

  function move(from, to) {
    if (to < 0 || to >= doc.blocks.length) return;
    var b = doc.blocks.splice(from, 1)[0];
    doc.blocks.splice(to, 0, b);
    changed(b.id, true);
  }
  function add(kind, at) {
    var w = widgets[kind]; if (!w) return;
    var b = { id: uid(), kind: kind, width: 'full', settings: defaults(w) };
    if (at === undefined) { var s = blockAt(selected); at = s === -1 ? doc.blocks.length : s + 1; }
    doc.blocks.splice(at, 0, b);
    selected = b.id;
    changed(b.id, true);
    var li = list.querySelector('[data-id="' + b.id + '"] .layout-open'); if (li) li.focus();
  }

  // -- Saving and the preview ----------------------------------------------------------------------
  function changed(focusId, redraw) {
    dirty = true;
    if (redraw !== false) draw(); else document.getElementById('cover-sub').textContent = doc.settings.title || document.getElementById('design-name').value;
    if (redraw !== false && focusId) { var open = list.querySelector('[data-id="' + focusId + '"] .layout-open'); if (open) open.setAttribute('aria-expanded', 'true'); }
    status.textContent = 'Unsaved changes'; status.className = 'designer-status pending';
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 700);
  }
  function save() {
    if (saving) { dirty = true; return; }
    saving = true; dirty = false;
    status.textContent = 'Saving…'; status.className = 'designer-status pending';
    var body = { settings: doc.settings, blocks: doc.blocks, name: document.getElementById('design-name').value,
                 description: document.getElementById('design-description').value };
    fetch(root.dataset.save, { method: 'POST', credentials: 'same-origin', body: JSON.stringify(body),
                               headers: { 'Content-Type': 'application/json', 'Accept': 'application/json',
                                          'X-CSRFToken': token ? token.value : '' } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) {
        saving = false;
        if (dirty) { save(); return; }
        status.textContent = 'Saved'; status.className = 'designer-status';
        reloadPreview();
      })
      .catch(function () {
        saving = false;
        status.textContent = "Couldn't save. Trying again…"; status.className = 'designer-status failed';
        saveTimer = setTimeout(save, 4000);
      });
  }
  var scrollTo = null;
  function reloadPreview() {
    if (!preview || !data.tenant) return;
    try { scrollTo = preview.contentWindow.scrollY; } catch (e) { scrollTo = null; }
    var url = root.dataset.preview + '?tenant=' + document.getElementById('preview-tenant').value + '&v=' + Date.now();
    preview.src = url;
  }
  preview && preview.addEventListener('load', function () {
    if (scrollTo !== null) { try { preview.contentWindow.scrollTo(0, scrollTo); } catch (e) {} }
    scrollTo = null;
  });
  function scrollPreviewTo(id) {
    if (!preview || !id) return;
    try {
      var win = preview.contentWindow, target = preview.contentDocument && preview.contentDocument.getElementById('b' + id);
      if (target) win.scrollTo({ top: target.getBoundingClientRect().top + win.scrollY - 12, behavior: 'smooth' });
    } catch (e) {}
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

  // -- The cover and the name -------------------------------------------------------------------------
  var coverToggle = document.getElementById('cover-toggle'), coverSettings = document.getElementById('cover-settings');
  coverToggle.addEventListener('click', function () {
    var open = coverSettings.hidden; coverSettings.hidden = !open; coverToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  function bindCover(id, key, isCheck) {
    var input = document.getElementById(id);
    if (isCheck) { input.checked = !!doc.settings[key]; input.addEventListener('change', function () { doc.settings[key] = input.checked; changed(null, false); }); }
    else { input.value = doc.settings[key] || ''; input.addEventListener('input', function () { doc.settings[key] = input.value; changed(null, false); }); }
  }
  bindCover('cover-title', 'title'); bindCover('cover-subtitle', 'subtitle');
  bindCover('cover-verdict', 'cover_verdict', true); bindCover('cover-contents', 'contents', true);
  document.getElementById('design-name').addEventListener('input', function () { changed(null, false); });
  document.getElementById('design-description').addEventListener('input', function () { changed(null, false); });
  var tenantPick = document.getElementById('preview-tenant');
  if (tenantPick) tenantPick.addEventListener('change', function () {
    data.tenant = tenantPick.value;
    var pdf = document.getElementById('open-pdf');
    if (pdf) pdf.href = pdf.dataset.pattern.replace('/tenants/0/', '/tenants/' + tenantPick.value + '/');
    history.replaceState(null, '', location.pathname + '?tenant=' + tenantPick.value);
    reloadPreview();
  });
  window.addEventListener('beforeunload', function (e) { if (dirty || saving) { e.preventDefault(); e.returnValue = ''; } });

  var gripSvg = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>';
  draw();
})();
