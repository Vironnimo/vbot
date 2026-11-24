<script module>
  const loadOverview = () => import('./OverviewTab.svelte');
  const loadJobs = () => import('./JobsTab.svelte');
  const loadTeam = () => import('./TeamTab.svelte');
  const loadSettings = () => import('./SettingsTab.svelte');
  const loadRunDetail = () => import('./JobRunDetail.svelte');
</script>

<script>
  import { untrack } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import { provideNavigation } from '@vbot/extension-sdk/lib/navigation.svelte.js';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import FormField from '@vbot/extension-sdk/ui/FormField.svelte';
  import InfoHint from '@vbot/extension-sdk/ui/InfoHint.svelte';
  import LazyContent from '@vbot/extension-sdk/ui/LazyContent.svelte';
  import Modal from '@vbot/extension-sdk/ui/Modal.svelte';
  import SearchableDropdown from '@vbot/extension-sdk/ui/SearchableDropdown.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
  } from '@vbot/extension-sdk/lib/modelSelection.js';
  import { createFactoryPageModel } from './pageModel.svelte.js';
  import { projectName } from './presentation.js';
  import './factoryPage.css';

  let { bridgeClient = null } = $props();
  const model = createFactoryPageModel(untrack(() => bridgeClient));
  provideNavigation({ registerLayer: model.client.registerLayer });

  let setupOpen = $state(false);
  let setupError = $state('');
  let showAllModels = $state(false);
  let setupForm = $state({
    leader_agent_id: '',
    leader_name: '',
    model: '',
    remit: '',
    request_id: '',
  });

  const projects = $derived(
    (model.catalog.projects || []).map((item) => ({
      value: item.id,
      label: item.name || item.id,
    })),
  );
  const currentProject = $derived(model.projectId || projects[0]?.value || '');
  const factoryRoute = (tab = '') =>
    `/factory/${encodeURIComponent(model.factoryId)}${tab ? '/' + tab : ''}`;

  const allModelOptions = $derived(
    buildModelSelectOptions({
      models: model.catalog.models ?? [],
      modelOnly: true,
      selectedModelValue: setupForm.model,
      emptyLabel: t('factory.team.inheritedModel'),
    }).filter((option) => option.value),
  );
  const modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, {
      showAll: showAllModels,
      selectedModelValue: setupForm.model,
    }),
  );
  const leaderAgentOptions = $derived.by(() => {
    const candidates = new SvelteMap(
      (model.catalog.agents ?? []).map((agent) => [
        agent.id,
        agent.name || agent.id,
      ]),
    );
    const pending =
      model.catalog.projects?.find((project) => project.id === currentProject)
        ?.pending_factory_agents ?? [];
    for (const agent of pending) {
      candidates.set(agent.id, agent.name || agent.id);
    }
    return [
      { value: '', label: t('factory.setup.newLeader') },
      ...[...candidates].map(([value, label]) => ({ value, label })),
    ];
  });

  async function chooseProject(value) {
    if (value === currentProject) return;
    const found = model.factories.find(
      (item) => item.project_id === value && item.state !== 'archived',
    );
    await model.navigate(
      found
        ? `/factory/${encodeURIComponent(found.id)}`
        : `/project/${encodeURIComponent(value)}`,
    );
  }

  async function toggleFactoryState() {
    const factory = model.factory;
    if (!factory) return;
    await model.mutate('factory.update', {
      id: factory.id,
      expected_revision: factory.revision,
      state: factory.state === 'active' ? 'paused' : 'active',
    });
  }

  function openSetup() {
    setupError = '';
    showAllModels = false;
    setupForm = {
      leader_agent_id: '',
      leader_name: '',
      model: '',
      remit: '',
      request_id: crypto.randomUUID(),
    };
    setupOpen = true;
  }

  async function submitSetup() {
    setupError = '';
    if (
      !currentProject ||
      (!setupForm.leader_agent_id &&
        (!setupForm.leader_name.trim() || !setupForm.model))
    ) {
      setupError = t('factory.setup.leaderRequired');
      return;
    }
    const result = await model.mutate('factory.create', {
      project_id: currentProject,
      remit: setupForm.remit,
      ...(setupForm.leader_agent_id
        ? { leader_agent_id: setupForm.leader_agent_id }
        : {
            leader_name: setupForm.leader_name.trim(),
            leader_model: setupForm.model,
            request_id: setupForm.request_id,
          }),
    });
    if (result?.factory) {
      setupOpen = false;
      await model.navigate(`/factory/${encodeURIComponent(result.factory.id)}`);
    }
  }
