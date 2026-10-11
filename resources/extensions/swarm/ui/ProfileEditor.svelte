<script>
  // The Swarm editor: one configuration page for a reusable Swarm (a
  // profile) in the shared section grammar. Saved Swarms autosave through
  // the page bridge and show the shared save state at the end; a new Swarm
  // is created explicitly, and leaving it with changes asks first. The
  // section components edit the bound draft; this owner keeps the draft, its
  // validation and every save.
  import { onDestroy, tick, untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import CopyButton from '../../../../webui/src/components/ui/CopyButton.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import InfoHint from '../../../../webui/src/components/ui/InfoHint.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import PathField from '../../../../webui/src/components/ui/PathField.svelte';
  import SaveStatus from '../../../../webui/src/components/ui/SaveStatus.svelte';
  import SearchableDropdown from '../../../../webui/src/components/SearchableDropdown.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import Toggle from '../../../../webui/src/components/ui/Toggle.svelte';
  import CompactionPolicyEditor from '../../../../webui/src/components/compaction/CompactionPolicyEditor.svelte';
  import { createDebouncedAutosave } from '../../../../webui/src/lib/autosave.js';
  import {
    DEFAULT_COMPACTION_POLICY,
    buildCompactionPolicyPayload,
    normalizeCompactionPolicy,
  } from '../../../../webui/src/lib/compactionPolicy.js';
  import { buildModelSelectOptions } from '../../../../webui/src/lib/modelSelection.js';
  import DeliverySettings from './DeliverySettings.svelte';
  import ProfileAccess from './ProfileAccess.svelte';
  import ProfileInstructions from './ProfileInstructions.svelte';
  import ProfileParticipants from './ProfileParticipants.svelte';
  import {
    DELIVERY_LIMITS,
    deliveryLimitError,
    normalizeDelivery,
  } from './runPresentation.js';
  import './profileEditor.css';

  let {
    profile = null,
    catalog = {},
    bridgeClient,
    onSave,
    onCancel,
    onDelete,
  } = $props();

  // The `/swarm <shortcut>` names the backend accepts.
  const SHORTCUT_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;
  // The delivery fields' id prefix; a batching limit's field is
  // `${DELIVERY_ID}-${limit.id}` (DeliverySettings.svelte).
  const DELIVERY_ID = 'swarm-delivery';
  const deliveryFieldId = (limit) => `${DELIVERY_ID}-${limit.id}`;
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
    compaction_policy: null,
  };
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const snapshot = (value = draft) => {
    const result = copy(value);
    delete result.revision;
    return JSON.stringify(result);
  };

  let draft = $state(untrack(() => copy(profile ?? defaults)));
  let savedProfile = $state(untrack(() => copy(draft)));
  let savedSnapshot = $state(untrack(() => snapshot()));
  let saving = $state(false);
  // A new Swarm keeps its fields still while it is created.
  const busy = $derived(saving && !profile);
  let saveError = $state('');
  // Field errors appear once a save found one and then follow the draft.
  let showValidation = $state(false);
  let advancedOpen = $state(false);
  // A saved Swarm whose changes could not be saved on leaving.
  let pendingTransition = $state.raw(null);
  let transitionSaving = $state(false);
  // A new Swarm with changes the user is leaving.
  let leaveAction = $state.raw(null);
  let leaveSaving = $state(false);

  const hasChanges = () => snapshot() !== savedSnapshot;
  const fieldErrors = $derived(
    showValidation
      ? Object.fromEntries(
          validate(draft).map((issue) => [issue.id, issue.message]),
        )
      : {},
  );
  // A save failed on the draft's own settings rather than on the server.
  const incomplete = $derived(Object.keys(fieldErrors).length > 0);
  // The batching limits' field errors by limit field, as the delivery fields
  // show them.
  const deliveryErrors = $derived(
    Object.fromEntries(
      DELIVERY_LIMITS.filter(
        (limit) => fieldErrors[deliveryFieldId(limit)],
      ).map((limit) => [limit.field, fieldErrors[deliveryFieldId(limit)]]),
    ),
  );
  const describedBy = (id, help = '') =>
    [help, fieldErrors[id] ? `${id}-error` : ''].filter(Boolean).join(' ') ||
    undefined;

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

  // Every navigation away from the editor passes here (the page model's
  // `navigate`). A saved Swarm flushes its autosave first; a new Swarm with
  // changes asks whether to create it, discard it or keep editing.
  export async function requestTransition(action) {
    if (transitionSaving || leaveAction) return;
    transitionSaving = true;
    // A Swarm being created finishes first and then leaves like a saved one.
    const saved = profile || saving ? await autosave.participant.flush() : true;
    transitionSaving = false;
    if (!profile) {
      if (hasChanges()) leaveAction = action;
      else action();
      return;
    }
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
    saveError = '';
    showValidation = false;
    action?.();
  }

  // Incomplete settings cannot be saved by retrying; the user completes
  // them, starting at the first one.
  async function keepEditing() {
    pendingTransition = null;
    const [first] = validate(draft);
    if (first) await focusField(first.id);
  }

  function discardDraft() {
    const action = leaveAction;
    leaveAction = null;
    draft = copy(savedProfile);
    saveError = '';
    showValidation = false;
    action?.();
  }

  async function createAndLeave() {
    const action = leaveAction;
    const issues = validate(draft);
    if (issues.length) {
      leaveAction = null;
      showValidation = true;
      await focusField(issues[0].id);
      return;
    }
    leaveSaving = true;
    const created = await save();
    leaveSaving = false;
    if (!created) return;
    leaveAction = null;
    action?.();
  }

  function save() {
    autosave.cancelPendingTimer();
    return autosave.participant.runSave('manual', { force: true });
  }

  // Each problem a save would reject, in page order, keyed by its control.
  function validate(value) {
    const issues = [];
    if (!value.name.trim())
      issues.push({
        id: 'swarm-profile-name',
        message: t('swarm.profile.nameRequired'),
      });
    if (value.slug?.trim() && !SHORTCUT_PATTERN.test(value.slug))
      issues.push({
        id: 'swarm-profile-slug',
        message: t('swarm.profile.shortcutValidation'),
      });
    value.participants.forEach((row, index) => {
      if (!row.model)
        issues.push({
          id: `swarm-model-${index}`,
          message: t('swarm.profile.modelRequired'),
        });
      const count = Number(row.count);
      if (!Number.isInteger(count) || count < 1)
        issues.push({
          id: `swarm-count-${index}`,
          message: t('swarm.profile.countRequired'),
        });
    });
    const directory = value.working_directory;
    if (directory.kind === 'project' && !directory.project_id)
      issues.push({
        id: 'swarm-project',
        message: t('swarm.profile.projectRequired'),
      });
    if (directory.kind === 'directory' && !directory.path?.trim())
      issues.push({
        id: 'swarm-directory',
        message: t('swarm.profile.directoryRequired'),
      });
    for (const limit of normalizeDelivery(value.delivery).invalid)
      issues.push({
        id: deliveryFieldId(limit),
        message: deliveryLimitError(limit),
      });
    return issues;
  }

  async function focusField(id) {
    await tick();
    document.getElementById(id)?.focus();
  }

  function profilePayload() {
    const payload = copy(draft);
    payload.name = payload.name.trim();
    payload.participants = payload.participants.map((row) => ({
      ...row,
      count: Number(row.count),
    }));
    payload.delivery = normalizeDelivery(payload.delivery).value;
    if (!payload.slug?.trim()) delete payload.slug;
    if (payload.compaction_policy)
      payload.compaction_policy = buildCompactionPolicyPayload(
        payload.compaction_policy,
      );
    return payload;
  }

  // Saves the draft and reports whether it was saved. Only a save the user
  // asked for moves focus to the first invalid field; background autosave
  // shows the problems in place.
  async function persist(reason) {
    if (profile && !hasChanges()) return true;
    saveError = '';
    const issues = validate(draft);
    if (issues.length) {
      showValidation = true;
      if (reason === 'manual') await focusField(issues[0].id);
      return false;
    }
    const submittedSnapshot = snapshot();
    saving = true;
    try {
      const saved = await onSave(profilePayload());
      if (profile) {
        savedProfile = copy(saved);
        if (snapshot() === submittedSnapshot) draft = copy(saved);
        else draft.revision = saved.revision;
        savedSnapshot = snapshot(saved);
      }
      showValidation = false;
      return true;
    } catch (cause) {
      saveError = cause.message;
      return false;
    } finally {
      saving = false;
    }
  }

  const projects = $derived(catalog.projects ?? []);
  const selectedProject = $derived(
    draft.working_directory.kind === 'project'
      ? projects.find(
          (project) => project.id === draft.working_directory.project_id,
        )
      : null,
  );
  const directoryOptions = $derived([
    { value: 'directory', label: t('swarm.profile.directoryOption') },
    { value: 'project', label: t('swarm.profile.projectOption') },
  ]);
  const projectOptions = $derived(
    projects.map((project) => ({
      value: project.id,
      label: project.name,
      secondaryLabel: project.cwd,
    })),
  );

  // Team agents belong to the selected Project; the backend rejects others.
  function keepTeamAgents(project) {
    const known = new Set(
      (project?.agent_profiles ?? []).map((agent) => agent.id),
    );
    for (const row of draft.participants)
      if (row.repository_profile && !known.has(row.repository_profile))
        delete row.repository_profile;
  }

  function setDirectoryKind(kind) {
    if (kind === draft.working_directory.kind) return;
    draft.working_directory =
      kind === 'project' ? { kind, project_id: '' } : { kind, path: '' };
    keepTeamAgents(null);
  }

  function selectProject(id) {
    draft.working_directory.project_id = id;
    keepTeamAgents(projects.find((project) => project.id === id));
  }

  // Absent or null inherits the global Compaction settings for every
  // participant.
  const compactionPolicy = $derived(
    draft.compaction_policy
      ? normalizeCompactionPolicy(draft.compaction_policy)
      : null,
  );
  const summaryModelOptions = $derived(
    buildModelSelectOptions({
      models: catalog.models ?? [],
      modelOnly: true,
      selectedModelValue: compactionPolicy?.strategy.summary_model ?? '',
      emptyLabel: t('swarm.profile.summaryModelDefault'),
    }),
  );

  function setCustomCompaction(enabled) {
    draft.compaction_policy = enabled ? copy(DEFAULT_COMPACTION_POLICY) : null;
  }

  function selectSummaryModel(value) {
    draft.compaction_policy = {
      ...compactionPolicy,
      strategy: { ...compactionPolicy.strategy, summary_model: value || null },
    };
  }
