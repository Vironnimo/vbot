<script>
  import Modal from '../ui/Modal.svelte';
  import { t } from '$lib/i18n.js';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import {
    PROJECT_SOURCE_FORMATS,
    presentFormats,
    shouldSuggestClaudeMd,
  } from '$lib/projectsView.js';
  import Toggle from '../ui/Toggle.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import { formatLabel } from './projectLabels.js';
  let { projectsState = $bindable(), projectsController } = $props();

  const addFormatsPresent = $derived(
    projectsState.addDetect ? presentFormats(projectsState.addDetect) : [],
  );

  const addShowsFormatChoice = $derived(addFormatsPresent.length > 1);

  const addDetectedFormat = $derived(
    addFormatsPresent.length === 1 ? addFormatsPresent[0] : '',
  );

  const addSuggestsClaudeMd = $derived(
    projectsState.addDetect !== null &&
      shouldSuggestClaudeMd(projectsState.addDetect),
  );

  let canSubmitAdd = $derived(
    projectsState.addForm.cwd.trim().length > 0 && !projectsState.addingProject,
  );

  function closeAdd() {
    projectsController.closeAdd();
  }

  function updateAddField(field, value) {
    projectsController.updateAddField(field, value);
  }

  function submitAdd(event) {
    event.preventDefault();
    void projectsController.submitAdd();
  }

  function cancelRemove() {
    projectsController.cancelRemove();
  }

  function confirmRemove() {
    void projectsController.confirmRemove();
  }

  function closeRePoint() {
    projectsController.closeRePoint();
  }

  function submitRePoint(event) {
    event.preventDefault();
    void projectsController.submitRePoint();
  }
</script>

