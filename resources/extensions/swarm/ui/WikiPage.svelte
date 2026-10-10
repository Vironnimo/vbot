<script>
  // One Wiki page as read: number and title, its revision line, the page
  // actions, the version history and the Markdown. WikiPanel owns the page,
  // its history and every request; this component shows them.
  import { t } from '../../../../webui/src/lib/i18n.js';
  import { formatMoment } from '../../../../webui/src/lib/timeText.js';
  import Badge from '../../../../webui/src/components/ui/Badge.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import CopyButton from '../../../../webui/src/components/ui/CopyButton.svelte';
  import MarkdownContent from '../../../../webui/src/components/chat/MarkdownContent.svelte';

  let {
    page,
    content,
    history,
    historyMore,
    historyOpen,
    busy,
    now,
    contentLinks,
    onEdit,
    onToggleHistory,
    onShowRevision,
    onLoadMoreHistory,
    onBackToCurrent,
    onRestore,
    onDelete,
  } = $props();

  let root = $state(null);
  let heading = $state(null);

  /** Moves keyboard focus to the page title. */
  export function focusTitle() {
    heading?.focus();
  }

  /** Moves keyboard focus to Edit, or to the title while Edit is unavailable. */
  export function focusEdit() {
    const edit = root?.querySelector('[data-wiki-action="edit"]');
    if (edit && !edit.disabled) edit.focus();
    else focusTitle();
  }

  /** Moves keyboard focus to the history row of `revision`. */
  export function focusVersion(revision) {
    root?.querySelector(`.wiki-version[data-revision="${revision}"]`)?.focus();
  }

  // A page usually opens with its title as a level-one heading, which the
  // page header already shows.
  const TITLE_HEADING =
    /^(?:[ \t]*\r?\n)*[ ]{0,3}#[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*(?:\r?\n|$)/;
  const comparable = (text) => text.replace(/\s+/g, ' ').trim().toLowerCase();
  function withoutTitleHeading(source, pageTitle) {
    const match = TITLE_HEADING.exec(source);
    return match && comparable(match[1]) === comparable(pageTitle)
      ? source.slice(match[0].length)
      : source;
  }

  const reference = $derived(page.number != null ? `w${page.number}` : '');
  const olderRevision = $derived(page.revision !== page.current_revision);
  const editable = $derived(!page.deleted && !olderRevision);
  const body = $derived(withoutTitleHeading(content, page.title));
  // Revision lines read like "Revision 4 · Ella · Oct 10, 2026, 7:57 PM ·
  // 2 hours ago", leaving out what is unknown.
  const metaLine = (...parts) => parts.filter(Boolean).join(' · ');
  const moment = (value) => (value ? formatMoment(value, { nowMs: now }) : '');
  const pageMeta = $derived(
    metaLine(
      t('swarm.wiki.revisionNumber', { revision: page.revision }),
      page.author?.name,
      moment(page.updated_at),
    ),
  );
  const versionLine = (version) =>
    metaLine(version.author?.name, moment(version.updated_at));
</script>

