<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { parseAgentAddress } from '$lib/agentAddress.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import {
    listTaskInputs,
    createTaskInput,
    updateTaskInput,
    deleteTaskInput,
    getTaskInputSecret,
    rotateTaskInputSecret,
  } from '$lib/api.js';
  import CronExecutionSettings from './CronExecutionSettings.svelte';
  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Banner from '../ui/Banner.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import { webhookReachText } from './presentation.js';

  let {
    navigation,
    agents = [],
    projects = [],
    agentOptions = [],
    newDraft = $bindable(null),
    subscribeAgentRenames = null,
    refreshToken = 0,
    serverUnavailable = false,
  } = $props();
  function form(row = null) {
    return {
      schedule_type: 'input',
      name: row?.name ?? '',
      agent_id: row?.agent_id ?? '',
      enabled: row?.enabled ?? true,
      session_mode: 'new',
      session_id: '',
      working_project_choice: row?.working_project_id
        ? `project:${row.working_project_id}`
        : 'workspace',
      model_override: row?.run_overrides?.model ?? '',
      thinking_effort_override: row?.run_overrides?.thinking_effort_default
        ? 'default'
        : (row?.run_overrides?.thinking_effort ?? ''),
    };
  }
  let rows = $state([]);
  let ready = $state(false);
  let creating = $state(false);
  let selectedId = $state('');
  let values = $state(form());
  let baseline = $state(JSON.stringify(form()));
  let saving = $state(false);
  let error = $state('');
  let secret = $state('');
  let secretBusy = $state(false);
  let confirmation = $state('');
  let destroyed = false;
  let revision = 0;
  let selected = $derived(rows.find((row) => row.id === selectedId));
  let dirty = $derived(JSON.stringify(values) !== baseline);
  const context = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => ({ id: selectedId, values }),
    hasChanges: () => !creating && Boolean(selectedId) && dirty,
    save: () => persist(),
  });
  const unregister = context.register(autosave.participant);
  onDestroy(() => {
    destroyed = true;
    unregister();
    autosave.cancelPendingTimer();
  });
  onMount(() => {
    void load();
    return subscribeAgentRenames?.((oldId, newId) => {
      rows = rows.map((row) =>
        row.agent_id === oldId ? { ...row, agent_id: newId } : row,
      );
      if (values.agent_id === oldId) values = { ...values, agent_id: newId };
      const saved = JSON.parse(baseline);
      if (saved.agent_id === oldId)
        baseline = JSON.stringify({ ...saved, agent_id: newId });
      if (newDraft?.schedule_type === 'input' && newDraft.agent_id === oldId)
        newDraft = { ...newDraft, agent_id: newId };
    });
  });
  let lastRefresh = untrack(() => refreshToken);
  $effect(() => {
    const token = refreshToken;
    if (token !== lastRefresh) {
      lastRefresh = token;
      void load();
    }
  });
  $effect(() => {
    if (creating || !selectedId || !dirty || saving) return;
    autosave.scheduleRun();
    return autosave.cancelPendingTimer;
  });
  $effect(() => {
    if (!creating) return;
    const draft = dirty ? $state.snapshot(values) : null;
    untrack(() => {
      newDraft = draft;
    });
  });
  $effect(() => {
    const place = navigation.place;
    void ready;
    if (!ready) return;
    untrack(() => {
      const target = place[1] ?? '';
      if (target === 'new') {
        if (!creating) startNewNow();
      } else {
        const row = rows.find((item) => item.id === target) ?? rows[0];
        if (row && (creating || row.id !== selectedId)) show(row);
        else if (!row) {
          creating = false;
          selectedId = '';
        }
        if (row && target !== row.id) navigation.replace(['inputs', row.id]);
      }
    });
  });

  const editor = {
    get formValues() {
      return values;
    },
    get isCreating() {
      return creating;
    },
    get submittingForm() {
      return saving;
    },
    updateFormField(field, value) {
      values = { ...values, [field]: value };
      if (field === 'agent_id' && parseAgentAddress(value).projectId)
        values.working_project_choice = 'workspace';
    },
  };
  async function load() {
    const current = ++revision;
    try {
      const response = await listTaskInputs();
      if (destroyed || current !== revision) return;
      rows = response.inputs;
      ready = true;
      if (!creating && !dirty && selectedId) {
        const row = rows.find((item) => item.id === selectedId);
        if (row) show(row);
      }
    } catch (caught) {
      if (!destroyed && current === revision) error = caught.message;
    }
  }
  function show(row) {
    selectedId = row.id;
    creating = false;
    values = form(row);
    baseline = JSON.stringify(values);
    secret = '';
    error = '';
  }
  function startNewNow() {
    if (newDraft && newDraft.schedule_type !== 'input') {
      confirmation = 'draft';
      return;
    }
    creating = true;
    selectedId = '';
    values = newDraft ? { ...newDraft } : form();
    baseline = JSON.stringify(form());
    secret = '';
    error = '';
  }
  function startNew() {
    void context.requestTransition(() =>
      navigation.navigate(['inputs', 'new']),
    );
  }
  function payload(snapshot) {
    const address = parseAgentAddress(snapshot.agent_id);
    const overrides = {};
    if (snapshot.model_override) overrides.model = snapshot.model_override;
    if (snapshot.thinking_effort_override === 'default')
      overrides.thinking_effort_default = true;
    else if (snapshot.thinking_effort_override)
      overrides.thinking_effort = snapshot.thinking_effort_override;
    return {
      name: snapshot.name.trim(),
      agent_id: snapshot.agent_id,
      enabled: snapshot.enabled,
      working_project_id:
        address.projectId ??
        (snapshot.working_project_choice === 'workspace'
          ? null
          : snapshot.working_project_choice.slice(8)),
      run_overrides: overrides,
    };
  }
  async function persist() {
    const snapshot = $state.snapshot(values);
    if (
      !snapshot.name.trim() ||
      !snapshot.agent_id ||
      !snapshot.working_project_choice
    ) {
      error = t('inputs.required');
      return false;
    }
    const wasCreating = creating;
    const identity = selectedId;
    saving = true;
    error = '';
    try {
      const response = wasCreating
        ? await createTaskInput(payload(snapshot))
        : await updateTaskInput({ id: identity, ...payload(snapshot) });
      if (destroyed) return true;
      const row = response.input;
      rows = [...rows.filter((item) => item.id !== row.id), row];
      baseline = JSON.stringify(snapshot);
      if (JSON.stringify(values) === JSON.stringify(snapshot))
        values = form(row);
      baseline = JSON.stringify(form(row));
      if (wasCreating) {
        newDraft = null;
        creating = false;
        selectedId = row.id;
        navigation.replace(['inputs', row.id]);
      }
      return true;
    } catch (caught) {
      if (!destroyed) error = caught.message;
      return false;
    } finally {
      if (!destroyed) saving = false;
    }
  }
  async function showSecret() {
    if (secret) {
      secret = '';
      return;
    }
    secretBusy = true;
    const id = selectedId;
    try {
      const response = await getTaskInputSecret(id);
      if (!destroyed && id === selectedId) secret = response.secret;
    } catch (caught) {
      if (!destroyed) error = caught.message;
    } finally {
      if (!destroyed) secretBusy = false;
    }
  }
  async function applyConfirmation() {
    const action = confirmation;
    if (action === 'draft') {
      confirmation = '';
      newDraft = null;
      startNewNow();
      return;
    }
    if (action === 'cancel') {
      confirmation = '';
      newDraft = null;
      creating = false;
      navigation.navigate(['inputs']);
      return;
    }
    secretBusy = true;
    error = '';
    try {
      await context.requestTransition(async () => {
        const id = selectedId;
        try {
          if (action === 'delete') {
            await deleteTaskInput(id);
            rows = rows.filter((row) => row.id !== id);
            selectedId = '';
            navigation.navigate(['inputs']);
          } else {
            const response = await rotateTaskInputSecret(id);
            if (!destroyed && id === selectedId) secret = response.secret;
          }
          confirmation = '';
        } catch (caught) {
          if (!destroyed) error = caught.message;
        }
      });
    } finally {
      if (!destroyed) secretBusy = false;
    }
  }
  let requestExample = $derived(
    selected
      ? `curl --request POST '${selected.url ?? selected.path}' --header 'Authorization: Bearer ${secret || '<secret>'}' --header 'Content-Type: application/json' --header 'Idempotency-Key: task-001' --data '${JSON.stringify({ prompt: t('inputs.samplePrompt') })}'`
      : '',
  );
  let resultExample = $derived(
    `curl '<status_url>' --header 'Authorization: Bearer ${secret || '<secret>'}'`,
  );
  let followupExample = $derived(
    JSON.stringify(
      {
        prompt: t('inputs.sampleFollowup'),
        session_id: '<session_id>',
        run_overrides: { model: '<provider/model>', thinking_effort: 'high' },
        wait: true,
      },
      null,
      2,
    ),
  );