{#if projectsState.isAddOpen}
  <Modal
    title={t('projects.add.title')}
    labelledById="projects-add-title"
    class="projects-view__modal"
    closeDisabled={projectsState.addingProject}
    onClose={closeAdd}
  >
    {#snippet body()}
      <form onsubmit={submitAdd}>
        <div class="modal-body">
          <p class="projects-help">
            {t('projects.add.subtitle')}
          </p>

          <FormField
            controlId="projects-add-cwd"
            label={t('projects.add.cwd')}
            help={t('projects.add.cwdHelp')}
          >
            <TextField
              id="projects-add-cwd"
              variant="modal"
              code
              value={projectsState.addForm.cwd}
              placeholder={t('projects.add.cwdPlaceholder')}
              disabled={projectsState.addingProject}
              onInput={(next) => updateAddField('cwd', next)}
            />
          </FormField>

          <FormField
            controlId="projects-add-display-name"
            label={t('projects.add.displayName')}
          >
            <TextField
              id="projects-add-display-name"
              variant="modal"
              value={projectsState.addForm.display_name}
              placeholder={t('projects.add.displayNamePlaceholder')}
              disabled={projectsState.addingProject}
              onInput={(next) => updateAddField('display_name', next)}
            />
          </FormField>

          {#if addShowsFormatChoice}
            <FormField
              label={t('projects.add.format')}
              help={t('projects.add.formatHelp')}
              role="radiogroup"
              aria-label={t('projects.add.format')}
            >
              <div class="projects-format-choice">
                {#each PROJECT_SOURCE_FORMATS as formatKey (formatKey)}
                  <button
                    type="button"
                    class="projects-format-option"
                    class:projects-format-option--selected={projectsState
                      .addForm.source_format === formatKey}
                    role="radio"
                    aria-checked={projectsState.addForm.source_format ===
                      formatKey}
                    disabled={projectsState.addingProject}
                    onclick={() => updateAddField('source_format', formatKey)}
                  >
                    <span class="projects-format-option__name">
                      {formatLabel(formatKey)}
                    </span>
                    <span class="projects-format-option__detail">
                      {t('projects.add.formatCounts', {
                        agents:
                          projectsState.addDetect?.formats?.[formatKey]
                            ?.agents ?? 0,
                        skills:
                          projectsState.addDetect?.formats?.[formatKey]
                            ?.skills ?? 0,
                      })}
                    </span>
                  </button>
                {/each}
              </div>
            </FormField>
          {:else if addDetectedFormat}
            <p class="projects-help">
              {t('projects.add.formatDetected', {
                format: formatLabel(addDetectedFormat),
              })}
            </p>
          {/if}

          {#if addSuggestsClaudeMd}
            <div class="projects-claude-md-suggestion">
              <Toggle
                size="sm"
                checked={projectsState.addForm.include_claude_md}
                disabled={projectsState.addingProject}
                ariaLabel={t('projects.add.claudeMdSuggestionLabel')}
                onChange={(next) => updateAddField('include_claude_md', next)}
              />
              <span>
                {t('projects.add.claudeMdSuggestion', {
                  path: projectsState.addDetect?.claude_md ?? 'CLAUDE.md',
                })}
              </span>
            </div>
          {/if}

          {#if projectsState.addError}
            <Banner variant="error" role="alert">
              {projectsState.addError}
            </Banner>
          {/if}
        </div>

        <div class="modal-footer">
          <Button
            variant="secondary"
            disabled={projectsState.addingProject}
            onClick={closeAdd}
          >
            {t('common.cancel')}
          </Button>
          <Button variant="primary" type="submit" disabled={!canSubmitAdd}>
            {projectsState.addingProject
              ? t('projects.add.submitting')
              : t('projects.add.submit')}
          </Button>
        </div>
      </form>
    {/snippet}
  </Modal>
{/if}

{#if projectsState.rePointProject}
  <Modal
    title={t('projects.rePoint.title')}
    labelledById="projects-repoint-title"
    class="projects-view__modal"
    onClose={closeRePoint}
  >
    {#snippet body()}
      <form onsubmit={submitRePoint}>
        <div class="modal-body">
          <p class="projects-help">
            {t('projects.rePoint.description')}
          </p>
          <FormField
            controlId="projects-repoint-cwd"
            label={t('projects.rePoint.cwd')}
          >
            <TextField
              id="projects-repoint-cwd"
              variant="modal"
              value={projectsState.rePointCwd}
              placeholder={t('projects.rePoint.cwdPlaceholder')}
              disabled={projectsState.rePointing}
              onInput={(next) => {
                projectsState.rePointCwd = next;
                projectsState.rePointError = '';
              }}
            />
          </FormField>

          {#if projectsState.rePointError}
            <Banner variant="error" role="alert">
              {projectsState.rePointError}
            </Banner>
          {/if}
        </div>

        <div class="modal-footer">
          <Button
            variant="secondary"
            disabled={projectsState.rePointing}
            onClick={closeRePoint}
          >
            {t('common.cancel')}
          </Button>
          <Button
            variant="primary"
            type="submit"
            disabled={projectsState.rePointing}
          >
            {projectsState.rePointing
              ? t('projects.rePoint.submitting')
              : t('projects.rePoint.submit')}
          </Button>
        </div>
      </form>
    {/snippet}
  </Modal>
{/if}

{#if projectsState.removeConfirmProject}
  <Modal
    title={t('projects.remove.confirmTitle')}
    onClose={cancelRemove}
    closeDisabled={Boolean(projectsState.removingProjectId)}
  >
    {#snippet body()}
      <p>
        {t('projects.remove.rootedAgentsBody', {
          name:
            projectsState.removeConfirmProject.display_name ||
            projectsState.removeConfirmProject.project_id,
        })}
      </p>
      <div class="projects-toggle-row">
        <span>
          {t('projects.remove.copyIdentityFiles')}
        </span>
        <Toggle
          size="sm"
          checked={projectsState.copyRootedAgentIdentityFiles}
          disabled={Boolean(projectsState.removingProjectId)}
          ariaLabel={t('projects.remove.copyIdentityFiles')}
          onChange={(next) =>
            (projectsState.copyRootedAgentIdentityFiles = next)}
        />
      </div>
      <p class="modal-hint">
        {t('projects.remove.copyIdentityFilesHelp')}
      </p>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={Boolean(projectsState.removingProjectId)}
        onClick={cancelRemove}
      >
        {t('common.cancel')}
      </Button>
      <Button
        variant="danger"
        disabled={Boolean(projectsState.removingProjectId)}
        onClick={confirmRemove}
      >
        {t('common.remove')}
      </Button>
    {/snippet}
  </Modal>
{/if}
