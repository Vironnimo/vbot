<script>
  import { onMount } from 'svelte';
  import { getApplicationChangelog } from '$lib/api.js';
  import { compareVersions, releasesUrl } from '$lib/applicationUpdate.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import Modal from '../components/ui/Modal.svelte';
  import Banner from '../components/ui/Banner.svelte';
  import MarkdownContent from '../components/chat/MarkdownContent.svelte';

  let { request, onClose } = $props();
  let bundled = $state.raw(null);
  let failed = $state(false);
  onMount(() => {
    if (!request.releases) {
      getApplicationChangelog()
        .then((result) => {
          bundled = result.releases;
        })
        .catch(() => {
          failed = true;
        });
    }
  });
  const dates = new Intl.DateTimeFormat(activeLocaleTag(), {
    dateStyle: 'medium',
    timeZone: 'UTC',
  });
  const releases = $derived(
    (request.releases ?? bundled ?? [])
      .filter(
        (r) =>
          (!request.after ||
            r.version === request.until ||
            compareVersions(r.version, request.after) > 0) &&
          (!request.until || compareVersions(r.version, request.until) <= 0),
      )
      .map((release) => ({ ...release, body: remaining(release) })),
  );
  const warnings = $derived(releases.filter((r) => r.before_update));
  // Move the warning section to the front, retaining future section headings.
  const remaining = (r) =>
    r.markdown
      .split(/(?=^## )/m)
      .filter(
        (section) =>
          section.split('\n', 1)[0].trimEnd() !== '## Before you update',
      )
      .join('')
      .trim();
</script>

{#snippet dateLabel(date)}
  <time datetime={date}>{dates.format(new Date(date))}</time>
{/snippet}

<Modal title={t('app.whatsNew')} size="lg" class="changelog-dialog" {onClose}>
  {#snippet body()}
    <div class="modal-body changelog">
      {#if warnings.length}
        <section class="warnings">
          <h4 class="warnings-title">
            <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
              <path
                d="m10.3 4.6-8 14A1.6 1.6 0 0 0 3.7 21h16.6a1.6 1.6 0 0 0 1.4-2.4l-8-14a2 2 0 0 0-3.4 0Z"
              />
              <path d="M12 9v5m0 3h.01" />
            </svg>
            {t('app.beforeUpdate')}
          </h4>
          {#each warnings as r (r.version)}
            <div class="warning-row">
              <div class="warning-meta">
                <h5>v{r.version}</h5>
                {#if !r.body}{@render dateLabel(r.date)}{/if}
              </div>
              <MarkdownContent class="msg-markdown" source={r.before_update} />
            </div>
          {/each}
        </section>
      {/if}
      {#each releases as r (r.version)}
        {#if r.body}
          <section class="release">
            <header>
              <h4>v{r.version}</h4>
              {@render dateLabel(r.date)}
            </header>
            <MarkdownContent class="msg-markdown" source={r.body} />
          </section>
        {/if}
      {:else}
        {#if failed}
          <Banner variant="warn">{t('app.changelogFailed')}</Banner>
        {:else if !bundled && !request.releases}
          <p>{t('common.loading')}</p>
        {:else}
          <p>{t('app.changelogEmpty')}</p>
        {/if}
      {/each}
    </div>
  {/snippet}
  {#snippet footer()}
    <a
      class="release-link"
      href={releasesUrl}
      target="_blank"
      rel="noopener noreferrer"
    >
      {t('app.allReleases')}
      <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M6 18 18 6M7 6h11v11" />
      </svg>
    </a>
  {/snippet}
</Modal>

<style>
  :global(.changelog-dialog .modal-title) {
    font-size: var(--fs-heading-lg);
  }
  .changelog {
    --md-text: var(--text-body);
    --md-emphasis: var(--text-hi);
    --md-font-size: var(--fs-chat-prose);
    --md-line-height: 1.55;
    --md-heading-size: var(--fs-body-md);
    --md-h2-size: var(--fs-heading-sm);
    --md-heading-margin: 16px 0 5px;
    --md-list-margin: 0 0 8px 1.25em;
    --md-paragraph-margin: 0 0 8px;
    gap: 24px;
    overflow-wrap: anywhere;
  }
  .changelog > section {
    flex: none;
    min-width: 0;
  }
  .changelog h4,
  .changelog h5 {
    margin: 0;
  }
  .changelog :global(.msg-markdown) {
    min-width: 0;
    padding: 0;
  }
  .changelog :global(li:last-child) {
    margin-bottom: 0;
  }
  .warnings {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: baseline;
    gap: 12px 16px;
    padding: 14px 16px;
    border: 1px solid color-mix(in srgb, var(--amber) 30%, transparent);
    border-radius: var(--r-md);
    background: var(--amber-dim);
  }
  .warnings-title {
    display: flex;
    grid-column: 1 / -1;
    align-items: center;
    gap: 8px;
    color: var(--text-hi);
    font-size: var(--fs-heading-sm);
    line-height: 1.4;
  }
  .warnings-title svg {
    flex: none;
    color: var(--amber);
  }
  .warning-row {
    display: grid;
    grid-column: 1 / -1;
    grid-template-columns: subgrid;
    align-items: baseline;
  }
  .warning-row :global(.msg-markdown > ul:only-child:has(> li:only-child)) {
    margin-inline-start: 0;
    list-style: none;
  }
  .warning-meta h5 {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    font-weight: 600;
  }
  time {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
  }
  .release {
    display: grid;
    grid-template-columns: 7.5rem minmax(0, 1fr);
    align-items: baseline;
    gap: 20px;
  }
  .release + .release {
    padding-top: 20px;
    border-top: 1px solid var(--border);
  }
  .release header {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
  }
  .release h4 {
    color: var(--text-hi);
    font-size: var(--fs-heading-md);
    letter-spacing: -0.015em;
    line-height: 1.35;
  }
  .release-link {
    display: inline-flex;
    min-height: 32px;
    align-items: center;
    gap: 5px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    text-decoration: none;
  }
  .release-link:hover,
  .release-link:focus-visible {
    color: var(--text-hi);
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  @media (max-width: 640px) {
    .release {
      grid-template-columns: minmax(0, 1fr);
      gap: 10px;
    }
    .release header {
      flex-direction: row;
      flex-wrap: wrap;
      align-items: baseline;
      justify-content: space-between;
      gap: 4px 12px;
    }
  }
  @media (max-width: 480px) {
    .warnings {
      grid-template-columns: minmax(0, 1fr);
      padding: 12px 14px;
    }
    .warning-row {
      row-gap: 4px;
    }
    .warning-meta {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
    }
  }
</style>
