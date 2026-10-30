const header = document.querySelector('.site-header');
const menuToggle = document.querySelector('.menu-toggle');
const mobileNav = document.querySelector('.mobile-nav');
const cursorGlow = document.querySelector('.cursor-glow');

const updateHeader = () => {
  header.dataset.elevated = String(window.scrollY > 18);
};

updateHeader();
window.addEventListener('scroll', updateHeader, { passive: true });

menuToggle?.addEventListener('click', () => {
  const open = menuToggle.getAttribute('aria-expanded') === 'true';
  menuToggle.setAttribute('aria-expanded', String(!open));
  header.classList.toggle('menu-open', !open);
  document.body.classList.toggle('menu-open', !open);
});

mobileNav?.querySelectorAll('a').forEach((link) => {
  link.addEventListener('click', () => {
    menuToggle?.setAttribute('aria-expanded', 'false');
    header.classList.remove('menu-open');
    document.body.classList.remove('menu-open');
  });
});

if (window.matchMedia('(pointer: fine)').matches) {
  window.addEventListener(
    'pointermove',
    (event) => {
      cursorGlow.style.left = `${event.clientX}px`;
      cursorGlow.style.top = `${event.clientY}px`;
    },
    { passive: true },
  );
}

const revealObserver = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      const delay = Number(entry.target.dataset.delay || 0);
      window.setTimeout(() => entry.target.classList.add('is-visible'), delay);
      revealObserver.unobserve(entry.target);
    });
  },
  { threshold: 0.12, rootMargin: '0px 0px -40px' },
);

document.querySelectorAll('.reveal').forEach((element) => revealObserver.observe(element));

document.querySelector('[data-year]').textContent = String(new Date().getFullYear());

const starsNode = document.querySelector('[data-stars]');
fetch('https://api.github.com/repos/HG-ha/Argusai', {
  headers: { Accept: 'application/vnd.github+json' },
})
  .then((response) => (response.ok ? response.json() : Promise.reject()))
  .then((data) => {
    if (!Number.isFinite(data.stargazers_count)) return;
    starsNode.textContent = `${data.stargazers_count.toLocaleString()} stars`;
  })
  .catch(() => {
    starsNode.textContent = 'GPL-3.0';
  });
