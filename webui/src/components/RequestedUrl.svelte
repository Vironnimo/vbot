<script>
  // An address an Extension asks the user to open, shown in full as text
  // with its registrable domain marked, so the user sees where it leads
  // before deciding. Only the explicit open action follows it; nothing here
  // loads or prefetches the address.
  import { onMount } from 'svelte';
  import {
    isDesktopAccessor,
    openDesktopExternalUrl,
  } from '$lib/desktopBridge.js';
  import { t } from '$lib/i18n.js';
  import { loadDomainParser, requestedUrl } from '$lib/requestedUrl.js';
  import Banner from './ui/Banner.svelte';

  const noop = () => {};

  let {
    url = '',
    openLabel = '',
    disabled = false,
    // Runs once the address was handed to the browser.
    onOpen = noop,
    onOpenFailed = noop,
  } = $props();

  let getDomain = $state(null);
  let address = $derived(requestedUrl(url, getDomain));
  let warnings = $derived(
    (address?.warnings ?? []).map(
      (warning) =>
        ({
          insecure: t('extensions.urlInsecure'),
          international: t('extensions.urlInternational'),
          credentials: t('extensions.urlCredentials'),
          ipAddress: t('extensions.urlIpAddress'),
        })[warning],
    ),
  );

  onMount(() => {
    let mounted = true;
    void loadDomainParser().then((parser) => {
      if (mounted && parser) getDomain = parser;
    });
    return () => {
      mounted = false;
    };
  });

  function open(event) {
    if (disabled) {
      event.preventDefault();
      return;
    }
    if (!isDesktopAccessor()) {
      onOpen();
      return;
    }
    // The Desktop window cannot open a new tab; its host opens the browser.
    event.preventDefault();
    openDesktopExternalUrl(address.href).then(() => onOpen(), onOpenFailed);
  }
</script>

{#if address}
  <div class="requested-url">
    <p class="requested-url__label">{t('extensions.urlAddress')}</p>
    <code class="requested-url__address"
      >{#each address.parts as part, index (index)}<span
          class={`requested-url__${part.kind}`}>{part.text}</span
        >{/each}</code
    >
    <p>
      {t('extensions.urlOpensOn')}
      <strong class="requested-url__site">{address.domain}</strong>
    </p>
    {#each warnings as warning (warning)}
      <Banner variant="warn">{warning}</Banner>
    {/each}
    <a
      class="btn-primary requested-url__open"
      href={address.href}
      target="_blank"
      rel="noopener noreferrer"
      referrerpolicy="no-referrer"
      aria-disabled={disabled || undefined}
      onclick={open}>{openLabel}</a
    >
  </div>
{:else}
  <Banner variant="error">{t('extensions.urlInvalid')}</Banner>
{/if}

<style>
  .requested-url {
    display: grid;
    gap: 8px;
    justify-items: start;
  }
  .requested-url p {
    margin: 0;
  }
  .requested-url__label {
    color: var(--text-med);
  }
  .requested-url__address {
    display: block;
    width: 100%;
    box-sizing: border-box;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--surface-2, transparent);
    color: var(--text-med);
    font-family: var(--font-mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    user-select: all;
  }
  .requested-url__domain,
  .requested-url__site {
    color: var(--text-hi);
    font-weight: 600;
  }
  /* The address and host render character by character: a font ligature
     would draw the "--" of an xn-- host as one dash. */
  .requested-url__address,
  .requested-url__site {
    font-variant-ligatures: none;
    font-feature-settings:
      'liga' 0,
      'calt' 0;
  }
  .requested-url__site {
    font-family: var(--font-mono);
    overflow-wrap: anywhere;
  }
  .requested-url__open {
    text-decoration: none;
  }
  .requested-url__open[aria-disabled='true'] {
    opacity: 0.6;
    pointer-events: none;
  }
</style>
