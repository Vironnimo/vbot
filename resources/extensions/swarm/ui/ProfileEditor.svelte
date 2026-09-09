<script>
  import { onDestroy, tick, untrack } from 'svelte';
  import { createDebouncedAutosave } from '../../../../webui/src/lib/autosave.js';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import Toggle from '../../../../webui/src/components/ui/Toggle.svelte';
  import InfoHint from '../../../../webui/src/components/ui/InfoHint.svelte';
  import TabList from '../../../../webui/src/components/ui/TabList.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import SearchableDropdown from '../../../../webui/src/components/SearchableDropdown.svelte';
  import ToggleChipList from '../../../../webui/src/components/ui/ToggleChipList.svelte';
  import ToolAccessEditor from '../../../../webui/src/components/tools/ToolAccessEditor.svelte';
  import {
    effortOptionsForReasoning,
    reasoningForModelValue,
  } from '../../../../webui/src/lib/agentForm.js';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
  } from '../../../../webui/src/lib/modelSelection.js';
  import { changeToolAccessMode } from '../../../../webui/src/lib/toolAccess.js';
  import { t } from '../../../../webui/src/lib/i18n.js';

  let {
    profile = null,
    catalog = {},
    bridgeClient,
    onSave,
    onCancel,
  } = $props();
  const defaults = {
    schema_version: 1,
    name: '',
    participants: [{ model: '', count: 2, thinking_effort: '' }],
    working_directory: { kind: 'directory', path: '' },
    tool_access: { mode: 'selected', allowed: [] },
    tools: {},
    allowed_skills: ['*'],
    ...untrack(() => catalog.prompt_defaults),
    delivery: {
      main: { mode: 'all', wake_idle: true },
      discussion: { mode: 'all', wake_idle: true },
      ping: { mode: 'all', wake_idle: true },
      coalesce_ms: 250,
      batch_messages: 20,
      batch_chars: 24000,
    },
  };
  let draft = $state(
    untrack(() => JSON.parse(JSON.stringify(profile ?? defaults))),
  );
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const snapshot = (value = draft) => {
    const result = copy(value);
    delete result.revision;
    return JSON.stringify(result);
  };
  let savedProfile = $state(untrack(() => copy(draft)));
  let savedSnapshot = $state(untrack(() => snapshot()));
  let saving = $state(false);
  const busy = $derived(saving && !profile);
  let saveReason = '';
  let pendingTransition = $state.raw(null);
  let transitionSaving = $state(false);
  const hasChanges = () => snapshot() !== savedSnapshot;
  const autosave = createDebouncedAutosave({
    getSnapshot: () => snapshot(),
    hasChanges,
    save: (reason) => persist(reason),
  });
  $effect(() => {
    if (profile)
      return untrack(() => bridgeClient.registerAutosave(autosave.participant));
  });
  $effect(() => {
    const pending = hasChanges();
    if (profile && pending && !saving) autosave.scheduleRun();
    else autosave.cancelPendingTimer();
    if (profile) bridgeClient.notifyAutosave();
    return autosave.cancelPendingTimer;
  });
  onDestroy(() => {
    autosave.cancelPendingTimer();
  });

  export async function requestTransition(action) {
    if (transitionSaving) return;
    transitionSaving = true;
    const saved = !profile || (await autosave.participant.flush());
    transitionSaving = false;
    if (!saved) {
      pendingTransition = action;
      return;
    }
    pendingTransition = null;
    action();
  }
  function discardTransition() {
    const action = pendingTransition;
    pendingTransition = null;
    autosave.cancelPendingTimer();
    draft = copy(savedProfile);
    error = '';
    action?.();
  }
  function save() {
    autosave.cancelPendingTimer();
    return autosave.participant.runSave('manual', { force: true });
  }

  let error = $state('');
  let tab = $state('overview');
  let showAllModels = $state(false);
  let scrollport;
  let preview = $state(null);
  let previewSnapshot = $state('');
  let previewBusy = $state(false);
  let previewError = $state('');
  let previewFormation = $state(0);
  let previewRequest = 0;
  const previewCurrent = $derived(
    preview && previewSnapshot === `${previewFormation}:${snapshot()}`,
  );
  const promptBlocks = $derived(
    (catalog.prompt_blocks ?? []).filter(
      (block) => block.id !== 'core:agent_body',
    ),
  );
  const blockTitle = (id) =>
    t(
      `systemPrompt.blockTitle.${id}`,
      {
        'tool:project': 'Project Tool guidance',
        'tool:subagent': 'Subagent Tool guidance',
        'tool:bash': 'Bash environment',
      }[id] || id,
    );
  const reminderTitles = $derived({
    delivery: t(
      'swarm.profile.reminderDelivery',
      'When Board messages are delivered',
    ),
    resume: t('swarm.profile.reminderResume', 'When you resume work'),
  });
  function setPromptBlock(id, enabled) {
    draft.prompt_blocks = enabled
      ? [...new Set([...draft.prompt_blocks, id])]
      : draft.prompt_blocks.filter((item) => item !== id);
  }
  async function inspectPrompt() {
    const request = ++previewRequest;
    const submitted = `${previewFormation}:${snapshot()}`;
    previewBusy = true;
    previewError = '';
    try {
      const result = await bridgeClient.operation('profiles.preview', {
        profile: copy(draft),
        formation_index: Number(previewFormation),
      });
      if (request !== previewRequest) return;
      preview = result.preview;
      previewSnapshot = submitted;
    } catch (cause) {
      if (request === previewRequest) previewError = cause.message;
    } finally {
      if (request === previewRequest) previewBusy = false;
    }
  }
  const models = $derived(catalog.models ?? []);
  const projects = $derived(catalog.projects ?? []);
  const tools = $derived(
    (catalog.tools ?? []).filter(
      (tool) => !(tool.constraints ?? []).includes('identity_agent'),
    ),
  );
  const skills = $derived(catalog.skills ?? []);
  const tabs = $derived([
    { id: 'overview', label: t('swarm.profile.overview', 'Overview') },
    { id: 'prompt', label: t('swarm.profile.systemPrompt', 'System Prompt') },
    { id: 'access', label: t('swarm.profile.access', 'Tools & Skills') },
    {
      id: 'communication',
      label: t('swarm.profile.communication', 'Communication'),
    },
  ]);
  const skillItems = $derived(
    skills.map((skill) => ({
      name: skill.name,
      description: skill.description,
      allowed:
        draft.allowed_skills.includes('*') ||
        draft.allowed_skills.includes(skill.name),
    })),
  );
  const totalParticipants = $derived(
    draft.participants.reduce((sum, row) => sum + (Number(row.count) || 0), 0),
  );
  const directoryOptions = $derived([
    {
      value: 'directory',
      label: t('swarm.profile.directoryOption', 'Directory'),
    },
    { value: 'project', label: t('swarm.profile.projectOption', 'Project') },
  ]);
  const deliveryOptions = $derived([
    {
      value: 'all',
      label: t('swarm.profile.deliveryAll', 'During work and when idle'),
    },
    { value: 'idle', label: t('swarm.profile.deliveryIdle', 'When idle') },
    {
      value: 'pull',
      label: t('swarm.profile.deliveryPull', 'On request or when waking'),
    },
  ]);
  const routes = $derived([
    { id: 'main', label: t('swarm.profile.mainMessages', 'Main discussion') },
    {
      id: 'discussion',
      label: t('swarm.profile.joinedMessages', 'Joined discussions'),
    },
    { id: 'ping', label: t('swarm.profile.mentions', 'Direct mentions') },
  ]);

  function modelOptions(value) {
    return buildModelSelectOptions({
      models,
      modelOnly: true,
      selectedModelValue: value,
      emptyLabel: t('swarm.profile.selectModel', 'Select a model'),
      translate: t,
    });
  }
  function effortOptions(model) {
    return effortOptionsForReasoning(reasoningForModelValue(model, models)).map(
      (value) => ({
        value,
        label:
          value === ''
            ? t('swarm.profile.providerDefault', 'Provider default')
            : t(`agents.form.thinkingEffortOption.${value}`, value),
      }),
    );
  }
  function selectModel(row, value) {
    row.model = value;
    if (
      reasoningForModelValue(value, models)?.supported === false ||
      !effortOptions(value).some(
        (option) => option.value === (row.thinking_effort ?? ''),
      )
    ) {
      row.thinking_effort = '';
    }
  }
  function changeTab(value) {
    return requestTransition(() => {
      tab = value;
      scrollport?.scrollTo?.({ top: 0 });
    });
  }
  function setSkill(name, next) {
    const current = draft.allowed_skills.includes('*')
      ? skills.map((skill) => skill.name)
      : draft.allowed_skills;
    draft.allowed_skills = next
      ? [...new Set([...current, name])]
      : current.filter((item) => item !== name);
  }
  function setToolAccess(next) {
    // Profiles pin the current selection. Materialize All/None through the
    // shared policy owner instead of relabeling an incompatible policy shape.
    draft.tool_access = changeToolAccessMode(next, 'selected', tools);
  }
  function setDirectoryKind(kind) {
    draft.working_directory =
      kind === 'project' ? { kind, project_id: '' } : { kind, path: '' };
  }
  async function invalid(message, id) {
    error = message;
    if (saveReason === 'auto') return false;
    tab = 'overview';
    await tick();
    document.getElementById(id)?.focus();
    return false;
  }
  async function persist(reason) {
    saveReason = reason;
    if (profile && !hasChanges()) {
      if (reason === 'manual')
        bridgeClient.toast(
          t('settings.alreadySaved', 'Already saved'),
          'success',
        );
      return true;
    }
    error = '';
    if (!draft.name.trim())
      return invalid(
        t('swarm.profile.nameRequired', 'Enter a Swarm name.'),
        'swarm-profile-name',
      );
    if (draft.slug?.trim() && !/^[a-z0-9][a-z0-9_-]*$/.test(draft.slug)) {
      await invalid(
        t(
          'swarm.profile.shortcutValidation',
          'Use lowercase letters, numbers, hyphens or underscores for the Chat shortcut.',
        ),
        'swarm-profile-slug',
      );
      const shortcut = document.getElementById('swarm-profile-slug');
      if (shortcut?.closest('details')) shortcut.closest('details').open = true;
      shortcut?.focus();
      return;
    }
    const invalidRow = draft.participants.findIndex(
      (row) =>
        !row.model ||
        !Number.isInteger(Number(row.count)) ||
        Number(row.count) < 1,
    );
    if (invalidRow >= 0)
      return invalid(
        t(
          'swarm.profile.modelsRequired',
          'Choose a Model and a whole participant count of at least 1.',
        ),
        `swarm-model-${invalidRow}`,
      );
    const directory = draft.working_directory;
    if (
      directory.kind === 'project'
        ? !directory.project_id
        : !directory.path?.trim()
    ) {
      return invalid(
        t(
          'swarm.profile.directoryValidation',
          'Choose a working directory before saving.',
        ),
        directory.kind === 'project' ? 'swarm-project' : 'swarm-directory',
      );
    }
    const submittedSnapshot = snapshot();
    saving = true;
    try {
      const payload = JSON.parse(JSON.stringify(draft));
      payload.name = payload.name.trim();
      payload.participants = payload.participants.map((row) => ({
        ...row,
        count: Number(row.count),
      }));
      if (!payload.slug?.trim()) delete payload.slug;
      const saved = await onSave(payload);
      if (profile) {
        savedProfile = copy(saved);
        if (snapshot() === submittedSnapshot) draft = copy(saved);
        else draft.revision = saved.revision;
        savedSnapshot = snapshot(saved);
      }
      return true;
    } catch (cause) {
      error = cause.message;
      return false;
    } finally {
      saving = false;
    }
  }