<article class="wiki-page" aria-labelledby="wiki-page-title" bind:this={root}>
  <header class="wiki-page__header">
    <h3
      id="wiki-page-title"
      class="wiki-page__title"
      tabindex="-1"
      bind:this={heading}
    >
      {#if reference}<span class="wiki-number">{reference}</span>{/if}<span
        >{page.title}</span
      >
    </h3>
    <p class="wiki-page__meta">{pageMeta}</p>
  </header>
  <div class="wiki-page__actions">
    <Button
      variant="secondary"
      data-wiki-action="edit"
      disabled={!editable}
      disabledReason={page.deleted
        ? t('swarm.wiki.editDeleted')
        : t('swarm.wiki.editOlder')}
      onClick={onEdit}>{t('common.edit')}</Button
    >
    <Button
      variant="tertiary"
      aria-expanded={historyOpen}
      aria-controls={historyOpen ? 'wiki-history' : undefined}
      onClick={onToggleHistory}>{t('swarm.wiki.history')}</Button
    >
    <CopyButton
      text={page.link}
      label={t('swarm.wiki.copyLink')}
      copiedLabel={t('swarm.wiki.linkCopied')}
    />
    {#if editable}<Button
        variant="tertiary"
        class="wiki-page__delete"
        disabled={busy}
        onClick={onDelete}>{t('swarm.wiki.delete')}</Button
      >{/if}
  </div>
  {#if historyOpen}
    <section
      id="wiki-history"
      class="wiki-history"
      aria-labelledby="wiki-history-title"
    >
      <h4 id="wiki-history-title" class="wiki-history__title">
        {t('swarm.wiki.historyTitle')}
      </h4>
      <ol class="wiki-history__list">
        {#each history as version (version.revision)}
          {@const shown = version.revision === page.revision}
          <li>
            <button
              type="button"
              class="secondary-list__item wiki-version"
              class:active={shown}
              aria-current={shown ? 'true' : undefined}
              data-revision={version.revision}
              onclick={() => onShowRevision(version.revision)}
            >
              <span class="wiki-version__name"
                >{t('swarm.wiki.revisionNumber', {
                  revision: version.revision,
                })}{#if version.revision === page.current_revision}<Badge
                    variant="info">{t('swarm.wiki.current')}</Badge
                  >{/if}{#if version.deleted}<span class="wiki-version__deleted"
                    >{t('swarm.wiki.deleted')}</span
                  >{/if}</span
              >
              <span class="wiki-version__meta">{versionLine(version)}</span>
            </button>
          </li>
        {/each}
      </ol>
      {#if historyMore}<div class="wiki-history__more">
          <Button variant="tertiary" onClick={onLoadMoreHistory}
            >{t('swarm.wiki.historyMore')}</Button
          >
        </div>{/if}
    </section>
  {/if}
  {#if olderRevision}<Banner variant="info" class="wiki-page__notice"
      ><p>
        {t('swarm.wiki.viewingRevision', {
          revision: page.revision,
          total: page.current_revision,
        })}
      </p>
      <div class="wiki-page__notice-actions">
        <Button variant="secondary" disabled={busy} onClick={onRestore}
          >{t('swarm.wiki.restore')}</Button
        ><Button variant="tertiary" onClick={onBackToCurrent}
          >{t('swarm.wiki.backToCurrent')}</Button
        >
      </div></Banner
    >{:else if page.deleted}<Banner variant="warn" class="wiki-page__notice"
      ><p>{t('swarm.wiki.deletedHelp')}</p>
      <div class="wiki-page__notice-actions">
        <Button variant="secondary" disabled={busy} onClick={onRestore}
          >{t('swarm.wiki.restorePage')}</Button
        >
      </div></Banner
    >{/if}
  <div class="wiki-page__body" use:contentLinks>
    <MarkdownContent source={body} class="msg-markdown md-document" />
  </div>
</article>

<style>
  .wiki-page {
    display: grid;
    gap: var(--space-md);
    min-width: 0;
  }
  .wiki-page__header {
    display: grid;
    gap: var(--space-xs);
    min-width: 0;
  }
  .wiki-page__title {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin: 0;
    color: var(--text-hi);
    font: 600 var(--fs-heading-lg) / 1.3 var(--font-ui);
    overflow-wrap: anywhere;
  }
  /* Focus lands on the title only from code (opening a page); it is no
     control, so it shows no ring. */
  .wiki-page__title:focus {
    outline: none;
  }
  .wiki-number {
    flex-shrink: 0;
    color: var(--text-lo);
    font: 600 var(--fs-label-md) / 1.4 var(--font-ui);
    font-variant-numeric: tabular-nums;
  }
  .wiki-page__meta {
    margin: 0;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
  }
  /* A fixed order: Edit, History, Copy link, and Delete apart at the end. */
  .wiki-page__actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
  }
  .wiki-page__actions :global(.wiki-page__delete) {
    margin-inline-start: auto;
  }
  .wiki-page__actions :global(.wiki-page__delete:not(:disabled):hover) {
    color: var(--red);
  }
  .wiki-history {
    display: grid;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
  }
  .wiki-history__title {
    margin: 2px 8px 2px;
    color: var(--text-lo);
    font: 600 var(--fs-label-sm) / 1.4 var(--font-ui);
  }
  .wiki-history__list {
    display: grid;
    gap: 2px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .wiki-version {
    display: grid;
    gap: 2px;
    padding: 7px 8px;
    color: var(--text-hi);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .wiki-version__name {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
    font-size: var(--fs-body-md);
    font-weight: 500;
  }
  .wiki-version__deleted,
  .wiki-version__meta {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-weight: 400;
  }
  .wiki-history__more {
    display: flex;
  }
  .wiki-page :global(.wiki-page__notice) {
    flex-wrap: wrap;
  }
  .wiki-page__notice-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-xs);
  }
  .wiki-page__body {
    min-width: 0;
    padding-top: var(--space-lg);
    border-top: 1px solid var(--border);
    overflow-wrap: anywhere;
  }
</style>
