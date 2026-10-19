<script>
  import { loadComponent } from '../../lib/lazyContent.js';
  import { t } from '../../lib/i18n.js';
  import Banner from './Banner.svelte';
  import Button from './Button.svelte';

  let { load, children, showFeedback = true } = $props();
  const content = $derived(loadComponent(load));
</script>

{#await content}
  {#if showFeedback}
    <div class="lazy-content-feedback" role="status" aria-busy="true">
      {t('common.loading')}
    </div>
  {/if}
{:then Component}
  {@render children(Component)}
{:catch}
  {#if showFeedback}
    <div class="lazy-content-feedback">
      <Banner variant="error" role="alert">
        <span>{t('common.viewLoadError')}</span>
        <Button variant="tertiary" onClick={() => window.location.reload()}
          >{t('common.reloadPage')}</Button
        >
      </Banner>
    </div>
  {/if}
{/await}

<style>
  .lazy-content-feedback {
    padding: var(--space-lg);
    color: var(--text-med);
  }
</style>
