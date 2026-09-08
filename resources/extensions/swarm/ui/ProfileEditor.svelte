<script>
  import { untrack } from 'svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import ToggleChipList from '../../../../webui/src/components/ui/ToggleChipList.svelte';
  import ToolAccessEditor from '../../../../webui/src/components/tools/ToolAccessEditor.svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';

  let { profile = null, catalog = {}, onSave, onCancel } = $props();
  const defaults = {
    schema_version: 1,
    name: '',
    slug: '',
    participants: [{ model: '', count: 2 }],
    working_directory: { kind: 'directory', path: '' },
    tool_access: { mode: 'selected', allowed: [] },
    tools: {},
    allowed_skills: ['*'],
    instructions: '',
    delivery: {
      main: { mode: 'all', wake_idle: true },
      discussion: { mode: 'all', wake_idle: true },
      ping: { mode: 'all', wake_idle: true },
      coalesce_ms: 250,
      batch_messages: 20,
      batch_chars: 24000,
    },
  };
  // A selected profile is a Svelte reactive proxy. Persisted profile drafts must
  // be plain data before the editor takes ownership.
  const copyProfile = (value) => JSON.parse(JSON.stringify(value));
  let draft = $state(untrack(() => copyProfile(profile ?? defaults)));
  let saving = $state(false);
  let error = $state('');
  const models = $derived(catalog.models ?? catalog.model_choices ?? []);
  const projects = $derived(catalog.projects ?? catalog.project_choices ?? []);
  const tools = $derived(catalog.tools ?? catalog.tool_choices ?? []);
  const skills = $derived(catalog.skills ?? catalog.skill_choices ?? []);
  const modelId = (item) =>
    typeof item === 'string'
      ? item
      : (item?.id ?? item?.model ?? item?.name ?? '');
  const label = (item) =>
    typeof item === 'string'
      ? item
      : (item?.name ?? item?.label ?? item?.id ?? item?.model ?? '');
  const skillName = (item) =>
    typeof item === 'string' ? item : (item?.name ?? item?.id ?? '');
  const skillItems = $derived(
    skills.map((item) => ({
      name: skillName(item),
      description: item?.description,
      allowed:
        draft.allowed_skills?.includes('*') ||
        draft.allowed_skills?.includes(skillName(item)),
    })),
  );
  const toolSettingEntries = $derived(
    tools.flatMap((tool) =>
      (tool.settings_schema ?? tool.settings ?? []).map((setting) => ({
        tool,
        setting,
      })),
    ),
  );

  function addFormation() {
    draft.participants = [...draft.participants, { model: '', count: 1 }];
  }
  function removeFormation(index) {
    if (draft.participants.length > 1)
      draft.participants = draft.participants.filter(
        (_, item) => item !== index,
      );
  }
  function setSkillsAll(next) {
    draft.allowed_skills = next ? ['*'] : [];
  }
  function setSkill(name, next) {
    const current = draft.allowed_skills?.includes('*')
      ? skills.map(skillName)
      : [...(draft.allowed_skills ?? [])];
    draft.allowed_skills = next
      ? [...new Set([...current, name])]
      : current.filter((item) => item !== name);
  }
  function setToolSetting(tool, setting, value) {
    const toolName = modelId(tool);
    draft.tools = {
      ...draft.tools,
      [toolName]: {
        ...(draft.tools?.[toolName] ?? {}),
        [setting.name ?? setting.id]: value,
      },
    };
  }
  function updateDelivery(route, field, value) {
    draft.delivery = {
      ...draft.delivery,
      [route]: { ...draft.delivery[route], [field]: value },
    };
  }
  function updateDeliverySetting(field, value) {
    draft.delivery = { ...draft.delivery, [field]: Number(value) };
  }
  async function save() {
    error = '';
    if (
      !draft.name.trim() ||
      !draft.slug.trim() ||
      !draft.participants.every((row) => row.model && Number(row.count) > 0)
    ) {
      error = t(
        'swarm.profile.validation',
        'Enter a name, slug, and model formation before saving.',
      );
      return;
    }
    if (
      draft.working_directory.kind === 'project'
        ? !draft.working_directory.project_id
        : !draft.working_directory.path?.trim()
    ) {
      error = t(
        'swarm.profile.directoryValidation',
        'Choose a working directory before saving.',
      );
      return;
    }
    saving = true;
    try {
      await onSave(JSON.parse(JSON.stringify(draft)));
    } catch (cause) {
      error = cause.message;
    } finally {
      saving = false;
    }
  }
