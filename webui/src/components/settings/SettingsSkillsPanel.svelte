<script>
  import { onDestroy, untrack } from 'svelte';

  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import SettingsSkillManagerPanel from './SettingsSkillManagerPanel.svelte';
  import { rpc } from '$lib/api.js';
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

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
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

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveSkillDirectories();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  onDestroy(() => {
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
    void saveSkillDirectories();
  }

  async function saveSkillDirectories() {
    if (saveDisabled) {
      return;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await rpc(
        'settings.update',
        createSkillDirectoriesUpdatePayload(skillDirectories),
      );
      onCommit(nextSettings);
      onToast({
        title: t('settings.skills.saveSuccess', 'Skill directories updated.'),
        variant: 'success',
      });
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
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
        'Absolute or home-relative paths from settings.json skill_directories.',
      )}
    </div>
  </div>

  <div class="s-skill-directory-list">
    {#if skillDirectories.length === 0}
      <div class="s-feedback s-feedback--neutral s-feedback--compact">
        {t(
          'settings.skills.emptyDirectories',
          'No additional skill directories configured.',
        )}
      </div>
    {:else}
      {#each skillDirectories as directory (directory)}
        <div class="s-skill-directory-item">
          <span>{directory}</span>
          <Button
            variant="secondary"
            class="s-directory-remove"
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

  <div class="s-skill-directory-add">
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

  <div class="s-footer">
    <Button
      variant="primary"
      class="s-save-button s-save-button--inline"
      onClick={handleManualSkillDirectoriesSave}
    >
      {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
    </Button>
  </div>
</div>

<SettingsSkillManagerPanel {onToast} {onError} />
