<script>
  import { onDestroy } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { parseAgentAddress } from '$lib/agentAddress.js';
  import { sessionDisplayName } from '$lib/sessionListView.js';
  import {
    listSessions,
    getSession,
    listModels,
    listConnections,
  } from '$lib/api.js';
  import {
    reasoningForModelValue,
    effortOptionsForReasoning,
    effortForReasoning,
  } from '$lib/agentForm.js';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    selectModelValue,
    modelFilterFooterLabel,
    createModelCatalogLoader,
  } from '$lib/modelSelection.js';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Banner from '../ui/Banner.svelte';

  let { editor, agentOptions = [], agents = [], projects = [] } = $props();
  let values = $derived(editor.formValues);
  let address = $derived(parseAgentAddress(values.agent_id));
  let existing = $derived(values.session_mode === 'existing');
  let busy = $derived(editor.isCreating && editor.submittingForm);
  let sessions = $state([]);
  let session = $state(null);
  let sessionLoading = $state(false);
  let selectedLoading = $state(false);
  let sessionError = $state('');
  let nextCursor = $state(null);
  let request = 0;
  let selectedRequest = 0;
  let destroyed = false;
  let models = $state([]);
  let connections = $state([]);
  let modelError = $state('');
  let advanced = $state(false);
  let showAllModels = $state(false);
  const modelCatalog = createModelCatalogLoader({
    listModels,
    listConnections,
    applyCatalogs(catalog) {
      if (!destroyed) {
        models = catalog.models;
        connections = catalog.connections;
      }
    },
  });

  onDestroy(() => {
    destroyed = true;
    request += 1;
    selectedRequest += 1;
    modelCatalog.invalidate();
  });

  $effect(() => {
    const target = values.agent_id;
    const pinned = existing;
    request += 1;
    sessions = [];
    nextCursor = null;
    sessionError = '';
    if (pinned && target) void loadSessions(target);
  });

  $effect(() => {
    const target = values.agent_id;
    const id = values.session_id;
    const current = ++selectedRequest;
    session = null;
    selectedLoading = false;
    if (!existing || !target || !id) return;
    selectedLoading = true;
    getSession(target, id).then(
      (result) => {
        if (!destroyed && current === selectedRequest) {
          session = result?.session ?? null;
          selectedLoading = false;
        }
      },
      (error) => {
        if (!destroyed && current === selectedRequest) {
          sessionError = error.message;
          selectedLoading = false;
        }
      },
    );
  });

  $effect(() => {
    if (!advanced) return;
    modelError = '';
    modelCatalog.load().catch((error) => {
      if (!destroyed) modelError = error.message;
    });
  });

  async function loadSessions(target, cursor = null) {
    const current = ++request;
    sessionLoading = true;
    sessionError = '';
    try {
      const result = await listSessions(target, {
        limit: 100,
        cursor,
        includeCron: true,
        includeChannels: true,
      });
      if (destroyed || current !== request) return;
      const rows = result?.sessions ?? [];
      sessions = cursor ? [...sessions, ...rows] : rows;
      nextCursor = result?.next_cursor ?? null;
    } catch (error) {
      if (!destroyed && current === request) sessionError = error.message;
    } finally {
      if (!destroyed && current === request) sessionLoading = false;
    }
  }

  function projectName(id) {
    return (
      projects.find((project) => project.project_id === id)?.display_name || id
    );
  }

  let projectOptions = $derived([
    { value: 'workspace', label: t('cron.execution.workspace') },
    ...projects.map((project) => ({
      value: `project:${project.project_id}`,
      label: project.display_name || project.project_id,
      secondaryLabel: project.project_id,
    })),
    ...(values.working_project_choice.startsWith('project:') &&
    !projects.some(
      (project) =>
        `project:${project.project_id}` === values.working_project_choice,
    )
      ? [
          {
            value: values.working_project_choice,
            label: t('cron.execution.unavailableProject', {
              project: values.working_project_choice.slice(8),
            }),
            disabled: true,
          },
        ]
      : []),
  ]);
  let sessionOptions = $derived.by(() => {
    const rows = [...sessions];
    if (session && !rows.some((row) => row.id === session.id))
      rows.unshift(session);
    const options = rows.map((row) => ({
      value: row.id,
      label: sessionDisplayName(row),
      secondaryLabel: `${row.working_project_id ? projectName(row.working_project_id) : t('cron.execution.workspace')} · ${row.id}`,
    }));
    if (
      values.session_id &&
      !options.some((option) => option.value === values.session_id)
    ) {
      options.unshift({
        value: values.session_id,
        label: selectedLoading
          ? t('common.loading')
          : t('cron.execution.unavailableSession'),
        secondaryLabel: values.session_id,
        disabled: true,
      });
    }
    return options;
  });
  let fixedProject = $derived(
    address.projectId || session?.working_project_id || null,
  );
  let identityAgent = $derived(
    agents.find((agent) => agent.id === address.agentId),
  );
  let projectAgent = $derived(
    projects
      .find((project) => project.project_id === address.projectId)
      ?.scan?.team?.find((member) => member.agent_id === address.agentId),
  );
  let targetAgent = $derived(address.projectId ? projectAgent : identityAgent);

  function inherited(field) {
    const overrides = existing ? session?.agent_overrides : null;
    if (field === 'thinking_effort' && overrides?.thinking_effort_default)
      return '';
    return (
      overrides?.[field] ??
      targetAgent?.effective?.[field]?.value ??
      targetAgent?.[field] ??
      ''
    );
  }
  function inheritLabel(field) {
    const value = inherited(field);
    return value
      ? t('cron.execution.inheritValue', { value })
      : existing
        ? t('cron.execution.inheritSession')
        : t('cron.execution.inheritAgent');
  }
  let allModelOptions = $derived(
    buildModelSelectOptions({
      models,
      connections,
      selectedModelValue: values.model_override,
      emptyLabel: inheritLabel('model'),
    }),
  );
  let modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, {
      showAll: showAllModels,
      selectedModelValue: values.model_override,
    }),
  );
  let reasoning = $derived(
    reasoningForModelValue(values.model_override || inherited('model'), models),
  );
  let effortOptions = $derived([
    { value: '', label: inheritLabel('thinking_effort') },
    { value: 'default', label: t('chat.sessionSettings.providerDefault') },
    ...effortOptionsForReasoning(reasoning)
      .filter(Boolean)
      .map((level) => ({
        value: level,
        label: t(`agents.form.thinkingEffortOption.${level}`),
      })),
  ]);

  function chooseModel(value) {
    editor.updateFormField('model_override', value);
    if (values.thinking_effort_override !== 'default') {
      editor.updateFormField(
        'thinking_effort_override',
        effortForReasoning(
          values.thinking_effort_override,
          reasoningForModelValue(value || inherited('model'), models),
        ),
      );
    }
  }
