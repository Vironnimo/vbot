<script>
  // The Matches section of expanded Tool details: what a Tool found, each item
  // with its title (a link when it has a web address), kind, local time and an
  // excerpt, in a scroll box of bounded height. Items come from
  // `toolDetailBlocks`.
  import { boundedScroll } from '$lib/boundedScroll.js';
  import { t } from '$lib/i18n.js';
  import { formatAbsoluteTime } from '$lib/timeText.js';

  let { items } = $props();
</script>

<div class="teb-row teb-section tool-results">
  <div class="teb-section-header">
    <span class="teb-label">{t('chat.toolMatches')}</span>
  </div>
  <div
    class="teb-scroll tool-results__body"
    role="group"
    aria-label={t('chat.toolMatches')}
    use:boundedScroll
  >
    <ol class="tool-results__list">
      {#each items as item, index (index)}
        {@const time = item.time ? formatAbsoluteTime(item.time) : ''}
        <li class="tool-results__item">
          <div class="tool-results__header">
            {#if item.url}
              <a
                class="tool-results__title tool-results__link"
                href={item.url}
                target="_blank"
                rel="noopener noreferrer">{item.title}</a
              >
            {:else}
              <span class="tool-results__title">{item.title}</span>
            {/if}
            {#if item.meta}
              <span class="tool-results__meta">{item.meta}</span>
            {/if}
            {#if time}
              <time class="tool-results__time" datetime={item.time}>{time}</time
              >
            {/if}
          </div>
          {#if item.text}
            <p class="tool-results__text">{item.text}</p>
          {/if}
        </li>
      {/each}
    </ol>
  </div>
</div>
