<script>
  import { portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';

  let { src = '', alt = '', onClose = () => {} } = $props();

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

<div
  use:portal
  class="image-lightbox"
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
  <img
    class="image-lightbox__image"
    {src}
    alt={alt || t('chat.image.alt', 'Image')}
  />
</div>
