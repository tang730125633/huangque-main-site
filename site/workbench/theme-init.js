(function (window, document) {
  'use strict';

  var STORAGE_KEY = 'hq_theme';

  function normalize(theme) {
    return theme === 'light' ? 'light' : 'dark';
  }

  function read() {
    try {
      return normalize(localStorage.getItem(STORAGE_KEY));
    } catch (error) {
      return 'dark';
    }
  }

  function apply(theme, persist) {
    var next = normalize(theme);
    document.documentElement.setAttribute('data-theme', next);
    document.documentElement.style.colorScheme = next;

    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      meta = document.createElement('meta');
      meta.name = 'theme-color';
      document.head.appendChild(meta);
    }
    meta.content = next === 'light' ? '#f4f6f9' : '#070b13';

    if (persist) {
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch (error) {}
    }

    document.dispatchEvent(new CustomEvent('hq-theme-change', {
      detail: { theme: next }
    }));
    return next;
  }

  window.HQTheme = {
    get: read,
    apply: function (theme) { return apply(theme, false); },
    set: function (theme) { return apply(theme, true); },
    storageKey: STORAGE_KEY
  };

  apply(read(), false);

  window.addEventListener('storage', function (event) {
    if (event.key === STORAGE_KEY) apply(event.newValue, false);
  });
})(window, document);
