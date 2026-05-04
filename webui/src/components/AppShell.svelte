<script>
  import { t } from '$lib/i18n.js';

  let { items = [], activeViewId, onSelectView, children } = $props();

  const handleSelectView = (viewId) => {
    if (onSelectView) {
      onSelectView(viewId);
    }
  };
</script>

<div class="app-shell">
  <aside
    class="app-shell__sidebar"
    aria-label={t('navigation.primary', 'Primary navigation')}
  >
    <div class="app-shell__brand" aria-label={t('app.title', 'vBot')}>
      <span class="app-shell__brand-mark" aria-hidden="true">v</span>
      <div>
        <p class="app-shell__eyebrow">
          {t('app.eyebrow', 'Local agent harness')}
        </p>
        <h1>{t('app.title', 'vBot')}</h1>
      </div>
    </div>

    <nav
      class="app-shell__navigation"
      aria-label={t('navigation.sections', 'Sections')}
    >
      {#each items as item (item.id)}
        <button
          class:app-shell__nav-item--active={item.id === activeViewId}
          class="app-shell__nav-item"
          type="button"
          aria-current={item.id === activeViewId ? 'page' : undefined}
          onclick={() => handleSelectView(item.id)}
        >
          <span class="app-shell__nav-sigil" aria-hidden="true"></span>
          <span>{t(item.labelKey, item.labelFallback)}</span>
        </button>
      {/each}
    </nav>
  </aside>

  <main class="app-shell__content">
    {@render children?.()}
  </main>
</div>
