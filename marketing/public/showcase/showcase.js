/* ============================================================
   Showcase system layer — shared behavior.

   Theme persistence uses the same `ms-theme` key as BaseLayout,
   so the choice follows a visitor across the whole site instead of
   resetting per page.

   Pair this with the inline pre-paint snippet in each showcase's
   <head>; this file only wires the toggle and the reveals.
   ============================================================ */
(function () {
  var root = document.documentElement;
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- theme toggle ---------- */
  var btn = document.getElementById('themeBtn');
  if (btn) {
    btn.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      btn.setAttribute('aria-pressed', String(next === 'dark'));
      try { localStorage.setItem('ms-theme', next); } catch (e) {}
    });
    btn.setAttribute('aria-pressed', String(root.getAttribute('data-theme') === 'dark'));
  }

  /* ---------- narrow-width nav ---------- */
  var header = document.querySelector('.header');
  var menu = header && header.querySelector('.menu');
  if (menu) {
    menu.addEventListener('click', function () {
      var open = header.toggleAttribute('data-open');
      menu.setAttribute('aria-expanded', String(open));
    });
    header.addEventListener('click', function (event) {
      if (!event.target.closest('.navlinks a')) return;
      header.removeAttribute('data-open');
      menu.setAttribute('aria-expanded', 'false');
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape' || !header.hasAttribute('data-open')) return;
      header.removeAttribute('data-open');
      menu.setAttribute('aria-expanded', 'false');
      menu.focus();
    });
  }

  /* ---------- reveal on scroll ----------
     Extra selectors let a page opt its own machinery in without
     duplicating the observer. */
  var selector = '.rv' + (root.dataset.reveal ? ', ' + root.dataset.reveal : '');
  var targets = [].slice.call(document.querySelectorAll(selector));

  if (reduce || !('IntersectionObserver' in window)) {
    targets.forEach(function (el) { el.classList.add('in'); });
    document.dispatchEvent(new CustomEvent('showcase:revealed'));
    return;
  }

  var io = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add('in');
        io.unobserve(e.target);
      });
    },
    { threshold: 0.18, rootMargin: '0px 0px -8% 0px' }
  );
  targets.forEach(function (el) { io.observe(el); });
})();
