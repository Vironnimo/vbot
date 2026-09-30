<script>
  // The Media section of expanded Tool details: the images a Tool produced as
  // previews that open in a new tab, its videos and audio as players. Items
  // come from `toolDetailMedia`.
  import { t } from '$lib/i18n.js';

  let { items } = $props();
</script>

<div class="teb-row teb-section tool-media">
  <div class="teb-section-header">
    <span class="teb-label">{t('chat.toolMedia')}</span>
  </div>
  <ul class="tool-media__list">
    {#each items as item (item.src)}
      <li class="tool-media__item">
        {#if item.kind === 'image'}
          <a
            class="tool-media__image"
            href={item.src}
            target="_blank"
            rel="noreferrer"
            aria-label={item.filename}
          >
            <img src={item.src} alt={item.filename} loading="lazy" />
          </a>
        {:else if item.kind === 'video'}
          <!-- Generated videos carry no captions to offer. -->
          <!-- svelte-ignore a11y_media_has_caption -->
          <video
            class="tool-media__video"
            src={item.src}
            controls
            preload="metadata"
            aria-label={item.filename}
          ></video>
        {:else}
          <audio
            class="tool-media__audio"
            src={item.src}
            controls
            preload="metadata"
            aria-label={item.filename}
          ></audio>
        {/if}
        <a
          class="tool-media__name"
          href={item.src}
          target="_blank"
          rel="noreferrer">{item.filename}</a
        >
      </li>
    {/each}
  </ul>
</div>
