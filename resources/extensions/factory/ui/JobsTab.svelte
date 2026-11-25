<script>
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import ConfirmDialog from '@vbot/extension-sdk/ui/ConfirmDialog.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import Modal from '@vbot/extension-sdk/ui/Modal.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import Toggle from '@vbot/extension-sdk/ui/Toggle.svelte';
  import TriggerEditor from './TriggerEditor.svelte';
  import {
    copy,
    hasEnabledTrigger,
    memberLabel,
    timestamp,
  } from './presentation.js';

  let { model } = $props();

  let editorOpen = $state(false);
  let editingJob = $state(null);
  let form = $state({
    name: '',
    prompt: '',
    description: '',
    agent_member_id: '',
    enabled: true,
    triggers: [],
  });
  let formError = $state('');
  let deleteTarget = $state(null);
  let notice = $state('');

  const archived = $derived(model.factory?.state === 'archived');
  const activeMembers = $derived(
    (model.members || []).filter(
      (item) => item.state === 'active' && Boolean(item.profile_available),
    ),
  );
  const agentOptions = $derived([
    { value: '', label: t('factory.jobs.leaderDefault') },
    ...activeMembers.map((member) => ({
      value: member.id,
      label: memberLabel(model.members, member.id),
    })),
  ]);

  function openCreate() {
    editingJob = null;
    formError = '';
    form = {
      name: '',
      prompt: '',
      description: '',
      agent_member_id: '',
      enabled: true,
      triggers: [],
    };
    editorOpen = true;
  }

  function openEdit(job) {
    editingJob = job;
    formError = '';
    form = {
      name: job.name ?? '',
      prompt: job.prompt ?? '',
      description: job.description ?? '',
      agent_member_id: job.agent_member_id ?? '',
      enabled: job.enabled !== false,
      triggers: copy(job.triggers ?? []),
    };
    editorOpen = true;
  }

  function addTrigger(type) {
    form.triggers = [
      ...form.triggers,
      {
        id: crypto.randomUUID(),
        type,
        title: '',
        schedule: type === 'schedule' ? '0 9 * * 1-5' : '',
        enabled: true,
      },
    ];
  }

  function removeTrigger(index) {
    form.triggers = form.triggers.filter((_, idx) => idx !== index);
  }

  async function saveJob() {
    formError = '';
    if (!form.name.trim() || !form.prompt.trim()) {
      formError = t('factory.jobs.required');
      return;
    }
    const triggers = form.triggers.map((item) => ({
      id: item.id,
      type: item.type,
      title: item.title ?? '',
      schedule: item.type === 'schedule' ? (item.schedule ?? '') : '',
      enabled: Boolean(item.enabled),
    }));
    const payload = {
      name: form.name.trim(),
      prompt: form.prompt.trim(),
      description: form.description ?? '',
      agent_member_id: form.agent_member_id ?? '',
      enabled: Boolean(form.enabled),
      triggers,
    };
    const result = editingJob
      ? await model.mutate('job.update', {
          job_id: editingJob.id,
          expected_revision: editingJob.revision,
          ...payload,
        })
      : await model.mutate('job.create', {
          factory_id: model.factory.id,
          ...payload,
        });
    if (result) {
      editorOpen = false;
    }
  }

  async function toggleJobEnabled(job, enabled) {
    await model.mutate('job.update', {
      job_id: job.id,
      expected_revision: job.revision,
      enabled,
    });
  }

  async function triggerJob(job) {
    notice = '';
    const result = await model.mutate('job.trigger', { job_id: job.id });
    if (result?.run) {
      notice = t('factory.jobs.triggered');
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    const result = await model.mutate('job.delete', {
      job_id: deleteTarget.id,
    });
    if (result) {
      deleteTarget = null;
    }
  }

  function nextFireOf(job) {
    return (job.triggers || [])
      .map((item) => item.next_fire)
      .filter(Boolean)
      .sort()[0];
  }
</script>

<section class="factory-jobs" aria-label={t('factory.tabs.jobs')}>
  <div class="factory-toolbar">
    <Button variant="primary" disabled={archived} onClick={openCreate}
      >{t('factory.jobs.new')}</Button
    >
  </div>

  {#if notice}
    <Banner variant="info" role="status">{notice}</Banner>
  {/if}

  {#if !model.jobs.length}
    <p class="factory-empty-state">{t('factory.jobs.empty')}</p>
  {:else}
    <ul class="factory-job-list">
      {#each model.jobs as job (job.id)}
        {@const automatic = hasEnabledTrigger(job)}
        {@const nextFire = nextFireOf(job)}
        <li class="factory-job-card" data-job-id={job.id}>
          <div class="factory-job-main">
            <div class="factory-job-title-row">
              <strong>{job.name}</strong>
              <span
                class="factory-badge {automatic
                  ? 'factory-badge--auto'
                  : 'factory-badge--manual'}"
              >
                {automatic
                  ? t('factory.jobs.automatic')
                  : t('factory.jobs.onDemand')}
              </span>
            </div>
            {#if job.description}
              <p class="factory-job-desc">{job.description}</p>
            {/if}
            <div class="factory-job-meta">
              <span
                >{t('factory.jobs.assignedTo', {
                  name: memberLabel(model.members, job.agent_member_id),
                })}</span
              >
              {#if nextFire}
                <span
                  >{t('factory.jobs.nextFire', {
                    time: timestamp(nextFire),
                  })}</span
                >
              {/if}
            </div>
          </div>

          <div class="factory-job-actions">
            <Toggle
              checked={job.enabled}
              disabled={archived || Boolean(model.pending)}
              ariaLabel={`${t('factory.jobs.enabled')} ${job.name}`}
              onChange={(value) => toggleJobEnabled(job, value)}
            />
            <Button
              variant="primary"
              disabled={archived || Boolean(model.pending)}
              onClick={() => triggerJob(job)}
              >{t('factory.jobs.triggerNow')}</Button
            >
            <Button
              variant="secondary"
              disabled={archived}
              onClick={() => openEdit(job)}>{t('factory.jobs.edit')}</Button
            >
            <Button
              variant="tertiary"
              disabled={archived || Boolean(model.pending)}
              onClick={() => (deleteTarget = job)}
              >{t('factory.jobs.delete')}</Button
            >
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</section>

{#if editorOpen}
  <Modal
    title={editingJob
      ? t('factory.jobs.editTitle')
      : t('factory.jobs.createTitle')}
    onClose={() => (editorOpen = false)}
    closeDisabled={Boolean(model.pending)}
  >
    {#snippet body()}
      <div class="modal-body factory-form">
        <label for="job-name">{t('factory.jobs.name')}</label>
        <TextField
          id="job-name"
          maxlength="200"
          value={form.name}
          onInput={(value) => (form.name = value)}
        />

        <label for="job-prompt">{t('factory.jobs.prompt')}</label>
        <TextArea
          id="job-prompt"
          rows={4}
          autoGrow
          value={form.prompt}
          onInput={(value) => (form.prompt = value)}
        />

        <label for="job-description">{t('factory.jobs.description')}</label>
        <TextArea
          id="job-description"
          rows={2}
          autoGrow
          value={form.description}
          onInput={(value) => (form.description = value)}
        />

        <label for="job-agent">{t('factory.jobs.agent')}</label>
        <Dropdown
          id="job-agent"
          ariaLabel={t('factory.jobs.agent')}
          options={agentOptions}
          value={form.agent_member_id}
          onOpenChange={model.pickerOpen}
          onValueChange={(value) => (form.agent_member_id = value)}
        />

        <label class="factory-check">
          {t('factory.jobs.enabled')}
          <Toggle
            ariaLabel={t('factory.jobs.enabled')}
            checked={form.enabled}
            onChange={(value) => (form.enabled = value)}
          />
        </label>

        <section class="factory-triggers-section">
          <header class="factory-triggers-header">
            <h3>{t('factory.jobs.triggers')}</h3>
            <div class="factory-triggers-add">
              <Button
                variant="secondary"
                disabled={model.catalog.triggers_available === false}
                onClick={() => addTrigger('schedule')}
                >{t('factory.jobs.addSchedule')}</Button
              >
              <Button
                variant="secondary"
                disabled={model.catalog.triggers_available === false}
                onClick={() => addTrigger('webhook')}
                >{t('factory.jobs.addWebhook')}</Button
              >
            </div>
          </header>
          {#if model.catalog.triggers_available === false}
            <Banner variant="warn"
              >{t('factory.error.triggersUnavailable')}</Banner
            >
          {/if}
          {#each form.triggers as item, index (item.id)}
            <TriggerEditor
              bind:trigger={form.triggers[index]}
              jobId={editingJob?.id || ''}
              jobEnabled={form.enabled}
              factoryId={model.factory.id}
              saved={editingJob?.triggers?.find((row) => row.id === item.id) ??
                null}
              {model}
              onRemove={() => removeTrigger(index)}
            />
          {/each}
        </section>

        {#if formError || model.error}
          <Banner variant="error" role="alert"
            >{formError || model.error}</Banner
          >
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={Boolean(model.pending)}
        onClick={() => (editorOpen = false)}>{t('common.cancel')}</Button
      >
      <Button
        variant="primary"
        loading={Boolean(model.pending)}
        onClick={saveJob}>{t('factory.jobs.save')}</Button
      >
    {/snippet}
  </Modal>
{/if}

{#if deleteTarget}
  <ConfirmDialog
    title={t('factory.jobs.deleteConfirmTitle')}
    body={t('factory.jobs.deleteConfirmBody', { name: deleteTarget.name })}
    confirmLabel={t('factory.jobs.delete')}
    busy={Boolean(model.pending)}
    error={model.error}
    onConfirm={confirmDelete}
    onCancel={() => (deleteTarget = null)}
  />
{/if}

<style>
  .factory-jobs {
    display: grid;
    gap: 16px;
  }
  .factory-toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .factory-empty-state {
    color: var(--text-med);
    margin: 0;
  }
  .factory-job-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 12px;
  }
  .factory-job-card {
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    padding: 16px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }
  .factory-job-main {
    display: grid;
    gap: 6px;
    min-width: 0;
    flex: 1 1 300px;
  }
  .factory-job-title-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .factory-job-desc {
    margin: 0;
    color: var(--text-med);
    font-size: 13px;
  }
  .factory-job-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    color: var(--text-med);
    font-size: 12px;
  }
  .factory-job-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .factory-triggers-section {
    display: grid;
    gap: 10px;
    margin-top: 4px;
  }
  .factory-triggers-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .factory-triggers-header h3 {
    margin: 0;
    font-size: 14px;
  }
  .factory-triggers-add {
    display: flex;
    gap: 8px;
  }
</style>
