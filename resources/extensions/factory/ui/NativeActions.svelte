<script>
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Checkbox from '@vbot/extension-sdk/ui/Checkbox.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Modal from '@vbot/extension-sdk/ui/Modal.svelte';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import Toggle from '@vbot/extension-sdk/ui/Toggle.svelte';
  import { isChatModel } from '@vbot/extension-sdk/lib/modelSelection.js';
  import LimitsEditor from './LimitsEditor.svelte';
  import { normalizeLimits, limitsProblem } from './limits.js';
  let { model, native, currentProject, onNavigate } = $props();
  let modal = $state(''),
    form = $state({}),
    formError = $state('');
  const task = $derived(native.detail?.task);
  const topic = $derived(native.topic?.topic);
  const memberName = (id) =>
    native.members.find((item) => item.id === id)?.role ||
    native.members.find((item) => item.id === id)?.agent_id ||
    id ||
    t('factory.native.unassigned');
  const active = $derived(
    native.members.filter((item) => item.state === 'active'),
  );
  const memberOptions = $derived([
    { value: '', label: t('factory.native.unassigned') },
    ...active.map((item) => ({
      value: item.id,
      label: item.role || item.agent_id,
    })),
  ]);
  const chatModels = $derived((model.catalog.models || []).filter(isChatModel));
  const agents = $derived(
    (model.catalog.agents || []).map((item) => ({
      value: item.id,
      label: item.name || item.id,
    })),
  );
  export function open(kind, values = {}) {
    modal = kind;
    formError = '';
    form = {
      request_id: crypto.randomUUID(),
      agent_id: '',
      name: '',
      model: '',
      role: '',
      instructions: '',
      leader_agent_id: '',
      leader_name: '',
      title: '',
      objective: '',
      conditions: '',
      assignee_id: '',
      parent_id: '',
      dependencies: [],
      priority: 0,
      reviewer_id: '',
      user_approval: false,
      workflow_id: '',
      limits: {},
      text: '',
      member_ids: [],
      task_id: '',
      outcome: '',
      evidence: '',
      ...values,
    };
  }
  async function submit() {
    formError = '';
    const id = native.factoryId;
    let operation, args;
    if (modal === 'setup') {
      if (
        !currentProject ||
        (!form.leader_agent_id && (!form.leader_name.trim() || !form.model))
      ) {
        formError = t('factory.native.leaderRequired');
        return;
      }
      operation = 'factory.create';
      args = {
        project_id: currentProject,
        remit: form.instructions,
        ...(form.leader_agent_id
          ? { leader_agent_id: form.leader_agent_id }
          : {
              leader_name: form.leader_name.trim(),
              leader_model: form.model,
              request_id: form.request_id,
            }),
      };
    } else if (modal === 'member') {
      if (form.new ? !form.name.trim() : !form.agent_id) {
        formError = t('factory.native.required');
        return;
      }
      operation = form.new ? 'member.create' : 'member.add';
      args = {
        factory_id: id,
        ...(form.new ? {} : { agent_id: form.agent_id }),
        role: form.role,
        instructions: form.instructions,
        ...(form.new
          ? {
              name: form.name.trim(),
              ...(form.model ? { model: form.model } : {}),
              request_id: form.request_id,
            }
          : {}),
      };
    } else if (modal === 'task') {
      if (
        !form.title.trim() ||
        !form.objective.trim() ||
        limitsProblem(form.limits)
      ) {
        formError = limitsProblem(form.limits) || t('factory.native.required');
        return;
      }
      operation = 'task.create';
      args = {
        factory_id: id,
        ...Object.fromEntries(
          [
            'title',
            'objective',
            'conditions',
            'assignee_id',
            'parent_id',
            'dependencies',
            'reviewer_id',
            'user_approval',
            'workflow_id',
          ].map((key) => [key, form[key]]),
        ),
        priority: Number(form.priority),
        limits: normalizeLimits(form.limits),
      };
    } else if (modal === 'topic') {
      if (!form.title.trim()) {
        formError = t('factory.native.required');
        return;
      }
      operation = 'topic.create';
      args = {
        factory_id: id,
        title: form.title,
        task_id: form.task_id,
        member_ids: form.member_ids,
        text: form.text,
      };
    } else if (modal === 'invite') {
      operation = 'topic.invite';
      args = {
        id: topic.id,
        expected_revision: topic.revision,
        member_ids: form.member_ids,
        text: form.text,
      };
    } else if (modal === 'post') {
      if (!form.text.trim()) {
        formError = t('factory.native.required');
        return;
      }
      operation = 'topic.post';
      args = {
        id: topic.id,
        text: form.text,
        recipients: form.member_ids,
        source_receipt: form.request_id,
      };
    } else if (modal === 'assign') {
      operation = 'task.assign';
      args = {
        id: task.id,
        expected_revision: task.revision,
        assignee_id: form.assignee_id,
      };
    } else if (modal === 'complete') {
      if (!form.outcome.trim() || !form.evidence.trim()) {
        formError = t('factory.native.resultRequired');
        return;
      }
      operation = 'task.complete';
      args = {
        id: task.id,
        expected_revision: task.revision,
        outcome: form.outcome,
        evidence: form.evidence,
      };
    } else if (modal === 'changes') {
      if (!form.text.trim()) {
        formError = t('factory.native.required');
        return;
      }
      operation = 'task.review';
      args = {
        id: task.id,
        expected_revision: task.revision,
        approve: false,
        findings: form.text,
      };
    }
    if (!operation) return;
    const result = await native.mutate(operation, args);
    if (!result) return;
    modal = '';
    if (operation === 'factory.create')
      await onNavigate(`/factory/${encodeURIComponent(result.factory.id)}`);
    if (operation === 'task.create')
      await onNavigate(`/task/${encodeURIComponent(result.task.id)}`);
    if (operation === 'topic.create')
      await onNavigate(`/topic/${encodeURIComponent(result.topic.id)}`);
  }
  const modalTitle = $derived(
    modal === 'setup'
      ? t('factory.native.setup')
      : modal === 'member'
        ? t('factory.native.addMember')
        : modal === 'task'
          ? t('factory.native.newTask')
          : modal === 'topic'
            ? t('factory.native.newTopic')
            : modal === 'invite'
              ? t('factory.native.invite')
              : modal === 'post'
                ? t('factory.native.post')
                : modal === 'assign'
                  ? t('factory.native.reassign')
                  : modal === 'complete'
                    ? t('factory.native.complete')
                    : t('factory.native.changes'),
  );
