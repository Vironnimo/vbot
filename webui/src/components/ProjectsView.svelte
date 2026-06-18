<script>
  import { onMount } from 'svelte';

  import Button from './ui/Button.svelte';
  import Modal from './ui/Modal.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import TextField from './ui/TextField.svelte';
  import {
    addProject,
    listProjects,
    removeProject,
    setProject,
    showProject,
  } from '$lib/api.js';
  import {
    buildAddProjectPayload,
    buildManageProjectPayload,
    buildRePointPayload,
    hasManageChanges,
    needsRePoint,
    normalizeProject,
    normalizeProjects,
    normalizeScanReport,
    projectTeam,
  } from '$lib/projectsView.js';
  import { t } from '$lib/i18n.js';

  const PROJECT_BUSY_CODE = 'project_busy';
  const PROJECT_IN_USE_CODE = 'project_in_use';

  // Add-form state: the server path is the only required field; the rest are
  // optional pointers the backend treats as "fall through the chain" when blank.
  let addForm = $state(createAddForm());
  let addingProject = $state(false);
  let addError = $state('');

  let projects = $state([]);
  let loadingProjects = $state(false);
  let listError = $state('');
  let statusMessage = $state('');

  // The most recent add/show response, shown as the Team + Report panel under
  // the list. Add-dann-Review: project.add creates the project AND returns its
  // scan, so this panel is the review surface (there is no dry-run preview).
  let activeProjectId = $state('');
  let activeTeam = $state([]);
  let activeReport = $state(null);
  let scanLoading = $state(false);

  // Manage modal state.
  let manageProject = $state(null);
  let manageForm = $state(createManageForm());
  let manageSaving = $state(false);
  let manageError = $state('');
  let removingProjectId = $state('');

  // Re-point modal state (a project whose cwd_exists === false).
  let rePointProject = $state(null);
  let rePointCwd = $state('');
  let rePointing = $state(false);
  let rePointError = $state('');

  let destroyed = false;
  let listRequestId = 0;

  let hasProjects = $derived(projects.length > 0);
  let canSubmitAdd = $derived(addForm.cwd.trim().length > 0 && !addingProject);
  let manageTitle = $derived(
    t('projects.manage.title', 'Manage {name}', {
      name: manageProject?.display_name || manageProject?.project_id || '',
    }),
  );

  onMount(() => {
    loadProjects();

    return () => {
      destroyed = true;
    };
  });

  function createAddForm() {
    return {
      cwd: '',
      display_name: '',
      default_agent: '',
      default_model: '',
      auto_load: '',
    };
  }

  function createManageForm(project = null) {
    return {
      display_name: project?.display_name ?? '',
      default_agent: project?.default_agent ?? '',
      default_model: project?.default_model ?? '',
      auto_load: (project?.auto_load ?? []).join('\n'),
    };
  }

  async function loadProjects() {
    const requestId = listRequestId + 1;
    listRequestId = requestId;
    loadingProjects = true;
    listError = '';

    try {
      const result = await listProjects();
      if (destroyed || requestId !== listRequestId) {
        return;
      }
      projects = normalizeProjects(result?.projects);
    } catch (error) {
      if (destroyed || requestId !== listRequestId) {
        return;
      }
      listError = `${t('projects.loadError', 'Projects could not be loaded.')} ${errorText(error)}`;
    } finally {
      if (!destroyed && requestId === listRequestId) {
        loadingProjects = false;
      }
    }
  }

  function updateAddField(field, value) {
    addForm[field] = value;
    addError = '';
  }

  async function submitAdd(event) {
    event.preventDefault();
    if (addForm.cwd.trim().length === 0) {
      addError = t(
        'projects.add.missingCwd',
        'Enter a repository path to add a project.',
      );
      return;
    }

    addingProject = true;
    addError = '';
    statusMessage = '';

    try {
      const payload = buildAddProjectPayload({
        cwd: addForm.cwd,
        display_name: addForm.display_name,
        default_agent: addForm.default_agent,
        default_model: addForm.default_model,
        auto_load: splitLines(addForm.auto_load),
      });
      const result = await addProject(payload);
      if (destroyed) {
        return;
      }
      const project = normalizeProject(result?.project);
      showScan(project.project_id, result?.scan);
      statusMessage = t('projects.add.success', 'Project added.');
      addForm = createAddForm();
      await loadProjects();
    } catch (error) {
      if (destroyed) {
        return;
      }
      addError = `${t('projects.add.error', 'Project could not be added.')} ${errorText(error)}`;
    } finally {
      if (!destroyed) {
        addingProject = false;
      }
    }
  }

  function showScan(projectId, scan) {
    activeProjectId = projectId;
    activeTeam = projectTeam(scan);
    activeReport = normalizeScanReport(scan?.report);
  }

  async function reviewProject(project) {
    activeProjectId = project.project_id;
    scanLoading = true;
    statusMessage = '';
    listError = '';

    try {
      const result = await showProject(project.project_id);
      if (destroyed || activeProjectId !== project.project_id) {
        return;
      }
      showScan(project.project_id, result?.scan);
    } catch (error) {
      if (destroyed) {
        return;
      }
      listError = `${t('projects.loadError', 'Projects could not be loaded.')} ${errorText(error)}`;
    } finally {
      if (!destroyed) {
        scanLoading = false;
      }
    }
  }

  function openManage(project) {
    manageProject = project;
    manageForm = createManageForm(project);
    manageError = '';
  }

  function closeManage() {
    if (manageSaving) {
      return;
    }
    manageProject = null;
    manageError = '';
  }

  function updateManageField(field, value) {
    manageForm[field] = value;
    manageError = '';
  }

  async function submitManage(event) {
    event.preventDefault();
    if (!manageProject) {
      return;
    }

    const changes = buildManageProjectPayload(
      {
        display_name: manageForm.display_name,
        default_agent: manageForm.default_agent,
        default_model: manageForm.default_model,
        auto_load: splitLines(manageForm.auto_load),
      },
      manageProject,
    );

    if (!hasManageChanges(changes)) {
      manageError = t('projects.manage.noChanges', 'No changes to save.');
      return;
    }

    manageSaving = true;
    manageError = '';
    statusMessage = '';

    try {
      const result = await setProject(manageProject.project_id, changes);
      if (destroyed) {
        return;
      }
      showScan(manageProject.project_id, result?.scan);
      statusMessage = t('projects.manage.saveSuccess', 'Project updated.');
      manageProject = null;
      await loadProjects();
    } catch (error) {
      if (destroyed) {
        return;
      }
      manageError = `${t('projects.manage.saveError', 'Project changes could not be saved.')} ${errorText(error)}`;
    } finally {
      if (!destroyed) {
        manageSaving = false;
      }
    }
  }

  async function removeOne(project) {
    const confirmRemove =
      typeof globalThis.confirm === 'function'
        ? globalThis.confirm(
            t(
              'projects.remove.confirm',
              'Remove project {name}? The project is archived and can be restored; the repository on disk is never touched.',
              { name: project.display_name || project.project_id },
            ),
          )
        : true;

    if (!confirmRemove) {
      return;
    }

    removingProjectId = project.project_id;
    statusMessage = '';
    listError = '';

    try {
      await removeProject(project.project_id);
      if (destroyed) {
        return;
      }
      if (activeProjectId === project.project_id) {
        clearScan();
      }
      statusMessage = t('projects.remove.success', 'Project removed.');
      await loadProjects();
    } catch (error) {
      if (destroyed) {
        return;
      }
      listError = removeErrorText(error);
    } finally {
      if (!destroyed) {
        removingProjectId = '';
      }
    }
  }

  function openRePoint(project) {
    rePointProject = project;
    rePointCwd = '';
    rePointError = '';
  }

  function closeRePoint() {
    if (rePointing) {
      return;
    }
    rePointProject = null;
    rePointError = '';
  }

  async function submitRePoint(event) {
    event.preventDefault();
    if (!rePointProject) {
      return;
    }
    if (rePointCwd.trim().length === 0) {
      rePointError = t(
        'projects.rePoint.missingCwd',
        'Enter the new repository path.',
      );
      return;
    }

    rePointing = true;
    rePointError = '';
    statusMessage = '';

    try {
      const result = await setProject(
        rePointProject.project_id,
        buildRePointPayload(rePointCwd),
      );
      if (destroyed) {
        return;
      }
      showScan(rePointProject.project_id, result?.scan);
      statusMessage = t('projects.rePoint.success', 'Project re-pointed.');
      rePointProject = null;
      await loadProjects();
    } catch (error) {
      if (destroyed) {
        return;
      }
      rePointError = `${t('projects.rePoint.error', 'The project could not be re-pointed.')} ${errorText(error)}`;
    } finally {
      if (!destroyed) {
        rePointing = false;
      }
    }
  }

  function clearScan() {
    activeProjectId = '';
    activeTeam = [];
    activeReport = null;
  }

  function groupLabel(type) {
    return t(`projects.report.group.${type}`, type);
  }

  function projectValue(value) {
    return value || t('projects.list.none', '—');
  }

  function removeErrorText(error) {
    if (error?.code === PROJECT_BUSY_CODE) {
      return t(
        'projects.remove.busy',
        'This project has an active or queued run and cannot be removed right now.',
      );
    }
    if (error?.code === PROJECT_IN_USE_CODE) {
      return t(
        'projects.remove.inUse',
        'A cron job points at one of this project’s agents, so it cannot be removed. Remove or retarget the cron job first.',
      );
    }
    return `${t('projects.remove.error', 'Project could not be removed.')} ${errorText(error)}`;
  }

  function errorText(error) {
    if (typeof error?.message === 'string' && error.message.trim()) {
      return error.message.trim();
    }
    if (typeof error === 'string' && error.trim()) {
      return error.trim();
    }
    return t('common.unknown', 'Unknown');
  }

  function splitLines(value) {
    return String(value ?? '')
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
  }
