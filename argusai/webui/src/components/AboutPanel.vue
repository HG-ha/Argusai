<script setup>
import { computed } from 'vue'
import { t } from '../i18n.js'
import { state } from '../state.js'
import UpdatePanel from './UpdatePanel.vue'

const principles = computed(() => [
  { title: t('about.principles.agent.title'), body: t('about.principles.agent.body') },
  { title: t('about.principles.local.title'), body: t('about.principles.local.body') },
  { title: t('about.principles.evidence.title'), body: t('about.principles.evidence.body') },
  { title: t('about.principles.extensible.title'), body: t('about.principles.extensible.body') },
])

const capabilities = computed(() => [
  { title: t('about.capabilities.foundation.title'), body: t('about.capabilities.foundation.body') },
  { title: t('about.capabilities.agent.title'), body: t('about.capabilities.agent.body') },
  { title: t('about.capabilities.web.title'), body: t('about.capabilities.web.body') },
  { title: t('about.capabilities.http.title'), body: t('about.capabilities.http.body') },
  { title: t('about.capabilities.proxy.title'), body: t('about.capabilities.proxy.body') },
  { title: t('about.capabilities.jsReverse.title'), body: t('about.capabilities.jsReverse.body') },
  { title: t('about.capabilities.mobile.title'), body: t('about.capabilities.mobile.body') },
  { title: t('about.capabilities.binary.title'), body: t('about.capabilities.binary.body') },
  { title: t('about.capabilities.network.title'), body: t('about.capabilities.network.body') },
  { title: t('about.capabilities.scanners.title'), body: t('about.capabilities.scanners.body') },
  { title: t('about.capabilities.adversarial.title'), body: t('about.capabilities.adversarial.body') },
  { title: t('about.capabilities.defense.title'), body: t('about.capabilities.defense.body') },
  { title: t('about.capabilities.cloud.title'), body: t('about.capabilities.cloud.body') },
  { title: t('about.capabilities.desktop.title'), body: t('about.capabilities.desktop.body') },
  { title: t('about.capabilities.runtimes.title'), body: t('about.capabilities.runtimes.body') },
  { title: t('about.capabilities.knowledge.title'), body: t('about.capabilities.knowledge.body') },
  { title: t('about.capabilities.automation.title'), body: t('about.capabilities.automation.body') },
  { title: t('about.capabilities.extensions.title'), body: t('about.capabilities.extensions.body') },
])

const allowedItems = computed(() => [
  t('about.scope.allowed.owned'),
  t('about.scope.allowed.authorized'),
  t('about.scope.allowed.lab'),
  t('about.scope.allowed.bounded'),
])

const deniedItems = computed(() => [
  t('about.scope.denied.unauthorized'),
  t('about.scope.denied.credentials'),
  t('about.scope.denied.destructive'),
  t('about.scope.denied.harm'),
])
</script>

