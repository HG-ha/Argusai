(() => {
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = 'light';
  try { preference = localStorage.getItem('argusai.site.theme') || 'light'; } catch (_) {}
  if (!['light', 'dark', 'auto'].includes(preference)) preference = 'light';
  function apply() {
    const theme = preference === 'auto' ? (media.matches ? 'dark' : 'light') : preference;
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#111923' : '#f6f7f9');
    document.querySelectorAll('[data-theme-option]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.themeOption === preference));
    });
    document.querySelectorAll('[data-shot-link]').forEach(link => {
      link.href = `assets/screens/${link.dataset.shotLink}-${theme}.webp`;
    });
  }
  apply();
  media.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.querySelectorAll('[data-theme-option]').forEach(button => button.addEventListener('click', () => {
      preference = button.dataset.themeOption;
      try { localStorage.setItem('argusai.site.theme', preference); } catch (_) {}
      apply();
    }));
  });
})();
