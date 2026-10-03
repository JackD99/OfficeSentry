/* Browser-made PDFs (web/printing.py): open the print window once the page and its fonts have loaded. */
(function () {
  function print() { window.print(); }
  document.querySelectorAll('[data-print]').forEach(function (b) { b.addEventListener('click', print); });
  document.querySelectorAll('[data-back]').forEach(function (b) {
    b.addEventListener('click', function () { if (history.length > 1) history.back(); else window.close(); });
  });
  window.addEventListener('load', function () {
    (document.fonts ? document.fonts.ready : Promise.resolve()).then(function () { setTimeout(print, 250); });
  });
})();
