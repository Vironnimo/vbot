<script>
  import { SvelteMap } from 'svelte/reactivity';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import ConfirmDialog from '@vbot/extension-sdk/ui/ConfirmDialog.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import Modal from '@vbot/extension-sdk/ui/Modal.svelte';
  import SearchableDropdown from '@vbot/extension-sdk/ui/SearchableDropdown.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import Toggle from '@vbot/extension-sdk/ui/Toggle.svelte';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
  } from '@vbot/extension-sdk/lib/modelSelection.js';
  import RecordEditor from './RecordEditor.svelte';

  let { model } = $props();
  let editor = $state(null);
  let editingMemberId = $state('');
  let retireTarget = $state(null);
  let addOpen = $state(false);
  let showAllModels = $state(false);
  let formError = $state('');
  let form = $state({
    newAgent: false,
    agent_id: '',
    name: '',
    model: '',
    role: '',
    instructions: '',
    request_id: '',
  });

  const archived = $derived(model.factory?.state === 'archived');
  const activeMembers = $derived(
    (model.members || []).filter(
      (item) => item.state === 'active' && Boolean(item.profile_available),
    ),
  );
  const agentName = (member) =>
    member.agent_name || member.name || member.agent_id;

  const allModelOptions = $derived(
    buildModelSelectOptions({
      models: model.catalog.models ?? [],
      modelOnly: true,
      selectedModelValue: form.model,
      emptyLabel: t('factory.team.inheritedModel'),
    }),
  );
  const modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, {
      showAll: showAllModels,
      selectedModelValue: form.model,
    }),
  );

  const agentOptions = $derived.by(() => {
    const candidates = new SvelteMap(
      (model.catalog.agents ?? []).map((agent) => [
        agent.id,
        agent.name || agent.id,
      ]),
    );
    for (const member of model.members || []) {
      if (
        member.profile_available &&
        member.created_by_factory &&
        member.state === 'retired'
      ) {
        candidates.set(
          member.agent_id,
          member.agent_name || member.name || member.agent_id,
        );
      }
    }
    for (const member of activeMembers) candidates.delete(member.agent_id);
    return [...candidates].map(([value, label]) => ({ value, label }));
  });

  function openAdd() {
    formError = '';
    showAllModels = false;
    form = {
      newAgent: false,
      agent_id: '',
      name: '',
      model: '',
      role: '',
      instructions: '',
      request_id: crypto.randomUUID(),
    };
    addOpen = true;
  }

  async function submitAdd() {
    formError = '';
    if (form.newAgent ? !form.name.trim() : !form.agent_id) {
      formError = t('factory.team.required');
      return;
    }
    const operation = form.newAgent ? 'member.create' : 'member.add';
    const result = await model.mutate(operation, {
      factory_id: model.factory.id,
      role: form.role,
      instructions: form.instructions,
      ...(form.newAgent
        ? {
            name: form.name.trim(),
            ...(form.model ? { model: form.model } : {}),
            request_id: form.request_id,
          }
        : { agent_id: form.agent_id }),
    });
    if (result) addOpen = false;
  }

  async function toggleEdit(memberId) {
    if (editor && !(await editor.flush())) return;
    editingMemberId = editingMemberId === memberId ? '' : memberId;
  }

  async function confirmRetireMember() {
    const member = retireTarget;
    retireTarget = null;
    if (!member) return;
    if (editor && !(await editor.flush())) return;
    await model.mutate('member.retire', {
      id: member.id,
      expected_revision: member.revision,
    });
  }

  async function makeLeader(member) {
    if (editor && !(await editor.flush())) return;
    await model.mutate('factory.update', {
      id: model.factory.id,
      expected_revision: model.factory.revision,
      leader_member_id: member.id,
    });
  }
</script>

