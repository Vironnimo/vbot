<script>
  import { t } from '$lib/i18n.js';
  import TextField from '../ui/TextField.svelte';
  let {
    projectsState = $bindable(),
    projectsController,
    repositoryActions,
    handleManualSave,
  } = $props();

  function updateEditField(field, value) {
    projectsController.updateEditField(field, value);
  }
</script>

<div class="management-topic" id="project-detail-panel-overview">
  <form
    class="projects-settings-form"
    id="project-settings-form"
    onsubmit={handleManualSave}
  >
    <section class="s-section" aria-labelledby="project-section-repository">
      <header class="s-section__head">
        <h3 class="s-section__title" id="project-section-repository">
          {t('projects.repositorySection')}
        </h3>
      </header>
      <div class="s-section__body">
        <div class="s-group">
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-name">
                {t('projects.manage.displayName')}
              </label>
            </div>
            <div class="s-row-control">
              <TextField
                id="project-edit-name"
                value={projectsState.editForm.display_name}
                onInput={(next) => updateEditField('display_name', next)}
              />
            </div>
          </div>
          <details class="s-disclosure projects-repository-actions">
            <summary>
              {t('projects.repositoryActions')}
            </summary>
            <div class="s-disclosure__body">
              {@render repositoryActions?.()}
            </div>
          </details>
        </div>
      </div>
    </section>
  </form>
</div>
