// Loaded as a classic (blocking) script in <head> so the theme is applied
// before first paint. Choice: 'light' | 'dark' | 'system' (default).
(function () {
  var KEY = 'cb-theme';

  function read() {
    try { return localStorage.getItem(KEY) || 'system'; } catch (e) { return 'system'; }
  }

  function apply(choice) {
    var root = document.documentElement;
    if (choice === 'light' || choice === 'dark') root.setAttribute('data-theme', choice);
    else root.removeAttribute('data-theme');
  }

  function effective(choice) {
    if (choice === 'light' || choice === 'dark') return choice;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }

  apply(read());

  window.cbTheme = {
    get: read,
    effective: function () { return effective(read()); },
    set: function (choice) {
      try { localStorage.setItem(KEY, choice); } catch (e) {}
      apply(choice);
      window.dispatchEvent(new CustomEvent('cb-theme-change', { detail: { choice: choice, effective: effective(choice) } }));
    },
    // Cycle system → light → dark → system
    cycle: function () {
      var order = ['system', 'light', 'dark'];
      var next = order[(order.indexOf(read()) + 1) % order.length];
      this.set(next);
      return next;
    },
  };
})();
