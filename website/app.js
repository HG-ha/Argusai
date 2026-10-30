const header = document.querySelector('.site-header');
const menuToggle = document.querySelector('.menu-toggle');
const mobileNav = document.querySelector('.mobile-nav');

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

const revealObserver = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      const delay = Number(entry.target.dataset.delay || 0);
      window.setTimeout(() => entry.target.classList.add('is-visible'), delay);
      revealObserver.unobserve(entry.target);
    });
  },
  { threshold: 0.1, rootMargin: '0px 0px -30px' },
);

document.querySelectorAll('.reveal').forEach((element) => revealObserver.observe(element));

const storySteps = [...document.querySelectorAll('.story-step')];
const storyScreens = [...document.querySelectorAll('.story-screen')];

const selectStory = (target) => {
  storySteps.forEach((step) => step.classList.toggle('is-active', step.dataset.target === target));
  storyScreens.forEach((screen) => screen.classList.toggle('story-screen-active', screen.dataset.screen === target));
};

storySteps.forEach((step) => {
  step.addEventListener('pointerenter', () => selectStory(step.dataset.target));
  step.addEventListener('click', () => selectStory(step.dataset.target));
  step.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') selectStory(step.dataset.target);
  });
});

document.querySelector('[data-year]').textContent = String(new Date().getFullYear());

const starsNode = document.querySelector('[data-stars]');
fetch('https://api.github.com/repos/HG-ha/Argusai', { headers: { Accept: 'application/vnd.github+json' } })
  .then((response) => (response.ok ? response.json() : Promise.reject()))
  .then((data) => {
    if (Number.isFinite(data.stargazers_count)) starsNode.textContent = `${data.stargazers_count.toLocaleString()} STARS`;
  })
  .catch(() => {
    starsNode.textContent = 'OPEN SOURCE';
  });
