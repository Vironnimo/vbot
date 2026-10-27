<script>
  import { onMount, onDestroy } from 'svelte';
  import { activateModalFocus } from './ui/modalFocus.js';
  import { portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import { useNavigation } from '$lib/navigation.svelte.js';
  import { tooltip } from '$lib/tooltip.js';

  let { src = '', alt = '', onClose = () => {} } = $props();

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
  aria-label={alt || t('chat.image.alt')}
  tabindex="-1"
  onclick={handleOverlayClick}
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
  {#if failedSrc === src}
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
      {src}
      alt={alt || t('chat.image.alt')}
      use:tooltip={zoomed
        ? t('chat.image.zoomOut')
        : canZoom
          ? t('chat.image.zoomIn')
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