</script>

<section
  class="swarm-profile-editor page-scroll"
  aria-label={t('swarm.profile.editorLabel')}
>
  <header class="view-header swarm-profile-editor__header">
    <div class="view-header__intro">
      <h2 class="view-header__title">
        {profile ? savedProfile.name : t('swarm.profile.new')}
      </h2>
      <p class="view-header__subtitle">
        {profile ? t('swarm.profile.scopeHelp') : t('swarm.profile.newHelp')}
      </p>
    </div>
  </header>

  <fieldset class="swarm-profile-editor__fields" disabled={busy}>
    <section class="s-section" aria-labelledby="swarm-section-swarm">
      <header class="s-section__head">
        <h3 class="s-section__title" id="swarm-section-swarm">
          {t('swarm.profile')}
        </h3>
      </header>
      <div class="s-section__body">
        <div class="s-group">
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="swarm-profile-name">
                {t('swarm.profile.name')}
              </label>
              {#if fieldErrors['swarm-profile-name']}
                <p
                  class="swarm-profile-editor__error"
                  id="swarm-profile-name-error"
                  role="alert"
                >
                  {fieldErrors['swarm-profile-name']}
                </p>
              {/if}
            </div>
            <div class="s-row-control">
              <TextField
                id="swarm-profile-name"
                value={draft.name}
                maxlength="120"
                aria-required="true"
                invalid={Boolean(fieldErrors['swarm-profile-name'])}
                aria-describedby={describedBy('swarm-profile-name')}
                onInput={(value) => (draft.name = value)}
              />
            </div>
          </div>
          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label swarm-profile-editor__label">
                <label for="swarm-profile-slug">
                  {t('swarm.profile.chatShortcut')}
                </label>
                <InfoHint text={t('swarm.profile.shortcutHelp')} />
              </div>
              {#if fieldErrors['swarm-profile-slug']}
                <p
                  class="swarm-profile-editor__error"
                  id="swarm-profile-slug-error"
                  role="alert"
                >
                  {fieldErrors['swarm-profile-slug']}
                </p>
              {/if}
            </div>
            <div class="s-row-control">
              <TextField
                id="swarm-profile-slug"
                code
                value={draft.slug ?? ''}
                placeholder={t('swarm.profile.automaticShortcut')}
                spellcheck="false"
                autocomplete="off"
                invalid={Boolean(fieldErrors['swarm-profile-slug'])}
                aria-describedby={describedBy('swarm-profile-slug')}
                onInput={(value) => (draft.slug = value)}
              />
            </div>
          </div>
        </div>
      </div>
    </section>

    <ProfileParticipants bind:draft {catalog} {fieldErrors} />

    <section class="s-section" aria-labelledby="swarm-section-directory">
      <header class="s-section__head">
        <h3 class="s-section__title" id="swarm-section-directory">
          {t('swarm.profile.directoryHeading')}
        </h3>
      </header>
      <p class="s-section__desc">{t('swarm.profile.directoryHelp')}</p>
      <div class="s-section__body">
        <div class="s-group">
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="swarm-directory-source">
                {t('swarm.profile.directorySource')}
              </label>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="swarm-directory-source"
                value={draft.working_directory.kind}
                options={directoryOptions}
                ariaLabel={t('swarm.profile.directorySource')}
                onValueChange={setDirectoryKind}
              />
            </div>
          </div>
          {#if draft.working_directory.kind === 'project'}
            <div class="s-row">
              <div class="s-row-info">
                <label class="s-row-label" for="swarm-project">
                  {t('swarm.profile.project')}
                </label>
                {#if selectedProject}
                  <div class="s-row-desc" id="swarm-project-help">
                    <code>{selectedProject.cwd}</code>
                  </div>
                {/if}
                {#if fieldErrors['swarm-project']}
                  <p
                    class="swarm-profile-editor__error"
                    id="swarm-project-error"
                    role="alert"
                  >
                    {fieldErrors['swarm-project']}
                  </p>
                {/if}
              </div>
              <div class="s-row-control">
                <SearchableDropdown
                  id="swarm-project"
                  value={draft.working_directory.project_id}
                  options={projectOptions}
                  placeholder={t('swarm.profile.selectProject')}
                  ariaLabel={t('swarm.profile.project')}
                  ariaDescribedby={describedBy(
                    'swarm-project',
                    selectedProject ? 'swarm-project-help' : '',
                  )}
                  onValueChange={selectProject}
                />
              </div>
            </div>
          {:else}
            <div class="s-row s-row--stacked swarm-profile-editor__path-row">
              <div class="s-row-info">
                <label class="s-row-label" for="swarm-directory">
                  {t('swarm.profile.directory')}
                </label>
                {#if fieldErrors['swarm-directory']}
                  <p
                    class="swarm-profile-editor__error"
                    id="swarm-directory-error"
                    role="alert"
                  >
                    {fieldErrors['swarm-directory']}
                  </p>
                {/if}
              </div>
              <PathField
                id="swarm-directory"
                mode="directory"
                listDirectory={bridgeClient.listDirectory}
                value={draft.working_directory.path}
                invalid={Boolean(fieldErrors['swarm-directory'])}
                aria-describedby={describedBy('swarm-directory')}
                onInput={(value) => (draft.working_directory.path = value)}
              />
            </div>
          {/if}
        </div>
      </div>
    </section>

    <ProfileInstructions
      bind:draft
      {catalog}
      {bridgeClient}
      {snapshot}
      payload={profilePayload}
    />

    <ProfileAccess bind:draft {catalog} />

    <section class="s-section" aria-labelledby="swarm-section-communication">
      <header class="s-section__head">
        <h3 class="s-section__title" id="swarm-section-communication">
          {t('swarm.profile.communication')}
        </h3>
      </header>
      <p class="s-section__desc">{t('swarm.profile.communicationHelp')}</p>
      <div class="s-section__body">
        <DeliverySettings
          value={draft.delivery}
          idPrefix={DELIVERY_ID}
          errors={deliveryErrors}
          onChange={(next) => (draft.delivery = next)}
        />
      </div>
    </section>

    <section class="s-section" aria-labelledby="swarm-section-compaction">
      <header class="s-section__head">
        <h3 class="s-section__title" id="swarm-section-compaction">
          {t('swarm.profile.compactionHeading')}
        </h3>
      </header>
      <div class="s-section__body">
        <div class="s-group">
          <div class="s-row s-row--compact">
            <div class="s-row-info">
              <div class="s-row-label" id="swarm-compaction-custom-label">
                {t('swarm.profile.customCompaction')}
              </div>
              <div class="s-row-desc" id="swarm-compaction-custom-help">
                {compactionPolicy
                  ? t('swarm.profile.compactionCustomHelp')
                  : t('swarm.profile.compactionInheritHelp')}
              </div>
            </div>
            <div class="s-row-control">
              <Toggle
                checked={compactionPolicy !== null}
                aria-labelledby="swarm-compaction-custom-label"
                aria-describedby="swarm-compaction-custom-help"
                onChange={setCustomCompaction}
              />
            </div>
          </div>
          {#if compactionPolicy}
            <CompactionPolicyEditor
              layout="rows"
              value={draft.compaction_policy}
              idPrefix="swarm-compaction"
              {summaryModelOptions}
              summaryModelSelectValue={compactionPolicy.strategy
                .summary_model ?? ''}
              onSummaryModelSelect={selectSummaryModel}
              onChange={(next) => (draft.compaction_policy = next)}
            />
          {/if}
        </div>
      </div>
    </section>

    {#if profile}
      <details
        class="s-section swarm-profile-editor__advanced"
        bind:open={advancedOpen}
      >
        <summary class="s-section__head swarm-profile-editor__advanced-summary">
          <span
            class="disclosure-chevron"
            class:disclosure-chevron--open={advancedOpen}
            aria-hidden="true"
          ></span>
          <h3 class="s-section__title">{t('swarm.profile.advanced')}</h3>
        </summary>
        <div class="s-section__body">
          <div class="s-group">
            <div class="s-row s-row--compact">
              <div class="s-row-info">
                <div class="s-row-label">{t('swarm.profile.id')}</div>
              </div>
              <div class="s-row-control swarm-profile-editor__id">
                <span class="swarm-profile-editor__id-value"
                  >{savedProfile.id}</span
                >
                <CopyButton
                  text={savedProfile.id}
                  label={t('swarm.profile.copyId')}
                />
              </div>
            </div>
          </div>
          <div class="s-group">
            <div class="s-row s-row--compact">
              <div class="s-row-info">
                <div class="s-row-label">{t('swarm.profile.deleteTitle')}</div>
                <div class="s-row-desc">{t('swarm.profile.deleteHelp')}</div>
              </div>
              <div class="s-row-control">
                <!-- Waits for a running autosave, so the dialog confirms
                     the saved revision. -->
                <Button
                  variant="danger"
                  disabled={saving}
                  onClick={() => onDelete(savedProfile)}
                  >{t('swarm.profile.deleteAction')}</Button
                >
              </div>
            </div>
          </div>
        </div>
      </details>
    {/if}
  </fieldset>

  {#if saveError}
    <Banner
      variant="error"
      role="alert"
      class="swarm-profile-editor__save-error">{saveError}</Banner
    >
  {/if}
  <footer class="swarm-profile-editor__footer">
    {#if profile}
      <!-- Saved Swarms autosave; their save state works like Settings. -->
      <SaveStatus {saving} pending={hasChanges()} onClick={save} />
    {:else}
      <Button disabled={busy} onClick={onCancel}>{t('common.cancel')}</Button>
      <Button variant="primary" loading={saving} onClick={save}
        >{t('swarm.profile.create')}</Button
      >
    {/if}
  </footer>
</section>

{#if pendingTransition}
  <Modal
    title={t('autosave.transitionFailureTitle')}
    closeDisabled={transitionSaving}
    onClose={() => (pendingTransition = null)}
  >
    {#snippet body()}
      <div class="modal-body">
        <p>
          {incomplete
            ? t('swarm.profile.leaveIncompleteBody')
            : t('autosave.transitionFailureBody')}
        </p>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button disabled={transitionSaving} onClick={discardTransition}
        >{t('autosave.discardAndContinue')}</Button
      >
      {#if incomplete}
        <Button variant="primary" onClick={keepEditing}
          >{t('swarm.profile.keepEditing')}</Button
        >
      {:else}
        <Button
          variant="primary"
          loading={transitionSaving}
          onClick={() => requestTransition(pendingTransition)}
          >{t('common.retry')}</Button
        >
      {/if}
    {/snippet}
  </Modal>
{/if}

{#if leaveAction}
  <Modal
    title={t('swarm.profile.leaveTitle')}
    closeDisabled={leaveSaving}
    onClose={() => (leaveAction = null)}
  >
    {#snippet body()}
      <div class="modal-body">
        <p>{t('swarm.profile.leaveBody')}</p>
        {#if saveError}
          <Banner variant="error" role="alert">{saveError}</Banner>
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button disabled={leaveSaving} onClick={() => (leaveAction = null)}
        >{t('swarm.profile.keepEditing')}</Button
      >
      <Button variant="danger" disabled={leaveSaving} onClick={discardDraft}
        >{t('common.discard')}</Button
      >
      <Button variant="primary" loading={leaveSaving} onClick={createAndLeave}
        >{t('swarm.profile.create')}</Button
      >
    {/snippet}
  </Modal>
{/if}
