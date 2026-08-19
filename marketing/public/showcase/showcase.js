/* ============================================================
   Showcase system layer — shared behavior.

   Theme persistence uses the same `ms-theme` key as the landing
   page and BaseLayout.astro, so the choice follows a visitor
   across the whole site instead of resetting per page.

   Pair this with the inline pre-paint snippet in each showcase's
   <head>; this file only wires the toggle and the reveals.
   ============================================================ */
(function () {
  var root = document.documentElement;
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- theme toggle ---------- */
  var btn = document.getElementById('theme');
  if (btn) {
    btn.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      btn.setAttribute('aria-pressed', String(next === 'light'));
      try { localStorage.setItem('ms-theme', next); } catch (e) {}
    });
    btn.setAttribute('aria-pressed', String(root.getAttribute('data-theme') === 'light'));
  }

  /* ---------- marketing-site navigation ---------- */
  var menus = [].slice.call(document.querySelectorAll('.site-nav .nav-menu'));
  if (menus.length) {
    var hoverable = window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches;
    var shut = null;

    function closeMenu(menu) {
      menu.classList.remove('open');
      menu.querySelector('.nav-trigger').setAttribute('aria-expanded', 'false');
    }
    function closeMenus(except) {
      menus.forEach(function (menu) { if (menu !== except) closeMenu(menu); });
    }
    function openMenu(menu) {
      closeMenus(menu);
      menu.classList.add('open');
      menu.querySelector('.nav-trigger').setAttribute('aria-expanded', 'true');
    }

    menus.forEach(function (menu) {
      var trigger = menu.querySelector('.nav-trigger');
      trigger.addEventListener('click', function () {
        if (shut) { clearTimeout(shut); shut = null; }
        menu.classList.contains('open') ? closeMenu(menu) : openMenu(menu);
      });
      menu.addEventListener('click', function (event) {
        if (event.target.closest('.nav-panel a')) closeMenu(menu);
      });
      if (hoverable) {
        menu.addEventListener('mouseenter', function () {
          if (shut) { clearTimeout(shut); shut = null; }
          openMenu(menu);
        });
        menu.addEventListener('mouseleave', function () {
          shut = setTimeout(function () { closeMenu(menu); shut = null; }, 140);
        });
      }
      menu.addEventListener('focusout', function (event) {
        if (!menu.contains(event.relatedTarget)) closeMenu(menu);
      });
    });

    document.addEventListener('click', function (event) {
      if (!event.target.closest('.site-nav .nav-menu')) closeMenus(null);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      var open = document.querySelector('.site-nav .nav-menu.open');
      if (!open) return;
      closeMenu(open);
      open.querySelector('.nav-trigger').focus();
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