<section class="factory-team" aria-label={t('factory.tabs.team')}>
  <div class="factory-toolbar">
    <Button variant="primary" disabled={archived} onClick={openAdd}
      >{t('factory.team.addMember')}</Button
    >
  </div>

  {#if model.factory.leader_available === false && !archived}
    <Banner variant="warn">{t('factory.team.replaceLeader')}</Banner>
  {/if}

  <div class="factory-members">
    {#each model.members as member (member.id)}
      <section class="factory-member-card">
        <div class="factory-member-header">
          <div>
            <h2>
              {agentName(member)}
              {#if member.id === model.factory.leader_member_id}
                <span class="factory-badge factory-badge--auto"
                  >{t('factory.setup.leader')}</span
                >
              {:else if member.role && member.role !== agentName(member)}
                <span class="factory-subtle">· {member.role}</span>
              {/if}
            </h2>
            <p class="factory-subtle">
              {member.state === 'active'
                ? t('factory.state.active')
                : t('factory.team.retired')}
              ·
              {member.created_by_factory
                ? t('factory.team.createdAgent')
                : t('factory.team.linkedAgent')}
            </p>
            {#if !member.profile_available}
              <p class="factory-subtle">
                {t('factory.chat.profileUnavailable')}
              </p>
            {/if}
          </div>
          <div class="factory-member-actions">
            <Button
              variant="secondary"
              disabled={!member.profile_available || !member.agent_proof}
              onClick={() =>
                model.openAgentProfile(
                  member.agent_id,
                  member.agent_proof,
                  member.factory_id,
                )}>{t('factory.chat.editAgent')}</Button
            >
            {#if model.factory.leader_available === false && !archived && member.state === 'active' && member.profile_available}
              <Button
                variant="secondary"
                disabled={Boolean(model.pending)}
                onClick={() => makeLeader(member)}
                >{t('factory.team.makeLeader')}</Button
              >
            {/if}
            <Button
              variant="tertiary"
              disabled={archived}
              onClick={() => toggleEdit(member.id)}
              >{t('factory.team.editMembership')}</Button
            >
            {#if member.state === 'active' && member.id !== model.factory.leader_member_id && !archived}
              <Button
                variant="tertiary"
                disabled={Boolean(model.pending)}
                onClick={() => {
                  retireTarget = member;
                }}>{t('factory.team.retire')}</Button
              >
            {/if}
          </div>
        </div>

        {#if editingMemberId === member.id}
          {#key member.id}
            <RecordEditor
              bind:this={editor}
              kind="member"
              record={member}
              {model}
            />
          {/key}
        {:else if member.instructions}
          <p class="factory-member-instructions">{member.instructions}</p>
        {/if}
      </section>
    {/each}
  </div>
</section>

{#if addOpen}
  <Modal
    title={t('factory.team.addMember')}
    onClose={() => (addOpen = false)}
    closeDisabled={Boolean(model.pending)}
  >
    {#snippet body()}
      <div class="modal-body factory-form">
        <label class="factory-check">
          {t('factory.team.newAgent')}
          <Toggle
            checked={form.newAgent}
            ariaLabel={t('factory.team.newAgent')}
            onChange={(value) => {
              form.newAgent = value;
              form.agent_id = '';
            }}
          />
        </label>

        {#if form.newAgent}
          <label for="member-new-name">{t('factory.setup.agentName')}</label>
          <TextField
            id="member-new-name"
            value={form.name}
            onInput={(value) => (form.name = value)}
          />
          <label for="member-new-model">{t('factory.setup.model')}</label>
          <SearchableDropdown
            id="member-new-model"
            ariaLabel={t('factory.setup.model')}
            value={form.model}
            options={modelOptions}
            placeholder={t('factory.team.inheritedModel')}
            searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
            emptyLabel={t('agents.form.modelSearchEmpty')}
            footerActionLabel={modelFilterFooterLabel({
              showAll: showAllModels,
              hiddenCount: allModelOptions.length - modelOptions.length,
            })}
            onFooterAction={() => (showAllModels = !showAllModels)}
            onOpenChange={model.pickerOpen}
            onValueChange={(value) => (form.model = value)}
          />
        {:else}
          <label for="member-existing-agent"
            >{t('factory.team.existingAgent')}</label
          >
          <Dropdown
            id="member-existing-agent"
            ariaLabel={t('factory.team.existingAgent')}
            options={agentOptions}
            value={form.agent_id}
            onOpenChange={model.pickerOpen}
            onValueChange={(value) => (form.agent_id = value)}
          />
        {/if}

        <label for="member-role">{t('factory.team.role')}</label>
        <TextField
          id="member-role"
          value={form.role}
          onInput={(value) => (form.role = value)}
        />

        <label for="member-instructions">{t('factory.team.instructions')}</label
        >
        <TextArea
          id="member-instructions"
          rows={3}
          autoGrow
          value={form.instructions}
          onInput={(value) => (form.instructions = value)}
        />

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
        onClick={() => (addOpen = false)}>{t('common.cancel')}</Button
      >
      <Button
        variant="primary"
        loading={Boolean(model.pending)}
        onClick={submitAdd}>{t('factory.team.addMember')}</Button
      >
    {/snippet}
  </Modal>
{/if}

{#if retireTarget}
  <ConfirmDialog
    title={t('factory.team.retireTitle')}
    body={t('factory.team.retireConfirm', {
      name: agentName(retireTarget),
    })}
    confirmLabel={t('factory.team.retire')}
    danger={true}
    onConfirm={confirmRetireMember}
    onCancel={() => {
      retireTarget = null;
    }}
  />
{/if}

<style>
  .factory-team {
    display: grid;
    gap: 16px;
  }
  .factory-toolbar {
    display: flex;
    gap: 12px;
  }
  .factory-members {
    display: grid;
    gap: 12px;
  }
  .factory-member-card {
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    padding: 16px;
    display: grid;
    gap: 12px;
  }
  .factory-member-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .factory-member-header h2 {
    margin: 0 0 4px;
    font-size: 16px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .factory-member-header p {
    margin: 0;
  }
  .factory-member-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .factory-member-instructions {
    margin: 0;
    color: var(--text-med);
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
  }
</style>
