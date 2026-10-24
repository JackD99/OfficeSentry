/* The office board (board.html): the clock ticks in the MSP's timezone, and when there are more clients than fit
   on the screen, the ones that need someone stay and the quiet ones take turns in the space left, a screenful
   every TURN. The page reloads itself for fresh figures (a meta refresh), so nothing here fetches anything. */
(function () {
  'use strict';
  var TURN = 15000;

  var clock = document.querySelector('[data-clock]');
  if (clock) {
    var format;
    try {
      format = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: clock.getAttribute('data-zone') });
    } catch (e) {
      format = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit' });
    }
    setInterval(function () { clock.textContent = format.format(new Date()); }, 5000);
  }

  var full = document.querySelector('[data-fullscreen]');
  if (full && document.documentElement.requestFullscreen) {
    full.hidden = !!document.fullscreenElement;
    full.addEventListener('click', function () { document.documentElement.requestFullscreen().catch(function () {}); });
    document.addEventListener('fullscreenchange', function () { full.hidden = !!document.fullscreenElement; });
  }

  var wall = document.querySelector('[data-wall]');
  var more = document.querySelector('[data-more]');
  if (!wall || !more) return;
  var stamps = Array.prototype.slice.call(wall.querySelectorAll('.stamp:not([data-more])'));
  var foot = document.querySelector('.board-foot');
  var turn = 0;

  // How many stamps fit: the wall's columns times the rows left above the footer.
  function room() {
    var style = getComputedStyle(wall);
    var columns = style.gridTemplateColumns.split(' ').filter(Boolean).length || 1;
    var gap = parseFloat(style.rowGap) || 0;
    var height = stamps[0].getBoundingClientRect().height + gap;
    var left = window.innerHeight - wall.getBoundingClientRect().top - (foot ? foot.offsetHeight : 0) - 16;
    return { fits: columns * Math.max(1, Math.floor((left + gap) / height)), columns: columns };
  }

  function show() {
    stamps.forEach(function (s) { s.hidden = false; });
    more.hidden = true;
    if (!stamps.length || window.matchMedia('(max-width: 640px)').matches) return;  // a phone scrolls instead
    var space = room();
    var fits = space.fits;
    if (stamps.length <= fits) return;
    // The stamps that need someone stay, worst first (one place is kept for "more"). When they don't all fit,
    // all but the last row stay and the rest take turns in that row with the quiet ones.
    var needing = stamps.filter(function (s) { return !s.hasAttribute('data-quiet'); }).length;
    var pinned = needing <= fits - 1 ? needing : Math.max(0, fits - 1 - space.columns);
    var kept = stamps.slice(0, pinned);
    var rest = stamps.slice(pinned);
    var places = Math.max(0, fits - 1 - kept.length);
    var start = rest.length ? (turn * places) % rest.length : 0;
    var shown = [];
    for (var i = 0; i < Math.min(places, rest.length); i++) shown.push(rest[(start + i) % rest.length]);
    rest.forEach(function (s) { s.hidden = shown.indexOf(s) < 0; });
    var hidden = rest.length - shown.length;
    var quiet = rest.every(function (s) { return s.hasAttribute('data-quiet') || shown.indexOf(s) >= 0; });
    more.querySelector('[data-more-text]').textContent = '… ' + hidden + ' more' + (quiet ? ', nothing new' : '');
    more.hidden = false;
  }

  show();
  window.addEventListener('resize', show);
  setInterval(function () { turn += 1; show(); }, TURN);
})();
