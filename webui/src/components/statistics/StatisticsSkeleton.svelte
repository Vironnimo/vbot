<script>
  // Placeholder in the shape of a Statistics tab while its first answer
  // loads: the tab's tiles and blocks as quiet shapes, so the layout does
  // not jump when the numbers arrive.
  import { t } from '$lib/i18n.js';

  let { tab = 'overview' } = $props();

  const SHAPES = {
    overview: { tiles: 6, blocks: ['chart', 'row'] },
    usage: { tiles: 6, blocks: ['table', 'chart'] },
    runs: { tiles: 5, blocks: ['table', 'chart'] },
    tools: { tiles: 4, blocks: ['table', 'table'] },
    extensions: { tiles: 6, blocks: ['table'] },
    diagnostics: { tiles: 0, blocks: ['line', 'line', 'line', 'line', 'line'] },
  };

  const shape = $derived(SHAPES[tab] ?? SHAPES.overview);
</script>

<div class="stats-panel stats-skeleton">
  <span class="stats-sr-only" role="status">{t('statistics.loading')}</span>
  {#if shape.tiles > 0}
    <div
      class={['stats-tiles', `stats-tiles--${shape.tiles}`]}
      aria-hidden="true"
    >
      {#each { length: shape.tiles }, index (index)}
        <div class="stats-tile stats-skeleton__tile">
          <span class="stats-skeleton__bar stats-skeleton__bar--label"></span>
          <span class="stats-skeleton__bar stats-skeleton__bar--value"></span>
          <span class="stats-skeleton__bar stats-skeleton__bar--detail"></span>
        </div>
      {/each}
    </div>
  {/if}
  {#each shape.blocks as block, index (index)}
    {#if block === 'row'}
      <div class="stats-columns stats-columns--three" aria-hidden="true">
        {#each { length: 3 }, column (column)}
          <div class="stats-block stats-skeleton__block">
            <span class="stats-skeleton__bar stats-skeleton__bar--title"></span>
            <span class="stats-skeleton__fill stats-skeleton__fill--short"
            ></span>
          </div>
        {/each}
      </div>
    {:else if block === 'line'}
      <div
        class="stats-skeleton__line stats-skeleton__bar"
        aria-hidden="true"
      ></div>
    {:else}
      <div class="stats-block stats-skeleton__block" aria-hidden="true">
        <span class="stats-skeleton__bar stats-skeleton__bar--title"></span>
        <span class={['stats-skeleton__fill', `stats-skeleton__fill--${block}`]}
        ></span>
      </div>
    {/if}
  {/each}
</div>