</script>

<section class="projects-view view active" aria-labelledby="projects-title">
  <header class="projects-view__header">
    <div>
      <p class="projects-view__eyebrow">
        {t('projects.eyebrow', 'Project workspaces')}
      </p>
      <h2 id="projects-title" class="projects-view__title">
        {t('projects.title', 'Projects')}
      </h2>
      <p class="projects-view__subtitle">
        {t(
          'projects.subtitle',
          'Add a repository as a project to discover its team and chat with project agents. Adding a project also scans its repo for issues.',
        )}
      </p>
    </div>

    <div class="projects-view__header-actions">
      <Button variant="secondary" onClick={() => loadProjects()}>
        {t('projects.refresh', 'Refresh')}
      </Button>
    </div>
  </header>

  {#if listError}
    <p class="projects-view__notice projects-view__notice--error" role="alert">
      {listError}
    </p>
  {/if}

  {#if statusMessage}
    <p class="projects-view__notice" role="status">{statusMessage}</p>
  {/if}

  <form class="projects-view__add" onsubmit={submitAdd}>
    <h3 class="projects-view__section-title">
      {t('projects.add.title', 'Add project')}
    </h3>
    <p class="projects-view__section-subtitle">
      {t(
        'projects.add.subtitle',
        'Enter the path to a repository on this machine. The folder must already exist; vBot reads it but never writes to it.',
      )}
    </p>

    <label class="projects-view__field">
      <span class="projects-view__label">
        {t('projects.add.cwd', 'Repository path')}
      </span>
      <TextField
        id="projects-add-cwd"
        value={addForm.cwd}
        placeholder={t('projects.add.cwdPlaceholder', 'C:/path/to/repository')}
        disabled={addingProject}
        onInput={(next) => updateAddField('cwd', next)}
      />
      <span class="projects-view__help">
        {t(
          'projects.add.cwdHelp',
          'The folder must exist. The project is created immediately and then scanned — you can remove it again afterwards.',
        )}
      </span>
    </label>

    <div class="projects-view__field-grid">
      <label class="projects-view__field">
        <span class="projects-view__label">
          {t('projects.add.displayName', 'Display name')}
        </span>
        <TextField
          id="projects-add-display-name"
          value={addForm.display_name}
          placeholder={t(
            'projects.add.displayNamePlaceholder',
            'Optional — defaults to the folder name',
          )}
          disabled={addingProject}
          onInput={(next) => updateAddField('display_name', next)}
        />
      </label>

      <label class="projects-view__field">
        <span class="projects-view__label">
          {t('projects.add.defaultAgent', 'Default agent')}
        </span>
        <TextField
          id="projects-add-default-agent"
          value={addForm.default_agent}
          placeholder={t('projects.add.defaultAgentPlaceholder', 'Optional')}
          disabled={addingProject}
          onInput={(next) => updateAddField('default_agent', next)}
        />
      </label>

      <label class="projects-view__field">
        <span class="projects-view__label">
          {t('projects.add.defaultModel', 'Default model')}
        </span>
        <TextField
          id="projects-add-default-model"
          value={addForm.default_model}
          placeholder={t('projects.add.defaultModelPlaceholder', 'Optional')}
          disabled={addingProject}
          onInput={(next) => updateAddField('default_model', next)}
        />
      </label>
    </div>

    <label class="projects-view__field">
      <span class="projects-view__label">
        {t('projects.add.autoLoad', 'Auto-load files')}
      </span>
      <textarea
        id="projects-add-auto-load"
        class="s-input projects-view__textarea"
        value={addForm.auto_load}
        placeholder={t('projects.add.autoLoadPlaceholder', 'One path per line')}
        disabled={addingProject}
        oninput={(event) =>
          updateAddField('auto_load', event.currentTarget.value)}
      ></textarea>
      <span class="projects-view__help">
        {t(
          'projects.add.autoLoadHelp',
          'Files loaded into context when a project session opens. One path per line.',
        )}
      </span>
    </label>

    {#if addError}
      <p
        class="projects-view__notice projects-view__notice--error"
        role="alert"
      >
        {addError}
      </p>
    {/if}

    <div class="projects-view__add-actions">
      <Button variant="primary" type="submit" disabled={!canSubmitAdd}>
        {addingProject
          ? t('projects.add.submitting', 'Adding project…')
          : t('projects.add.submit', 'Add project')}
      </Button>
    </div>
  </form>

  <div class="projects-view__list">
    <h3 class="projects-view__section-title">
      {t('projects.list.title', 'Your projects')}
    </h3>

    {#if loadingProjects}
      <p class="projects-view__notice" role="status">
        {t('projects.loading', 'Loading projects…')}
      </p>
    {:else if !hasProjects}
      <div class="projects-view__empty">
        <p class="projects-view__empty-title">
          {t('projects.emptyTitle', 'No projects yet')}
        </p>
        <p class="projects-view__empty-subtitle">
          {t(
            'projects.emptySubtitle',
            'Add a repository path below to create your first project.',
          )}
        </p>
      </div>
    {:else}
      <ul class="projects-view__items">
        {#each projects as project (project.project_id)}
          <li
            class="projects-view__item"
            data-testid={`project-${project.project_id}`}
          >
            <div class="projects-view__item-main">
              <div class="projects-view__item-head">
                <span class="projects-view__item-name">
                  {project.display_name || project.project_id}
                </span>
                {#if needsRePoint(project)}
                  <StatusChip variant="error">
                    {t('projects.rePoint.title', 'Repository not found')}
                  </StatusChip>
                {/if}
              </div>
              <p class="projects-view__item-cwd" title={project.cwd}>
                {project.cwd}
              </p>
              <div class="projects-view__item-meta">
                <span>
                  {t('projects.list.defaultAgent', 'Default agent')}:
                  {projectValue(project.default_agent)}
                </span>
                <span>
                  {t('projects.list.defaultModel', 'Default model')}:
                  {projectValue(project.default_model)}
                </span>
              </div>
            </div>
            <div class="projects-view__item-actions">
              <Button
                variant="secondary"
                data-testid={`project-review-${project.project_id}`}
                disabled={removingProjectId === project.project_id}
                onClick={() => reviewProject(project)}
              >
                {t('projects.team.title', 'Team')}
              </Button>
              {#if needsRePoint(project)}
                <Button
                  variant="primary"
                  data-testid={`project-repoint-${project.project_id}`}
                  disabled={removingProjectId === project.project_id}
                  onClick={() => openRePoint(project)}
                >
                  {t('projects.rePoint.submit', 'Re-point')}
                </Button>
              {/if}
              <Button
                variant="secondary"
                data-testid={`project-manage-${project.project_id}`}
                disabled={removingProjectId === project.project_id}
                onClick={() => openManage(project)}
              >
                {t('projects.manage', 'Manage')}
              </Button>
              <Button
                variant="danger"
                data-testid={`project-remove-${project.project_id}`}
                disabled={removingProjectId === project.project_id}
                onClick={() => removeOne(project)}
              >
                {t('projects.remove', 'Remove')}
              </Button>
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  {#if activeProjectId}
    <div class="projects-view__scan" data-testid="project-scan">
      <h3 class="projects-view__section-title">
        {t('projects.team.title', 'Team')}
      </h3>

      {#if scanLoading}
        <p class="projects-view__notice" role="status">
          {t('projects.loading', 'Loading projects…')}
        </p>
      {:else}
        {#if activeTeam.length === 0}
          <p class="projects-view__notice" role="status">
            {t(
              'projects.team.empty',
              'No agents discovered in this repository yet. An empty project is valid — add agent files to the repo to build a team.',
            )}
          </p>
        {:else}
          <ul class="projects-view__team">
            {#each activeTeam as member (member.agent_id)}
              <li class="projects-view__team-member">
                <span class="projects-view__team-name"
                  >{member.display_name}</span
                >
                <span class="projects-view__team-model">
                  {member.model || t('projects.team.noModel', 'No model')}
                </span>
                {#if member.description}
                  <span class="projects-view__team-desc"
                    >{member.description}</span
                  >
                {/if}
              </li>
            {/each}
          </ul>
        {/if}

        {#if activeReport}
          <h3 class="projects-view__section-title">
            {t('projects.report.title', 'Scan report')}
          </h3>
          {#if activeReport.clean}
            <p class="projects-view__notice" role="status">
              {t(
                'projects.report.clean',
                'No issues found in this repository.',
              )}
            </p>
          {:else}
            <p
              class="projects-view__notice projects-view__notice--warn"
              role="status"
            >
              {t('projects.report.findingCount', '{count} issues found', {
                count: activeReport.findingCount,
              })}
            </p>
            {#each activeReport.groups as group (group.type)}
              <div class="projects-view__finding-group">
                <h4 class="projects-view__finding-title">
                  {groupLabel(group.type)}
                </h4>
                <ul class="projects-view__findings">
                  {#each group.findings as finding, index (`${group.type}-${index}`)}
                    <li class="projects-view__finding">
                      <span class="projects-view__finding-detail"
                        >{finding.detail}</span
                      >
                      {#if finding.agent_id}
                        <span class="projects-view__finding-meta">
                          {t(
                            'projects.report.finding.agent',
                            'Agent {agentId}',
                            {
                              agentId: finding.agent_id,
                            },
                          )}
                        </span>
                      {/if}
                      {#if finding.source_path}
                        <span class="projects-view__finding-meta">
                          {t(
                            'projects.report.finding.source',
                            'Source: {source}',
                            {
                              source: finding.source_path,
                            },
                          )}
                        </span>
                      {/if}
                    </li>
                  {/each}
                </ul>
              </div>
            {/each}
          {/if}
        {/if}
      {/if}
    </div>
  {/if}

  {#if manageProject}
    <Modal
      title={manageTitle}
      labelledById="projects-manage-title"
      class="projects-view__modal"
      onClose={closeManage}
    >
      {#snippet body()}
        <form onsubmit={submitManage}>
          <div class="modal-body">
            <label class="modal-field">
              <span class="modal-label">
                {t('projects.manage.displayName', 'Display name')}
              </span>
              <TextField
                id="projects-manage-display-name"
                value={manageForm.display_name}
                disabled={manageSaving}
                onInput={(next) => updateManageField('display_name', next)}
              />
            </label>

            <label class="modal-field">
              <span class="modal-label">
                {t('projects.manage.defaultAgent', 'Default agent')}
              </span>
              <TextField
                id="projects-manage-default-agent"
                value={manageForm.default_agent}
                disabled={manageSaving}
                onInput={(next) => updateManageField('default_agent', next)}
              />
            </label>

            <label class="modal-field">
              <span class="modal-label">
                {t('projects.manage.defaultModel', 'Default model')}
              </span>
              <TextField
                id="projects-manage-default-model"
                value={manageForm.default_model}
                disabled={manageSaving}
                onInput={(next) => updateManageField('default_model', next)}
              />
            </label>

            <label class="modal-field">
              <span class="modal-label">
                {t('projects.manage.autoLoad', 'Auto-load files')}
              </span>
              <textarea
                id="projects-manage-auto-load"
                class="s-input projects-view__textarea"
                value={manageForm.auto_load}
                placeholder={t(
                  'projects.manage.autoLoadPlaceholder',
                  'One path per line',
                )}
                disabled={manageSaving}
                oninput={(event) =>
                  updateManageField('auto_load', event.currentTarget.value)}
              ></textarea>
            </label>

            {#if manageError}
              <p
                class="projects-view__notice projects-view__notice--error"
                role="alert"
              >
                {manageError}
              </p>
            {/if}
          </div>

          <div class="modal-footer">
            <Button
              variant="secondary"
              disabled={manageSaving}
              onClick={closeManage}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button variant="primary" type="submit" disabled={manageSaving}>
              {manageSaving
                ? t('projects.manage.saving', 'Saving…')
                : t('projects.manage.save', 'Save changes')}
            </Button>
          </div>
        </form>
      {/snippet}
    </Modal>
  {/if}

  {#if rePointProject}
    <Modal
      title={t('projects.rePoint.title', 'Repository not found')}
      labelledById="projects-repoint-title"
      class="projects-view__modal"
      onClose={closeRePoint}
    >
      {#snippet body()}
        <form onsubmit={submitRePoint}>
          <div class="modal-body">
            <p class="projects-view__help">
              {t(
                'projects.rePoint.description',
                'The repository folder for this project no longer exists. Point it at the new location to restore the project.',
              )}
            </p>
            <label class="modal-field">
              <span class="modal-label">
                {t('projects.rePoint.cwd', 'New repository path')}
              </span>
              <TextField
                id="projects-repoint-cwd"
                value={rePointCwd}
                placeholder={t(
                  'projects.rePoint.cwdPlaceholder',
                  'C:/path/to/repository',
                )}
                disabled={rePointing}
                onInput={(next) => {
                  rePointCwd = next;
                  rePointError = '';
                }}
              />
            </label>

            {#if rePointError}
              <p
                class="projects-view__notice projects-view__notice--error"
                role="alert"
              >
                {rePointError}
              </p>
            {/if}
          </div>

          <div class="modal-footer">
            <Button
              variant="secondary"
              disabled={rePointing}
              onClick={closeRePoint}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button variant="primary" type="submit" disabled={rePointing}>
              {rePointing
                ? t('projects.rePoint.submitting', 'Re-pointing…')
                : t('projects.rePoint.submit', 'Re-point')}
            </Button>
          </div>
        </form>
      {/snippet}
    </Modal>
  {/if}
</section>

<style>
  .projects-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 16px;
    overflow: auto;
    padding: 24px 28px 28px;
    background: var(--bg);
  }

  .projects-view__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .projects-view__eyebrow {
    margin: 0 0 6px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .projects-view__title {
    margin: 0;
    color: var(--text-hi);
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .projects-view__subtitle {
    max-width: 760px;
    margin: 6px 0 0;
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .projects-view__header-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
  }

  .projects-view__notice {
    margin: 0;
    padding: 11px 14px;
    border: 1px solid var(--border-2);
    border-left: 2px solid var(--green);
    border-radius: var(--r-md);
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.4;
    background: var(--surface);
  }

  .projects-view__notice--error {
    border-left-color: var(--red);
    color: var(--red);
  }

  .projects-view__notice--warn {
    border-left-color: var(--amber);
    color: var(--amber);
  }

  .projects-view__add,
  .projects-view__list,
  .projects-view__scan {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 18px 20px;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  .projects-view__section-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 14px;
    font-weight: 600;
  }

  .projects-view__section-subtitle {
    max-width: 760px;
    margin: 0;
    color: var(--text-med);
    font-size: 12px;
    line-height: 1.5;
  }

  .projects-view__field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .projects-view__field-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
  }

  .projects-view__label {
    color: var(--text-med);
    font-size: 12px;
    font-weight: 500;
  }

  .projects-view__help {
    color: var(--text-lo);
    font-size: 11.5px;
    line-height: 1.4;
  }

  .projects-view__textarea {
    min-height: 72px;
    resize: vertical;
  }

  .projects-view__add-actions {
    display: flex;
    justify-content: flex-end;
  }

  .projects-view__empty {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 22px;
    border: 1px dashed var(--border);
    border-radius: var(--r-lg);
    background: rgba(255, 255, 255, 0.02);
    text-align: center;
  }

  .projects-view__empty-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 14px;
    font-weight: 600;
  }

  .projects-view__empty-subtitle {
    margin: 0;
    color: var(--text-med);
    font-size: 12.5px;
  }

  .projects-view__items {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .projects-view__item {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 14px;
    padding: 12px 14px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
  }

  .projects-view__item-main {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 4px;
  }

  .projects-view__item-head {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .projects-view__item-name {
    color: var(--text-hi);
    font-size: 13.5px;
    font-weight: 600;
  }

  .projects-view__item-cwd {
    margin: 0;
    overflow: hidden;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .projects-view__item-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    color: var(--text-lo);
    font-size: 11.5px;
  }

  .projects-view__item-actions {
    display: inline-flex;
    flex-shrink: 0;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .projects-view__team {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .projects-view__team-member {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 10px;
    padding: 8px 12px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
  }

  .projects-view__team-name {
    color: var(--text-hi);
    font-size: 12.5px;
    font-weight: 600;
  }

  .projects-view__team-model {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11.5px;
  }

  .projects-view__team-desc {
    flex-basis: 100%;
    color: var(--text-lo);
    font-size: 11.5px;
    line-height: 1.4;
  }

  .projects-view__finding-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .projects-view__finding-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 12.5px;
    font-weight: 600;
  }

  .projects-view__findings {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .projects-view__finding {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 12px;
    border: 1px solid var(--border-2);
    border-left: 2px solid var(--amber);
    border-radius: var(--r-md);
    background: var(--surface-2);
  }

  .projects-view__finding-detail {
    color: var(--text-med);
    font-size: 12px;
    line-height: 1.4;
  }

  .projects-view__finding-meta {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  :global(.projects-view__modal) {
    width: 540px;
    max-width: calc(100vw - 40px);
  }

  @media (max-width: 960px) {
    .projects-view {
      padding: 20px;
    }

    .projects-view__header,
    .projects-view__header-actions {
      align-items: stretch;
      flex-direction: column;
    }

    .projects-view__item {
      flex-direction: column;
    }
  }
</style>
