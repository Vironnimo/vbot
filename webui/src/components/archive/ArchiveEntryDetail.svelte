<script>
  // The page of one archive entry, opened in place of the list: Back and a
  // breadcrumb to return; a header with its kind and the actions Restore,
  // Restore as... and Delete permanently; why it cannot be restored and what
  // a restore changes; when vBot deletes it; its recorded details, Sessions
  // and files.
  import Badge from '../ui/Badge.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    archiveDeletionText,
    archiveKindLabel,
    canRestoreAs,
    isPurgeable,
    missingScopeEntryId,
    ownerGroupReasonText,
    restoreProblemText,
    treeRoleLabel,
  } from '$lib/archiveView.js';
  import { formatAgentAddress } from '$lib/agentAddress.js';
  import { t } from '$lib/i18n.js';
  import { formatMoment } from '$lib/timeText.js';
  import { tooltip } from '$lib/tooltip.js';

  const noop = () => {};

  let {
    entryId = '',
    detail = null,
    loading = false,
    error = '',
    notFound = false,
    busy = false,
    retentionDays = undefined,
    agentNames = new Map(),
    projectNames = new Map(),
    onBack = noop,
    onRetry = noop,
    onRestore = noop,
    onRestoreAs = noop,
    onPurge = noop,
    onOpenEntry = noop,
  } = $props();

  let element = $state();
  let entry = $derived(detail?.entry ?? null);
  let label = $derived(entry?.label || entry?.subject_id || entryId);
  let details = $derived(detail?.details ?? {});
  let restoreCheck = $derived(detail?.restore ?? null);
  let blockers = $derived(restoreCheck?.blockers ?? []);
  let warnings = $derived(restoreCheck?.warnings ?? []);
  let restorableKind = $derived(
    entry?.kind === 'agent' ||
      entry?.kind === 'project' ||
      entry?.kind === 'session',
  );
  let restoreAs = $derived(canRestoreAs(entry, restoreCheck));
  let trees = $derived(detail?.files?.trees ?? []);
  let userFolders = $derived(trees.filter((tree) => tree.user_folder));
  let sessions = $derived(detail?.sessions ?? []);
  let moreSessions = $derived(
    Math.max(0, (detail?.session_count ?? 0) - sessions.length),
  );
  let deletionText = $derived(
    entry ? archiveDeletionText(entry, retentionDays, userFolders) : '',
  );
  let restoreDisabledReason = $derived(
    blockers.length ? restoreProblemText(blockers[0]) : '',
  );

  const agentName = (id) => agentNames.get(id) || id;
  const projectName = (id) => projectNames.get(id) || id;

  export function focus() {
    element?.focus();
  }
</script>

<section
  class="archive-detail"
  tabindex="-1"
  bind:this={element}
  aria-labelledby="archive-detail-title"
