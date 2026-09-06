<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import { openFilePreview, getFilePreviewRevision } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import Banner from '../ui/Banner.svelte';
  import Toggle from '../ui/Toggle.svelte';

  let { active = true, request = null, workspaceActions } = $props();
  let source = $state('');
  let preview = $state(null);
  let loading = $state(false);
  let openError = $state('');
  let refreshError = $state('');
  let frameUnavailable = $state(false);
  let error = $derived(
    openError ||
      refreshError ||
      (frameUnavailable
        ? t(
            'preview.unavailable',
            'The page is unavailable. Live preview will retry when files change.',
          )
        : ''),
  );
  let autoRefresh = $state(true);
  let frame = $state(null);
  let refreshCount = $state(0);
  let currentUrl = '';
  let pageLabel = $state('');
  let abortController;
  let openGeneration = 0;
  let handledRequest = null;

  async function open(nextSource = source) {
    if (!nextSource.trim()) return;
    source = nextSource;
    const generation = ++openGeneration;
    abortController?.abort();
    abortController = new AbortController();
    loading = true;
    openError = '';
    refreshError = '';
    try {
      const next = await openFilePreview(nextSource.trim(), {
        signal: abortController.signal,
      });
      if (generation !== openGeneration) return;
      preview = next;
      currentUrl = next.url;
      frameUnavailable = false;
      pageLabel = next.filename;
      refreshCount = 0;
    } catch (cause) {
      if (generation === openGeneration) openError = cause.message;
    } finally {
      if (generation === openGeneration) loading = false;
    }
  }

  $effect(() => {
    const next = request;
    if (next?.source && next !== handledRequest) {
      handledRequest = next;
      untrack(() => void open(next.source));
    }
  });

  function reload() {
    if (frame && currentUrl) frame.src = currentUrl;
  }

  onMount(() => {
    const onNavigation = (event) => {
      if (
        event.source !== frame?.contentWindow ||
        event.origin !== 'null' ||
        !['vbot-preview-ready', 'vbot-preview-unavailable'].includes(
          event.data?.type,
        ) ||
        typeof event.data.url !== 'string'
      )
        return;
      try {
        const url = new URL(event.data.url);
        const expected = new URL(preview.url, window.location.href);
        if (
          url.origin === expected.origin &&
          url.pathname.startsWith(`/api/preview-assets/${preview.token}/`)
        ) {
          currentUrl = url.href;
          frameUnavailable = event.data.type === 'vbot-preview-unavailable';
          pageLabel = decodeURIComponent(
            url.pathname.split(`/api/preview-assets/${preview.token}/`)[1],
          );
        }
      } catch {
        /* Ignore malformed messages from preview content. */
      }
    };
    window.addEventListener('message', onNavigation);
    return () => window.removeEventListener('message', onNavigation);
  });

  $effect(() => {
    const token = preview?.token;
    if (!active || !autoRefresh || !token || loading) return;
    let disposed = false;
    let timer;
    let revision = untrack(() => preview.revision);
    const controller = new AbortController();
    async function check() {
      if (disposed) return;
      if (document.visibilityState !== 'hidden') {
        try {
          const next = await getFilePreviewRevision(token, {
            signal: controller.signal,
          });
          if (disposed) return;
          refreshError = '';
          if (next.revision !== revision) {
            revision = next.revision;
            preview.revision = revision;
            refreshCount += 1;
            reload();
          }
        } catch (cause) {
          if (!disposed) refreshError = cause.message;
        }
      }
      if (!disposed) timer = setTimeout(check, 1500);
    }
    timer = setTimeout(check, 1500);
    return () => {
      disposed = true;
      controller.abort();
      clearTimeout(timer);
    };
  });

  onDestroy(() => {
    openGeneration += 1;
    abortController?.abort();
  });
</script>

<div class="html-preview" hidden={!active}>
  <div class="html-preview__toolbar">
    <span class="html-preview__filename"
      >{pageLabel || preview?.filename || t('split.preview', 'Preview')}</span
    >
    {#if preview}
      <span class="html-preview__live" aria-live="polite">
        <span class:paused={!autoRefresh} class="html-preview__dot"></span>
        {autoRefresh
          ? t('preview.live', 'Live')
          : t('preview.paused', 'Paused')}
        {#if refreshCount}<span class="html-preview__count">{refreshCount}</span
          >{/if}
      </span>
      <Toggle
        size="sm"
        checked={autoRefresh}
        onChange={(value) => (autoRefresh = value)}
        ariaLabel={t('preview.autoRefresh', 'Automatically refresh preview')}
      />
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('preview.reload', 'Reload preview')}
        tooltip={t('preview.reload', 'Reload preview')}
        onClick={reload}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          aria-hidden="true"><path d="M20 11a8 8 0 1 0-2 6M20 4v7h-7" /></svg
        >
      </Button>
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('preview.home', 'Back to entry page')}
        tooltip={t('preview.home', 'Back to entry page')}
        onClick={() => {
          currentUrl = preview.url;
          reload();
        }}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          aria-hidden="true"
          ><path d="m3 11 9-8 9 8M5 9v12h14V9M9 21v-8h6v8" /></svg
        >
      </Button>
    {/if}
    {@render workspaceActions?.()}
  </div>
  {#if error}
    <Banner variant="error" role="alert" class="html-preview__feedback">
      {t('preview.failed', 'Preview could not be updated.')}
      {error}
      <Button onClick={() => void open()}>{t('common.retry', 'Retry')}</Button>
    </Banner>
  {:else if loading}
    <Banner class="html-preview__feedback" role="status"
      >{t('preview.loading', 'Opening preview…')}</Banner
    >
  {/if}
  {#if preview}
    <iframe
      class:unavailable={frameUnavailable}
      bind:this={frame}
      src={preview.url}
      title={t('preview.frame', 'Website preview')}
      sandbox="allow-scripts"
      referrerpolicy="no-referrer"
      allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'"
    >
    </iframe>
  {:else if !loading && !error}
    <EmptyState
      fill
      title={t('preview.emptyTitle', 'Your website, beside the conversation')}
      description={t(
        'preview.emptyDescription',
        'Choose an HTML file shared by the agent in the conversation to preview it here.',
      )}
    >
      {#snippet icon()}<svg
          width="38"
          height="38"
          viewBox="0 0 32 32"
          fill="none"
          stroke="currentColor"
          stroke-width="1.2"
          aria-hidden="true"
          ><rect x="3" y="5" width="26" height="22" rx="3" /><path
            d="M3 11h26M7 8h1m3 0h1m-1 8-4 4 4 4m10-8 4 4-4 4m-3-9-4 10"
          /></svg
        >{/snippet}
    </EmptyState>
  {/if}
</div>

<style>
  .html-preview {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    min-width: 0;
    background: var(--bg);
  }
  .html-preview[hidden] {
    display: none;
  }
  .html-preview__toolbar {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 42px;
    padding: 4px 12px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  .html-preview__filename {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font: 12px var(--font-mono);
    color: var(--text-hi);
  }
  .html-preview__live {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--text-med);
    font-size: 12px;
  }
  .html-preview__dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--green);
  }
  .html-preview__dot.paused {
    background: var(--text-lo);
  }
  .html-preview__count {
    color: var(--text-lo);
    font: 11px var(--font-mono);
  }
  :global(.html-preview__feedback) {
    margin: 10px;
  }
  iframe {
    width: 100%;
    flex: 1;
    min-height: 0;
    border: 0;
    background: white;
  }
  iframe.unavailable {
    visibility: hidden;
  }
</style>
