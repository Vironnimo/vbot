<script>
  import { t } from '$lib/i18n.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import InfoHint from '../ui/InfoHint.svelte';
  import Button from '../ui/Button.svelte';
  import SortableList from '../ui/SortableList.svelte';
  import TextField from '../ui/TextField.svelte';
  let { projectsState = $bindable(), projectsController } = $props();

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
          <SortableList
            class="projects-file-list"
            itemClass="projects-file-row"
            itemFocusable
            items={projectsState.editForm.auto_load}
            getKey={(filePath) => filePath}
            getLabel={(filePath) => filePath}
            disabled={projectsState.editSaving}
            aria-label={t('projects.detail.sectionAutoLoad')}
            onReorder={(from, to) =>
              projectsController.moveAutoLoadEntry(from, to)}
          >
            {#snippet item(filePath, index)}
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
            {/snippet}
          </SortableList>
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
    </div>
  </section>
</div>
