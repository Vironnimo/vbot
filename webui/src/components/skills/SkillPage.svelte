<script>
  // The page of one Skill package, opened in place of its collection: Back
  // and a breadcrumb to return, a header with where it lives, its status and
  // actions (Edit, Turn off everywhere / Turn on, Delete), the description as
  // body text (the one place that shows it as content), who gets it
  // (editable), requirement notes and its instructions.
  import { t } from '$lib/i18n.js';
  import MarkdownContent from '../chat/MarkdownContent.svelte';
  import Badge from '../ui/Badge.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TabList from '../ui/TabList.svelte';
  import SkillAccessSection from './SkillAccessSection.svelte';
  import { skillDuplicateNotes } from './skillAccess.js';
  import {
    skillDiagnosticLines,
    skillInstructionBody,
    skillSourceDetail,
    skillStatusLabel,
    skillStatusVariant,
  } from './skillsView.js';

  const noop = () => {};

  let {
    entry,
    collectionLabel = '',
    inspected = null,
    inspectLoading = false,
    inspectError = '',
    contentTab = 'instructions',
    agents = [],
    projects = [],
    inventory = [],
    busy = false,
    onBack = noop,
    onRetry = noop,
    onTab = noop,
    onEdit = noop,
    onDelete = noop,
    onSetDisabled = noop,
    onAgentAccess = noop,
    onShare = noop,
    onProjectSkills = noop,
  } = $props();

  let element = $state();
  let diagnostics = $derived(skillDiagnosticLines(entry));
  let duplicates = $derived(
    skillDuplicateNotes(entry, { inventory, agents, projects }),
  );
  let contentTabs = $derived([
    { id: 'instructions', label: t('skills.instructions') },
    { id: 'original', label: t('skills.original') },
  ]);

  export function focus() {
    element?.focus();
  }
</script>

<section
  class="skills-page"
  tabindex="-1"
  bind:this={element}
  aria-labelledby="skill-page-title"
>
  <nav class="skills-crumbs" aria-label={t('skills.page.breadcrumb')}>
    <Button
      variant="tertiary"
      icon
      class="skills-crumbs__back"
      ariaLabel={t('skills.page.backTo', { name: collectionLabel })}
      tooltip={t('skills.page.backHint')}
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
    <ol class="skills-crumbs__trail">
      <li>
        <button type="button" class="skills-crumbs__link" onclick={onBack}
          >{collectionLabel}</button
        >
      </li>
      <li class="skills-crumbs__current" aria-current="page">{entry.name}</li>
    </ol>
  </nav>
  <div class="skills-page-scroll">
    <header class="view-header skills-page-header">
      <div class="view-header__intro">
        <h2 id="skill-page-title" class="view-header__title skills-page-title">
          {entry.name}
        </h2>
        <p class="skills-page-meta">
          <span class="skills-page-source"
            >{skillSourceDetail(entry, agents, projects)}</span
          >
          <StatusChip variant={skillStatusVariant(entry)}
            >{skillStatusLabel(entry)}</StatusChip
          >
          {#if !entry.editable_scope}<Badge>{t('skills.readOnly')}</Badge>{/if}
        </p>
      </div>
      <div class="view-header__actions">
        {#if entry.editable_scope}
          <Button
            variant="secondary"
            disabled={busy || inspectLoading || inspected?.id !== entry.id}
            onClick={() => onEdit(entry)}>{t('skills.editInstructions')}</Button
          >
        {/if}
        {#if entry.disabled}
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => onSetDisabled(entry, false)}
            >{t('skills.detail.turnOn')}</Button
          >
        {:else}
          <Button
            variant="secondary"
            disabled={busy}
            tooltip={t('skills.detail.turnOffHelp')}
            onClick={() => onSetDisabled(entry, true)}
            >{t('skills.detail.turnOff')}</Button
          >
        {/if}
        {#if entry.editable_scope}
          <Button
            variant="danger"
            icon
            disabled={busy}
            ariaLabel={t('skills.deleteNamed', { name: entry.name })}
            tooltip={t('common.delete')}
            onClick={() => onDelete(entry)}
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              aria-hidden="true"
              ><path d="M2 4h12M6 4V2h4v2M4 4l1 10h6l1-10M7 6v6M9 6v6" /></svg
            >
          </Button>
        {/if}
      </div>
    </header>
    <p
      class="skills-page-description"
      class:skills-page-description--empty={!entry.description}
    >
      {entry.description || t('skills.noDescription')}
    </p>
    {#each duplicates as note, index (index)}
      <p class="skills-page-note">{note}</p>
    {/each}
    {#if entry.disabled}
      <Banner variant="warn" class="skills-page-off"
        >{t('skills.detail.offBanner')}</Banner
      >
    {:else}
      <SkillAccessSection
        {entry}
        {agents}
        {projects}
        {inventory}
        {onAgentAccess}
        {onShare}
        {onProjectSkills}
      />
    {/if}
    {#if diagnostics.length}
      <details
        class="skills-diagnostics"
        open={['invalid', 'unavailable'].includes(entry.status)}
      >
        <summary
          >{t('skills.diagnostics', {
            count: diagnostics.length,
          })}</summary
        >
        <ul>
          {#each diagnostics as line, index (index)}<li>
              {line}
            </li>{/each}
        </ul>
      </details>
    {/if}
    <div class="skills-content-head">
      <TabList
        items={contentTabs}
        value={contentTab}
        idPrefix="skill-content"
        ariaLabel={t('skills.contentView')}
        onChange={onTab}
      />
      {#if inspected}<CopyButton
          text={inspected.content}
          label={t('skills.copyContent')}
        />{/if}
    </div>
    <div
      class="skills-content"
      role="tabpanel"
      id={`skill-content-panel-${contentTab}`}
      aria-labelledby={`skill-content-tab-${contentTab}`}
      tabindex="0"
    >
      {#if inspectLoading}<Banner variant="neutral"
          >{t('skills.loadingContent')}</Banner
        >
      {:else if inspectError}<Banner variant="error" role="alert"
          >{inspectError}<Button variant="secondary" onClick={onRetry}
            >{t('common.retry')}</Button
          ></Banner
        >
      {:else if inspected}
        {#if contentTab === 'original'}<pre>{inspected.content}</pre>
        {:else}<MarkdownContent
            class="msg-markdown"
            source={skillInstructionBody(inspected.content)}
          />{/if}
      {/if}
    </div>
  </div>
</section>