<template>
  <section class="about-page">
    <div class="about-scroll">
      <header class="about-hero">
        <div class="about-kicker">{{ t('about.kicker') }}</div>
        <h1>{{ t('about.title') }}</h1>
        <p>{{ t('about.intro') }}</p>
        <div class="about-agent-core">
          <strong>{{ t('about.agentCoreTitle') }}</strong>
          <span>{{ t('about.agentCoreBody') }}</span>
        </div>
      </header>

      <UpdatePanel />

      <section class="about-section">
        <div class="about-section-heading">
          <span>{{ t('about.principlesKicker') }}</span>
          <h2>{{ t('about.principlesTitle') }}</h2>
        </div>
        <div class="about-principles">
          <article v-for="item in principles" :key="item.title">
            <h3>{{ item.title }}</h3>
            <p>{{ item.body }}</p>
          </article>
        </div>
      </section>

      <section class="about-section">
        <div class="about-section-heading">
          <span>{{ t('about.capabilitiesKicker') }}</span>
          <h2>{{ t('about.capabilitiesTitle') }}</h2>
        </div>
        <div class="about-capabilities">
          <article v-for="item in capabilities" :key="item.title">
            <h3>{{ item.title }}</h3>
            <p>{{ item.body }}</p>
          </article>
        </div>
      </section>

      <section class="about-section about-scope">
        <div class="about-section-heading">
          <span>{{ t('about.scopeKicker') }}</span>
          <h2>{{ t('about.scopeTitle') }}</h2>
          <p>{{ t('about.scopeIntro') }}</p>
        </div>
        <div class="about-scope-grid">
          <article class="scope-column allowed">
            <header>
              <span>{{ t('about.scope.allowedLabel') }}</span>
              <strong>{{ t('about.scope.allowedTitle') }}</strong>
            </header>
            <ul>
              <li v-for="item in allowedItems" :key="item">{{ item }}</li>
            </ul>
          </article>
          <article class="scope-column denied">
            <header>
              <span>{{ t('about.scope.deniedLabel') }}</span>
              <strong>{{ t('about.scope.deniedTitle') }}</strong>
            </header>
            <ul>
              <li v-for="item in deniedItems" :key="item">{{ item }}</li>
            </ul>
          </article>
        </div>
        <p class="about-safety-note">{{ t('about.scope.safetyNote') }}</p>
      </section>

      <section class="about-section about-license" aria-labelledby="about-license-heading">
        <div class="about-section-heading">
          <span>GPL-3.0-only</span>
          <h2 id="about-license-heading">{{ t('about.licenseTitle') }}</h2>
          <p>{{ t('about.licenseSummary') }}</p>
          <p>{{ t('about.licenseWarranty') }}</p>
          <p>{{ t('about.licenseThirdParty') }}</p>
        </div>
        <div class="about-license-links">
          <a href="https://github.com/HG-ha/Argusai/blob/main/LICENSE" target="_blank" rel="noopener noreferrer">
            {{ t('about.licenseRead') }}
          </a>
          <a href="https://github.com/HG-ha/Argusai/blob/main/THIRD_PARTY_NOTICES.md" target="_blank" rel="noopener noreferrer">
            {{ t('about.licenseNotices') }}
          </a>
        </div>
      </section>

      <footer class="about-footer">
        <div class="about-footer-links">
          <span>ArgusAI<template v-if="state.version"> v{{ state.version }}</template></span>
          <a href="https://github.com/HG-ha/Argusai" target="_blank" rel="noopener noreferrer">
            {{ t('about.github') }}
          </a>
        </div>
        <span>{{ t('about.footer') }}</span>
      </footer>
    </div>
  </section>
</template>

<style scoped>
.about-page {
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  background: transparent;
}

.about-scroll {
  height: 100%;
  overflow-y: auto;
  padding: clamp(22px, 3.2vw, 42px);
}

.about-hero,
.about-section,
.about-footer {
  width: min(1040px, 100%);
  margin-right: auto;
  margin-left: auto;
}

.about-hero {
  padding: clamp(20px, 3vw, 34px) 0 clamp(26px, 4vw, 46px);
  border-bottom: 1px solid var(--border);
}

.about-kicker,
.about-section-heading > span {
  color: var(--accent);
  font: 600 10px/1.4 var(--mono);
  letter-spacing: .08em;
}

.about-hero h1 {
  max-width: 780px;
  margin: 9px 0 0;
  color: var(--fg-strong);
  font-size: clamp(28px, 4vw, 46px);
  font-weight: 650;
  letter-spacing: -.035em;
  line-height: 1.08;
}

.about-hero > p {
  max-width: 760px;
  margin: 14px 0 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.75;
}

.about-agent-core {
  display: grid;
  grid-template-columns: minmax(150px, 220px) minmax(0, 1fr);
  gap: 18px;
  margin-top: 24px;
  padding: 14px 16px;
  border-left: 3px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 7%, var(--panel));
}

