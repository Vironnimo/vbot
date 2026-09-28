<script>
  import { tick } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { onboardingProviders } from '$lib/onboarding.js';
  import TextField from './ui/TextField.svelte';
  import Button from './ui/Button.svelte';

  let { settings, resetToken = 0, disabled = false, onSelect } = $props();
  let search = $state('');
  let expanded = $state(false);
  let searchContainer = $state();
  let listElement = $state();
  let catalogWidth = $state(600);
  let items = $derived(onboardingProviders(settings, search));
  let singleColumn = $derived(catalogWidth > 0 && catalogWidth <= 500);
  let previewCount = $derived(singleColumn ? 3 : 10);
  let visibleItems = $derived(
    expanded || search.trim() ? items : items.slice(0, previewCount),
  );

  $effect(() => {
    if (resetToken > 0) {
      search = '';
      void tick().then(() => searchContainer?.querySelector('input')?.focus());
    }
  });

  function methodLabel(type) {
    if (type === 'api_key') return t('onboarding.connect.apiKey');
    if (type === 'none') return t('onboarding.connect.local');
    return t('onboarding.connect.signIn');
  }

  async function toggleExpanded() {
    const previousCount = visibleItems.length;
    expanded = !expanded;
    if (expanded) {
      await tick();
      Array.from(listElement?.querySelectorAll('button') ?? [])
        .slice(previousCount)
        .find((button) => !button.disabled)
        ?.focus();
    }
  }
</script>

<div class="onboarding-catalog">
  <div
    class="onboarding-provider-search"
    bind:this={searchContainer}
    bind:clientWidth={catalogWidth}
  >
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      stroke-width="1.6"
      aria-hidden="true"
      ><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></svg
    >
    <TextField
      id="onboarding-provider-search"
      type="search"
      value={search}
      placeholder={t('onboarding.service.search')}
      ariaLabel={t('onboarding.service.search')}
      autocomplete="off"
      {disabled}
      onInput={(value) => (search = value)}
    />
    {#if search}
      <Button variant="tertiary" onClick={() => (search = '')}
        >{t('onboarding.service.clearSearch')}</Button
      >
    {:else}
      <span class="onboarding-provider-count"
        >{t('onboarding.service.count', {
          count: items.length,
        })}</span
      >
    {/if}
  </div>

  <ul
    id="onboarding-provider-list"
    class="onboarding-provider-list"
    class:onboarding-provider-list--single-column={singleColumn}
    bind:this={listElement}
    aria-label={t('onboarding.progress.service')}
  >
    {#each visibleItems as item (item.provider.id)}
      <li>
        <button
          type="button"
          class="onboarding-provider-row"
          class:onboarding-provider-row--connected={item.connected}
          disabled={disabled || !item.scope}
          onclick={() => onSelect(item.scope)}
        >
          <span class="onboarding-row-copy">
            <strong>{item.provider.name ?? item.provider.id}</strong>
            <small>{item.methodTypes.map(methodLabel).join(' · ')}</small>
          </span>
          {#if item.connected}
            <span class="onboarding-provider-status">
              <svg
                viewBox="0 0 24 24"
                width="16"
                height="16"
                fill="none"
                stroke="currentColor"
                stroke-width="1.8"
                aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg
              >
              {t('onboarding.service.connected')}
            </span>
          {:else if !item.scope}
            <span class="onboarding-provider-unavailable"
              >{t('onboarding.service.manage')}</span
            >
          {/if}
          {#if item.scope}
            <svg
              class="onboarding-provider-arrow"
              viewBox="0 0 24 24"
              width="18"
              height="18"
              fill="none"
              stroke="currentColor"
              stroke-width="1.6"
              aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg
            >
          {/if}
        </button>
      </li>
    {:else}
      <li class="onboarding-provider-empty" role="status">
        {search
          ? t('onboarding.service.noMatches')
          : t('onboarding.service.empty')}
      </li>
    {/each}
  </ul>
  {#if !search.trim() && items.length > previewCount}
    <Button
      variant="tertiary"
      class="onboarding-provider-expand"
      aria-expanded={expanded}
      aria-controls="onboarding-provider-list"
      onClick={toggleExpanded}
    >
      {expanded
        ? t('onboarding.service.showFewer')
        : t('onboarding.service.showAll', {
            count: items.length,
          })}
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"
        ><path d={expanded ? 'm6 15 6-6 6 6' : 'm6 9 6 6 6-6'} /></svg
      >
    </Button>
  {/if}
</div>
