(function () {
  var body = document.body;
  var sidebar = document.getElementById('narriSidebar');
  var backdrop = document.getElementById('narriSidebarBackdrop');
  var toggle = document.querySelector('.narri-menu-toggle');
  var media = window.matchMedia('(max-width: 991px)');
  var search = document.getElementById('narriNavSearch');

  function syncState(open) {
    body.classList.toggle('narri-drawer-open', open);
    if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (sidebar) sidebar.setAttribute('aria-hidden', media.matches && !open ? 'true' : 'false');
  }
  function open() {
    if (media.matches) syncState(true);
  }
  function close() {
    syncState(false);
  }

  window.NarriLayout = {
    openSidebar: open,
    closeSidebar: close,
    toggleSidebar: function () {
      if (media.matches) syncState(!body.classList.contains('narri-drawer-open'));
      else body.classList.toggle('sidebar-main');
    }
  };

  if (backdrop) backdrop.addEventListener('click', close);
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') close();
    if (event.key === '/' && !/INPUT|TEXTAREA|SELECT/.test((event.target.tagName || ''))) {
      if (search && media.matches) { event.preventDefault(); open(); search.focus(); }
    }
  });
  document.addEventListener('click', function (event) {
    if (!media.matches || !body.classList.contains('narri-drawer-open')) return;
    var link = event.target.closest('#narriSidebar a[href]');
    if (link && !link.getAttribute('href').startsWith('#')) close();
  });
  window.addEventListener('resize', function () {
    if (!media.matches) close();
    else syncState(body.classList.contains('narri-drawer-open'));
  }, { passive: true });

  if (search && sidebar) {
    search.addEventListener('input', function () {
      var query = search.value.trim().toLocaleLowerCase('vi');
      var items = sidebar.querySelectorAll('.narri-nav li.sidebar-layout');
      items.forEach(function (item) {
        item.hidden = query && !item.textContent.toLocaleLowerCase('vi').includes(query);
      });
      var headings = sidebar.querySelectorAll('.narri-nav li.narri-nav-heading');
      headings.forEach(function (heading) {
        var hasVisible = false;
        var next = heading.nextElementSibling;
        while (next && !next.classList.contains('narri-nav-heading')) {
          if (next.matches('li.sidebar-layout') && !next.hidden) hasVisible = true;
          if (next.querySelectorAll) {
            next.querySelectorAll('li.sidebar-layout').forEach(function (child) { if (!child.hidden) hasVisible = true; });
          }
          next = next.nextElementSibling;
        }
        heading.hidden = query && !hasVisible;
      });
    });
  }

  if (sidebar) {
    var currentPath = window.location.pathname.replace(/\/$/, '') || '/';
    sidebar.querySelectorAll('.narri-nav a[href]').forEach(function (link) {
      var href = link.getAttribute('href') || '';
      if (!href || href.charAt(0) === '#' || href.indexOf('#') >= 0) return;
      try {
        var target = new URL(href, window.location.origin).pathname.replace(/\/$/, '') || '/';
        if (target === currentPath) {
          link.classList.add('active');
          var parent = link.closest('.submenu');
          if (parent) {
            parent.classList.add('show');
            var trigger = sidebar.querySelector('[href="#' + parent.id + '"]');
            if (trigger) { trigger.classList.remove('collapsed'); trigger.setAttribute('aria-expanded', 'true'); }
          }
        }
      } catch (e) {}
    });
  }

  syncState(false);
})();