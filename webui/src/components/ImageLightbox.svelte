<script>
  import { portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  let { src = '', alt = '', onClose = () => {} } = $props();

  let imageElement = $state();
  let zoomed = $state(false);
  let canZoom = $state(false);
  let failedSrc = $state(null);

  function evaluateZoomable() {
    if (!imageElement || !imageElement.naturalWidth) {
      canZoom = false;
      return;
    }
    // In fit mode the image is bounded by the viewport, so it can only be
    // zoomed in when its natural size exceeds the available space.
    canZoom =
      imageElement.naturalWidth > window.innerWidth ||
      imageElement.naturalHeight > window.innerHeight;
  }

  function handleResize() {
    if (!zoomed) {
      evaluateZoomable();
    }
  }

  function handleImageClick(event) {
    event.stopPropagation();
    if (zoomed) {
      zoomed = false;
      evaluateZoomable();
    } else if (canZoom) {
      zoomed = true;
    }
  }

  function handleOverlayClick(event) {
    if (event.target === event.currentTarget) {
      onClose();
    }
  }

  function handleKeydown(event) {
    if (event.key === 'Escape') {
      onClose();
    }
  }
</script>

<svelte:document onkeydown={handleKeydown} />
<svelte:window onresize={handleResize} />

<div
  use:portal
  class="image-lightbox"
  class:image-lightbox--zoomed={zoomed}
  role="presentation"
  onclick={handleOverlayClick}
>
  <button
    type="button"
    class="image-lightbox__close"
    aria-label={t('common.close', 'Close')}
    onclick={onClose}
  >
    ×
  </button>
  {#if failedSrc === src}
    <span
      class="image-unavailable"
      role="img"
      aria-label={t('chat.image.unavailable', 'Image not available')}
    >
      <svg viewBox="0 0 32 24" aria-hidden="true"
        ><rect x="1" y="1" width="30" height="22" rx="2" /><circle
          cx="10"
          cy="8"
          r="2"
        /><path d="m3 20 8-8 6 6 4-4 8 6M3 2l26 20" /></svg
      >
      <span>{t('chat.image.unavailable', 'Image not available')}</span>
    </span>
  {:else}
    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
    <img
      bind:this={imageElement}
      class="image-lightbox__image"
      class:zoomable={canZoom}
      class:zoomed
      {src}
      alt={alt || t('chat.image.alt', 'Image')}
      use:tooltip={zoomed
        ? t('chat.image.zoomOut', 'Click to fit')
        : canZoom
          ? t('chat.image.zoomIn', 'Click to view full size')
          : ''}
      onerror={() => {
        failedSrc = src;
        zoomed = false;
        canZoom = false;
      }}
      onload={evaluateZoomable}
      onclick={handleImageClick}
    />
  {/if}
</div>
