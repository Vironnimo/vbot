<script>
  import { toolRowMedia } from '$lib/chatToolDetails.js';
  import { t } from '$lib/i18n.js';
  import { mountHold } from '$lib/mountHold.js';
  import { tooltip } from '$lib/tooltip.js';
  import MediaLightbox from '../MediaLightbox.svelte';

  let { tool, result } = $props();
  let items = $derived(toolRowMedia(tool, result));
  const visibleLimit = 3;
  let visible = $derived(items.slice(0, visibleLimit));
  let remaining = $derived(items.length - visible.length);
  let strip = $state();
  let bounds = $state();
  let gallery = $state.raw(null);
  const holdMounted = mountHold();

  $effect(() => {
    if (gallery && strip) return holdMounted(strip);
  });

  function open(event, index) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey)
      return;
    event.preventDefault();
    event.stopPropagation();
    gallery = { items, index };
  }
</script>

{#if visible.length}
  <span
    class="tool-media-previews"
    bind:this={strip}
    bind:contentRect={bounds}
    style:--tool-media-height={bounds && `${bounds.height}px`}
  >
    {#each visible as item, index (item.src)}
      {@const label = item.source
        ? t('chat.toolMedia.openSource', { filename: item.filename })
        : t('chat.toolMedia.open', { filename: item.filename })}
      <a
        class="tool-media-preview"
        href={item.src}
        target="_blank"
        rel="noreferrer"
        aria-label={label}
        aria-haspopup="dialog"
        use:tooltip={label}
        onclick={(event) => open(event, index)}
      >
        <span class="tool-media-preview__frame">
          {#if item.kind === 'image'}
            <img
              src={item.src}
              alt=""
              loading="lazy"
              onerror={(event) => {
                event.currentTarget.hidden = true;
              }}
            />
            <svg
              class="tool-media-preview__missing"
              role="img"
              aria-label={t('chat.image.unavailable')}
              viewBox="0 0 24 24"
            >
              <rect x="2" y="4" width="20" height="16" rx="2" /><path
                d="m3 18 7-7 6 6 3-3 3 3M3 3l18 18"
              />
            </svg>
          {:else if item.kind === 'video'}
            <!-- A paused first frame; playback controls live in the viewer. -->
            <video
              src={item.src + '#t=0.001'}
              preload="metadata"
              muted
              playsinline
              aria-hidden="true"
              tabindex="-1"
            ></video>
            <svg
              class="tool-media-preview__play"
              viewBox="0 0 16 16"
              aria-hidden="true"><path d="M5 3v10l8-5z" /></svg
            >
          {:else}
            <svg viewBox="0 0 24 24" aria-hidden="true"
              ><path d="M3 10v4m4-7v10m5-14v18m5-14v10m4-7v4" /></svg
            >
          {/if}
        </span>
      </a>
    {/each}
    {#if remaining}
      {@const label = t('chat.toolMedia.openMore', { count: remaining })}
      <a
        class="tool-media-more"
        href={items[visibleLimit].src}
        target="_blank"
        rel="noreferrer"
        aria-label={label}
        aria-haspopup="dialog"
        use:tooltip={label}
        onclick={(event) => open(event, visibleLimit)}
      >
        <span aria-hidden="true">+{remaining}</span>
        <svg viewBox="0 0 12 12" aria-hidden="true"
          ><path d="m4.5 2.5 3.5 3.5-3.5 3.5" /></svg
        >
      </a>
    {/if}
  </span>
  {#if gallery}
    <MediaLightbox
      items={gallery.items}
      index={gallery.index}
      showCaption
      onClose={() => {
        gallery = null;
      }}
    />
  {/if}
{/if}
