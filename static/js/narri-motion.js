(function () {
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduceMotion) return;

  var selectors = '.dashboard-stats .stat-card, .mini-card, .product-card, .shop-panel, .activity-card';
  var items = document.querySelectorAll(selectors);
  if (!items.length) return;

  if (!('IntersectionObserver' in window)) return;
  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('narri-visible');
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.08, rootMargin: '0px 0px -24px 0px' });

  items.forEach(function (item, index) {
    item.classList.add('narri-reveal');
    item.style.transitionDelay = Math.min(index % 4, 3) * 55 + 'ms';
    observer.observe(item);
  });
})();