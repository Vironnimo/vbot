<script>
  // The skill-directory rows moved out of Settings: a read-only default
  // directory plus the autosaved additional-directories list. Ported from the
  // retired SettingsSkillsPanel with its exact save semantics.
  import { onDestroy, untrack } from 'svelte';

  import Button from '../ui/Button.svelte';
  import SaveButton from '../ui/SaveButton.svelte';
  import TextField from '../ui/TextField.svelte';
  import { updateSettings } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import {
    createSkillDirectoriesUpdatePayload,
    getDefaultSkillDirectoryValue,
    getSkillDirectories,
  } from '$lib/settingsView.js';

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
  let addElement = $state();

  export function focusNewDirectory() {
    addElement?.querySelector('input')?.focus();
  }

  let defaultSkillDirectoryValue = $derived(
    getDefaultSkillDirectoryValue(settings, t),
  );
  let saveDisabled = $derived(
    saving || directoriesMatch(skillDirectories, getSkillDirectories(settings)),
  );
  const autosaveContext = useAutosaveContext();
  const directoryAutosave = createDebouncedAutosave({
    getSnapshot: () => [...skillDirectories],
    hasChanges: () =>
      !directoriesMatch(skillDirectories, getSkillDirectories(settings)),
    save: saveSkillDirectories,
  });
  const unregisterDirectoryAutosave = autosaveContext.register(
    directoryAutosave.participant,
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    directoryAutosave.scheduleRun();

    return () => {
      directoryAutosave.cancelPendingTimer();
    };
  });

  onDestroy(() => {
    unregisterDirectoryAutosave();
    directoryAutosave.cancelPendingTimer();
  });

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
    if (event.key !== 'Enter' || isImeComposing(event)) {
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

    directoryAutosave.cancelPendingTimer();
    void directoryAutosave.participant.runSave('manual');
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

<!-- Two titled sections in the page grammar (styles/settings/sections.css):
     the editable additional directories with their save state on the
     heading line, then the read-only default directory. -->
<section class="s-section" aria-labelledby="skills-section-directories">
  <div class="s-section__head">
    <h3 class="s-section__title" id="skills-section-directories">
      {t('settings.skills.extraDirectories', 'Additional skill directories')}
    </h3>
    <div class="s-section__aside">
      <SaveButton
        {saving}
        pending={directoryAutosave.participant.hasChanges()}
        onClick={handleManualSkillDirectoriesSave}
      />
    </div>
  </div>
  <p class="s-section__desc">
    {t(
      'settings.skills.extraDirectoriesDescription',
      'Extra folders scanned for skills as part of the global library — their skills are available to every agent. Useful for keeping a skill collection outside the vBot data directory.',
    )}
  </p>
  <div class="s-section__body">
    <div class="s-group">
      {#if skillDirectories.length === 0}
        <p class="s-group__block s-group__note skills-directory-empty">
          {t(
            'settings.skills.emptyDirectories',
            'No additional skill directories configured.',
          )}
        </p>
      {:else}
        {#each skillDirectories as directory (directory)}
          <div class="skills-directory-item">
            <span class="skills-directory-path">{directory}</span>
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

      <div class="s-group__block skills-directory-add" bind:this={addElement}>
        <TextField
          code
          value={newSkillDirectory}
          onInput={(next) => (newSkillDirectory = next)}
          placeholder={t(
            'settings.skills.pathPlaceholder',
            'C:/path/to/skills',
          )}
          ariaLabel={t(
            'settings.skills.extraDirectories',
            'Additional skill directories',
          )}
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
    </div>
  </div>
</section>

<section class="s-section" aria-labelledby="skills-section-default-directory">
  <div class="s-section__head">
    <h3 class="s-section__title" id="skills-section-default-directory">
      {t('settings.skills.defaultDirectory', 'Default skill directory')}
    </h3>
  </div>
  <p class="s-section__desc">
    {t(
      'settings.skills.defaultDirectoryDescription',
      'Always scanned from the vBot data directory and kept read-only here.',
    )}
  </p>
  <div class="s-section__body">
    <div class="s-group">
      <div class="skills-directory-item">
        <span class="skills-directory-path">{defaultSkillDirectoryValue}</span>
      </div>
    </div>
  </div>
</section>

<style>
  .skills-directory-add {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-sm);
  }

  .skills-directory-add :global(input) {
    flex: 1;
    min-width: 180px;
  }

  .skills-directory-empty {
    margin: 0;
  }

  .skills-directory-item {
    display: flex;
    min-height: 48px;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    padding: 8px 18px;
  }

  .skills-directory-path {
    min-width: 0;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    word-break: break-all;
  }

  @media (max-width: 640px) {
    .skills-directory-item {
      padding-inline: 14px;
    }
  }
</style>