</script>

<svelte:head><title>{t('factory.title')}</title></svelte:head>

<main class="factory-page">
  <header class="factory-header">
    <div class="factory-brand">
      <h1>{t('factory.title')}</h1>
    </div>

    <div class="factory-project-picker">
      <Dropdown
        id="factory-project"
        ariaLabel={t('factory.selectProject')}
        placeholder={t('factory.selectProject')}
        options={projects}
        value={currentProject}
        onOpenChange={model.pickerOpen}
        onValueChange={chooseProject}
      />
    </div>

    {#if model.factory}
      <div class="factory-header-meta">
        <span class="factory-name">
          {model.factory.name && model.factory.name !== model.factory.project_id
            ? model.factory.name
            : projectName(model.catalog, model.factory.project_id)}
        </span>
        <span class="factory-badge">
          {model.factory.state === 'active'
            ? t('factory.state.active')
            : model.factory.state === 'paused'
              ? t('factory.state.paused')
              : t('factory.state.archived')}
        </span>
        <span class="factory-wip" role="status">
          {t('factory.runningCount', { count: model.running.length })}
        </span>
        {#if model.factory.state !== 'archived'}
          <Button
            variant={model.factory.state === 'active' ? 'danger' : 'secondary'}
            disabled={Boolean(model.pending)}
            onClick={toggleFactoryState}
          >
            {model.factory.state === 'active'
              ? t('factory.pauseTeam')
              : t('factory.resumeTeam')}
          </Button>
        {/if}
      </div>
    {/if}
  </header>

  <div class="factory-scrollport">
    {#if model.error}
      <Banner variant="error" role="alert">{model.error}</Banner>
    {/if}
    {#if model.refreshError}
      <Banner variant="error" role="status">
        {model.refreshError}
        <Button variant="tertiary" onClick={() => model.refresh({ full: true })}
          >{t('common.retry')}</Button
        >
      </Banner>
    {/if}

    {#if model.loading}
      <p class="factory-loading" role="status">{t('factory.loading')}</p>
    {:else if model.view === 'job-run'}
      <LazyContent load={loadRunDetail}>
        {#snippet children(Component)}
          <Component {model} onNavigate={model.navigate} />
        {/snippet}
      </LazyContent>
    {:else if !model.factory}
      <section class="factory-setup-empty">
        <p>
          {projects.length ? t('factory.noFactory') : t('factory.noProjects')}
        </p>
        {#if projects.length}
          <Button
            variant="primary"
            disabled={!currentProject}
            onClick={openSetup}>{t('factory.setup.button')}</Button
          >
        {/if}
      </section>
    {:else}
      <nav class="factory-tabs" aria-label={t('factory.title')}>
        <Button
          variant={model.view === 'overview' ? 'secondary' : 'tertiary'}
          aria-current={model.view === 'overview' ? 'page' : undefined}
          onClick={() => model.navigate(factoryRoute())}
          >{t('factory.tabs.overview')}</Button
        >
        <Button
          variant={model.view === 'jobs' ? 'secondary' : 'tertiary'}
          aria-current={model.view === 'jobs' ? 'page' : undefined}
          onClick={() => model.navigate(factoryRoute('jobs'))}
          >{t('factory.tabs.jobs')}</Button
        >
        <Button
          variant={model.view === 'team' ? 'secondary' : 'tertiary'}
          aria-current={model.view === 'team' ? 'page' : undefined}
          onClick={() => model.navigate(factoryRoute('team'))}
          >{t('factory.tabs.team')}</Button
        >
        <Button
          variant={model.view === 'settings' ? 'secondary' : 'tertiary'}
          aria-current={model.view === 'settings' ? 'page' : undefined}
          onClick={() => model.navigate(factoryRoute('settings'))}
          >{t('factory.tabs.settings')}</Button
        >
      </nav>

      {#if model.view === 'overview'}
        <LazyContent load={loadOverview}>
          {#snippet children(Component)}
            <Component {model} onNavigate={model.navigate} />
          {/snippet}
        </LazyContent>
      {:else if model.view === 'jobs'}
        <LazyContent load={loadJobs}>
          {#snippet children(Component)}
            <Component {model} />
          {/snippet}
        </LazyContent>
      {:else if model.view === 'team'}
        <LazyContent load={loadTeam}>
          {#snippet children(Component)}
            <Component {model} />
          {/snippet}
        </LazyContent>
      {:else if model.view === 'settings'}
        <LazyContent load={loadSettings}>
          {#snippet children(Component)}
            <Component {model} />
          {/snippet}
        </LazyContent>
      {/if}
    {/if}
  </div>
</main>

{#if setupOpen}
  <Modal
    title={t('factory.setup.title')}
    onClose={() => (setupOpen = false)}
    closeDisabled={Boolean(model.pending)}
  >
    {#snippet body()}
      <div class="modal-body factory-form">
        <FormField controlId="factory-setup-leader">
          {#snippet labelContent()}
            {t('factory.setup.leader')}
            <InfoHint text={t('factory.setup.leaderHelp')} />
          {/snippet}
          {#snippet children(field)}
            <Dropdown
              id={field.controlId}
              ariaLabelledby={field.labelId}
              value={setupForm.leader_agent_id}
              options={leaderAgentOptions}
              onOpenChange={model.pickerOpen}
              onValueChange={(value) => (setupForm.leader_agent_id = value)}
            />
          {/snippet}
        </FormField>

        {#if !setupForm.leader_agent_id}
          <label for="factory-setup-leader-name"
            >{t('factory.setup.agentName')}</label
          >
          <TextField
            id="factory-setup-leader-name"
            value={setupForm.leader_name}
            onInput={(value) => (setupForm.leader_name = value)}
          />
          <label for="factory-setup-model">{t('factory.setup.model')}</label>
          <SearchableDropdown
            id="factory-setup-model"
            ariaLabel={t('factory.setup.model')}
            value={setupForm.model}
            options={modelOptions}
            placeholder={t('factory.setup.selectModel')}
            searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
            emptyLabel={t('agents.form.modelSearchEmpty')}
            footerActionLabel={modelFilterFooterLabel({
              showAll: showAllModels,
              hiddenCount: allModelOptions.length - modelOptions.length,
            })}
            onFooterAction={() => (showAllModels = !showAllModels)}
            onOpenChange={model.pickerOpen}
            onValueChange={(value) => (setupForm.model = value)}
          />
        {/if}

        <FormField controlId="factory-setup-remit">
          {#snippet labelContent()}
            {t('factory.setup.remit')}
            <InfoHint text={t('factory.setup.remitHelp')} />
          {/snippet}
          <TextArea
            id="factory-setup-remit"
            rows={3}
            autoGrow
            value={setupForm.remit}
            onInput={(value) => (setupForm.remit = value)}
          />
        </FormField>

        {#if setupError || model.error}
          <Banner variant="error" role="alert"
            >{setupError || model.error}</Banner
          >
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={Boolean(model.pending)}
        onClick={() => (setupOpen = false)}>{t('common.cancel')}</Button
      >
      <Button
        variant="primary"
        loading={Boolean(model.pending)}
        onClick={submitSetup}>{t('factory.setup.button')}</Button
      >
    {/snippet}
  </Modal>
{/if}