</script>

{#if modal}<Modal
    title={modalTitle}
    onClose={() => (modal = '')}
    closeDisabled={Boolean(native.pending)}
    >{#snippet body()}<div class="modal-body factory-form">
        {#if modal === 'setup'}
          <label for="native-leader">{t('factory.native.existingAgent')}</label
          ><Dropdown
            id="native-leader"
            ariaLabel={t('factory.native.existingAgent')}
            value={form.leader_agent_id}
            options={[
              { value: '', label: t('factory.native.newLeader') },
              ...agents,
            ]}
            onValueChange={(value) => (form.leader_agent_id = value)}
          />
          {#if !form.leader_agent_id}<label for="native-leader-name"
              >{t('factory.native.agentName')}</label
            ><TextField
              id="native-leader-name"
              value={form.leader_name}
              onInput={(value) => (form.leader_name = value)}
            />{@render modelField()}{/if}
          <label for="native-setup-remit">{t('factory.native.remit')}</label
          ><TextArea
            id="native-setup-remit"
            value={form.instructions}
            onInput={(value) => (form.instructions = value)}
          />
        {:else if modal === 'member'}
          <label class="native-check"
            >{t('factory.native.newAgent')}<Toggle
              checked={Boolean(form.new)}
              ariaLabel={t('factory.native.newAgent')}
              onChange={(value) => {
                form.new = value;
                form.agent_id = '';
              }}
            /></label
          >
          {#if form.new}<label for="native-agent-name"
              >{t('factory.native.agentName')}</label
            ><TextField
              id="native-agent-name"
              value={form.name}
              onInput={(value) => (form.name = value)}
            />{@render modelField()}{:else}<Dropdown
              id="native-agent"
              ariaLabel={t('factory.native.existingAgent')}
              options={agents}
              value={form.agent_id}
              onValueChange={(value) => (form.agent_id = value)}
            />{/if}
          <label for="native-member-role">{t('factory.native.role')}</label
          ><TextField
            id="native-member-role"
            value={form.role}
            onInput={(value) => (form.role = value)}
          /><label for="native-member-instructions"
            >{t('factory.native.instructions')}</label
          ><TextArea
            id="native-member-instructions"
            value={form.instructions}
            onInput={(value) => (form.instructions = value)}
          />
        {:else if modal === 'task'}
          <label for="native-create-title">{t('factory.cardTitle')}</label
          ><TextField
            id="native-create-title"
            value={form.title}
            onInput={(value) => (form.title = value)}
          />
          <label for="native-create-objective"
            >{t('factory.native.objective')}</label
          ><TextArea
            id="native-create-objective"
            value={form.objective}
            onInput={(value) => (form.objective = value)}
          />
          <label for="native-create-conditions"
            >{t('factory.native.criteria')}</label
          ><TextArea
            id="native-create-conditions"
            value={form.conditions}
            onInput={(value) => (form.conditions = value)}
          />
          {@render ownerField()}
          <label for="native-create-priority"
            >{t('factory.native.priority')}</label
          ><TextField
            id="native-create-priority"
            type="number"
            value={form.priority}
            onInput={(value) => (form.priority = value)}
          />
          <label for="native-create-parent">{t('factory.native.parent')}</label
          ><Dropdown
            id="native-create-parent"
            ariaLabel={t('factory.native.parent')}
            value={form.parent_id}
            options={[
              { value: '', label: t('factory.native.none') },
              ...native.tasks.map((item) => ({
                value: item.id,
                label: item.title,
              })),
            ]}
            onValueChange={(value) => (form.parent_id = value)}
          />
          <fieldset>
            <legend>{t('factory.native.dependencies')}</legend
            >{#each native.tasks as item (item.id)}<Checkbox
                checked={form.dependencies.includes(item.id)}
                onChange={(checked) =>
                  (form.dependencies = checked
                    ? [...form.dependencies, item.id]
                    : form.dependencies.filter((id) => id !== item.id))}
                >{item.title}</Checkbox
              >{/each}
          </fieldset>
          <label for="native-create-reviewer"
            >{t('factory.native.reviewer')}</label
          ><Dropdown
            id="native-create-reviewer"
            ariaLabel={t('factory.native.reviewer')}
            value={form.reviewer_id}
            options={memberOptions}
            onValueChange={(value) => (form.reviewer_id = value)}
          />
          <label class="native-check"
            >{t('factory.native.userApproval')}<Toggle
              ariaLabel={t('factory.native.userApproval')}
              checked={form.user_approval}
              onChange={(value) => (form.user_approval = value)}
            /></label
          >
          <label for="native-create-workflow"
            >{t('factory.native.workflowOptional')}</label
          ><Dropdown
            id="native-create-workflow"
            ariaLabel={t('factory.native.workflowOptional')}
            value={form.workflow_id}
            options={[
              { value: '', label: t('factory.native.none') },
              ...model.workflows
                .filter((item) => item.enabled)
                .map((item) => ({ value: item.id, label: item.name })),
            ]}
            onValueChange={(value) => (form.workflow_id = value)}
          />
          <LimitsEditor
            bind:value={form.limits}
            prefix="native-create-limit"
            label={t('factory.native.budget')}
          />
        {:else if modal === 'assign'}{@render ownerField()}
        {:else if modal === 'complete'}<label for="native-outcome"
            >{t('factory.native.outcome')}</label
          ><TextArea
            id="native-outcome"
            value={form.outcome}
            onInput={(value) => (form.outcome = value)}
          /><label for="native-evidence">{t('factory.native.evidence')}</label
          ><TextArea
            id="native-evidence"
            value={form.evidence}
            onInput={(value) => (form.evidence = value)}
          />
        {:else}
          {#if modal === 'topic'}<label for="native-topic-title"
              >{t('factory.native.topicTitle')}</label
            ><TextField
              id="native-topic-title"
              value={form.title}
              onInput={(value) => (form.title = value)}
            /><label for="native-topic-task"
              >{t('factory.native.taskLink')}</label
            ><Dropdown
              id="native-topic-task"
              ariaLabel={t('factory.native.taskLink')}
              value={form.task_id}
              options={[
                { value: '', label: t('factory.native.none') },
                ...native.tasks.map((item) => ({
                  value: item.id,
                  label: item.title,
                })),
              ]}
              onValueChange={(value) => (form.task_id = value)}
            />{/if}
          {#if modal !== 'changes'}<fieldset>
              <legend>{t('factory.native.participants')}</legend
              >{#each active as member (member.id)}<Checkbox
                  checked={form.member_ids.includes(member.id)}
                  onChange={(checked) =>
                    (form.member_ids = checked
                      ? [...form.member_ids, member.id]
                      : form.member_ids.filter((id) => id !== member.id))}
                  >{memberName(member.id)}</Checkbox
                >{/each}
            </fieldset>{/if}
          <label for="native-message">{t('factory.native.message')}</label
          ><TextArea
            id="native-message"
            value={form.text}
            onInput={(value) => (form.text = value)}
          />
        {/if}
        {#if formError || native.error}<Banner variant="error" role="alert"
            >{formError || native.error}</Banner
          >{/if}
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={Boolean(native.pending)}
        onClick={() => (modal = '')}>{t('common.cancel')}</Button
      ><Button
        variant="primary"
        loading={Boolean(native.pending)}
        onClick={submit}>{modalTitle}</Button
      >{/snippet}</Modal
  >{/if}
{#snippet ownerField()}<label for="native-owner"
    >{t('factory.native.owner')}</label
  ><Dropdown
    id="native-owner"
    ariaLabel={t('factory.native.owner')}
    options={memberOptions}
    value={form.assignee_id}
    onValueChange={(value) => (form.assignee_id = value)}
  />{/snippet}
{#snippet modelField()}<label for="native-new-model">{t('factory.model')}</label
  ><Dropdown
    id="native-new-model"
    ariaLabel={t('factory.model')}
    value={form.model}
    options={[
      ...(form.new
        ? [{ value: '', label: t('factory.native.inheritedModel') }]
        : []),
      ...chatModels.map((item) => ({
        value: item.id,
        label: item.name || item.id,
      })),
    ]}
    onValueChange={(value) => (form.model = value)}
  />{/snippet}

<style>
  .native-check {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 8px 0;
    color: var(--text-hi);
  }
  fieldset {
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
  }
</style>
