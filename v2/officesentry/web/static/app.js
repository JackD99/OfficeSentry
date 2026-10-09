/* Every portal page. No dependencies.
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
