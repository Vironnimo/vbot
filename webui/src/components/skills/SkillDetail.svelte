<script>
  // One Skill package: where it lives, who gets it (editable), requirement
  // notes and its instructions. The global off switch and Delete live in the
  // header; content editing opens the edit dialog.
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
  class="skills-detail"
  tabindex="-1"
  bind:this={element}
  aria-labelledby="skill-detail-name"
>
  <div class="skills-detail-top">
    <Button
      variant="secondary"
      class="skills-detail-back"
      ariaLabel={t('skills.backToList')}
      onClick={onBack}>← {t('skills.backToList')}</Button
    >
    <StatusChip variant={skillStatusVariant(entry)}
      >{skillStatusLabel(entry)}</StatusChip
    >
  </div>
  <header class="skills-detail-header">
    <div class="skills-detail-title">
      <h3 id="skill-detail-name">{entry.name}</h3>
      <div class="skills-detail-actions">
        {#if !entry.disabled}
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
    </div>
    <p class="skills-detail-source">
      {skillSourceDetail(entry, agents, projects)}
    </p>
    <p
      class="skills-detail-description"
      class:skills-detail-description--empty={!entry.description}
    >
      {entry.description || t('skills.noDescription')}
    </p>
    {#each duplicates as note, index (index)}
      <p class="skills-detail-note">{note}</p>
    {/each}
  </header>
  <div class="skills-detail-scroll">
    {#if entry.disabled}
      <Banner variant="warn" class="skills-detail-off">
        <span>{t('skills.detail.offBanner')}</span>
        <Button
          variant="secondary"
          disabled={busy}
          onClick={() => onSetDisabled(entry, false)}
          >{t('skills.detail.turnOn')}</Button
        >
      </Banner>
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
      <div class="skills-content-actions">
        {#if entry.editable_scope}<Button
            variant="tertiary"
            disabled={busy || inspectLoading || inspected?.id !== entry.id}
            onClick={() => onEdit(entry)}>{t('skills.editInstructions')}</Button
          >{:else}<Badge>{t('skills.readOnly')}</Badge>{/if}
        {#if inspected}<CopyButton
            text={inspected.content}
            label={t('skills.copyContent')}
          />{/if}
      </div>
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
