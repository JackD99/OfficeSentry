/* Sortable, filterable tables and the report picker. No dependencies.
 * Sorting: <table class="sortable"> with <button class="sort"> in each <th>; cells may carry data-sort.
 * Filtering: <input data-filter="table-id"> matches each row's data-name.
 * Report pickers: <form data-autosubmit> submits when its <select> changes.
 * Report search: <input data-report-search="list-id"> filters the list's <li data-name>, hiding empty groups. */
(function () {
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
  document.querySelectorAll('table.sortable').forEach(function (table) {
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
      });
    });
  });
  document.querySelectorAll('input[data-filter]').forEach(function (input) {
    var table = document.getElementById(input.getAttribute('data-filter'));
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase();
      table.querySelectorAll('tbody tr').forEach(function (tr) {
        tr.hidden = q && (tr.getAttribute('data-name') || '').indexOf(q) === -1;
      });
    });
  });
  document.querySelectorAll('input[data-report-search]').forEach(function (input) {
    var list = document.getElementById(input.getAttribute('data-report-search'));
    var none = list.parentNode.querySelector('.no-match');
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase(), shown = 0;
      list.querySelectorAll('.report-group').forEach(function (group) {
        var visible = 0;
        group.querySelectorAll('li').forEach(function (li) {
          li.hidden = q && li.getAttribute('data-name').indexOf(q) === -1;
          if (!li.hidden) visible++;
        });
        group.hidden = !visible;
        shown += visible;
      });
      if (none) none.hidden = shown > 0;
    });
  });
  document.querySelectorAll('form[data-autosubmit]').forEach(function (form) {
    form.querySelector('select').addEventListener('change', function () { form.submit(); });
  });
})();
