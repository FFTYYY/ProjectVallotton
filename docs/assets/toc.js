// The left-hand "本页" list: a small triangle marks the section the reader
// is in, and the list scrolls on its own so that entry stays in view.
(function () {
  var list = document.querySelector('aside.toc .toc-page');
  if (!list) return;
  var pairs = [];
  Array.prototype.forEach.call(list.querySelectorAll('a[href^="#"]'), function (a) {
    var el = document.getElementById(decodeURIComponent(a.getAttribute('href').slice(1)));
    if (el) pairs.push({ a: a, el: el });
  });
  if (!pairs.length) return;

  var LINE = 100;   // a heading above this line (just under the sticky header) has been reached
  var current = null;
  var queued = false;

  function pick() {
    queued = false;
    var chosen = null;
    for (var i = 0; i < pairs.length; i++) {
      if (pairs[i].el.getBoundingClientRect().top <= LINE) chosen = pairs[i]; else break;
    }
    // at the very bottom the last section counts as the one being read, even if it is short
    if (window.innerHeight + window.pageYOffset >= document.documentElement.scrollHeight - 2) chosen = pairs[pairs.length - 1];
    if (chosen === current) return;
    if (current) current.a.classList.remove('now');
    current = chosen;
    if (!current) return;
    current.a.classList.add('now');
    // keep the marked entry visible inside the list, without touching the page's own scroll
    var top = current.a.offsetTop, bottom = top + current.a.offsetHeight, pad = 12;
    if (top < list.scrollTop + pad) list.scrollTop = top - pad;
    else if (bottom > list.scrollTop + list.clientHeight - pad) list.scrollTop = bottom - list.clientHeight + pad;
  }
  function schedule() {
    if (queued) return;
    queued = true;
    window.requestAnimationFrame(pick);
  }
  window.addEventListener('scroll', schedule, { passive: true });
  window.addEventListener('resize', schedule);
  window.addEventListener('hashchange', schedule);
  window.addEventListener('load', schedule);
  pick();
})();
