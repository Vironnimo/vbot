<script>
  import { onMount } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

  const GLOBAL_SCOPE = 'global';
  const noop = () => {};

  let { onToast = noop, onError = noop } = $props();

  let scope = $state(GLOBAL_SCOPE);
  let agents = $state([]);
  let skills = $state([]);
  let loading = $state(true);
  let loadError = $state('');
  let busy = $state(false);

  let editingName = $state(null);
  let editContent = $state('');
  let newName = $state('');
  let newContent = $state('');

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
  let createDisabled = $derived(busy || !newName.trim() || !newContent.trim());

  onMount(() => {
    void loadAgents();
    void loadSkills();
  });

  async function loadAgents() {
    try {
      const result = await rpc('agent.list');
      agents = Array.isArray(result?.agents) ? result.agents : [];
    } catch {
      // Non-fatal: without the agent list the scope selector just offers Global.
      agents = [];
    }
  }

  async function loadSkills() {
    loading = true;
    loadError = '';
    try {
      const result = await rpc('skill.read', { scope });
      skills = Array.isArray(result?.skills) ? result.skills : [];
    } catch (error) {
      loadError = `${t('settings.skills.loadError', 'Skills could not be loaded.')} ${error.message}`;
      skills = [];
    } finally {
      loading = false;
    }
  }

  function selectScope(next) {
    if (next === scope) {
      return;
    }
    scope = next;
    cancelEdit();
    onError('');
    void loadSkills();
  }

  function startEdit(skill) {
    editingName = skill.name;
    editContent = skill.content;
    onError('');
  }

  function cancelEdit() {
    editingName = null;
    editContent = '';
  }

  async function saveEdit() {
    if (busy || !editingName) {
      return;
    }
    busy = true;
    onError('');
    try {
      await rpc('skill.update', {
        scope,
        name: editingName,
        content: editContent,
      });
      onToast({
        title: t('settings.skills.saved', 'Skill saved.'),
        variant: 'success',
      });
      cancelEdit();
      await loadSkills();
    } catch (error) {
      onError(
        `${t('settings.skills.contentSaveError', 'Skill could not be saved.')} ${error.message}`,
      );
    } finally {
      busy = false;
    }
  }

  async function deleteSkill(name) {
    if (busy) {
      return;
    }
    busy = true;
    onError('');
    try {
      await rpc('skill.delete', { scope, name });
      onToast({
        title: t('settings.skills.deleted', 'Skill deleted.'),
        variant: 'success',
      });
      if (editingName === name) {
        cancelEdit();
      }
      await loadSkills();
    } catch (error) {
      onError(
        `${t('settings.skills.deleteError', 'Skill could not be deleted.')} ${error.message}`,
      );
    } finally {
      busy = false;
    }
  }

  async function createSkill() {
    if (createDisabled) {
      return;
    }
    busy = true;
    onError('');
    try {
      await rpc('skill.create', {
        scope,
        name: newName.trim(),
        content: newContent,
      });
      onToast({
        title: t('settings.skills.created', 'Skill created.'),
        variant: 'success',
      });
      newName = '';
      newContent = '';
      await loadSkills();
    } catch (error) {
      onError(
        `${t('settings.skills.createError', 'Skill could not be created.')} ${error.message}`,
      );
    } finally {
      busy = false;
    }
  }
</script>

<div class="s-row s-row--stacked">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.skills.manageLabel', 'Manage skills')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.skills.manageDescription',
        'View, create, edit, and delete skills in your global library or an agent’s private home.',
      )}
    </div>
  </div>

  <div class="s-skill-manager-scope">
    <Dropdown
      value={scope}
      options={scopeOptions}
      onValueChange={(next) => selectScope(next)}
      ariaLabel={t('settings.skills.scopeLabel', 'Skill scope')}
    />
  </div>

  {#if loadError}
    <div class="s-feedback s-feedback--error s-feedback--compact">
      {loadError}
    </div>
  {:else if loading}
    <div class="s-feedback s-feedback--neutral s-feedback--compact">
      {t('settings.loading', 'Loading…')}
    </div>
  {:else}
    <div class="s-skill-manager-list">
      {#if skills.length === 0}
        <div class="s-feedback s-feedback--neutral s-feedback--compact">
          {t('settings.skills.empty', 'No skills in this scope yet.')}
        </div>
      {:else}
        {#each skills as skill (skill.name)}
          <div class="s-skill-manager-item">
            <div class="s-skill-manager-item-head">
              <span class="s-skill-manager-name">{skill.name}</span>
              <span class="s-skill-manager-desc">{skill.description}</span>
              <div class="s-skill-manager-actions">
                <Button
                  variant="secondary"
                  disabled={busy}
                  onClick={() => startEdit(skill)}
                >
                  {t('common.edit', 'Edit')}
                </Button>
                <Button
                  variant="danger"
                  disabled={busy}
                  onClick={() => deleteSkill(skill.name)}
                >
                  {t('common.delete', 'Delete')}
                </Button>
              </div>
            </div>
            {#if editingName === skill.name}
              <textarea
                class="s-input s-skill-manager-editor"
                rows="10"
                value={editContent}
                oninput={(event) => (editContent = event.currentTarget.value)}
              ></textarea>
              <div class="s-skill-manager-editor-actions">
                <Button variant="primary" disabled={busy} onClick={saveEdit}>
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
            {/if}
          </div>
        {/each}
      {/if}
    </div>

    <div class="s-skill-manager-create">
      <div class="s-row-label">
        {t('settings.skills.newSkill', 'New skill')}
      </div>
      <TextField
        value={newName}
        onInput={(next) => (newName = next)}
        placeholder={t('settings.skills.namePlaceholder', 'skill-name')}
      />
      <textarea
        class="s-input s-skill-manager-editor"
        rows="10"
        value={newContent}
        oninput={(event) => (newContent = event.currentTarget.value)}
        placeholder={t(
          'settings.skills.contentPlaceholder',
          '---\nname: skill-name\ndescription: When to use this skill.\n---\n\n# Overview',
        )}
      ></textarea>
      <Button variant="primary" disabled={createDisabled} onClick={createSkill}>
        {t('settings.skills.create', 'Create skill')}
      </Button>
    </div>
  {/if}
</div>
