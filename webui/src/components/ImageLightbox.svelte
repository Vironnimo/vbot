<script>
  import { portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';

  let { src = '', alt = '', onClose = () => {} } = $props();

  let imageElement = $state();
  let zoomed = $state(false);
  let canZoom = $state(false);

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
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
  <img
    bind:this={imageElement}
    class="image-lightbox__image"
    class:zoomable={canZoom}
    class:zoomed
    {src}
    alt={alt || t('chat.image.alt', 'Image')}
    title={zoomed
      ? t('chat.image.zoomOut', 'Click to fit')
      : canZoom
        ? t('chat.image.zoomIn', 'Click to view full size')
        : ''}
    onload={evaluateZoomable}
    onclick={handleImageClick}
  />
</div>
