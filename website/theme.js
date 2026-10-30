(() => {
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = 'light';
  let mobileToggle;
  try { preference = localStorage.getItem('argusai.site.theme') || 'light'; } catch (_) {}
  if (!['light', 'dark', 'auto'].includes(preference)) preference = 'light';
  function apply() {
    const theme = preference === 'auto' ? (media.matches ? 'dark' : 'light') : preference;
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#111923' : '#f6f7f9');
    document.querySelectorAll('[data-theme-option]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.themeOption === preference));
    });
    if (mobileToggle) {
      mobileToggle.innerHTML = document.querySelector('[data-theme-option="'+preference+'"]')?.innerHTML || '';
      mobileToggle.title = {light:'浅色',dark:'深色',auto:'跟随系统'}[preference];
    }
    document.querySelectorAll('[data-shot-link]').forEach(link => {
      link.href = `assets/screens/${link.dataset.shotLink}-${theme}.webp`;
    });
  }
  apply();
  media.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', () => {
    if(document.querySelector('.theme-switch')) {
      mobileToggle=document.createElement('button');
      mobileToggle.type='button';mobileToggle.className='mobile-theme-toggle';
      mobileToggle.setAttribute('aria-label','切换主题');
      document.querySelector('.header-tools').prepend(mobileToggle);
      mobileToggle.addEventListener('click',()=>{
        preference={light:'dark',dark:'auto',auto:'light'}[preference];
        try {localStorage.setItem('argusai.site.theme',preference);}catch(_){}
        apply();
      });
    }
    apply();
    document.querySelectorAll('[data-theme-option]').forEach(button => button.addEventListener('click', () => {
      preference = button.dataset.themeOption;
      try { localStorage.setItem('argusai.site.theme', preference); } catch (_) {}
      apply();
    }));
  });
})();
