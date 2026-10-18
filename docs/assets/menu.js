// The header's menu on a narrow screen: folded behind one button, opened and
// closed by it, and closed again by a tap anywhere else or by Escape.
(function () {
  var top = document.querySelector('header.top');
  var button = top && top.querySelector('.menu-toggle');
  if (!button) return;
  function set(open) {
    top.classList.toggle('open', open);
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  button.addEventListener('click', function () { set(!top.classList.contains('open')); });
  document.addEventListener('click', function (e) { if (!top.contains(e.target)) set(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') set(false); });
})();
