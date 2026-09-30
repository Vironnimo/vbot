<script>
  // The media of expanded Tool details, from `toolDetailMedia`: the files a
  // call started from under Sources, the ones it read, looked at or produced
  // under Media. Images are thumbnails that open the timeline lightbox (a
  // modifier click opens the file) and show a placeholder when the file is
  // gone; videos play in the browser's player, audio in the shared one.
  import { t } from '$lib/i18n.js';
  import AudioPlayer from '../ui/AudioPlayer.svelte';

  let { items } = $props();

  let groups = $derived(
    [
      {
        label: t('chat.toolMediaSources'),
        items: items.filter((item) => item.source),
      },
      {
        label: t('chat.toolMedia'),
        items: items.filter((item) => !item.source),
      },
    ].filter((group) => group.items.length > 0),
  );
</script>

{#each groups as group (group.label)}
  {@const images = group.items.filter((item) => item.kind === 'image')}
  {@const players = group.items.filter((item) => item.kind !== 'image')}
  <div class="teb-row teb-section tool-media">
    <div class="teb-section-header">
      <span class="teb-label">{group.label}</span>
    </div>
    {#if images.length > 0}
      <div class="tool-image-previews">
        {#each images as image (image.src)}
          <a
            class="tool-image-preview"
            href={image.src}
            target="_blank"
            rel="noreferrer"
            aria-label={image.filename}
          >
            <img
              src={image.src}
              alt={image.filename}
              loading="lazy"
              onerror={(event) => {
                event.currentTarget.hidden = true;
              }}
            />
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
            <span>{image.filename}</span>
          </a>
        {/each}
      </div>
    {/if}
    {#each players as item (item.src)}
      <figure class="tool-media__player">
        {#if item.kind === 'video'}
          <!-- Tool videos carry no captions to offer. -->
          <!-- svelte-ignore a11y_media_has_caption -->
          <video
            class="tool-media__video"
            src={item.src}
            controls
            preload="metadata"
            aria-label={item.filename}
          ></video>
        {:else}
          <AudioPlayer src={item.src} ariaLabel={item.filename} />
        {/if}
        <figcaption>
          <a href={item.src} target="_blank" rel="noreferrer">{item.filename}</a
          >
        </figcaption>
      </figure>
    {/each}
  </div>
{/each}
