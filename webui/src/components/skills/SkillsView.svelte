<script>
  // The dedicated Skills management view: one grouped control plane over every
  // Skill source — directories (moved from Settings), origin-grouped inventory,
  // per-Skill status/diagnostics, the policy disable switch, sharing of private
  // Agent Skills, and the existing global/private create-edit-delete UX.
  import { onMount } from 'svelte';

  import SkillDirectoryEditor from './SkillDirectoryEditor.svelte';
  import {
    groupInventorySkills,
    skillDiagnosticLines,
    skillStatusLabel,
    skillStatusVariant,
    skillSupportsEditAndDelete,
  } from './skillsView.js';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import Badge from '../ui/Badge.svelte';
  import {
    createSkill as createSkillRequest,
    deleteSkill as deleteSkillRequest,
    listAgents,
    readSkills,
    setSkillDisabled,
    shareSkill,
    skillInventory,
    updateSkill,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

  const GLOBAL_SCOPE = 'global';
  const noop = () => {};

  let {
    settings = null,
    onSettingsCommit = noop,
    onToast = noop,
    skillsRefreshToken = 0,
  } = $props();

  let agents = $state([]);
  let inventory = $state([]);
  let staleShared = $state([]);
  let loading = $state(true);
  let loadError = $state('');
  let busy = $state(false);

  // Create-form state: a target scope (global pool or an agent's private home)
  // plus the name/content draft, ported from the retired Settings panel.
  let createScope = $state(GLOBAL_SCOPE);
  let createExpanded = $state(false);
  let newName = $state('');
  let newContent = $state('');

  // Inline editor state: which entry (scope + name) is open with which content.
  let editing = $state(null); // { scope, name }
  let editContent = $state('');

  // The skill awaiting delete confirmation (null = dialog closed).
  let deleteTarget = $state(null); // { scope, name }

  // The entry whose diagnostics disclosure is expanded.
  let expandedName = $state(null);

  let groups = $derived(groupInventorySkills(inventory, t));
  let createDisabled = $derived(busy || !newName.trim() || !newContent.trim());
  let scopeOptions = $derived([
    {
      value: GLOBAL_SCOPE,
      label: t('settings.skills.scopeGlobal', 'Global skills'),
    },
    ...agents.map((agent) => ({
      value: `agent:${agent.id}`,
      label: t('settings.skills.scopeAgent', '{name} (private)', {
        name: agent.name || agent.id,
      }),
    })),
  ]);

  onMount(() => {
    void loadAgents();
    void loadInventory();
  });

  // A skills resource event (our own mutations included) bumps the token;
  // refresh server truth without tearing down open drafts.
  let lastSkillsRefreshToken = 0;
  $effect(() => {
    const token = skillsRefreshToken;
    if (token === lastSkillsRefreshToken) {
      return;
    }
    lastSkillsRefreshToken = token;
    void loadInventory();
  });

  async function loadAgents() {
    try {
      const result = await listAgents();
      agents = Array.isArray(result?.agents) ? result.agents : [];
    } catch {
      // Non-fatal: without the agent list the create selector just offers Global.
      agents = [];
    }
  }

  async function loadInventory() {
    loading = true;
    loadError = '';
    try {
      const result = await skillInventory();
      inventory = Array.isArray(result?.skills) ? result.skills : [];
      staleShared = Array.isArray(result?.stale_shared)
        ? result.stale_shared
        : [];
    } catch (error) {
      loadError = `${t('skills.loadError', 'Skills could not be loaded.')} ${error.message}`;
      inventory = [];
      staleShared = [];
    } finally {
      loading = false;
    }
  }

  function scopeForEntry(entry) {
    return entry.owner_id ? `agent:${entry.owner_id}` : GLOBAL_SCOPE;
  }

  function startCreate() {
    cancelEdit();
    createExpanded = true;
  }

  function cancelCreate() {
    createExpanded = false;
    newName = '';
    newContent = '';
  }

  async function createSkill() {
    if (createDisabled) {
      return;
    }
    busy = true;
    try {
      await createSkillRequest({
        scope: createScope,
        name: newName.trim(),
        content: newContent,
      });
      onToast({
        title: t('settings.skills.created', 'Skill created.'),
        variant: 'success',
      });
      cancelCreate();
      await loadInventory();
    } catch (error) {
      onToast({
        title: `${t('settings.skills.createError', 'Skill could not be created.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  async function startEdit(entry) {
    cancelCreate();
    const scope = scopeForEntry(entry);
    busy = true;
    try {
      const result = await readSkills(scope);
      const match = (result?.skills ?? []).find(
        (skill) => skill.name === entry.name,
      );
      editing = { scope, name: entry.name };
      editContent = match?.content ?? '';
    } catch (error) {
      onToast({
        title: `${t('skills.editLoadError', 'Skill content could not be loaded.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  function cancelEdit() {
    editing = null;
    editContent = '';
  }

  async function saveEdit() {
    if (busy || !editing) {
      return;
    }
    busy = true;
    try {
      await updateSkill({
        scope: editing.scope,
        name: editing.name,
        content: editContent,
      });
      onToast({
        title: t('settings.skills.saved', 'Skill saved.'),
        variant: 'success',
      });
      cancelEdit();
      await loadInventory();
    } catch (error) {
      onToast({
        title: `${t('settings.skills.contentSaveError', 'Skill could not be saved.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  async function toggleDisabled(entry) {
    if (busy) {
      return;
    }
    busy = true;
    try {
      await setSkillDisabled(entry.name, !entry.disabled);
      onToast({
        title: entry.disabled
          ? t('skills.enabledToast', 'Skill “{name}” enabled.', {
              name: entry.name,
            })
          : t('skills.disabledToast', 'Skill “{name}” disabled everywhere.', {
              name: entry.name,
            }),
        variant: 'success',
      });
      await loadInventory();
    } catch (error) {
      onToast({
        title: `${t('skills.toggleError', 'The skill could not be changed.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  async function toggleShared(entry) {
    if (busy || !entry.owner_id) {
      return;
    }
    busy = true;
    try {
      await shareSkill(entry.owner_id, entry.name, !entry.shared);
      onToast({
        title: entry.shared
          ? t('skills.unsharedToast', 'Sharing stopped.', {})
          : t(
              'skills.sharedToast',
              'Skill shared with all other identity agents.',
            ),
        variant: 'success',
      });
      await loadInventory();
    } catch (error) {
      onToast({
        title: `${t('skills.shareError', 'Sharing could not be changed.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  function requestDelete(entry) {
    if (busy) {
      return;
    }
    deleteTarget = { scope: scopeForEntry(entry), name: entry.name };
  }

  function cancelDelete() {
    deleteTarget = null;
  }

  async function confirmDelete() {
    const target = deleteTarget;
    deleteTarget = null;
    if (!target || busy) {
      return;
    }
    busy = true;
    try {
      await deleteSkillRequest(target.scope, target.name);
      onToast({
        title: t('settings.skills.deleted', 'Skill deleted.'),
        variant: 'success',
      });
      if (editing?.name === target.name) {
        cancelEdit();
      }
      await loadInventory();
    } catch (error) {
      onToast({
        title: `${t('settings.skills.deleteError', 'Skill could not be deleted.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  function toggleDiagnostics(entry) {
    expandedName = expandedName === entry.name ? null : entry.name;
  }
</script>

<section class="skills-view view active" aria-labelledby="skills-title">
  <div class="skills-layout">
    <div class="skills-scroll view-frame">
      <header class="skills-header view-header">
        <div class="view-header__intro">
          <p class="skills-eyebrow view-header__eyebrow">
            {t('skills.eyebrow', 'Configure')}
          </p>
          <h2 id="skills-title" class="skills-title view-header__title">
            {t('skills.title', 'Skills')}
          </h2>
          <p class="skills-subtitle view-header__subtitle">
            {t(
              'skills.subtitle',
              'Every skill from every source — manage scan directories, availability, and sharing.',
            )}
          </p>
        </div>
        <div class="view-toolbar__actions">
          <Button
            variant="secondary"
            disabled={loading}
            onClick={() => loadInventory()}
          >
            {t('skills.refresh', 'Refresh')}
          </Button>
        </div>
      </header>

      {#if loadError}
        <Banner variant="error" role="alert">{loadError}</Banner>
      {:else if loading && inventory.length === 0}
        <Banner variant="neutral">{t('settings.loading', 'Loading…')}</Banner>
      {:else}
        <section class="skills-card" aria-labelledby="skills-directories-title">
          <h3 id="skills-directories-title" class="skills-card-title">
            {t('skills.directoriesTitle', 'Scan directories')}
          </h3>
          <SkillDirectoryEditor
            {settings}
            onCommit={onSettingsCommit}
            {onToast}
            onError={(message) => (loadError = message)}
          />
        </section>

        <section class="skills-card" aria-labelledby="skills-manager-title">
          <div class="skills-manager-head">
            <h3 id="skills-manager-title" class="skills-card-title">
              {t('skills.managerTitle', 'Installed skills')}
            </h3>
            {#if !createExpanded}
              <Button variant="primary" disabled={busy} onClick={startCreate}>
                <span aria-hidden="true">+</span>
                {t('settings.skills.newSkill', 'New skill')}
              </Button>
            {/if}
          </div>

          {#if staleShared.length > 0}
            <Banner variant="warn">
              {t(
                'skills.staleShared',
                '{count} shared-skill entries point at a missing agent or skill and are ignored.',
                { count: staleShared.length },
              )}
            </Banner>
          {/if}

          {#if !createExpanded}
            <div class="skills-create-row">
              <Dropdown
                value={createScope}
                options={scopeOptions}
                onValueChange={(next) => (createScope = next)}
                ariaLabel={t(
                  'skills.createScopeLabel',
                  'Where new skills are created',
                )}
              />
            </div>
          {/if}

          {#if createExpanded}
            <div class="skills-create">
              <div class="skills-create-title">
                <span>{t('settings.skills.newSkill', 'New skill')}</span>
                <InfoHint
                  text={t(
                    'settings.skills.newSkillHelp',
                    'A skill is a Markdown playbook: a header with a name and a short description, followed by the instructions.\n\nThe description matters most — it is what the agent reads to decide when to apply the skill, so state clearly what task it is for.',
                  )}
                />
              </div>
              <div class="skills-field">
                <label class="skills-field-label" for="new-skill-name">
                  {t('settings.skills.nameLabel', 'Skill name')}
                </label>
                <TextField
                  id="new-skill-name"
                  value={newName}
                  onInput={(next) => (newName = next)}
                  placeholder={t(
                    'settings.skills.namePlaceholder',
                    'skill-name',
                  )}
                />
              </div>
              <div class="skills-field">
                <label class="skills-field-label" for="new-skill-content">
                  {t('settings.skills.contentLabel', 'SKILL.md content')}
                </label>
                <TextArea
                  id="new-skill-content"
                  code
                  rows="10"
                  value={newContent}
                  onInput={(value) => (newContent = value)}
                  placeholder={t(
                    'settings.skills.contentPlaceholder',
                    '---\nname: skill-name\ndescription: When to use this skill.\n---\n\n# Overview',
                  )}
                />
              </div>
              <div class="skills-actions-row">
                <Button
                  variant="secondary"
                  disabled={busy}
                  onClick={cancelCreate}
                >
                  {t('common.cancel', 'Cancel')}
                </Button>
                <Button
                  variant="primary"
                  disabled={createDisabled}
                  onClick={createSkill}
                >
                  {t('settings.skills.create', 'Create skill')}
                </Button>
              </div>
            </div>
          {:else if groups.length === 0}
            <EmptyState
              density="compact"
              description={t('skills.empty', 'No skills found in any source.')}
            />
          {:else}
            {#each groups as group (group.key)}
              <div
                class="skills-group"
                data-testid={`skill-group-${group.key}`}
              >
                <h4 class="skills-group-title">{group.label}</h4>
                {#each group.skills as entry (entry.owner_id + '/' + entry.origin + '/' + entry.name)}
                  {@const diagnostics = skillDiagnosticLines(entry)}
                  <div
                    class="skills-item"
                    class:skills-item--disabled={entry.disabled}
                  >
                    <div class="skills-item-head">
                      <div class="skills-item-copy">
                        <span class="skills-item-name">{entry.name}</span>
                        {#if entry.shared}
                          <Badge variant="info">
                            {t('skills.sharedBadge', 'Shared')}
                          </Badge>
                        {/if}
                        <span class="skills-item-desc">{entry.description}</span
                        >
                      </div>
                      <div class="skills-item-state">
                        <StatusChip variant={skillStatusVariant(entry)}>
                          {skillStatusLabel(entry, t)}
                        </StatusChip>
                        {#if diagnostics.length > 0}
                          <Button
                            variant="tertiary"
                            ariaExpanded={expandedName === entry.name}
                            onClick={() => toggleDiagnostics(entry)}
                          >
                            {expandedName === entry.name
                              ? t('skills.hideDetails', 'Hide details')
                              : t('skills.showDetails', 'Details')}
                          </Button>
                        {/if}
                      </div>
                      <div class="skills-item-actions">
                        <Button
                          variant="secondary"
                          disabled={busy}
                          onClick={() => toggleDisabled(entry)}
                        >
                          {entry.disabled
                            ? t('skills.enable', 'Enable')
                            : t('skills.disable', 'Disable')}
                        </Button>
                        {#if entry.owner_id}
                          <Button
                            variant="secondary"
                            disabled={busy}
                            onClick={() => toggleShared(entry)}
                          >
                            {entry.shared
                              ? t('skills.unshare', 'Unshare')
                              : t('skills.share', 'Share')}
                          </Button>
                        {/if}
                        {#if skillSupportsEditAndDelete(entry)}
                          <Button
                            variant="secondary"
                            disabled={busy}
                            onClick={() => startEdit(entry)}
                          >
                            {t('common.edit', 'Edit')}
                          </Button>
                          <Button
                            variant="danger"
                            disabled={busy}
                            onClick={() => requestDelete(entry)}
                          >
                            {t('common.delete', 'Delete')}
                          </Button>
                        {/if}
                      </div>
                    </div>
                    {#if expandedName === entry.name && diagnostics.length > 0}
                      <ul class="skills-diagnostics">
                        {#each diagnostics as line (line)}
                          <li>{line}</li>
                        {/each}
                      </ul>
                    {/if}
                    {#if editing && editing.name === entry.name && editing.scope === scopeForEntry(entry)}
                      <div class="skills-editor">
                        <label
                          class="skills-field-label"
                          for={`skill-content-${entry.name}`}
                        >
                          {t(
                            'settings.skills.contentLabel',
                            'SKILL.md content',
                          )}
                        </label>
                        <TextArea
                          id={`skill-content-${entry.name}`}
                          code
                          rows="10"
                          value={editContent}
                          onInput={(value) => (editContent = value)}
                        />
                        <div class="skills-actions-row">
                          <Button
                            variant="primary"
                            disabled={busy}
                            onClick={saveEdit}
                          >
                            {busy
                              ? t('common.saving', 'Saving…')
                              : t('common.save', 'Save')}
                          </Button>
                          <Button
                            variant="secondary"
                            disabled={busy}
                            onClick={cancelEdit}
                          >
                            {t('common.cancel', 'Cancel')}
                          </Button>
                        </div>
                      </div>
                    {/if}
                  </div>
                {/each}
              </div>
            {/each}
          {/if}
        </section>
      {/if}
    </div>
  </div>
</section>

{#if deleteTarget}
  <ConfirmDialog
    title={t('settings.skills.deleteConfirmTitle', 'Delete skill')}
    body={t(
      'settings.skills.deleteConfirm',
      'Delete skill “{name}” permanently? The skill file is removed from disk.',
      { name: deleteTarget.name },
    )}
    confirmLabel={t('common.delete', 'Delete')}
    onConfirm={confirmDelete}
    onCancel={cancelDelete}
  />
{/if}

<style>
  .skills-view {
    display: flex;
    flex-direction: column;
    min-height: 0;
    height: 100%;
  }

  .skills-layout {
    display: flex;
    min-height: 0;
    height: 100%;
    justify-content: center;
  }

  .skills-scroll {
    width: 100%;
    max-width: var(--content-max-narrow);
    overflow-y: auto;
    padding: var(--space-lg);
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }

  .skills-header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .skills-eyebrow,
  .skills-card-title,
  .skills-group-title {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--text-lo);
  }

  .skills-title {
    margin: 0;
    font-size: var(--fs-heading-lg);
    font-weight: 600;
    letter-spacing: -0.02em;
    color: var(--text-hi);
  }

  .skills-subtitle {
    margin: 0;
    font-size: var(--fs-body-sm);
    color: var(--text-med);
  }

  .skills-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: var(--space-lg);
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }

  .skills-manager-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .skills-create-row {
    display: flex;
    justify-content: flex-start;
  }

  .skills-create {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    border-left: 2px solid var(--border-2);
    padding-left: var(--space-md);
  }

  .skills-create-title {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    color: var(--text-hi);
    font-weight: 600;
  }

  .skills-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .skills-field-label {
    font-size: var(--fs-body-sm);
    color: var(--text-med);
  }

  .skills-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .skills-group-title {
    margin-top: var(--space-sm);
  }

  .skills-item {
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: var(--space-sm) var(--space-md);
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .skills-item--disabled {
    opacity: 0.55;
  }

  .skills-item-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
    flex-wrap: wrap;
  }

  .skills-item-copy {
    display: flex;
    align-items: baseline;
    gap: var(--space-sm);
    min-width: 0;
    flex: 1 1 240px;
  }

  .skills-item-name {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
    color: var(--text-hi);
  }

  .skills-item-desc {
    font-size: var(--fs-body-sm);
    color: var(--text-med);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .skills-item-state {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }

  .skills-item-actions {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    flex-wrap: wrap;
  }

  .skills-diagnostics {
    margin: 0;
    padding: var(--space-sm) var(--space-md);
    border-left: 2px solid var(--border-2);
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    color: var(--text-med);
  }

  .skills-editor {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    border-left: 2px solid var(--border-2);
    padding-left: var(--space-md);
  }

  .skills-actions-row {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  @media (max-width: 640px) {
    .skills-scroll {
      padding: var(--space-md);
    }
  }
</style>