>
  <nav class="archive-crumbs" aria-label={t('archive.detail.breadcrumb')}>
    <Button
      variant="tertiary"
      icon
      ariaLabel={t('archive.detail.backToList')}
      tooltip={t('archive.detail.backHint')}
      onClick={onBack}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        stroke-width="1.5"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"><path d="M13 8H3m4.5-4.5L3 8l4.5 4.5" /></svg
      >
    </Button>
    <ol class="archive-crumbs__trail">
      <li>
        <button type="button" class="archive-crumbs__link" onclick={onBack}
          >{t('archive.title')}</button
        >
      </li>
      <li class="archive-crumbs__current" aria-current="page">{label}</li>
    </ol>
  </nav>

  {#if notFound}
    <EmptyState
      title={t('archive.detail.notFound')}
      description={t('archive.detail.notFoundDescription')}
    >
      {#snippet actions()}
        <Button variant="secondary" onClick={onBack}
          >{t('archive.detail.backToList')}</Button
        >
      {/snippet}
    </EmptyState>
  {:else if !detail && loading}
    <Banner variant="neutral">{t('common.loading')}</Banner>
  {:else if !detail && error}
    <Banner variant="error" aria-live="polite"
      ><span>{error}</span><Button variant="secondary" onClick={onRetry}
        >{t('common.retry')}</Button
      ></Banner
    >
  {:else if detail}
    <div class="archive-detail__body">
      <header class="view-header archive-detail__header">
        <div class="view-header__intro">
          <h2
            id="archive-detail-title"
            class="view-header__title archive-detail__title"
          >
            {label}
          </h2>
          <p class="archive-detail__meta">
            <Badge>{archiveKindLabel(entry.kind)}</Badge>
            <span
              >{t('archive.detail.archivedAt', {
                moment: formatMoment(entry.archived_at),
              })}</span
            >
          </p>
        </div>
        <div class="view-header__actions">
          {#if restorableKind}
            <Button
              variant="primary"
              disabled={busy || !restoreCheck?.possible}
              disabledReason={busy ? '' : restoreDisabledReason}
              onClick={onRestore}>{t('archive.action.restore')}</Button
            >
          {/if}
          {#if restoreAs}
            <Button variant="secondary" disabled={busy} onClick={onRestoreAs}
              >{t('archive.action.restoreAs')}</Button
            >
          {/if}
          <Button
            variant="danger"
            disabled={busy || !isPurgeable(entry)}
            onClick={onPurge}>{t('archive.deletePermanently')}</Button
          >
        </div>
      </header>

      {#if error}
        <Banner variant="error" aria-live="polite"
          ><span>{error}</span><Button variant="secondary" onClick={onRetry}
            >{t('common.retry')}</Button
          ></Banner
        >
      {/if}

      {#if blockers.length > 0}
        <Banner variant="warn" class="archive-detail__problems">
          <div>
            <p>{t('archive.detail.blocked')}</p>
            <ul>
              {#each blockers as blocker, index (index)}
                <li>
                  {restoreProblemText(blocker)}
                  {#if missingScopeEntryId(blocker)}
                    <button
                      type="button"
                      class="archive-detail__link"
                      onclick={() => onOpenEntry(missingScopeEntryId(blocker))}
                      >{t('archive.detail.openMissingScope')}</button
                    >
                  {/if}
                </li>
              {/each}
            </ul>
          </div>
        </Banner>
      {/if}
      {#if warnings.length > 0}
        <Banner variant="info" class="archive-detail__problems">
          <div>
            <p>{t('archive.detail.warnings')}</p>
            <ul>
              {#each warnings as warning, index (index)}
                <li>{restoreProblemText(warning, { warning: true })}</li>
              {/each}
            </ul>
          </div>
        </Banner>
      {/if}

      <section class="s-section" aria-labelledby="archive-detail-facts">
        <div class="s-section__head">
          <h3 id="archive-detail-facts" class="s-section__title">
            {t('archive.detail.facts')}
          </h3>
        </div>
        <dl class="s-group archive-facts">
          {#if deletionText}
            <div class="archive-fact">
              <dt>{t('archive.detail.deletion')}</dt>
              <dd>{deletionText}</dd>
            </div>
          {/if}
          <div class="archive-fact">
            <dt>{t('archive.detail.id')}</dt>
            <dd class="archive-fact__code">{entry.subject_id}</dd>
          </div>
          {#if entry.kind === 'session' && entry.agent_id}
            <div class="archive-fact">
              <dt>{t('archive.detail.agent')}</dt>
              <dd>{agentName(entry.agent_id)}</dd>
            </div>
          {/if}
          {#if entry.kind === 'session' && entry.project_id}
            <div class="archive-fact">
              <dt>{t('archive.detail.project')}</dt>
              <dd>{projectName(entry.project_id)}</dd>
            </div>
          {/if}
          {#if entry.kind === 'owner_group'}
            <div class="archive-fact">
              <dt>{t('archive.detail.extension')}</dt>
              <dd>{entry.owner_name}</dd>
            </div>
            {#if details.reason}
              <div class="archive-fact">
                <dt>{t('archive.detail.reason')}</dt>
                <dd>{ownerGroupReasonText(details.reason)}</dd>
              </div>
            {/if}
          {/if}
          {#if details.cwd}
            <div class="archive-fact">
              <dt>{t('archive.detail.repository')}</dt>
              <dd class="archive-fact__code">{details.cwd}</dd>
            </div>
          {/if}
          {#if details.root_project_id}
            <div class="archive-fact">
              <dt>{t('archive.detail.rootProject')}</dt>
              <dd>{projectName(details.root_project_id)}</dd>
            </div>
          {/if}
          {#if details.workspace?.external && details.workspace?.path}
            <div class="archive-fact">
              <dt>{t('archive.detail.workspace')}</dt>
              <dd>
                <span class="archive-fact__code">{details.workspace.path}</span>
                <span class="archive-fact__note"
                  >{t('archive.detail.workspaceExternal')}</span
                >
              </dd>
            </div>
          {/if}
          {#if details.grants?.length}
            <div class="archive-fact">
              <dt>{t('archive.detail.grants')}</dt>
              <dd>{details.grants.map(agentName).join(', ')}</dd>
            </div>
          {/if}
          {#if details.unrooted_agents?.length}
            <div class="archive-fact">
              <dt>{t('archive.detail.unrootedAgents')}</dt>
              <dd>{details.unrooted_agents.map(agentName).join(', ')}</dd>
            </div>
          {/if}
        </dl>
      </section>

      {#if sessions.length > 0}
        <section class="s-section" aria-labelledby="archive-detail-sessions">
          <div class="s-section__head">
            <h3 id="archive-detail-sessions" class="s-section__title">
              {t('archive.detail.sessions')}
            </h3>
            <span class="archive-detail__count">{detail.session_count}</span>
          </div>
          <ul class="s-group archive-items">
            {#each sessions as session (session.session_id + session.agent_id + (session.project_id ?? ''))}
              <li class="archive-item">
                <span class="archive-item__main">
                  <span class="archive-item__title"
                    >{session.title || session.session_id}</span
                  >
                  {#if entry.kind !== 'session'}
                    <span class="archive-item__sub archive-fact__code"
                      >{formatAgentAddress(
                        session.agent_id,
                        session.project_id,
                      )}</span
                    >
                  {/if}
                </span>
                {#if session.last_activity_at || session.created_at}
                  <span class="archive-item__aside"
                    >{t('archive.detail.lastActivity', {
                      moment: formatMoment(
                        session.last_activity_at || session.created_at,
                      ),
                    })}</span
                  >
                {/if}
              </li>
            {/each}
            {#if moreSessions > 0}
              <li class="archive-item archive-item--more">
                {t('archive.detail.moreSessions', { count: moreSessions })}
              </li>
            {/if}
          </ul>
        </section>
      {/if}

      {#if trees.length > 0}
        <section class="s-section" aria-labelledby="archive-detail-files">
          <div class="s-section__head">
            <h3 id="archive-detail-files" class="s-section__title">
              {t('archive.detail.files')}
            </h3>
          </div>
          <ul class="s-group archive-items">
            {#each trees as tree (tree.path)}
              <li class="archive-item">
                <span class="archive-item__main">
                  <span class="archive-item__title"
                    >{treeRoleLabel(tree.role)}</span
                  >
                  <span class="archive-item__sub archive-fact__code"
                    >{tree.path}</span
                  >
                </span>
                {#if tree.user_folder}
                  <span
                    class="archive-item__aside tooltip-anchor"
                    use:tooltip={t('archive.detail.userFolderHint')}
                    ><Badge variant="info"
                      >{t('archive.detail.userFolder')}</Badge
                    ></span
                  >
                {/if}
              </li>
            {/each}
          </ul>
        </section>
      {/if}
    </div>
  {/if}
</section>

<style>
  .archive-detail {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 14px;
    outline: none;
  }

  .archive-crumbs {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    margin-left: -8px;
  }

  .archive-crumbs__trail {
    display: flex;
    align-items: center;
    min-width: 0;
    margin: 0;
    padding: 0;
    list-style: none;
    color: var(--text-lo);
    font: 400 var(--fs-body-sm) / 1.4 var(--font-ui);
  }

  .archive-crumbs__trail li {
    display: flex;
    align-items: center;
    min-width: 0;
  }

  .archive-crumbs__trail li + li::before {
    content: '›';
    margin: 0 8px;
    color: var(--text-faint);
  }

  .archive-crumbs__link,
  .archive-detail__link {
    padding: 2px 4px;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-med);
    background: transparent;
    font: inherit;
    white-space: nowrap;
    cursor: pointer;
  }

  .archive-crumbs__link:hover,
  .archive-detail__link:hover {
    color: var(--text-hi);
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .archive-crumbs__link:focus-visible,
  .archive-detail__link:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .archive-detail__link {
    color: var(--accent);
  }

  .archive-crumbs__current {
    overflow: hidden;
    color: var(--text-hi);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .archive-detail__body {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 18px;
  }

  .archive-detail__title {
    overflow-wrap: anywhere;
  }

  .archive-detail__meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 12px;
    margin: 8px 0 0;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }

  :global(.archive-detail__problems) p,
  :global(.archive-detail__problems) ul {
    margin: 0;
  }

  :global(.archive-detail__problems) ul {
    padding-left: 18px;
    margin-top: 4px;
  }

  .archive-detail__count {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }

  .s-section + .s-section {
    margin-top: 12px;
  }

  .archive-facts {
    margin: 0;
  }

  .archive-fact {
    display: grid;
    grid-template-columns: minmax(120px, 200px) minmax(0, 1fr);
    gap: 16px;
    padding: 12px 18px;
  }

  .archive-fact dt {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }

  .archive-fact dd {
    display: grid;
    gap: 4px;
    min-width: 0;
    margin: 0;
    color: var(--text-hi);
    font-size: var(--fs-body-sm);
    overflow-wrap: anywhere;
  }

  .archive-fact__code {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    overflow-wrap: anywhere;
  }

  .archive-fact__note {
    color: var(--text-lo);
  }

  .archive-items {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .archive-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px 16px;
    padding: 10px 18px;
  }

  .archive-item__main {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .archive-item__title {
    overflow: hidden;
    color: var(--text-hi);
    font-size: var(--fs-body-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .archive-item__sub,
  .archive-item__aside,
  .archive-item--more {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }

  .archive-item__sub {
    overflow-wrap: anywhere;
  }

  .archive-item__aside {
    flex-shrink: 0;
  }

  @media (max-width: 640px) {
    .archive-fact {
      grid-template-columns: minmax(0, 1fr);
      gap: 4px;
      padding: 12px 14px;
    }

    .archive-item {
      flex-direction: column;
      align-items: flex-start;
      padding: 10px 14px;
    }
  }
</style>
