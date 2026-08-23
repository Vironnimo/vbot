<script>
  // The skill-directory rows moved out of Settings: a read-only default
  // directory plus the autosaved additional-directories list. Ported from the
  // retired SettingsSkillsPanel with its exact save semantics.
  import { onDestroy, untrack } from 'svelte';

  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import TextField from '../ui/TextField.svelte';
  import { updateSettings } from '$lib/api.js';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import {
    createSkillDirectoriesUpdatePayload,
    getDefaultSkillDirectoryValue,
    getSkillDirectories,
  } from '$lib/settingsView.js';

  const AUTO_SAVE_DEBOUNCE_MS = 800;
  const noop = () => {};

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
  } = $props();

  let skillDirectories = $state(untrack(() => getSkillDirectories(settings)));
  let newSkillDirectory = $state('');
  let saving = $state(false);
  let autoSaveTimer = null;

  let defaultSkillDirectoryValue = $derived(
    getDefaultSkillDirectoryValue(settings, t),
  );
  let saveDisabled = $derived(
    saving || directoriesMatch(skillDirectories, getSkillDirectories(settings)),
  );
  const autosaveContext = useAutosaveContext();
  const directoryAutosave = createAutosaveParticipant({
    cancelPending: clearAutoSaveTimer,
    getSnapshot: () => [...skillDirectories],
    hasChanges: () =>
      !directoriesMatch(skillDirectories, getSkillDirectories(settings)),
    save: saveSkillDirectories,
  });
  const unregisterDirectoryAutosave =
    autosaveContext.register(directoryAutosave);

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void directoryAutosave.runSave();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  onDestroy(() => {
    unregisterDirectoryAutosave();
    clearAutoSaveTimer();
  });

  function clearAutoSaveTimer() {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
  }

  function directoriesMatch(left, right) {
    if (left.length !== right.length) {
      return false;
    }

    return left.every((item, index) => item === right[index]);
  }

  function addSkillDirectory() {
    const directory = newSkillDirectory.trim();
    if (!directory) {
      return;
    }

    if (!skillDirectories.includes(directory)) {
      skillDirectories = [...skillDirectories, directory];
    }

    newSkillDirectory = '';
    onError('');
  }

  function removeSkillDirectory(directory) {
    skillDirectories = skillDirectories.filter((item) => item !== directory);
    onError('');
  }

  function handleSkillDirectoryKeydown(event) {
    if (event.key !== 'Enter') {
      return;
    }

    event.preventDefault();
    addSkillDirectory();
  }

  function handleManualSkillDirectoriesSave() {
    if (saving) {
      return;
    }

    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'success',
      });
      return;
    }

    clearAutoSaveTimer();
    void directoryAutosave.runSave('manual');
  }

  async function saveSkillDirectories() {
    if (directoriesMatch(skillDirectories, getSkillDirectories(settings))) {
      return true;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await updateSettings(
        createSkillDirectoriesUpdatePayload(skillDirectories),
      );
      onCommit(nextSettings);
      onToast({
        title: t('settings.skills.saveSuccess', 'Skill directories updated.'),
        variant: 'success',
      });
      return true;
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
      return false;
    } finally {
      saving = false;
    }
  }
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.skills.defaultDirectory', 'Default skill directory')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.skills.defaultDirectoryDescription',
        'Always scanned from the vBot data directory and kept read-only here.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--input">
    <TextField readonly value={defaultSkillDirectoryValue} />
  </div>
</div>

<div class="s-row s-row--stacked">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.skills.extraDirectories', 'Additional skill directories')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.skills.extraDirectoriesDescription',
        'Extra folders scanned for skills as part of the global library — their skills are available to every agent. Useful for keeping a skill collection outside the vBot data directory.',
      )}
    </div>
  </div>

  <div class="skills-directory-list">
    {#if skillDirectories.length === 0}
      <EmptyState
        density="compact"
        description={t(
          'settings.skills.emptyDirectories',
          'No additional skill directories configured.',
        )}
      />
    {:else}
      {#each skillDirectories as directory (directory)}
        <div class="skills-directory-item">
          <span>{directory}</span>
          <Button
            variant="secondary"
            ariaLabel={t(
              'settings.skills.removeDirectory',
              'Remove skill directory {path}',
              { path: directory },
            )}
            onClick={() => removeSkillDirectory(directory)}
          >
            {t('common.remove', 'Remove')}
          </Button>
        </div>
      {/each}
    {/if}
  </div>

  <div class="skills-directory-add">
    <TextField
      value={newSkillDirectory}
      onInput={(next) => (newSkillDirectory = next)}
      placeholder={t('settings.skills.pathPlaceholder', 'C:/path/to/skills')}
      onkeydown={handleSkillDirectoryKeydown}
    />
    <Button
      variant="secondary"
      disabled={!newSkillDirectory.trim()}
      onClick={addSkillDirectory}
    >
      {t('settings.skills.addDirectory', 'Add directory')}
    </Button>
  </div>

  <div class="skills-footer">
    <Button variant="primary" onClick={handleManualSkillDirectoriesSave}>
      {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
    </Button>
  </div>
</div>

<style>
  .skills-directory-list,
  .skills-directory-add {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .skills-directory-item {
    display: flex;
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .skills-directory-item span {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
    color: var(--text-med);
    word-break: break-all;
  }

  .skills-footer {
    display: flex;
    justify-content: flex-end;
  }
</style>
