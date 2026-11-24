<script>
  import { untrack } from 'svelte';
  import PermissionProfileSelect from '@vbot/extension-sdk/ui/PermissionProfileSelect.svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import SaveStatus from '@vbot/extension-sdk/ui/SaveStatus.svelte';
  import { createRecordEditor } from './recordEditor.svelte.js';

  let { kind, record, model } = $props();
  const identity = untrack(() => record.id);
  const fields = untrack(() =>
    kind === 'factory'
      ? ['name', 'remit', 'capacity', 'config']
      : ['role', 'instructions', 'capacity', 'config'],
  );

  const listKeys = [
    'allowed_models',
    'allowed_skills',
    'allowed_tools',
    'denied_tools',
  ];

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
                  listKeys.map((name) => [
                    name,
                    (row.config?.[name] || []).join(', '),
                  ]),
                ),
              }
            : row[key]) ??
            (key === 'config' ? {} : key === 'capacity' ? 1 : ''),
        ]),
      ),
    normalize: (draft) => ({
      ...draft,
      ...(draft.config
        ? {
            config: Object.fromEntries(
              Object.entries(draft.config)
                .filter(
                  ([key, value]) => !listKeys.includes(key) || value.trim(),
                )
                .map(([key, value]) => [
                  key,
                  listKeys.includes(key)
                    ? value
                        .split(',')
                        .map((item) => item.trim())
                        .filter(Boolean)
                    : value,
                ]),
            ),
          }
        : {}),
      capacity: Number(draft.capacity),
    }),
    validate: () => '',
    save: async (values, revision, baseline) => {
      const patch = Object.fromEntries(
        Object.entries(values).filter(
          ([key, value]) =>
            JSON.stringify(value) !== JSON.stringify(baseline[key]),
        ),
      );
      if (patch.config) {
        const clear = [
          'allowed_skills',
          'allowed_tools',
          'denied_tools',
          ...(kind === 'factory'
            ? [
                'allowed_models',
                'permission_ceiling',
                'permission_ceiling_profile',
              ]
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
      const result = await model.call('member.get', { id: identity });
      return result.member;
    },
    onSave: (saved) => {
      if (kind === 'factory') model.acceptFactory(saved);
      else model.acceptMember(saved);
    },
  });

  export const flush = () => editor.flush();
</script>

<div class="factory-form" data-record-editor={kind}>
  {#if kind === 'factory'}
    <label for="factory-settings-name">{t('factory.settings.name')}</label>
    <TextField
      id="factory-settings-name"
      value={editor.draft.name}
      onInput={(value) => (editor.draft.name = value)}
    />
    <label for="factory-settings-remit">{t('factory.setup.remit')}</label>
    <TextArea
      id="factory-settings-remit"
      rows={3}
      autoGrow
      value={editor.draft.remit}
      onInput={(value) => (editor.draft.remit = value)}
    />
  {:else}
    <label for={`member-role-${identity}`}>{t('factory.team.role')}</label>
    <TextField
      id={`member-role-${identity}`}
      value={editor.draft.role}
      onInput={(value) => (editor.draft.role = value)}
    />
    <label for={`member-instructions-${identity}`}
      >{t('factory.team.instructions')}</label
    >
    <TextArea
      id={`member-instructions-${identity}`}
      rows={3}
      autoGrow
      value={editor.draft.instructions}
      onInput={(value) => (editor.draft.instructions = value)}
    />
  {/if}

  <label for={`${kind}-capacity-${identity}`}
    >{t('factory.settings.capacity')}</label
  >
  <TextField
    id={`${kind}-capacity-${identity}`}
    type="number"
    min="1"
    value={editor.draft.capacity}
    onInput={(value) => (editor.draft.capacity = value)}
  />

  <details>
    <summary>{t('factory.settings.boundaries')}</summary>
    <p class="factory-help">{t('factory.settings.boundaryHelp')}</p>
    {#if kind === 'factory'}
      <label for="factory-permission-limit"
        >{t('permissions.profile.groupLimit')}</label
      >
      <PermissionProfileSelect
        id="factory-permission-limit"
        label={t('permissions.profile.groupLimit')}
        names={model.catalog.permission_profiles}
        value={editor.draft.config.permission_ceiling_profile}
        emptyLabel={t('permissions.profile.noGroupLimit')}
        onChange={(value) => {
          if (value) editor.draft.config.permission_ceiling_profile = value;
          else delete editor.draft.config.permission_ceiling_profile;
        }}
      />
      <p class="factory-help">{t('permissions.profile.groupLimitHelp')}</p>
      {#if editor.draft.config.permission_ceiling}
        <p class="factory-help">
          {t('factory.settings.existingPermissionLimit')}
        </p>
      {/if}
      <label for="factory-allowed-models"
        >{t('factory.settings.allowedModels')}</label
      >
      <TextField
        id="factory-allowed-models"
        value={editor.draft.config.allowed_models}
        onInput={(value) =>
          (editor.draft.config = {
            ...editor.draft.config,
            allowed_models: value,
          })}
      />
    {/if}
    <label for={`${kind}-allowed-skills-${identity}`}
      >{t('factory.settings.allowedSkills')}</label
    >
    <TextField
      id={`${kind}-allowed-skills-${identity}`}
      value={editor.draft.config.allowed_skills}
      onInput={(value) =>
        (editor.draft.config = {
          ...editor.draft.config,
          allowed_skills: value,
        })}
    />
    <label for={`${kind}-allowed-tools-${identity}`}
      >{t('factory.settings.allowedTools')}</label
    >
    <TextField
      id={`${kind}-allowed-tools-${identity}`}
      value={editor.draft.config.allowed_tools}
      onInput={(value) =>
        (editor.draft.config = {
          ...editor.draft.config,
          allowed_tools: value,
        })}
    />
    <label for={`${kind}-denied-tools-${identity}`}
      >{t('factory.settings.deniedTools')}</label
    >
    <TextField
      id={`${kind}-denied-tools-${identity}`}
      value={editor.draft.config.denied_tools}
      onInput={(value) =>
        (editor.draft.config = {
          ...editor.draft.config,
          denied_tools: value,
        })}
    />
  </details>

  {#if editor.error}
    <Banner variant="error" role="alert">{editor.error}</Banner>
  {/if}
  {#if editor.notice}
    <Banner variant="warn">{editor.notice}</Banner>
  {/if}
  <SaveStatus
    saving={editor.saving}
    pending={editor.hasChanges()}
    onClick={editor.save}
  />
</div>