</script>

<section class="s-section" aria-labelledby="cron-execution-title">
  <header class="s-section__head">
    <h3 class="s-section__title" id="cron-execution-title">
      {t('cron.execution.title')}
    </h3>
  </header>
  <p class="s-section__desc">{t('cron.execution.description')}</p>
  <div class="s-section__body">
    <div class="s-group">
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="cron-form-agent"
            >{t('cron.form.agent')}</label
          >
        </div>
        <div class="s-row-control">
          <SearchableDropdown
            id="cron-form-agent"
            disabled={busy}
            value={values.agent_id}
            options={agentOptions}
            ariaLabel={t('cron.form.agent')}
            placeholder={t('cron.form.agentPlaceholder')}
            onValueChange={(value) => editor.updateFormField('agent_id', value)}
          />
        </div>
      </div>
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="cron-session-mode"
            >{t('cron.execution.sessionMode')}</label
          >
        </div>
        <div class="s-row-control">
          <Dropdown
            id="cron-session-mode"
            disabled={busy}
            value={values.session_mode}
            options={[
              { value: 'new', label: t('cron.execution.newSession') },
              { value: 'existing', label: t('cron.execution.existingSession') },
            ]}
            ariaLabel={t('cron.execution.sessionMode')}
            onValueChange={(value) =>
              editor.updateFormField('session_mode', value)}
          />
        </div>
      </div>
      {#if existing}
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="cron-job-session"
              >{t('cron.execution.session')}</label
            >
            <div class="s-row-desc">{t('cron.execution.sessionHelp')}</div>
          </div>
          <div class="s-row-control">
            <SearchableDropdown
              id="cron-job-session"
              disabled={busy}
              value={values.session_id}
              options={sessionOptions}
              ariaLabel={t('cron.execution.session')}
              placeholder={sessionLoading
                ? t('common.loading')
                : t('cron.execution.selectSession')}
              searchPlaceholder={t('cron.execution.searchSessions')}
              emptyLabel={t('cron.execution.noSessions')}
              footerActionLabel={nextCursor
                ? t('cron.execution.moreSessions')
                : ''}
              onFooterAction={() => loadSessions(values.agent_id, nextCursor)}
              onValueChange={(value) =>
                editor.updateFormField('session_id', value)}
            />
          </div>
        </div>
        {#if sessionError}<Banner variant="error"
            >{t('cron.execution.loadSessionsError')} {sessionError}</Banner
          >{/if}
      {/if}
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="cron-working-project"
            >{t('cron.execution.project')}</label
          >
          <div class="s-row-desc">
            {existing
              ? t('cron.execution.sessionProjectHelp')
              : t('cron.execution.projectHelp')}
          </div>
        </div>
        <div class="s-row-control">
          {#if existing || address.projectId}
            <span data-testid="cron-fixed-project"
              >{existing && !session
                ? selectedLoading
                  ? t('common.loading')
                  : values.session_id
                    ? t('cron.execution.unavailableSession')
                    : t('cron.execution.selectSessionFirst')
                : fixedProject
                  ? projects.some(
                      (project) => project.project_id === fixedProject,
                    )
                    ? projectName(fixedProject)
                    : t('cron.execution.unavailableProject', {
                        project: fixedProject,
                      })
                  : t('cron.execution.workspace')}</span
            >
          {:else}
            <SearchableDropdown
              id="cron-working-project"
              disabled={busy}
              value={values.working_project_choice}
              options={projectOptions}
              ariaLabel={t('cron.execution.project')}
              placeholder={t('cron.execution.selectProject')}
              onValueChange={(value) =>
                editor.updateFormField('working_project_choice', value)}
            />
          {/if}
        </div>
      </div>
      {#if !existing && !address.projectId && !values.working_project_choice && !editor.isCreating}
        <Banner variant="warning">{t('cron.execution.legacyProject')}</Banner>
      {/if}
    </div>
  </div>
</section>

<details
  class="s-section cron-advanced"
  ontoggle={(event) => (advanced = event.currentTarget.open)}
>
  <summary>{t('cron.execution.advanced')}</summary>
  <p class="s-section__desc">{t('cron.execution.overridesHelp')}</p>
  <div class="s-section__body">
    <div class="s-group">
      {#if modelError}<Banner variant="error"
          >{t('cron.execution.loadModelsError')} {modelError}</Banner
        >{/if}
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="cron-run-model"
            >{t('agents.form.model')}</label
          >
        </div>
        <div class="s-row-control">
          <SearchableDropdown
            id="cron-run-model"
            disabled={busy}
            value={selectModelValue(values.model_override, modelOptions)}
            options={modelOptions}
            ariaLabel={t('agents.form.model')}
            searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
            emptyLabel={t('agents.form.modelSearchEmpty')}
            onValueChange={chooseModel}
            footerActionLabel={modelFilterFooterLabel({
              showAll: showAllModels,
              hiddenCount: allModelOptions.length - modelOptions.length,
            })}
            onFooterAction={() => (showAllModels = !showAllModels)}
            onOpenChange={modelCatalog.trackPickerOpen}
          />
        </div>
      </div>
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="cron-run-effort"
            >{t('agents.form.thinkingEffort')}</label
          >{#if reasoning?.supported === false}<div class="s-row-desc">
              {t('agents.form.thinkingEffortUnsupported')}
            </div>{/if}
        </div>
        <div class="s-row-control">
          <Dropdown
            id="cron-run-effort"
            value={values.thinking_effort_override}
            options={effortOptions}
            disabled={busy || reasoning?.supported === false}
            ariaLabel={t('agents.form.thinkingEffort')}
            onValueChange={(value) =>
              editor.updateFormField('thinking_effort_override', value)}
          />
        </div>
      </div>
    </div>
  </div>
</details>

<style>
  .cron-advanced > summary {
    color: var(--text-hi);
    cursor: pointer;
    font-weight: 600;
    padding-block: 8px;
  }
  .cron-advanced > summary:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }
</style>
