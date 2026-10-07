<script>
  import Modal from '../ui/Modal.svelte';
  import { t } from '$lib/i18n.js';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import PathField from '../ui/PathField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ArchiveDeleteOption from '../archive/ArchiveDeleteOption.svelte';
  let { projectsState = $bindable(), projectsController } = $props();

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
            <PathField
              id="projects-add-cwd"
              variant="modal"
              mode="directory"
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

          {#if projectsState.addDetect?.sources?.length}
            <p class="projects-help">
              {t('projects.sources.detected', {
                count: projectsState.addDetect.sources.length,
              })}
            </p>
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
            <PathField
              id="projects-repoint-cwd"
              variant="modal"
              mode="directory"
              value={projectsState.rePointCwd}
              startPath={projectsState.rePointProject.cwd}
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
    class="projects-view__modal"
    onClose={cancelRemove}
    closeDisabled={Boolean(projectsState.removingProjectId)}
  >
    {#snippet body()}
      <div class="modal-body">
        <p>
          {projectsState.removePermanently
            ? t('projects.remove.permanentBody', {
                name:
                  projectsState.removeConfirmProject.display_name ||
                  projectsState.removeConfirmProject.project_id,
              })
            : t('projects.remove.body', {
                name:
                  projectsState.removeConfirmProject.display_name ||
                  projectsState.removeConfirmProject.project_id,
              })}
        </p>
        <div class="projects-remove-copy">
          <Toggle
            size="sm"
            checked={projectsState.copyRootedAgentIdentityFiles}
            disabled={Boolean(projectsState.removingProjectId)}
            ariaLabel={t('projects.remove.copyIdentityFiles')}
            onChange={(next) =>
              (projectsState.copyRootedAgentIdentityFiles = next)}
          />
          <div>
            <span>{t('projects.remove.copyIdentityFiles')}</span>
            <p class="projects-help">
              {t('projects.remove.copyIdentityFilesHelp')}
            </p>
          </div>
        </div>
        <ArchiveDeleteOption
          permanent={projectsState.removePermanently}
          disabled={Boolean(projectsState.removingProjectId)}
          onChange={(next) => (projectsState.removePermanently = next)}
        />
      </div>
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
        {projectsState.removePermanently
          ? t('archive.deletePermanently')
          : t('common.remove')}
      </Button>
    {/snippet}
  </Modal>
{/if}