</script>

<div
  class="cron-layout"
  role="tabpanel"
  id="automation-categories-panel-inputs"
  aria-labelledby="automation-categories-tab-inputs"
>
  <aside class="cron-list-pane secondary-pane" aria-label={t('inputs.title')}>
    <div class="pane-header secondary-pane__header">
      <span class="secondary-pane__title">{t('inputs.title')}</span>
      <Button variant="tertiary" onClick={startNew} disabled={serverUnavailable}
        >{t('common.create')}</Button
      >
    </div>
    <div class="secondary-pane__scroll secondary-list">
      {#each rows as row (row.id)}
        <button
          type="button"
          class="secondary-list__item task-input-row"
          class:active={row.id === selectedId}
          onclick={() =>
            context.requestTransition(() =>
              navigation.navigate(['inputs', row.id]),
            )}
        >
          <span>{row.name}</span><span class="task-input-agent"
            >{row.agent_id}</span
          >
        </button>
      {/each}
    </div>
  </aside>
  <div class="cron-detail-pane">
    {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
    {#if creating || selected}
      <form
        class="cron-detail-scroll page-scroll"
        onsubmit={(event) => {
          event.preventDefault();
          if (creating) void persist();
          else void autosave.participant.runSave('manual');
        }}
      >
        <header class="s-section__head">
          <h2>{creating ? t('inputs.create') : selected.name}</h2>
        </header>
        <p class="s-section__desc">{t('inputs.description')}</p>
        <section class="s-section">
          <div class="s-group">
            <div class="s-row">
              <label class="s-row-label" for="task-input-name"
                >{t('inputs.name')}</label
              >
              <div class="s-row-control">
                <TextField
                  id="task-input-name"
                  value={values.name}
                  onInput={(value) => editor.updateFormField('name', value)}
                />
              </div>
            </div>
            <div class="s-row">
              <span class="s-row-label">{t('inputs.enabled')}</span><Toggle
                checked={values.enabled}
                ariaLabel={t('inputs.enabled')}
                onChange={(value) => editor.updateFormField('enabled', value)}
              />
            </div>
          </div>
        </section>
        <CronExecutionSettings
          {editor}
          {agents}
          {projects}
          {agentOptions}
          taskInput
        />
        <p class="s-section__desc">{t('inputs.overrides')}</p>
        {#if selected && !creating}
          <section class="s-section">
            <h3>{t('cron.webhook.url')}</h3>
            <p>{webhookReachText(selected.reach)}</p>
            <div class="input-copy">
              <code>{selected.url ?? selected.path}</code><CopyButton
                text={selected.url ?? selected.path}
                label={t('cron.webhook.copyUrl')}
              />
            </div>
            <h3>{t('cron.webhook.secret')}</h3>
            <div class="input-copy">
              <code>{secret || '••••••••••••••••'}</code>
              <Button
                variant="secondary"
                onClick={showSecret}
                disabled={secretBusy}
                >{secret
                  ? t('cron.webhook.hideSecret')
                  : t('cron.webhook.showSecret')}</Button
              >
              {#if secret}<CopyButton
                  text={secret}
                  label={t('cron.webhook.copySecret')}
                />{/if}
              <Button
                variant="secondary"
                onClick={() => (confirmation = 'rotate')}
                >{t('cron.webhook.rotate')}</Button
              >
            </div>
          </section>
          <section class="s-section">
            <h3>{t('inputs.request')}</h3>
            <p>{t('inputs.requestHelp')}</p>
            <pre>{requestExample}</pre>
            <CopyButton text={requestExample} label={t('common.copy')} />
          </section>
          <section class="s-section">
            <h3>{t('inputs.result')}</h3>
            <p>{t('inputs.resultHelp')}</p>
            <pre>{resultExample}</pre>
            <CopyButton text={resultExample} label={t('common.copy')} />
          </section>
          <section class="s-section">
            <h3>{t('inputs.followup')}</h3>
            <p>{t('inputs.followupHelp')}</p>
            <pre>{followupExample}</pre>
            <CopyButton text={followupExample} label={t('common.copy')} />
          </section>
        {/if}
        <div class="cron-detail-footer">
          {#if creating}
            <Button
              variant="secondary"
              onClick={() => {
                if (dirty) confirmation = 'cancel';
                else {
                  newDraft = null;
                  creating = false;
                  navigation.navigate(['inputs']);
                }
              }}>{t('common.cancel')}</Button
            >
            <Button
              variant="primary"
              type="submit"
              disabled={saving || serverUnavailable}
              >{t('common.create')}</Button
            >
          {:else}
            <Button variant="danger" onClick={() => (confirmation = 'delete')}
              >{t('common.delete')}</Button
            >
            <SaveStatus
              {saving}
              pending={autosave.participant.hasChanges()}
              onClick={() => autosave.participant.runSave('manual')}
            />
          {/if}
        </div>
      </form>
    {:else}
      <EmptyState
        title={t('inputs.empty')}
        description={t('inputs.description')}
      />
    {/if}
  </div>
</div>
{#if confirmation}
  <ConfirmDialog
    title={confirmation === 'delete'
      ? t('inputs.deleteTitle', { name: selected?.name ?? '' })
      : confirmation === 'rotate'
        ? t('cron.webhook.rotateConfirmTitle')
        : t('inputs.replaceDraftTitle')}
    body={confirmation === 'delete'
      ? t('inputs.deleteBody')
      : confirmation === 'rotate'
        ? t('cron.webhook.rotateConfirm')
        : t('inputs.replaceDraftBody')}
    confirmLabel={confirmation === 'delete'
      ? t('common.delete')
      : confirmation === 'rotate'
        ? t('cron.webhook.rotateConfirmLabel')
        : t('common.discard')}
    busy={secretBusy}
    {error}
    onConfirm={applyConfirmation}
    onCancel={() => (confirmation = '')}
  />
{/if}

<style>
  .task-input-row {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    width: 100%;
    padding: 10px 12px;
    text-align: left;
  }
  .task-input-agent {
    font-size: 12px;
    color: var(--text-med);
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    padding: 12px;
    background: var(--surface-1);
    border-radius: 6px;
  }
  .input-copy {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    overflow-wrap: anywhere;
  }
</style>
