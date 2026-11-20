(function () {
  var body = document.body;
  var sidebar = document.getElementById('narriSidebar');
  var backdrop = document.getElementById('narriSidebarBackdrop');
  var toggle = document.querySelector('.narri-menu-toggle');
  var media = window.matchMedia('(max-width: 991px)');

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

  syncState(false);
})();