<script>
  import AudioPlayer from './ui/AudioPlayer.svelte';
  import { onMount, onDestroy, tick } from 'svelte';
  import { activateModalFocus } from './ui/modalFocus.js';
  import { portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import { useNavigation } from '$lib/navigation.svelte.js';
  import { tooltip } from '$lib/tooltip.js';

  let { items, index = 0, showCaption = false, onClose = () => {} } = $props();
  let item = $derived(items[index]);

  // Back closes the lightbox instead of navigating.
  const navigation = useNavigation();
  let overlayElement;
  let closeElement;
  let releaseFocus = () => {};
  onMount(() => {
    releaseFocus = activateModalFocus({
      dialog: overlayElement,
      initialFocus: closeElement,
      onClose: () => onClose(),
      navigation,
    });
  });
  onDestroy(() => releaseFocus());

  let imageElement = $state();
  let zoomed = $state(false);
  let canZoom = $state(false);
  let failedSrc = $state(null);

  function evaluateZoomable() {
    if (!imageElement || !imageElement.naturalWidth) {
      canZoom = false;
      return;
    }
    // Compare with the fitted image, including space reserved for the toolbar.
    canZoom =
      imageElement.naturalWidth > imageElement.clientWidth ||
      imageElement.naturalHeight > imageElement.clientHeight;
  }

  function handleResize() {
    if (!zoomed) {
      evaluateZoomable();
    }
  }

  async function handleImageClick(event) {
    event.stopPropagation();
    if (zoomed) {
      zoomed = false;
      await tick();
      evaluateZoomable();
    } else if (canZoom) {
      zoomed = true;
    }
  }

  function move(step) {
    index = (index + step + items.length) % items.length;
    zoomed = false;
    canZoom = false;
    failedSrc = null;
    overlayElement.scrollTop = 0;
    overlayElement.scrollLeft = 0;
  }

  function handleGalleryKey(event) {
    // Player shortcuts (seek, volume, speed) belong to the active player.
    if (
      items.length < 2 ||
      event.defaultPrevented ||
      event.target.closest('video, .audio-player')
    )
      return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      move(event.key === 'ArrowLeft' ? -1 : 1);
    }
  }

  function handleOverlayClick(event) {
    if (event.target === event.currentTarget) {
      onClose();
    }
  }
</script>

<svelte:window onresize={handleResize} />

<!-- The shared modal owner handles Escape and traps keyboard focus. -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions, a11y_click_events_have_key_events -->
<div
  bind:this={overlayElement}
  use:portal
  class="image-lightbox"
  class:image-lightbox--zoomed={zoomed}
  role="dialog"
  aria-modal="true"
  aria-label={item.filename || t('chat.image.alt')}
  tabindex="-1"
  onclick={handleOverlayClick}
  onkeydown={handleGalleryKey}
  class:image-lightbox--captioned={showCaption}
>
  <button
    bind:this={closeElement}
    type="button"
    class="image-lightbox__close"
    aria-label={t('common.close')}
    onclick={onClose}
  >
    ×
  </button>
  {#if showCaption}
    <div class="media-lightbox__toolbar">
      {#if items.length > 1}
        <button
          type="button"
          aria-label={t('common.previous')}
          onclick={() => move(-1)}>‹</button
        >
      {/if}
      <div
        class="media-lightbox__caption"
        aria-live="polite"
        aria-atomic="true"
      >
        {#if items.length > 1}
          <span
            >{t('chat.toolMedia.position', {
              current: index + 1,
              total: items.length,
            })}</span
          >
        {/if}
        {#if item.source}<span>{t('chat.toolMedia.source')}</span>{/if}
        <a href={item.src} target="_blank" rel="noreferrer">{item.filename}</a>
      </div>
      {#if items.length > 1}
        <button
          type="button"
          aria-label={t('common.next')}
          onclick={() => move(1)}>›</button
        >
      {/if}
    </div>
  {/if}
  {#key item.src}
    {#if item.kind === 'audio'}
      <AudioPlayer src={item.src} ariaLabel={item.filename} />
    {:else if item.kind === 'video'}
      {#if failedSrc === item.src}
        <span class="image-unavailable" role="status"
          >{t('chat.toolMedia.videoUnavailable')}</span
        >
      {:else}
        <!-- Tool videos carry no captions to offer. -->
        <!-- svelte-ignore a11y_media_has_caption -->
        <video
          class="media-lightbox__video"
          src={item.src}
          controls
          tabindex="0"
          playsinline
          preload="metadata"
          aria-label={item.filename}
          onerror={() => {
            failedSrc = item.src;
          }}
        ></video>
      {/if}
    {:else if failedSrc === item.src}
      <span
        class="image-unavailable"
        role="img"
        aria-label={t('chat.image.unavailable')}
      >
        <svg viewBox="0 0 32 24" aria-hidden="true"
          ><rect x="1" y="1" width="30" height="22" rx="2" /><circle
            cx="10"
            cy="8"
            r="2"
          /><path d="m3 20 8-8 6 6 4-4 8 6M3 2l26 20" /></svg
        >
        <span>{t('chat.image.unavailable')}</span>
      </span>
    {:else}
      <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
      <img
        bind:this={imageElement}
        class="image-lightbox__image"
        class:zoomable={canZoom}
        class:zoomed
        src={item.src}
        alt={item.filename || t('chat.image.alt')}
        use:tooltip={zoomed
          ? t('chat.image.zoomOut')
          : canZoom
            ? t('chat.image.zoomIn')
            : ''}
        onerror={() => {
          failedSrc = item.src;
          zoomed = false;
          canZoom = false;
        }}
        onload={evaluateZoomable}
        onclick={handleImageClick}
      />
    {/if}
  {/key}
</div>
