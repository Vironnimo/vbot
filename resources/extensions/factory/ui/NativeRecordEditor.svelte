<script>
  import { isChatModel } from '@vbot/extension-sdk/lib/modelSelection.js';
  import { untrack } from 'svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Checkbox from '@vbot/extension-sdk/ui/Checkbox.svelte';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import Toggle from '@vbot/extension-sdk/ui/Toggle.svelte';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import SaveStatus from '@vbot/extension-sdk/ui/SaveStatus.svelte';
  import LimitsEditor from './LimitsEditor.svelte';
  import { normalizeLimits, limitsProblem } from './limits.js';
  import { createRecordEditor } from './recordEditor.svelte.js';
  let { kind, record, native, model } = $props();
  const identity = untrack(() => record.id);
  const fields = untrack(() =>
    kind === 'factory'
      ? ['name', 'remit', 'capacity', 'limits', 'config']
      : kind === 'member'
        ? ['role', 'instructions', 'capacity', 'config']
        : [
            'title',
            'objective',
            'conditions',
            'priority',
            'parent_id',
            'dependencies',
            'reviewer_id',
            'user_approval',
            'workflow_id',
            'limits',
          ],
  );
  const editor = createRecordEditor({
    identity,
    client: untrack(() => model.client),
    get record() {
      return record;
    },
    project: (row) =>
      Object.fromEntries(
        fields.map((key) => [
          key,
          (key === 'config'
            ? {
                ...row.config,
                ...Object.fromEntries(
                  [
                    'allowed_models',
                    'allowed_skills',
                    'allowed_tools',
                    'denied_tools',
                  ].map((name) => [
                    name,
                    (row.config?.[name] || []).join(', '),
                  ]),
                ),
              }
            : row[key]) ??
            (key === 'limits' || key === 'config'
              ? {}
              : key === 'dependencies'
                ? []
                : key === 'user_approval'
                  ? false
                  : key === 'capacity'
                    ? 1
                    : key === 'priority'
                      ? 0
                      : ''),
        ]),
      ),
    normalize: (draft) => ({
      ...draft,
      ...(draft.config
        ? {
            config: Object.fromEntries(
              Object.entries(draft.config)
                .filter(
                  ([key, value]) =>
                    ![
                      'model',
                      'thinking_effort',
                      'allowed_models',
                      'allowed_skills',
                      'allowed_tools',
                      'denied_tools',
                    ].includes(key) || value.trim(),
                )
                .map(([key, value]) => [
                  key,
                  [
                    'allowed_models',
                    'allowed_skills',
                    'allowed_tools',
                    'denied_tools',
                  ].includes(key)
                    ? value
                        .split(',')
                        .map((item) => item.trim())
                        .filter(Boolean)
                    : value,
                ]),
            ),
          }
        : {}),
      ...(fields.includes('limits')
        ? { limits: normalizeLimits(draft.limits) }
        : {}),
      ...(fields.includes('capacity')
        ? { capacity: Number(draft.capacity) }
        : {}),
      ...(fields.includes('priority')
        ? { priority: Number(draft.priority) }
        : {}),
    }),
    validate: (draft) =>
      (fields.includes('limits') && limitsProblem(draft.limits)) ||
      (kind === 'task' && (!draft.title.trim() || !draft.objective.trim())
        ? t('factory.native.required')
        : ''),
    save: async (values, revision, baseline) => {
      const patch = Object.fromEntries(
        Object.entries(values).filter(
          ([key, value]) =>
            JSON.stringify(value) !== JSON.stringify(baseline[key]),
        ),
      );
      if (['member', 'factory'].includes(kind) && patch.config) {
        const clear = [
          'model',
          'thinking_effort',
          'allowed_skills',
          'allowed_tools',
          'denied_tools',
          ...(kind === 'factory'
            ? ['allowed_models', 'permission_ceiling']
            : []),
        ].filter(
          (key) =>
            Object.hasOwn(baseline.config, key) &&
            !Object.hasOwn(values.config, key),
        );
        if (clear.length) patch.clear_fields = clear;
      }
      return (
        await model.call(`${kind}.update`, {
          id: identity,
          expected_revision: revision,
          ...patch,
        })
      )[kind];
    },
    read: async () => {
      if (kind !== 'member')
        return (await model.call(`${kind}.get`, { id: identity }))[kind];
      let after;
      do {
        const result = await model.call('member.list', {
          factory_id: record.factory_id,
          limit: 100,
          ...(after ? { after } : {}),
        });
        const member = result.members.find((row) => row.id === identity);
        if (member) return member;
        after = result.next_after;
      } while (after);
      return { ...record, revision: 0 };
    },
    onSave: (saved) => native.accept(kind, saved),
  });
  export const flush = () => editor.flush();
  const memberOptions = $derived([
    { value: '', label: t('factory.native.none') },
    ...native.members
      .filter((item) => item.state === 'active')
      .map((item) => ({ value: item.id, label: item.role || item.agent_id })),
  ]);
  const taskOptions = $derived([
    { value: '', label: t('factory.native.none') },
    ...native.tasks
      .filter((item) => item.id !== identity)
      .map((item) => ({ value: item.id, label: item.title })),
  ]);
  const workflowOptions = $derived([
    { value: '', label: t('factory.native.none') },
    ...model.workflows
      .filter((item) => item.enabled)
      .map((item) => ({ value: item.id, label: item.name })),
  ]);