</script>

<section
  class="editor"
  aria-label={t('swarm.profile.editorLabel', 'Swarm editor')}
>
  <header class="editor-head">
    <div>
      <h2>
        {profile ? savedProfile.name : t('swarm.profile.new', 'New Swarm')}
      </h2>
      <p>
        {t(
          'swarm.profile.scopeHelp',
          'A reusable setup. Changes apply to new Runs.',
        )}
      </p>
    </div>
    <Button
      variant="tertiary"
      icon
      disabled={busy}
      ariaLabel={t('common.close', 'Close')}
      tooltip={t('common.close', 'Close')}
      onClick={onCancel}
    >
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.7"
        aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg
      >
    </Button>
  </header>
  <TabList
    items={tabs}
    value={tab}
    idPrefix="swarm-profile"
    ariaLabel={t('swarm.profile.sections', 'Swarm sections')}
    onChange={changeTab}
  />
  {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
  <div class="editor-scroll" bind:this={scrollport}>
    <fieldset disabled={busy}>
      <div
        class="topic"
        hidden={tab !== 'overview'}
        role="tabpanel"
        tabindex="0"
        id="swarm-profile-panel-overview"
        aria-labelledby="swarm-profile-tab-overview"
      >
        <FormField
          controlId="swarm-profile-name"
          label={t('swarm.profile.name', 'Name')}
          required
        >
          <TextField
            id="swarm-profile-name"
            value={draft.name}
            maxlength="120"
            onInput={(value) => (draft.name = value)}
          />
        </FormField>
        <section class="form-section">
          <div class="section-head">
            <h3>{t('swarm.profile.models', 'Models & participants')}</h3>
            <Button
              onClick={() =>
                (draft.participants = [
                  ...draft.participants,
                  { model: '', count: 1, thinking_effort: '' },
                ])}
              ><svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="1.7"
                aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg
              >{t('swarm.profile.addModel', 'Add Model')}</Button
            >
          </div>
          {#each draft.participants as row, index (index)}
            {@const allOptions = modelOptions(row.model)}
            {@const visibleOptions = filterModelSelectOptions(allOptions, {
              showAll: showAllModels,
              selectedModelValue: row.model,
            })}
            {@const noReasoning =
              reasoningForModelValue(row.model, models)?.supported === false}
            <div class="formation">
              <FormField
                controlId={`swarm-model-${index}`}
                label={t('swarm.profile.model', 'Model')}
                required
              >
                <SearchableDropdown
                  id={`swarm-model-${index}`}
                  value={row.model}
                  options={visibleOptions}
                  ariaLabel={t('swarm.profile.model', 'Model')}
                  disabled={busy}
                  searchPlaceholder={t(
                    'agents.form.modelSearchPlaceholder',
                    'Filter models…',
                  )}
                  footerActionLabel={modelFilterFooterLabel({
                    showAll: showAllModels,
                    hiddenCount: allOptions.length - visibleOptions.length,
                    translate: t,
                  })}
                  onFooterAction={() => (showAllModels = !showAllModels)}
                  onValueChange={(value) => selectModel(row, value)}
                />
              </FormField>
              <div class="formation-settings">
                <FormField
                  controlId={`swarm-effort-${index}`}
                  help={noReasoning
                    ? t(
                        'agents.form.thinkingEffortUnsupported',
                        'This model does not support reasoning.',
                      )
                    : ''}
                >
                  {#snippet labelContent()}{t(
                      'agents.form.thinkingEffort',
                      'Thinking effort',
                    )}<InfoHint
                      text={t(
                        'swarm.profile.effortHelp',
                        'Reasoning effort for these participants. Provider default leaves it to the Provider; shared Agent defaults do not apply.',
                      )}
                    />{/snippet}
                  <Dropdown
                    id={`swarm-effort-${index}`}
                    value={row.thinking_effort ?? ''}
                    options={effortOptions(row.model)}
                    ariaLabel={t(
                      'agents.form.thinkingEffort',
                      'Thinking effort',
                    )}
                    disabled={busy || !row.model || noReasoning}
                    onValueChange={(value) => (row.thinking_effort = value)}
                  />
                </FormField>
                <FormField
                  controlId={`swarm-count-${index}`}
                  label={t('swarm.profile.participants', 'Participants')}
                  required
                >
                  <TextField
                    id={`swarm-count-${index}`}
                    type="number"
                    min="1"
                    step="1"
                    value={row.count}
                    onInput={(value) => (row.count = value)}
                  />
                </FormField>
                <Button
                  variant="danger"
                  icon
                  ariaLabel={t(
                    'swarm.profile.removeModel',
                    'Remove Model row {number}',
                    { number: index + 1 },
                  )}
                  tooltip={t('swarm.profile.remove', 'Remove Model')}
                  disabled={busy || draft.participants.length === 1}
                  onClick={() =>
                    (draft.participants = draft.participants.filter(
                      (_, i) => i !== index,
                    ))}
                >
                  <svg
                    width="16"
                    height="16"
                    viewBox="0 0 16 16"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="1.5"
                    aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" /></svg
                  >
                </Button>
              </div>
            </div>
          {/each}
          <p class="hint">
            {t(
              'swarm.profile.sessionCount',
              '{count} participants, each with its own Session.',
              { count: totalParticipants },
            )}
          </p>
        </section>
        <section class="form-section">
          <h3>{t('swarm.profile.directoryHeading', 'Working directory')}</h3>
          <div class="directory-fields">
            <FormField
              controlId="swarm-directory-source"
              label={t('swarm.profile.directorySource', 'Source')}
            >
              <Dropdown
                id="swarm-directory-source"
                ariaLabel={t('swarm.profile.directorySource', 'Source')}
                value={draft.working_directory.kind}
                options={directoryOptions}
                disabled={busy}
                onValueChange={setDirectoryKind}
              />
            </FormField>
            {#if draft.working_directory.kind === 'project'}
              <FormField
                controlId="swarm-project"
                label={t('swarm.profile.project', 'Project')}
                required
              >
                <SearchableDropdown
                  id="swarm-project"
                  ariaLabel={t('swarm.profile.project', 'Project')}
                  value={draft.working_directory.project_id}
                  disabled={busy}
                  options={projects.map((project) => ({
                    value: project.id,
                    label: project.name,
                    secondaryLabel: project.cwd,
                  }))}
                  placeholder={t(
                    'swarm.profile.selectProject',
                    'Select a project',
                  )}
                  onValueChange={(value) =>
                    (draft.working_directory.project_id = value)}
                />
              </FormField>
            {:else}
              <FormField
                controlId="swarm-directory"
                label={t('swarm.profile.directory', 'Directory')}
                required
              >
                <TextField
                  id="swarm-directory"
                  value={draft.working_directory.path}
                  onInput={(value) => (draft.working_directory.path = value)}
                />
              </FormField>
            {/if}
          </div>
        </section>
        <details class="advanced">
          <summary
            >{t(
              'swarm.profile.chatShortcut',
              'Chat shortcut (optional)',
            )}</summary
          >
          <p class="hint">
            {t(
              'swarm.profile.shortcutHelp',
              'A shortcut is generated from the name when you save. Use it to choose this Swarm with /swarm from Chat.',
            )}
          </p>
          <FormField
            controlId="swarm-profile-slug"
            label={t('swarm.profile.shortcutName', 'Shortcut name')}
          >
            <TextField
              id="swarm-profile-slug"
              value={draft.slug ?? ''}
              placeholder={t(
                'swarm.profile.automaticShortcut',
                'Generated automatically',
              )}
              onInput={(value) => (draft.slug = value)}
            />
          </FormField>
          {#if draft.slug}<code>/swarm {draft.slug}</code>{/if}
        </details>
      </div>
      <div
        class="topic"
        hidden={tab !== 'prompt'}
        role="tabpanel"
        tabindex="0"
        id="swarm-profile-panel-prompt"
        aria-labelledby="swarm-profile-tab-prompt"
      >
        <FormField
          controlId="swarm-instructions"
          label={t('swarm.profile.instructions', 'Your instructions')}
        >
          <TextArea
            id="swarm-instructions"
            rows="14"
            value={draft.instructions}
            onInput={(value) => (draft.instructions = value)}
          />
        </FormField>
        <section class="form-section">
          <h3>
            {t(
              'swarm.profile.promptContributions',
              'Additional System Prompt content',
            )}
          </h3>
          <p class="hint">
            {t(
              'swarm.profile.promptSelectionHelp',
              'Only selected blocks are included. New blocks stay off until you enable them. Project context is independent of the working directory and Tool access.',
            )}
          </p>
          {#each promptBlocks as block (block.id)}
            {@const detail = previewCurrent
              ? preview.blocks.find((item) => item.id === block.id)
              : null}
            <div class="prompt-block">
              <div class="prompt-block-head">
                <span>{blockTitle(block.id)}</span>
                <Toggle
                  checked={draft.prompt_blocks.includes(block.id)}
                  ariaLabel={blockTitle(block.id)}
                  onChange={(enabled) => setPromptBlock(block.id, enabled)}
                />
              </div>
              <details>
                <summary
                  >{t('swarm.profile.inspectContent', 'Show content')}</summary
                >
                {#if detail}
                  {#if !detail.active}<p class="hint">
                      {t(
                        'swarm.profile.blockInactive',
                        'Unavailable for this configuration; not included.',
                      )}
                    </p>{/if}
                  <pre>{detail.text}</pre>
                {:else if block.text !== undefined}<pre>{block.text}</pre>
                {:else}<p class="hint">
                    {t(
                      'swarm.profile.dynamicPreview',
                      'Generate the preview to inspect this content for the selected Model and Project.',
                    )}
                  </p>{/if}
              </details>
            </div>
          {/each}
          {#each draft.prompt_blocks.filter((id) => !promptBlocks.some((block) => block.id === id)) as id (id)}
            <div class="prompt-block-head">
              <span
                >{blockTitle(id)} — {t(
                  'swarm.profile.blockUnavailable',
                  'Currently unavailable',
                )}</span
              >
              <Toggle
                checked={true}
                ariaLabel={blockTitle(id)}
                onChange={(enabled) => setPromptBlock(id, enabled)}
              />
            </div>
          {/each}
        </section>
        <section class="form-section">
          <h3>{t('swarm.profile.promptPreview', 'Combined System Prompt')}</h3>
          <div class="prompt-block-head">
            <Dropdown
              id="swarm-preview-model"
              value={previewFormation}
              options={draft.participants.map((row, index) => ({
                value: index,
                label: `${index + 1}: ${row.model || t('swarm.profile.selectModel', 'Select a model')}`,
              }))}
              ariaLabel={t('swarm.profile.previewModel', 'Preview Model')}
              onValueChange={(value) => (previewFormation = value)}
            />
            <Button
              variant="secondary"
              disabled={previewBusy}
              onClick={inspectPrompt}
              >{t('swarm.profile.generatePreview', 'Generate preview')}</Button
            >
          </div>
          {#if previewError}<Banner variant="error" role="alert"
              >{previewError}</Banner
            >{/if}
          {#if preview && !previewCurrent}<Banner
              >{t(
                'swarm.profile.previewStale',
                'Configuration changed. Generate a new preview to see the current prompt.',
              )}</Banner
            >{/if}
          {#if previewCurrent}
            <pre
              class="prompt-preview"
              data-testid="swarm-prompt-preview">{preview.text}</pre>
            <details>
              <summary
                >{t(
                  'swarm.profile.nativeTools',
                  'Tool definitions sent separately',
                )}</summary
              >
              {#each preview.tools as tool (tool.name)}
                <details>
                  <summary>{tool.name}</summary>
                  <pre>{JSON.stringify(tool, null, 2)}</pre>
                </details>
              {/each}
            </details>
          {/if}
        </section>
        <section class="form-section">
          <h3>{t('swarm.profile.reminders', 'Swarm reminders during work')}</h3>
          <p class="hint">
            {t(
              'swarm.profile.reminderHelp',
              'These event-triggered instructions are separate from the System Prompt. Turning one off removes its guidance; Board messages and lifecycle actions still work. Previously delivered text remains in an existing Session.',
            )}
          </p>
          {#each Object.entries(catalog.reminder_texts ?? {}) as [event, text] (event)}
            <div class="prompt-block">
              <div class="prompt-block-head">
                <span>{reminderTitles[event]}</span>
                <Toggle
                  checked={draft.reminders[event]}
                  ariaLabel={reminderTitles[event]}
                  onChange={(value) => (draft.reminders[event] = value)}
                />
              </div>
              <details>
                <summary
                  >{t('swarm.profile.inspectContent', 'Show content')}</summary
                >
                <pre>{text}</pre>
              </details>
            </div>
          {/each}
        </section>
      </div>
      <div
        class="topic"
        hidden={tab !== 'access'}
        role="tabpanel"
        tabindex="0"
        id="swarm-profile-panel-access"
        aria-labelledby="swarm-profile-tab-access"
      >
        <section class="form-section">
          <h3>{t('swarm.profile.tools', 'Tool access')}</h3>
          <p class="hint">
            {t(
              'swarm.profile.toolSelectionHelp',
              'Applies to every participant. All selects the currently available Tools; newly added Tools are not included automatically.',
            )}
          </p>
          <ToolAccessEditor
            value={draft.tool_access}
            memoryPromptMode="off"
            {tools}
            disabled={busy}
            onChange={setToolAccess}
          />
        </section>
        <section class="form-section">
          <h3>{t('swarm.profile.skillsHeading', 'Skills')}</h3>
          <ToggleChipList
            items={skillItems}
            emptyLabel={t('swarm.profile.noSkills', 'No Skills are available.')}
            note={draft.allowed_skills.includes('*')
              ? t(
                  'swarm.profile.allSkills',
                  'All current and future Skills are allowed.',
                )
              : ''}
            ariaToggleLabel={(name) =>
              t('swarm.profile.toggleSkill', 'Toggle Skill {name}', { name })}
            onToggle={setSkill}
            onSetAll={(next) => (draft.allowed_skills = next ? ['*'] : [])}
          />
        </section>
      </div>
      <div
        class="topic"
        hidden={tab !== 'communication'}
        role="tabpanel"
        tabindex="0"
        id="swarm-profile-panel-communication"
        aria-labelledby="swarm-profile-tab-communication"
      >
        <p class="hint">
          {t(
            'swarm.profile.communicationHelp',
            'Choose when participants receive Board messages and whether a new message starts work when they are idle.',
          )}
        </p>
        {#each routes as route (route.id)}
          <section class="form-section">
            <h3>{route.label}</h3>
            <FormField
              controlId={`swarm-delivery-${route.id}`}
              label={t('swarm.profile.receiveMessages', 'Receive messages')}
            >
              <Dropdown
                id={`swarm-delivery-${route.id}`}
                ariaLabel={t(
                  'swarm.profile.receiveMessages',
                  'Receive messages',
                )}
                value={draft.delivery[route.id].mode}
                options={deliveryOptions}
                disabled={busy}
                onValueChange={(value) =>
                  (draft.delivery[route.id].mode = value)}
              />
            </FormField>
            <div class="switch-row">
              <span
                >{t(
                  'swarm.profile.wakeIdle',
                  'Start work when a message arrives',
                )}</span
              >
              <Toggle
                checked={draft.delivery[route.id].wake_idle}
                disabled={busy}
                ariaLabel={t(
                  'swarm.profile.wakeRoute',
                  'Start idle participants for {route}',
                  { route: route.label },
                )}
                onChange={(value) =>
                  (draft.delivery[route.id].wake_idle = value)}
              />
            </div>
          </section>
        {/each}
        <details class="advanced">
          <summary
            >{t(
              'swarm.profile.advancedDelivery',
              'Advanced delivery settings',
            )}</summary
          >
          <div class="three">
            <FormField
              controlId="swarm-coalesce"
              label={t('swarm.profile.batchDelay', 'Batch delay (ms)')}
            >
              <TextField
                id="swarm-coalesce"
                type="number"
                min="0"
                max="5000"
                value={draft.delivery.coalesce_ms}
                onInput={(value) =>
                  (draft.delivery.coalesce_ms = Number(value))}
              />
            </FormField>
            <FormField
              controlId="swarm-batch-messages"
              label={t('swarm.delivery.batchMessages', 'Messages per batch')}
            >
              <TextField
                id="swarm-batch-messages"
                type="number"
                min="1"
                max="100"
                value={draft.delivery.batch_messages}
                onInput={(value) =>
                  (draft.delivery.batch_messages = Number(value))}
              />
            </FormField>
            <FormField
              controlId="swarm-batch-chars"
              label={t('swarm.delivery.batchChars', 'Characters per batch')}
            >
              <TextField
                id="swarm-batch-chars"
                type="number"
                min="16000"
                max="128000"
                value={draft.delivery.batch_chars}
                onInput={(value) =>
                  (draft.delivery.batch_chars = Number(value))}
              />
            </FormField>
          </div>
        </details>
      </div>
    </fieldset>
  </div>
  <footer class="editor-footer">
    <span class="management-save-note" aria-live="polite"
      >{saving
        ? t('common.saving', 'Saving…')
        : profile
          ? error
            ? t('swarm.profile.unsaved', 'Changes not saved')
            : t('management.savedAutomatically', 'Changes save automatically')
          : ''}</span
    >
    <div>
      {#if !profile}<Button disabled={busy} onClick={onCancel}
          >{t('common.cancel', 'Cancel')}</Button
        >{/if}
      <Button
        variant={profile ? 'secondary' : 'primary'}
        loading={saving}
        onClick={save}
      >
        {profile
          ? t('common.saveChanges', 'Save changes')
          : t('swarm.profile.save', 'Save Swarm')}
      </Button>
    </div>
  </footer>
</section>

{#if pendingTransition}
  <Modal
    title={t('autosave.transitionFailureTitle', 'Changes could not be saved')}
    closeDisabled={transitionSaving}
    onClose={() => (pendingTransition = null)}
  >
    {#snippet body()}<p class="transition-copy">
        {t(
          'autosave.transitionFailureBody',
          'Retry saving your changes, or discard them and continue.',
        )}
      </p>{/snippet}
    {#snippet footer()}
      <Button
        variant="primary"
        loading={transitionSaving}
        onClick={() => requestTransition(pendingTransition)}
        >{t('common.retry', 'Retry')}</Button
      >
      <Button disabled={transitionSaving} onClick={discardTransition}
        >{t('autosave.discardAndContinue', 'Discard and continue')}</Button
      >
    {/snippet}
  </Modal>
{/if}

<style>
  .editor {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    width: 100%;
  }
  .editor-head {
    padding: 24px 28px 20px;
  }
  .editor :global(.tab-list) {
    margin: 0 28px;
    flex-shrink: 0;
  }
  .editor :global(.banner) {
    margin: 14px 28px 0;
    flex-shrink: 0;
  }
  .transition-copy {
    margin: 0;
    padding: 0 20px 20px;
  }
  .editor-head,
  .section-head,
  .switch-row,
  .editor-footer,
  .editor-footer > div {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .editor-head,
  .editor-footer {
    flex-shrink: 0;
  }
  h2,
  h3,
  p {
    margin: 0;
  }
  h2 {
    font-size: var(--fs-display);
  }
  h3 {
    font-size: var(--fs-heading-sm);
    font-weight: 600;
  }
  .editor-head p,
  .hint,
  .editor-footer p {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .editor-head p {
    margin-top: 4px;
  }
  .editor-scroll {
    flex: 1;
    min-height: 0;
    overflow: auto;
    scrollbar-gutter: stable;
    padding: 24px 28px;
  }
  fieldset {
    border: 0;
    margin: 0;
    padding: 0;
    max-width: var(--content-max-narrow);
    margin-inline: auto;
    min-width: 0;
  }
  .topic {
    display: grid;
    gap: 28px;
  }
  .topic[hidden] {
    display: none;
  }
  .form-section {
    display: grid;
    gap: 14px;
  }
  .formation {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(280px, 0.9fr);
    align-items: start;
    gap: 16px;
    padding: 16px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
  }
  .formation-settings {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 80px auto;
    align-items: end;
    gap: 12px;
  }
  .formation-settings :global(.btn-icon) {
    justify-self: end;
  }
  .editor :global(.form-field__label) {
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
    letter-spacing: normal;
    text-transform: none;
    line-height: 1.4;
  }
  .directory-fields {
    display: grid;
    grid-template-columns: 160px minmax(0, 1fr);
    gap: 14px;
  }
  .three {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 14px;
  }
  .advanced {
    border-top: 1px solid var(--border);
    padding-top: 14px;
  }
  .prompt-block {
    border-bottom: 1px solid var(--border);
    padding: 12px 0;
  }
  .prompt-block-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 14px;
  }
  .prompt-block details {
    margin-top: 8px;
  }
  .topic pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 480px;
    overflow: auto;
    color: var(--text-hi);
    font: inherit;
  }
  .prompt-preview {
    background: var(--surface);
    padding: 14px;
    border: 1px solid var(--border-2);
    border-radius: 6px;
  }
  .advanced summary {
    cursor: pointer;
    color: var(--text-med);
  }
  .advanced summary:hover {
    color: var(--text-hi);
  }
  .advanced > :not(summary) {
    margin-top: 14px;
  }
  .editor-footer {
    border-top: 1px solid var(--border);
    padding: 12px 28px;
    background: var(--bg);
  }
  .editor-footer > div {
    flex-shrink: 0;
  }
  @media (max-width: 1200px) {
    .formation {
      grid-template-columns: 1fr;
    }
  }
  @media (max-width: 640px) {
    .editor-head {
      padding: 16px 16px 14px;
    }
    .editor :global(.tab-list) {
      margin-inline: 16px;
    }
    .editor-scroll {
      padding: 20px 16px;
    }
    .editor-footer {
      padding: 12px 16px;
    }
    .directory-fields,
    .three {
      grid-template-columns: 1fr;
    }
    .formation-settings {
      grid-template-columns: minmax(0, 1fr) 86px auto;
      gap: 8px;
    }
    .formation {
      grid-template-columns: 1fr;
      padding: 12px;
    }
    .editor-footer p {
      display: none;
    }
    .editor-footer {
      justify-content: flex-end;
    }
  }
</style>
