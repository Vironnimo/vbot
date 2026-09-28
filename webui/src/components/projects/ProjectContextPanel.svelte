<script>
  import { t } from '$lib/i18n.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import InfoHint from '../ui/InfoHint.svelte';
  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import { tick } from 'svelte';
  let { projectsState = $bindable(), projectsController } = $props();

  let autoLoadDrag = $state(null);

  let autoLoadDropIndex = $state(null);

  let autoLoadAnnouncement = $state('');

  let autoLoadList = $state(null);

  function addAutoLoadEntry() {
    const entry = projectsState.autoLoadDraft.trim();
    if (entry === '') {
      return;
    }
    if (!projectsState.editForm.auto_load.includes(entry)) {
      projectsController.updateEditField('auto_load', [
        ...projectsState.editForm.auto_load,
        entry,
      ]);
    }
    projectsState.autoLoadDraft = '';
  }

  function removeAutoLoadEntry(index) {
    projectsController.updateEditField(
      'auto_load',
      projectsState.editForm.auto_load.filter(
        (_, position) => position !== index,
      ),
    );
  }

  function handleAutoLoadKeydown(event) {
    if (event.key === 'Enter' && !isImeComposing(event)) {
      event.preventDefault();
      addAutoLoadEntry();
    }
  }

  function startAutoLoadDrag(index, event) {
    if (projectsState.editSaving) {
      event.preventDefault();
      return;
    }
    autoLoadDrag = {
      index,
      projectId: projectsState.selectedProjectId,
      files: [...projectsState.editForm.auto_load],
    };
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', String(index));
    }
  }

  function validAutoLoadDrag() {
    return (
      autoLoadDrag !== null &&
      !projectsState.editSaving &&
      autoLoadDrag.projectId === projectsState.selectedProjectId &&
      autoLoadDrag.files.length === projectsState.editForm.auto_load.length &&
      autoLoadDrag.files.every(
        (file, index) => file === projectsState.editForm.auto_load[index],
      )
    );
  }

  function endAutoLoadDrag() {
    autoLoadDrag = null;
    autoLoadDropIndex = null;
  }

  function dragOverAutoLoad(index, event) {
    if (!validAutoLoadDrag()) return;
    event.preventDefault();
    autoLoadDropIndex = index;
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
  }

  function dropAutoLoad(index, event) {
    event.preventDefault();
    if (validAutoLoadDrag()) {
      void moveAutoLoadEntry(autoLoadDrag.index, index);
    }
    endAutoLoadDrag();
  }

  async function moveAutoLoadEntry(from, to) {
    if (!projectsController.moveAutoLoadEntry(from, to)) return;
    autoLoadAnnouncement = t('projects.manage.autoLoadMoved', {
      file: projectsState.editForm.auto_load[to],
      position: to + 1,
      total: projectsState.editForm.auto_load.length,
    });
    await tick();
    autoLoadList?.querySelector(`[data-auto-load-handle="${to}"]`)?.focus();
  }

  function reorderAutoLoadKeydown(index, event) {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    event.preventDefault();
    void moveAutoLoadEntry(index, index + (event.key === 'ArrowUp' ? -1 : 1));
  }
</script>

<div class="management-topic" id="project-detail-panel-context">
  <section class="s-section" aria-labelledby="project-section-auto-load">
    <header class="s-section__head">
      <h3 class="s-section__title" id="project-section-auto-load">
        {t('projects.detail.sectionAutoLoad')}
      </h3>
      <InfoHint text={t('projects.detail.autoLoadInfo')} />
    </header>
    <div class="s-section__body">
      <div class="s-group projects-auto-load">
        {#if projectsState.editForm.auto_load.length > 0}
          <ul class="projects-file-list" bind:this={autoLoadList}>
            {#each projectsState.editForm.auto_load as filePath, index (index)}
              <li
                class="projects-file-row"
                class:projects-file-row--drop={autoLoadDropIndex === index &&
                  autoLoadDrag?.index !== index}
                ondragover={(event) => dragOverAutoLoad(index, event)}
                ondragleave={() => {
                  autoLoadDropIndex = null;
                }}
                ondrop={(event) => dropAutoLoad(index, event)}
              >
                <Button
                  variant="tertiary"
                  icon
                  class="projects-file-handle"
                  draggable={!projectsState.editSaving}
                  disabled={projectsState.editForm.auto_load.length < 2}
                  data-auto-load-handle={index}
                  ariaLabel={t('projects.manage.autoLoadReorder', {
                    file: filePath,
                  })}
                  tooltip={t('projects.manage.autoLoadReorder', {
                    file: filePath,
                  })}
                  ondragstart={(event) => startAutoLoadDrag(index, event)}
                  ondragend={endAutoLoadDrag}
                  onkeydown={(event) => reorderAutoLoadKeydown(index, event)}
                >
                  <svg
                    width="12"
                    height="12"
                    viewBox="0 0 12 12"
                    aria-hidden="true"
                    focusable="false"
                  >
                    <circle cx="3.5" cy="2.5" r="1.1" fill="currentColor" />
                    <circle cx="8.5" cy="2.5" r="1.1" fill="currentColor" />
                    <circle cx="3.5" cy="6" r="1.1" fill="currentColor" />
                    <circle cx="8.5" cy="6" r="1.1" fill="currentColor" />
                    <circle cx="3.5" cy="9.5" r="1.1" fill="currentColor" />
                    <circle cx="8.5" cy="9.5" r="1.1" fill="currentColor" />
                  </svg>
                </Button>
                <span class="projects-file-name">{filePath}</span>
                <button
                  type="button"
                  class="projects-file-remove"
                  data-testid={`project-auto-load-remove-${index}`}
                  aria-label={t('projects.manage.autoLoadRemove', {
                    file: filePath,
                  })}
                  onclick={() => removeAutoLoadEntry(index)}
                >
                  ×
                </button>
              </li>
            {/each}
          </ul>
        {:else}
          <div class="s-group__block s-group__note">
            {t('projects.manage.autoLoadEmpty')}
          </div>
        {/if}
        <div class="s-group__block projects-file-add">
          <TextField
            id="project-edit-auto-load"
            class="projects-file-input"
            code
            value={projectsState.autoLoadDraft}
            placeholder={t('projects.manage.autoLoadPlaceholder')}
            ariaLabel={t('projects.manage.autoLoad')}
            onInput={(next) => {
              projectsState.autoLoadDraft = next;
            }}
            onkeydown={handleAutoLoadKeydown}
          />
          <Button
            variant="secondary"
            data-testid="project-auto-load-add"
            disabled={projectsState.autoLoadDraft.trim().length === 0}
            onClick={addAutoLoadEntry}
          >
            {t('projects.manage.autoLoadAdd')}
          </Button>
        </div>
      </div>
      <span
        class="projects-file-announcement"
        aria-live="polite"
        aria-atomic="true">{autoLoadAnnouncement}</span
      >
    </div>
  </section>
</div>
