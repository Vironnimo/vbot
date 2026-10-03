<script>
  import { onDestroy, onMount, untrack } from 'svelte';

  import AgentModelSettings from '../agents/AgentModelSettings.svelte';
  import { formatSkillTime } from '../skills/skillRecords.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    getAgent,
    librarianOverview,
    listConnections,
    listModels,
    updateAgent,
  } from '$lib/api.js';
  import {
    AGENT_FORM_MODE_EDIT,
    createAgentFormValues,
    normalizeAgentForm,
  } from '$lib/agentForm.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import {
    LIBRARIAN_AGENT_ID,
    librarianMergeText,
    librarianProblemText,
    librarianTriggerText,
  } from '$lib/librarian.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';

  // Skill maintenance: the `librarian` settings section (the schedule, when
  // unused background-made Skills are retired, whether a pass merges
  // overlapping Skills), the Model settings of the Librarian, a hidden Agent
  // of its own that runs the merges (saved with `agent.update`), and its
  // recent passes over all Agents, each opening the Librarian Session of its
  // merge. A pass started by hand from the Skills manager uses the settings
  // even while the schedule is off.

  const noop = () => {};

  const LIBRARIAN_SETTING_DEFAULTS = Object.freeze({
    enabled: true,
    interval_days: 7,
    archive_after_days: 90,
    consolidate: true,
  });
  const SWITCH_FIELDS = ['enabled', 'consolidate'];
  const DAY_FIELDS = ['interval_days', 'archive_after_days'];
  const MIN_DAYS = 1;
  const MAX_DAYS = 3650;

  function validDays(value) {
    return Number.isInteger(value) && value >= MIN_DAYS && value <= MAX_DAYS;
  }

  function daysOr(value, fallback) {
    const parsed = Number(value);
    return validDays(parsed) ? parsed : fallback;
  }

  function getLibrarianSettings(rawSettings) {
    const librarian = rawSettings?.librarian ?? {};
    const result = {};
    for (const field of SWITCH_FIELDS)
      result[field] =
        typeof librarian[field] === 'boolean'
          ? librarian[field]
          : LIBRARIAN_SETTING_DEFAULTS[field];
    for (const field of DAY_FIELDS)
      result[field] = daysOr(
        librarian[field],
        LIBRARIAN_SETTING_DEFAULTS[field],
      );
    return result;
  }

  let {
    settings = null,
    agents = [],
    onCommit = noop,
    onError = noop,
    onOpenSession = noop,
    modelsRefreshToken = 0,
    agentsRefreshToken = 0,
    skillsRefreshToken = 0,
  } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let librarianSettings = $state(untrack(() => getLibrarianSettings(settings)));
  let saving = $state(false);
  const librarianDraft = createSettingsDraft({
    settings: untrack(() => settings),
    fromSettings: getLibrarianSettings,
    read: () => librarianSettings,
    write: (next) => (librarianSettings = next),
    toPayload: (values) => ({
      librarian: getLibrarianSettings({ librarian: values }),
    }),
  });

  let availableModels = $state([]);
  let availableConnections = $state([]);

  // The Librarian Agent and its Model draft. `overview` says whether it is
  // available; an Agent of the user that holds its id is never edited here.
  let overview = $state(null);
  let overviewError = $state('');
  let librarianAgent = $state(null);
  let agentLoadError = $state('');
  let agentForm = $state(createAgentFormValues({}));
  let agentBaseline = $state(createAgentFormValues({}));
  let agentFormErrors = $state({});
  let agentSaving = $state(false);
  let disposed = false;
  let overviewVersion = 0;
  let agentVersion = 0;

  let problem = $derived(
    typeof overview?.problem === 'string' ? overview.problem : '',
  );
  let passes = $derived(
    Array.isArray(overview?.passes)
      ? overview.passes.filter((pass) => pass && typeof pass === 'object')
      : [],
  );
  let runningName = $derived(agentName(overview?.running));
  let runningSessionId = $derived(
    typeof overview?.running_session_id === 'string'
      ? overview.running_session_id
      : '',
  );
  let effectiveConfig = $derived(
    librarianAgent?.effective && typeof librarianAgent.effective === 'object'
      ? librarianAgent.effective
      : {},
  );

  let saveDisabled = $derived(
    saving ||
      librarianSettingsMatch(librarianSettings, getLibrarianSettings(settings)),
  );
  const autosaveContext = useAutosaveContext();
  const librarianAutosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...librarianSettings }),
    hasChanges: () =>
      !librarianSettingsMatch(
        librarianSettings,
        getLibrarianSettings(settings),
      ),
    save: saveLibrarianSettings,
  });
  const agentAutosave = createDebouncedAutosave({
    getSnapshot: () => JSON.stringify(agentForm),
    hasChanges: agentHasChanges,
    save: saveLibrarianAgent,
  });
  const unregisterLibrarianAutosave = autosaveContext.register(
    librarianAutosave.participant,
  );
  const unregisterAgentAutosave = autosaveContext.register(
    agentAutosave.participant,
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    librarianAutosave.scheduleRun();

    return () => {
      librarianAutosave.cancelPendingTimer();
    };
  });

  $effect(() => {
    if (agentSaving || !agentHasChanges()) {
      return;
    }

    agentAutosave.scheduleRun();

    return () => {
      agentAutosave.cancelPendingTimer();
    };
  });

  onMount(() => {
    void loadModelCatalogs();
    void loadLibrarian();
  });
  onDestroy(() => {
    disposed = true;
    unregisterLibrarianAutosave();
    unregisterAgentAutosave();
    librarianAutosave.cancelPendingTimer();
    agentAutosave.cancelPendingTimer();
  });

  watchToken(
    () => modelsRefreshToken,
    () => void loadModelCatalogs(),
  );
  // A pass starts and ends as a Skills change.
  watchToken(
    () => skillsRefreshToken,
    () => void loadOverview(),
  );
  watchToken(
    () => agentsRefreshToken,
    () => void loadLibrarian(),
  );

  // Runs `reload` when the token changes after mount.
  function watchToken(read, reload) {
    let last = null;
    $effect(() => {
      const token = read();
      if (last === null) {
        last = token;
        return;
      }
      if (token !== last) {
        last = token;
        untrack(reload);
      }
    });
  }

  async function loadModelCatalogs() {
    try {
      const [modelsResult, connectionsResult] = await Promise.all([
        listModels(),
        listConnections(),
      ]);
      availableModels = modelsResult.models;
      availableConnections = connectionsResult.connections;
    } catch (error) {
      onError(`${t('settings.models.loadError')} ${error.message}`);
    }
  }

  async function loadOverview() {
    const current = ++overviewVersion;
    try {
      const result = await librarianOverview();
      if (disposed || current !== overviewVersion) return null;
      overview = result ?? {};
      overviewError = '';
      return overview;
    } catch (error) {
      if (!disposed && current === overviewVersion)
        overviewError = error.message;
      return null;
    }
  }

  // The overview first: while the Librarian is unavailable, `agent.get
  // librarian` may name an Agent of the user.
  async function loadLibrarian() {
    const current = ++agentVersion;
    const result = await loadOverview();
    if (disposed || current !== agentVersion || !result) return;
    if (typeof result.problem === 'string') {
      librarianAgent = null;
      return;
    }
    try {
      const agent = await getAgent(LIBRARIAN_AGENT_ID);
      if (disposed || current !== agentVersion) return;
      if (agent?.builtin !== 'librarian') {
        librarianAgent = null;
        return;
      }
      applyLibrarianAgent(agent);
      agentLoadError = '';
    } catch (error) {
      if (!disposed && current === agentVersion) agentLoadError = error.message;
    }
  }

  // The loaded or saved Agent becomes the baseline. It replaces the form
  // unless the form holds edits: edits since the load, or since `draft` was
  // sent to be saved.
  function applyLibrarianAgent(agent, draft = agentBaseline) {
    const replaceForm = formValuesMatch(agentForm, draft);
    librarianAgent = agent;
    agentBaseline = createAgentFormValues(agent);
    if (replaceForm) agentForm = createAgentFormValues(agent);
  }

  function formValuesMatch(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function agentChanges() {
    return normalizeAgentForm(agentForm, {
      mode: AGENT_FORM_MODE_EDIT,
      initialValues: agentBaseline,
    });
  }

  function agentHasChanges() {
    if (!librarianAgent) return false;
    const result = agentChanges();
    if (!result.isValid) return !formValuesMatch(agentForm, agentBaseline);
    return Object.keys(result.payload).some((field) => field !== 'id');
  }

  async function saveLibrarianAgent(reason) {
    if (!librarianAgent || agentSaving) return false;
    const result = agentChanges();
    if (reason !== 'auto' || result.isValid) agentFormErrors = result.errors;
    if (!result.isValid) {
      if (reason !== 'auto') onError(t('errors.validation'));
      return false;
    }
    if (!Object.keys(result.payload).some((field) => field !== 'id'))
      return true;
    const draft = JSON.parse(JSON.stringify(agentForm));
    agentSaving = true;
    try {
      const saved = await updateAgent({
        ...result.payload,
        id: LIBRARIAN_AGENT_ID,
      });
      if (disposed) return true;
      applyLibrarianAgent(
        saved && typeof saved === 'object'
          ? saved
          : { ...librarianAgent, ...result.payload },
        draft,
      );
      onError('');
      return true;
    } catch (error) {
      if (!disposed) onError(error?.message || t('agents.saveError'));
      return false;
    } finally {
      agentSaving = false;
    }
  }

  function inheritSource(fieldName) {
    const field = effectiveConfig[fieldName];
    return field && typeof field === 'object' ? (field.source ?? null) : null;
  }

  function inheritDisplayValue(fieldName) {
    const field = effectiveConfig[fieldName];
    const value = field && typeof field === 'object' ? field.value : null;
    return value === null || value === undefined ? '' : String(value);
  }

  function fieldError(fieldName) {
    return agentFormErrors[fieldName] ? t('errors.validation') : '';
  }

  function agentName(agentId) {
    if (typeof agentId !== 'string' || !agentId) return '';
    return agents.find((agent) => agent?.id === agentId)?.name || agentId;
  }

  function passAgentName(pass) {
    return pass.agent_name || agentName(pass.agent_id);
  }

  function passText(pass) {
    return [
      t('skills.librarian.passValue', {
        time: formatSkillTime(pass.finished_at),
        trigger: librarianTriggerText(pass),
      }),
      librarianMergeText(pass),
    ].join(' · ');
  }

  function librarianSettingsMatch(left, right) {
    const normalizedLeft = getLibrarianSettings({ librarian: left });
    const normalizedRight = getLibrarianSettings({ librarian: right });

    return [...SWITCH_FIELDS, ...DAY_FIELDS].every(
      (field) => normalizedLeft[field] === normalizedRight[field],
    );
  }

  function setSwitch(field, next) {
    librarianSettings = { ...librarianSettings, [field]: next };
    onError('');
  }

  function handleDaysInput(field, next) {
    if (next === '') {
      librarianSettings = { ...librarianSettings, [field]: next };
      onError('');
      return;
    }
    const numberValue = Number(next);
    if (validDays(numberValue)) {
      librarianSettings = { ...librarianSettings, [field]: numberValue };
      onError('');
    }
  }

  async function saveLibrarianSettings() {
    if (
      librarianSettingsMatch(librarianSettings, getLibrarianSettings(settings))
    ) {
      return true;
    }

    return librarianDraft.save({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
    });
  }

  function saveNow() {
    void librarianAutosave.participant.runSave('manual');
    void agentAutosave.participant.runSave('manual');
  }
</script>

<div class="s-group">
  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.librarian.enabled')}
        <InfoHint text={t('settings.librarian.enabledHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.librarian.enabledDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Toggle
        checked={librarianSettings.enabled === true}
        ariaLabel={t('settings.librarian.enabled')}
        onChange={(next) => setSwitch('enabled', next)}
      />
    </div>
  </div>

  <!-- The interval only matters while the schedule is on. The hidden row
       stays mounted so settings search still finds it. A pass started by
       hand uses the rows below, so they stay visible. -->
  <div class="s-row s-row--compact" hidden={librarianSettings.enabled !== true}>
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.librarian.interval')}
        <InfoHint text={t('settings.librarian.intervalHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.librarian.intervalDescription')}
      </div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-librarian-interval"
        type="number"
        min={MIN_DAYS}
        max={MAX_DAYS}
        step="1"
        value={librarianSettings.interval_days}
        ariaLabel={t('settings.librarian.interval')}
        onInput={(next) => handleDaysInput('interval_days', next)}
      />
    </div>
  </div>

  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.librarian.archiveAfter')}
        <InfoHint text={t('settings.librarian.archiveAfterHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.librarian.archiveAfterDescription')}
      </div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-librarian-archive-after"
        type="number"
        min={MIN_DAYS}
        max={MAX_DAYS}
        step="1"
        value={librarianSettings.archive_after_days}
        ariaLabel={t('settings.librarian.archiveAfter')}
        onInput={(next) => handleDaysInput('archive_after_days', next)}
      />
    </div>
  </div>

  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.librarian.consolidate')}
        <InfoHint text={t('settings.librarian.consolidateHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.librarian.consolidateDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Toggle
        checked={librarianSettings.consolidate === true}
        ariaLabel={t('settings.librarian.consolidate')}
        onChange={(next) => setSwitch('consolidate', next)}
      />
    </div>
  </div>
</div>

<div class="s-footer">
  <SaveStatus
    saving={saving || agentSaving}
    pending={librarianAutosave.participant.hasChanges() ||
      agentAutosave.participant.hasChanges()}
    onClick={saveNow}
  />
</div>

<div class="s-subhead">
  <h4 class="s-subhead__title">{t('settings.librarian.agent')}</h4>
  <p class="s-subhead__desc">{t('settings.librarian.agentDescription')}</p>
</div>

{#if problem}
  <Banner variant="warn">
    {t('skills.librarian.unavailable', {
      problem: librarianProblemText(problem),
    })}
  </Banner>
{:else if agentLoadError}
  <Banner variant="error"
    >{t('settings.librarian.agentLoadError')} {agentLoadError}</Banner
  >
{:else if librarianAgent}
  <AgentModelSettings
    bind:formValues={agentForm}
    {availableModels}
    {availableConnections}
    formErrors={agentFormErrors}
    {fieldError}
    {inheritSource}
    {inheritDisplayValue}
    idPrefix="settings-librarian"
  />
{/if}

<div class="s-subhead">
  <h4 class="s-subhead__title">{t('settings.librarian.passes')}</h4>
  <p class="s-subhead__desc">{t('settings.librarian.passesDescription')}</p>
</div>

{#if overviewError}
  <Banner variant="error"
    >{t('settings.librarian.passesError')} {overviewError}</Banner
  >
{:else if !overview}
  <Banner variant="neutral">{t('settings.librarian.passesLoading')}</Banner>
{:else}
  {#if runningName}
    <div class="s-row s-row--compact">
      <div class="s-row-info">
        <div class="s-row-desc">
          {t('settings.librarian.running', { name: runningName })}
        </div>
      </div>
      {#if runningSessionId}
        <div class="s-row-control">
          <Button
            variant="secondary"
            ariaLabel={t('settings.librarian.openRunningSessionLabel', {
              name: runningName,
            })}
            onClick={() => onOpenSession(LIBRARIAN_AGENT_ID, runningSessionId)}
            >{t('settings.librarian.openSession')}</Button
          >
        </div>
      {/if}
    </div>
  {/if}
  {#if passes.length === 0}
    <EmptyState
      density="compact"
      description={t('settings.librarian.passesEmpty')}
    />
  {:else}
    <div class="s-group">
      {#each passes as pass (`${pass.agent_id}:${pass.started_at}`)}
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <div class="s-row-label">{passAgentName(pass)}</div>
            <div class="s-row-desc">{passText(pass)}</div>
          </div>
          {#if typeof pass.session_id === 'string' && pass.session_id}
            <div class="s-row-control">
              <Button
                variant="secondary"
                ariaLabel={t('settings.librarian.openSessionLabel', {
                  name: passAgentName(pass),
                  time: formatSkillTime(pass.finished_at),
                })}
                onClick={() =>
                  onOpenSession(LIBRARIAN_AGENT_ID, pass.session_id)}
                >{t('settings.librarian.openSession')}</Button
              >
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
{/if}
