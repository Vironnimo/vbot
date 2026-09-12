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
    title={t('projects.add.title', 'Add project')}
    labelledById="projects-add-title"
    class="projects-view__modal"
    closeDisabled={projectsState.addingProject}
    onClose={closeAdd}
  >
    {#snippet body()}
      <form onsubmit={submitAdd}>
        <div class="modal-body">
          <p class="projects-help">
            {t(
              'projects.add.subtitle',
              'Enter the path to a repository on this machine. The folder must already exist; vBot reads it but never writes to it.',
            )}
          </p>

          <FormField
            controlId="projects-add-cwd"
            label={t('projects.add.cwd', 'Repository path')}
            help={t(
              'projects.add.cwdHelp',
              'The folder must exist. The project is created immediately and then scanned — you can remove it again afterwards.',
            )}
          >
            <TextField
              id="projects-add-cwd"
              variant="modal"
              value={projectsState.addForm.cwd}
              placeholder={t(
                'projects.add.cwdPlaceholder',
                'C:/path/to/repository',
              )}
              disabled={projectsState.addingProject}
              onInput={(next) => updateAddField('cwd', next)}
            />
          </FormField>

          <FormField
            controlId="projects-add-display-name"
            label={t('projects.add.displayName', 'Display name')}
          >
            <TextField
              id="projects-add-display-name"
              variant="modal"
              value={projectsState.addForm.display_name}
              placeholder={t(
                'projects.add.displayNamePlaceholder',
                'Optional — defaults to the folder name',
              )}
              disabled={projectsState.addingProject}
              onInput={(next) => updateAddField('display_name', next)}
            />
          </FormField>

          {#if addShowsFormatChoice}
            <FormField
              label={t('projects.add.format', 'Source format')}
              help={t(
                'projects.add.formatHelp',
                'This repository carries both ecosystems. Pick which one this project uses — its agents and skills come only from that one. You can switch later in the project settings.',
              )}
              role="radiogroup"
              aria-label={t('projects.add.format', 'Source format')}
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
                      {t(
                        'projects.add.formatCounts',
                        '{agents} agents · {skills} skills',
                        {
                          agents:
                            projectsState.addDetect?.formats?.[formatKey]
                              ?.agents ?? 0,
                          skills:
                            projectsState.addDetect?.formats?.[formatKey]
                              ?.skills ?? 0,
                        },
                      )}
                    </span>
                  </button>
                {/each}
              </div>
            </FormField>
          {:else if addDetectedFormat}
            <p class="projects-help">
              {t('projects.add.formatDetected', 'Detected: {format}', {
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
                ariaLabel={t(
                  'projects.add.claudeMdSuggestionLabel',
                  'Load CLAUDE.md as a project file',
                )}
                onChange={(next) => updateAddField('include_claude_md', next)}
              />
              <span>
                {t(
                  'projects.add.claudeMdSuggestion',
                  'Load {path} as a project file. The repository has no AGENTS.md; the file is loaded as-is into project agent prompts.',
                  { path: projectsState.addDetect?.claude_md ?? 'CLAUDE.md' },
                )}
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
            {t('common.cancel', 'Cancel')}
          </Button>
          <Button variant="primary" type="submit" disabled={!canSubmitAdd}>
            {projectsState.addingProject
              ? t('projects.add.submitting', 'Adding project…')
              : t('projects.add.submit', 'Add project')}
          </Button>
        </div>
      </form>
    {/snippet}
  </Modal>
{/if}

{#if projectsState.rePointProject}
  <Modal
    title={t('projects.rePoint.title', 'Repository not found')}
    labelledById="projects-repoint-title"
    class="projects-view__modal"
    onClose={closeRePoint}
  >
    {#snippet body()}
      <form onsubmit={submitRePoint}>
        <div class="modal-body">
          <p class="projects-help">
            {t(
              'projects.rePoint.description',
              'The repository folder for this project no longer exists. Point it at the new location to restore the project.',
            )}
          </p>
          <FormField
            controlId="projects-repoint-cwd"
            label={t('projects.rePoint.cwd', 'New repository path')}
          >
            <TextField
              id="projects-repoint-cwd"
              variant="modal"
              value={projectsState.rePointCwd}
              placeholder={t(
                'projects.rePoint.cwdPlaceholder',
                'C:/path/to/repository',
              )}
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
            {t('common.cancel', 'Cancel')}
          </Button>
          <Button
            variant="primary"
            type="submit"
            disabled={projectsState.rePointing}
          >
            {projectsState.rePointing
              ? t('projects.rePoint.submitting', 'Re-pointing…')
              : t('projects.rePoint.submit', 'Re-point')}
          </Button>
        </div>
      </form>
    {/snippet}
  </Modal>
{/if}

{#if projectsState.removeConfirmProject}
  <Modal
    title={t('projects.remove.confirmTitle', 'Remove project')}
    onClose={cancelRemove}
    closeDisabled={Boolean(projectsState.removingProjectId)}
  >
    {#snippet body()}
      <p>
        {t(
          'projects.remove.rootedAgentsBody',
          'Removing {name} clears it from every affected Rooted Agent and resets those Agents to their Default Workspace. Their Sessions and history stay unchanged. The repository and old Workspace files are never touched.',
          {
            name:
              projectsState.removeConfirmProject.display_name ||
              projectsState.removeConfirmProject.project_id,
          },
        )}
      </p>
      <div class="projects-toggle-row">
        <span>
          {t(
            'projects.remove.copyIdentityFiles',
            'Copy SOUL.md, USER.md, and MEMORY.md to affected Default Workspaces',
          )}
        </span>
        <Toggle
          size="sm"
          checked={projectsState.copyRootedAgentIdentityFiles}
          disabled={Boolean(projectsState.removingProjectId)}
          ariaLabel={t(
            'projects.remove.copyIdentityFiles',
            'Copy SOUL.md, USER.md, and MEMORY.md to affected Default Workspaces',
          )}
          onChange={(next) =>
            (projectsState.copyRootedAgentIdentityFiles = next)}
        />
      </div>
      <p class="modal-hint">
        {t(
          'projects.remove.copyIdentityFilesHelp',
          'When enabled, existing destination versions are backed up before replacement. One choice applies to every affected Agent.',
        )}
      </p>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={Boolean(projectsState.removingProjectId)}
        onClick={cancelRemove}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        variant="danger"
        disabled={Boolean(projectsState.removingProjectId)}
        onClick={confirmRemove}
      >
        {t('common.remove', 'Remove')}
      </Button>
    {/snippet}
  </Modal>
{/if}
