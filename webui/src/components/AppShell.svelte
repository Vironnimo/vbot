<script>
  import { t } from '$lib/i18n.js';

  let { items = [], activeViewId, onSelectView, children } = $props();

  const handleSelectView = (viewId) => {
    if (onSelectView) {
      onSelectView(viewId);
    }
  };

  const navIcons = {
    chat: 'chat',
    agents: 'agents',
    'system-prompt': 'system-prompt',
    settings: 'settings',
  };
</script>

<div class="app-shell">
  <aside
    class="app-shell__sidebar"
    aria-label={t('navigation.primary', 'Primary navigation')}
  >
    <div class="app-shell__brand" aria-label={t('app.title', 'vBot')}>
      <span class="app-shell__brand-mark" aria-hidden="true">
        <svg viewBox="0 0 14 14">
          <path d="M7 1 13 4v6l-6 3-6-3V4l6-3z" />
        </svg>
      </span>
      <span class="app-shell__brand-text">{t('app.title', 'vBot')}</span>
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
          <span class="app-shell__nav-icon" aria-hidden="true">
            {#if navIcons[item.id] === 'chat'}
              <svg viewBox="0 0 16 16">
                <path d="M2 3h12v8H9l-4 3v-3H2z" />
              </svg>
            {:else if navIcons[item.id] === 'agents'}
              <svg viewBox="0 0 16 16">
                <circle cx="8" cy="5" r="2.5" />
                <path d="M2 14c0-3.3 2.7-6 6-6s6 2.7 6 6" />
              </svg>
            {:else if navIcons[item.id] === 'system-prompt'}
              <svg viewBox="0 0 16 16">
                <rect x="2" y="2" width="12" height="12" rx="2" />
                <path d="M5 6h6M5 9h4" />
              </svg>
            {:else if navIcons[item.id] === 'settings'}
              <svg viewBox="0 0 16 16">
                <circle cx="8" cy="8" r="2.5" />
                <path
                  d="M8 1v2m0 10v2M1 8h2m10 0h2m-2.6-4.4-1.4 1.4M4 12l1.4-1.4M12 12l-1.4-1.4M4 4l1.4 1.4"
                />
              </svg>
            {/if}
          </span>
          <span>{t(item.labelKey, item.labelFallback)}</span>
        </button>
      {/each}
    </nav>

    <div
      class="app-shell__footer"
      aria-label={t('app.serverStatus', 'Server status')}
    >
      <span class="app-shell__pulse-dot" aria-hidden="true"></span>
      <span class="app-shell__footer-text"
        >{t('app.serverReady', 'server: ready')}</span
      >
    </div>
  </aside>

  <main class="app-shell__content">
    {@render children?.()}
  </main>
</div>
