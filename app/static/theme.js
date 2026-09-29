/* Applies the saved (or system) theme before first paint so the page never flashes the wrong one. */
(function () {
  var theme = null;
  try { theme = localStorage.getItem('stocklab-theme'); } catch (e) {}
  if (theme !== 'light' && theme !== 'dark') {
    theme = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.documentElement.setAttribute('data-theme', theme);
}());