</script>

<div class="factory-form native-record" data-native-editor={kind}>
  {#if kind === 'factory'}
    <label for="native-name">{t('factory.cardTitle')}</label><TextField
      id="native-name"
      value={editor.draft.name}
      onInput={(value) => (editor.draft.name = value)}
    />
    <label for="native-remit">{t('factory.native.remit')}</label><TextArea
      id="native-remit"
      value={editor.draft.remit}
      onInput={(value) => (editor.draft.remit = value)}
    />
  {:else if kind === 'member'}
    <label for="native-role">{t('factory.native.role')}</label><TextField
      id="native-role"
      value={editor.draft.role}
      onInput={(value) => (editor.draft.role = value)}
    />
    <label for="native-instructions">{t('factory.native.instructions')}</label
    ><TextArea
      id="native-instructions"
      value={editor.draft.instructions}
      onInput={(value) => (editor.draft.instructions = value)}
    />
  {:else}
    <label for="native-title">{t('factory.cardTitle')}</label><TextField
      id="native-title"
      value={editor.draft.title}
      onInput={(value) => (editor.draft.title = value)}
    />
    <label for="native-objective">{t('factory.native.objective')}</label
    ><TextArea
      id="native-objective"
      value={editor.draft.objective}
      onInput={(value) => (editor.draft.objective = value)}
    />
    <label for="native-conditions">{t('factory.native.criteria')}</label
    ><TextArea
      id="native-conditions"
      value={editor.draft.conditions}
      onInput={(value) => (editor.draft.conditions = value)}
    />
    <label for="native-priority">{t('factory.native.priority')}</label
    ><TextField
      id="native-priority"
      type="number"
      value={editor.draft.priority}
      onInput={(value) => (editor.draft.priority = value)}
    />
    <label for="native-parent">{t('factory.native.parent')}</label><Dropdown
      id="native-parent"
      ariaLabel={t('factory.native.parent')}
      options={taskOptions}
      value={editor.draft.parent_id}
      onValueChange={(value) => (editor.draft.parent_id = value)}
    />
    <fieldset>
      <legend>{t('factory.native.dependencies')}</legend>
      <p class="factory-help">{t('factory.native.dependencyHelp')}</p>
      {#each native.tasks.filter((item) => item.id !== identity) as task (task.id)}<Checkbox
          checked={editor.draft.dependencies.includes(task.id)}
          onChange={(checked) =>
            (editor.draft.dependencies = checked
              ? [...editor.draft.dependencies, task.id]
              : editor.draft.dependencies.filter((id) => id !== task.id))}
          >{task.title}</Checkbox
        >{/each}
    </fieldset>
    <label for="native-reviewer">{t('factory.native.reviewer')}</label><Dropdown
      id="native-reviewer"
      ariaLabel={t('factory.native.reviewer')}
      options={memberOptions}
      value={editor.draft.reviewer_id}
      onValueChange={(value) => (editor.draft.reviewer_id = value)}
    />
    <label class="native-check"
      >{t('factory.native.userApproval')}<Toggle
        checked={editor.draft.user_approval}
        ariaLabel={t('factory.native.userApproval')}
        onChange={(value) => (editor.draft.user_approval = value)}
      /></label
    >
    <label for="native-workflow">{t('factory.native.workflowOptional')}</label
    ><Dropdown
      id="native-workflow"
      ariaLabel={t('factory.native.workflowOptional')}
      options={workflowOptions}
      value={editor.draft.workflow_id}
      onValueChange={(value) => (editor.draft.workflow_id = value)}
    />
  {/if}
  {#if kind !== 'task'}
    <label for="native-capacity">{t('factory.native.capacity')}</label
    ><TextField
      id="native-capacity"
      type="number"
      min="1"
      value={editor.draft.capacity}
      onInput={(value) => (editor.draft.capacity = value)}
    />
    <label for="native-model">{t('factory.native.localModel')}</label><Dropdown
      id="native-model"
      ariaLabel={t('factory.native.localModel')}
      options={[
        { value: '', label: t('factory.native.inheritedModel') },
        ...(model.catalog.models || []).filter(isChatModel).map((item) => ({
          value: item.id,
          label: item.name || item.id,
        })),
      ]}
      value={editor.draft.config.model || ''}
      onValueChange={(value) =>
        (editor.draft.config = { ...editor.draft.config, model: value })}
    />
    <label for="native-effort">{t('factory.native.localEffort')}</label
    ><TextField
      id="native-effort"
      value={editor.draft.config.thinking_effort || ''}
      onInput={(value) =>
        (editor.draft.config = {
          ...editor.draft.config,
          thinking_effort: value,
        })}
    />
  {/if}
  {#if kind !== 'task'}
    <details>
      <summary>{t('factory.native.boundaries')}</summary>
      <p class="factory-help">{t('factory.native.boundaryHelp')}</p>
      {#if kind === 'factory'}
        <label for="native-allowed-models"
          >{t('factory.native.allowedModels')}</label
        ><TextField
          id="native-allowed-models"
          value={editor.draft.config.allowed_models}
          onInput={(value) =>
            (editor.draft.config = {
              ...editor.draft.config,
              allowed_models: value,
            })}
        />
      {/if}
      <label for="native-allowed-skills"
        >{t('factory.native.allowedSkills')}</label
      ><TextField
        id="native-allowed-skills"
        value={editor.draft.config.allowed_skills}
        onInput={(value) =>
          (editor.draft.config = {
            ...editor.draft.config,
            allowed_skills: value,
          })}
      />
      <label for="native-allowed-tools"
        >{t('factory.native.allowedTools')}</label
      ><TextField
        id="native-allowed-tools"
        value={editor.draft.config.allowed_tools}
        onInput={(value) =>
          (editor.draft.config = {
            ...editor.draft.config,
            allowed_tools: value,
          })}
      />
      <label for="native-denied-tools">{t('factory.native.deniedTools')}</label
      ><TextField
        id="native-denied-tools"
        value={editor.draft.config.denied_tools}
        onInput={(value) =>
          (editor.draft.config = {
            ...editor.draft.config,
            denied_tools: value,
          })}
      />
    </details>
  {/if}
  {#if fields.includes('limits')}<LimitsEditor
      bind:value={editor.draft.limits}
      prefix="native-limit"
      label={kind === 'factory'
        ? t('factory.native.factoryLimits')
        : t('factory.native.budget')}
      usage={record.consumption}
    />{/if}
  {#if editor.error}<Banner variant="error" role="alert">{editor.error}</Banner
    >{/if}
  {#if editor.notice}<Banner variant="warn">{editor.notice}</Banner>{/if}
  <SaveStatus
    saving={editor.saving}
    pending={editor.hasChanges()}
    onClick={editor.save}
  />
</div>