.about-agent-core strong {
  color: var(--fg-strong);
  font-size: 13px;
}

.about-agent-core span {
  color: var(--fg);
  font-size: 12px;
  line-height: 1.65;
}

.about-section {
  padding: clamp(28px, 4vw, 46px) 0;
  border-bottom: 1px solid var(--border);
}

.about-section-heading {
  max-width: 760px;
  margin-bottom: 20px;
}

.about-section-heading h2 {
  margin: 5px 0 0;
  color: var(--fg-strong);
  font-size: 20px;
  font-weight: 620;
  letter-spacing: -.015em;
}

.about-section-heading p {
  margin: 7px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.65;
}

.about-principles,
.about-capabilities {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  border-top: 1px solid var(--border);
}

.about-principles article,
.about-capabilities article {
  min-width: 0;
  padding: 17px 18px 18px 0;
  border-bottom: 1px solid var(--border);
}

.about-principles article:nth-child(odd),
.about-capabilities article:nth-child(odd) {
  padding-right: 24px;
  border-right: 1px solid var(--border);
}

.about-principles article:nth-child(even),
.about-capabilities article:nth-child(even) {
  padding-left: 24px;
}

.about-principles h3,
.about-capabilities h3 {
  margin: 0;
  color: var(--fg-strong);
  font-size: 13px;
  font-weight: 620;
}

.about-principles p,
.about-capabilities p {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 11.5px;
  line-height: 1.65;
}

.about-scope-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.scope-column {
  min-width: 0;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--panel);
}

.scope-column header {
  display: flex;
  align-items: baseline;
  gap: 9px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}

.scope-column header span {
  color: var(--muted-2);
  font: 600 9px/1 var(--mono);
  letter-spacing: .08em;
}

.scope-column header strong {
  color: var(--fg-strong);
  font-size: 13px;
}

.scope-column ul {
  display: grid;
  gap: 10px;
  margin: 14px 0 0;
  padding: 0;
  list-style: none;
}

.scope-column li {
  position: relative;
  padding-left: 14px;
  color: var(--fg);
  font-size: 11.5px;
  line-height: 1.55;
}

.scope-column li::before {
  position: absolute;
  top: .68em;
  left: 0;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--muted-2);
  content: '';
}

.scope-column.allowed li::before {
  background: var(--ok);
}

.scope-column.denied li::before {
  background: var(--err);
}

.about-safety-note {
  margin: 14px 0 0;
  padding: 11px 13px;
  border: 1px solid color-mix(in srgb, var(--warn) 24%, var(--border));
  border-radius: 9px;
  background: color-mix(in srgb, var(--warn) 6%, transparent);
  color: var(--muted);
  font-size: 11px;
  line-height: 1.6;
}

.about-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 0 4px;
  color: var(--muted-2);
  font: 10px/1.5 var(--mono);
}

.about-footer-links {
  display: flex;
  align-items: center;
  gap: 12px;
}

.about-license-links {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  font-size: 12px;
  line-height: 1.6;
}

.about-footer a,
.about-license-links a {
  color: var(--accent);
  text-decoration: none;
}

.about-footer a:hover,
.about-license-links a:hover {
  text-decoration: underline;
}

@media (max-width: 720px) {
  .about-agent-core,
  .about-principles,
  .about-capabilities,
  .about-scope-grid {
    grid-template-columns: 1fr;
  }

  .about-principles article,
  .about-capabilities article,
  .about-principles article:nth-child(odd),
  .about-capabilities article:nth-child(odd),
  .about-principles article:nth-child(even),
  .about-capabilities article:nth-child(even) {
    padding-right: 0;
    padding-left: 0;
    border-right: 0;
  }

  .about-footer {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .about-footer-links {
    align-items: flex-start;
    flex-direction: column;
    gap: 3px;
  }
}
</style>