</script>

<section
  class="editor"
  aria-label={t('swarm.profile.editorLabel', 'Swarm profile editor')}
>
  <div class="editor-head">
    <div>
      <p class="eyebrow">{t('swarm.profile.kicker', 'Profile')}</p>
      <h2>
        {profile
          ? t('swarm.profile.edit', 'Edit profile')
          : t('swarm.profile.new', 'New profile')}
      </h2>
    </div>
    <Button variant="tertiary" onClick={onCancel}
      >{t('common.close', 'Close')}</Button
    >
  </div>
  {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
  <div class="fields two">
    <FormField
      controlId="swarm-profile-name"
      label={t('swarm.profile.name', 'Name')}
      required
      >{#snippet children(control)}<input
          id={control.controlId}
          aria-describedby={control.describedBy}
          bind:value={draft.name}
        />{/snippet}</FormField
    >
    <FormField
      controlId="swarm-profile-slug"
      label={t('swarm.profile.slug', 'Command slug')}
      required
      >{#snippet children(control)}<input
          id={control.controlId}
          aria-describedby={control.describedBy}
          bind:value={draft.slug}
          pattern="[a-z0-9][a-z0-9_-]*"
        />{/snippet}</FormField
    >
  </div>
  <div class="section-head">
    <div>
      <p class="eyebrow">{t('swarm.profile.formation', 'Formation')}</p>
      <p>
        {t(
          'swarm.profile.formationHelp',
          'Each participant receives an independent Session.',
        )}
      </p>
    </div>
    <Button variant="tertiary" onClick={addFormation}
      >{t('swarm.profile.addGroup', 'Add model group')}</Button
    >
  </div>
  {#each draft.participants as formation, index (index)}<div class="formation">
      <FormField
        controlId={`swarm-model-${index}`}
        label={t('swarm.profile.model', 'Model')}
        required
        >{#snippet children(control)}<select
            id={control.controlId}
            aria-describedby={control.describedBy}
            bind:value={formation.model}
            ><option value=""
              >{t('swarm.profile.selectModel', 'Select a model')}</option
            >{#each models as model (modelId(model))}<option
                value={modelId(model)}>{label(model)}</option
              >{/each}</select
          >{/snippet}</FormField
      ><FormField
        controlId={`swarm-count-${index}`}
        label={t('swarm.profile.participants', 'Participants')}
        required
        >{#snippet children(control)}<input
            id={control.controlId}
            aria-describedby={control.describedBy}
            type="number"
            min="1"
            bind:value={formation.count}
          />{/snippet}</FormField
      ><Button
        variant="tertiary"
        ariaLabel={t('swarm.profile.removeGroup', 'Remove model group')}
        disabled={draft.participants.length === 1}
        onClick={() => removeFormation(index)}
        >{t('common.remove', 'Remove')}</Button
      >
    </div>{/each}
  <div class="section-head">
    <div>
      <p class="eyebrow">{t('swarm.profile.directory', 'Working directory')}</p>
      <p>
        {t(
          'swarm.profile.directoryHelp',
          'All participants use this explicit working directory.',
        )}
      </p>
    </div>
  </div>
  <div class="fields two">
    <FormField
      controlId="swarm-directory-source"
      label={t('swarm.profile.directorySource', 'Source')}
      >{#snippet children(control)}<select
          id={control.controlId}
          bind:value={draft.working_directory.kind}
          ><option value="directory"
            >{t('swarm.profile.directoryOption', 'Directory')}</option
          ><option value="project"
            >{t('swarm.profile.projectOption', 'Project')}</option
          ></select
        >{/snippet}</FormField
    >{#if draft.working_directory.kind === 'project'}<FormField
        controlId="swarm-project"
        label={t('swarm.profile.project', 'Project')}
        required
        >{#snippet children(control)}<select
            id={control.controlId}
            bind:value={draft.working_directory.project_id}
            ><option value=""
              >{t('swarm.profile.selectProject', 'Select a project')}</option
            >{#each projects as project (project.id)}<option value={project.id}
                >{label(project)}</option
              >{/each}</select
          >{/snippet}</FormField
      >{:else}<FormField
        controlId="swarm-directory"
        label={t('swarm.profile.directory', 'Directory')}
        required
        >{#snippet children(control)}<input
            id={control.controlId}
            bind:value={draft.working_directory.path}
            placeholder={t(
              'swarm.profile.directoryPlaceholder',
              'C:\\work\\project',
            )}
          />{/snippet}</FormField
      >{/if}
  </div>
  <FormField
    controlId="swarm-instructions"
    label={t('swarm.profile.instructions', 'Standing instructions')}
    help={t(
      'swarm.profile.instructionsHelp',
      'Optional instructions shared by every participant.',
    )}
    full
    >{#snippet children(control)}<textarea
        id={control.controlId}
        rows="5"
        bind:value={draft.instructions}></textarea>{/snippet}</FormField
  >
  <section>
    <div class="section-head">
      <div>
        <p class="eyebrow">{t('swarm.profile.tools', 'Tool access')}</p>
        <p>
          {t(
            'swarm.profile.toolsHelp',
            'Choose the ordinary Tools available to every participant.',
          )}
        </p>
      </div>
    </div>
    <ToolAccessEditor
      value={draft.tool_access}
      {tools}
      onChange={(next) => (draft.tool_access = { ...next, mode: 'selected' })}
    />
  </section>
  {#if toolSettingEntries.length}<section>
      <div class="section-head">
        <div>
          <p class="eyebrow">
            {t('swarm.profile.toolSettings', 'Tool settings')}
          </p>
          <p>
            {t(
              'swarm.profile.toolSettingsHelp',
              'These settings are saved with the profile for the selected Tools.',
            )}
          </p>
        </div>
      </div>
      <div class="fields two">
        {#each toolSettingEntries as entry (`${modelId(entry.tool)}-${entry.setting.name ?? entry.setting.id}`)}<FormField
            controlId={`swarm-tool-${modelId(entry.tool)}-${entry.setting.name ?? entry.setting.id}`}
            label={`${label(entry.tool)}: ${entry.setting.label ?? entry.setting.name ?? entry.setting.id}`}
            >{#snippet children(
              control,
            )}{#if entry.setting.type === 'boolean'}<input
                  id={control.controlId}
                  type="checkbox"
                  checked={Boolean(
                    draft.tools?.[modelId(entry.tool)]?.[
                      entry.setting.name ?? entry.setting.id
                    ],
                  )}
                  onchange={(event) =>
                    setToolSetting(
                      entry.tool,
                      entry.setting,
                      event.currentTarget.checked,
                    )}
                />{:else}<input
                  id={control.controlId}
                  type={entry.setting.type === 'number' ? 'number' : 'text'}
                  value={draft.tools?.[modelId(entry.tool)]?.[
                    entry.setting.name ?? entry.setting.id
                  ] ?? ''}
                  onchange={(event) =>
                    setToolSetting(
                      entry.tool,
                      entry.setting,
                      event.currentTarget.value,
                    )}
                />{/if}{/snippet}</FormField
          >{/each}
      </div>
    </section>{/if}
  <section>
    <div class="section-head">
      <div>
        <p class="eyebrow">{t('swarm.profile.skills', 'Allowed Skills')}</p>
        <p>
          {t(
            'swarm.profile.skillsHelp',
            'Choose the Skills available to every participant.',
          )}
        </p>
      </div>
    </div>
    <ToggleChipList
      items={skillItems}
      emptyLabel={t('swarm.profile.noSkills', 'No Skills are available.')}
      note={draft.allowed_skills?.includes('*')
        ? t(
            'swarm.profile.allSkills',
            'All current and future Skills are allowed.',
          )
        : ''}
      ariaToggleLabel={(name) =>
        t('swarm.profile.toggleSkill', 'Toggle Skill {name}', { name })}
      onToggle={setSkill}
      onSetAll={setSkillsAll}
    />
  </section>
  <section>
    <div class="section-head">
      <div>
        <p class="eyebrow">{t('swarm.profile.delivery', 'Delivery')}</p>
        <p>
          {t(
            'swarm.profile.deliveryHelp',
            'Changes to a saved profile apply only to future Swarms.',
          )}
        </p>
      </div>
    </div>
    <div class="delivery">
      {#each ['main', 'discussion', 'ping'] as route (route)}<div>
          <strong>{t(`swarm.delivery.${route}`, route)}</strong><FormField
            controlId={`swarm-delivery-${route}`}
            label={t('swarm.delivery.mode', 'Mode')}
            ><select
              value={draft.delivery[route].mode}
              onchange={(event) =>
                updateDelivery(route, 'mode', event.currentTarget.value)}
              ><option value="all"
                >{t('swarm.delivery.all', 'All messages')}</option
              ><option value="idle"
                >{t('swarm.delivery.idle', 'When idle')}</option
              ><option value="pull"
                >{t('swarm.delivery.pull', 'Pull only')}</option
              ></select
            ></FormField
          ><label class="check"
            ><input
              type="checkbox"
              checked={draft.delivery[route].wake_idle}
              onchange={(event) =>
                updateDelivery(route, 'wake_idle', event.currentTarget.checked)}
            />
            {t('swarm.delivery.wake', 'Wake idle participants')}</label
          >
        </div>{/each}
    </div>
    <div class="fields three">
      <FormField
        controlId="swarm-coalesce"
        label={t('swarm.delivery.coalesce', 'Coalesce messages (ms)')}
        ><input
          id="swarm-coalesce"
          type="number"
          min="0"
          max="5000"
          value={draft.delivery.coalesce_ms}
          onchange={(event) =>
            updateDeliverySetting('coalesce_ms', event.currentTarget.value)}
        /></FormField
      >
      <FormField
        controlId="swarm-batch-messages"
        label={t('swarm.delivery.batchMessages', 'Messages per batch')}
        ><input
          id="swarm-batch-messages"
          type="number"
          min="1"
          max="100"
          value={draft.delivery.batch_messages}
          onchange={(event) =>
            updateDeliverySetting('batch_messages', event.currentTarget.value)}
        /></FormField
      >
      <FormField
        controlId="swarm-batch-chars"
        label={t('swarm.delivery.batchChars', 'Characters per batch')}
        ><input
          id="swarm-batch-chars"
          type="number"
          min="16000"
          max="128000"
          value={draft.delivery.batch_chars}
          onchange={(event) =>
            updateDeliverySetting('batch_chars', event.currentTarget.value)}
        /></FormField
      >
    </div>
  </section>
  <div class="actions">
    <Button variant="tertiary" onClick={onCancel}
      >{t('common.cancel', 'Cancel')}</Button
    ><Button variant="primary" loading={saving} onClick={save}
      >{saving
        ? t('swarm.profile.saving', 'Saving…')
        : t('swarm.profile.save', 'Save profile')}</Button
    >
  </div>
</section>

<style>
  .editor {
    display: grid;
    gap: 18px;
    max-width: 900px;
  }
  .editor-head,
  .section-head,
  .actions {
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: 12px;
  }
  h2,
  p {
    margin: 0;
  }
  .eyebrow {
    color: var(--accent);
    font: var(--fs-mono-xs) var(--font-mono);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .section-head p + p {
    margin-top: 4px;
    color: var(--text-med);
  }
  .fields {
    display: grid;
    gap: 12px;
  }
  .two {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .three {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .formation {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 130px auto;
    gap: 10px;
    align-items: end;
  }
  .delivery {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
  }
  .delivery > div {
    border-left: 2px solid var(--border-2);
    padding-left: 10px;
    display: grid;
    gap: 8px;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 7px;
    color: var(--text-med);
  }
  .actions {
    justify-content: end;
    position: sticky;
    bottom: 0;
    padding: 12px 0;
    background: var(--bg);
  }
  @media (max-width: 640px) {
    .two,
    .three,
    .delivery,
    .formation {
      grid-template-columns: 1fr;
    }
    .editor-head,
    .section-head {
      align-items: center;
    }
    .actions {
      padding-bottom: 8px;
    }
  }
</style>
