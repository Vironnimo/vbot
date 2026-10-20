<script>
  // The Agent jobs of one calendar event: the Cron jobs bound to it as a
  // compact list (when each runs relative to the event, its Agent and state;
  // the instruction, next Run and last result in its details card), each with
  // a row menu to open its Session, edit or delete it. Adding or editing a job
  // opens its own dialog above the event's card; an existing job saves itself
  // while it is edited, a new one when it is added.
  import { onDestroy, onMount } from 'svelte';
  import Button from './ui/Button.svelte';
  import Banner from './ui/Banner.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import ContextMenu from './ui/ContextMenu.svelte';
  import { contextMenuAnchor, isContextMenuKey } from './ui/contextMenu.js';
  import FormField from './ui/FormField.svelte';
  import InfoHint from './ui/InfoHint.svelte';
  import Modal from './ui/Modal.svelte';
  import SaveStatus from './ui/SaveStatus.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import TextArea from './ui/TextArea.svelte';
  import TextField from './ui/TextField.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { portal } from '$lib/dropdownPanel.js';
  import { tooltip } from '$lib/tooltip.js';
  import Dropdown from './Dropdown.svelte';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import {
    createCronJob,
    updateCronJob,
    deleteCronJob,
    listAgents,
    listProjects,
    showProject,
    listSessions,
  } from '$lib/api.js';
  import {
    buildAgentTargetOptions,
    createAgentTargetCatalogLoader,
  } from '$lib/agentTargetOptions.js';
  import { eventTimeFields, eventTimeText } from '$lib/cronView.js';
  import { eventJobDueAt, eventJobs } from '$lib/calendarView.js';
  import {
    compactTimestamp,
    eventDirectionOptions,
    eventEdgeOptions,
    eventTimeLabel,
    eventUnitOptions,
    outcomeLabel,
    statusChipVariant,
    statusLabel,
  } from './cron/presentation.js';

  let {
    eventId,
    // The shown occurrence: its due times are listed.
    occurrence,
    // Every Agent job (Cron jobs of schedule type event); this event's show.
    jobs = [],
    // Why the jobs could not be read, or ''.
    jobsError = '',
    timeZone = 'UTC',
    serverUnavailable = false,
    // Bumped when the Agents change, such as a new name.
    agentsRefreshToken = 0,
    onChanged = () => {},
    onOpenSession = null,
  } = $props();
  let options = $state([]);
  // The target catalog the options show. An Agent change rereads only the
  // Agents; reads are numbered, and a response older than the shown Agents is
  // dropped, also when a newer read failed.
  let targetAgents = [];
  let targetTeams = [];
  let agentsRequested = 0;
  let agentsShown = 0;
  let lastAgentsRefreshToken = null;
  let sessions = $state([]);
  let sessionCursor = $state(null);
  let sessionsLoading = $state(false);
  let sessionRequest = 0;
  // The job in the dialog: null while it is closed, without an id while it
  // is being added.
  let editor = $state(null);
  // Why the Agents could not be listed; shown with the list.
  let catalogError = $state('');
  // Why the job in the dialog could not be saved; shown in the dialog.
  let error = $state('');
  let busy = $state(false);
  let saving = $state(false);
  let baseline = $state('');
  // The job whose deletion waits for confirmation, and why it failed.
  let deleting = $state(null);
  let deleteError = $state('');
  // The open row menu (./ui/ContextMenu.svelte), or null.
  let menu = $state(null);
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => ({ eventId, ...editor }),
    hasChanges: () => Boolean(editor?.id) && fingerprint(editor) !== baseline,
    save,
  });
  const unregister = autosaveContext.register(autosave.participant);
  $effect(() => {
    if (saving || serverUnavailable || !autosave.participant.hasChanges())
      return;
    autosave.scheduleRun();
    return autosave.cancelPendingTimer;
  });
  onDestroy(() => {
    unregister();
    autosave.cancelPendingTimer();
  });

  function fingerprint(values) {
    return JSON.stringify(
      values && {
        id: values.id,
        target: values.target,
        prompt: values.prompt.trim(),
        session: values.session || '',
        event_time: eventTimeText(values),
      },
    );
  }

  let shownJobs = $derived(eventJobs(jobs, eventId));
  // A stored target or Session that is no longer listed stays selectable under
  // its raw id, so opening an older job never silently changes it.
  let targetOptions = $derived(
    editor?.target && !options.some((option) => option.value === editor.target)
      ? [{ value: editor.target, label: editor.target }, ...options]
      : options,
  );
  let sessionOptions = $derived([
    {
      value: '',
      label: t('calendar.jobs.newSession'),
    },
    ...(editor?.session &&
    !sessions.some((session) => session.id === editor.session)
      ? [{ value: editor.session, label: editor.session }]
      : []),
    ...sessions.map((session) => ({
      value: session.id,
      label: session.title || session.auto_title || session.id,
    })),
  ]);
  const directionOptions = eventDirectionOptions();
  const unitOptions = eventUnitOptions();
  const edgeOptions = eventEdgeOptions();

  const targetCatalog = createAgentTargetCatalogLoader({
    listAgents,
    listProjects,
    showProject,
  });
  onMount(() => {
    async function loadTargets() {
      const agentsRequest = ++agentsRequested;
      const catalog = await targetCatalog.load();
      if (!catalog) return;
      targetTeams = catalog.projectTeams;
      showTargets(agentsRequest, catalog.agents);
      const failure = catalog.agentError ?? catalog.projectError;
      catalogError = failure
        ? (failure.message ?? String(failure))
        : catalog.failedProjects.length
          ? t('calendar.jobs.targetsPartial')
          : '';
    }
    loadTargets();
    return () => {
      targetCatalog.dispose();
      sessionRequest += 1;
    };
  });

  $effect(() => {
    const token = agentsRefreshToken;
    if (lastAgentsRefreshToken === null) {
      lastAgentsRefreshToken = token;
      return;
    }
    if (token === lastAgentsRefreshToken) return;
    lastAgentsRefreshToken = token;
    void reloadAgents();
  });

  async function reloadAgents() {
    const request = ++agentsRequested;
    try {
      const result = await listAgents();
      showTargets(request, result?.agents);
    } catch {
      // The shown names stay until the next change.
    }
  }

  // Shows the target options with `agents` unless newer Agents are shown.
  function showTargets(request, agents) {
    if (request >= agentsShown) {
      agentsShown = request;
      targetAgents = agents;
    }
    options = buildAgentTargetOptions(targetAgents, targetTeams);
  }

  function agentLabel(target) {
    return options.find((option) => option.value === target)?.label ?? target;
  }

  // A new job runs 30 minutes before the event starts.
  function begin(job = null) {
    return autosaveContext.requestTransition(() => beginNow(job));
  }

  function beginNow(job) {
    const time = job
      ? eventTimeFields(job.event_edge, job.event_offset_minutes)
      : eventTimeFields('start', -30);
    editor = {
      id: job?.id ?? '',
      target: job?.target ?? options[0]?.value ?? '',
      prompt: job?.prompt ?? '',
      session: job?.session_id ?? '',
      ...time,
    };
    baseline = fingerprint(editor);
    error = '';
    loadSessions();
  }

  // Closing the dialog keeps an existing job's last edit: the pending save
  // runs first. A job being added is discarded.
  function close() {
    return autosaveContext.requestTransition(() => {
      editor = null;
      error = '';
      sessionRequest += 1;
    });
  }

  async function loadSessions(append = false) {
    const target = editor?.target;
    const request = ++sessionRequest;
    if (!append) {
      sessions = [];
      sessionCursor = null;
    }
    if (!target) return;
    sessionsLoading = true;
    try {
      const result = await listSessions(target, {
        limit: 50,
        ...(append && sessionCursor ? { cursor: sessionCursor } : {}),
        ...(editor.session
          ? { requiredSession: { agentId: target, sessionId: editor.session } }
          : {}),
      });
      if (request !== sessionRequest) return;
      const rows = result.sessions ?? [];
      sessions = append
        ? [
            ...sessions,
            ...rows.filter(
              (row) => !sessions.some((item) => item.id === row.id),
            ),
          ]
        : rows;
      sessionCursor = result.next_cursor ?? null;
    } catch (e) {
      if (request === sessionRequest) error = e.message ?? String(e);
    } finally {
      if (request === sessionRequest) sessionsLoading = false;
    }
  }

  async function save() {
    if (!editor || busy || saving) return false;
    const eventTime = eventTimeText(editor);
    if (!editor.target || !editor.prompt.trim() || !eventTime) {
      error = t('calendar.jobs.required');
      return false;
    }
    const submitted = { ...editor };
    const submittedEventId = eventId;
    const fields = {
      agent_id: editor.target,
      prompt: editor.prompt.trim(),
      event_time: eventTime,
    };
    if (submitted.id) saving = true;
    else busy = true;
    error = '';
    try {
      if (submitted.id) {
        await updateCronJob({
          id: submitted.id,
          ...fields,
          session_id: submitted.session || null,
        });
      } else {
        await createCronJob({
          ...fields,
          schedule_type: 'event',
          event_id: submittedEventId,
          ...(submitted.session ? { session_id: submitted.session } : {}),
        });
      }
      if (editor?.id === submitted.id && eventId === submittedEventId) {
        if (submitted.id) baseline = fingerprint(submitted);
        else editor = null;
      }
      await onChanged();
      return true;
    } catch (e) {
      error = e.message ?? String(e);
      return false;
    } finally {
      busy = false;
      saving = false;
    }
  }

  function requestRemove(job) {
    return autosaveContext.requestTransition(() => {
      deleteError = '';
      deleting = job;
    });
  }

  async function removeNow() {
    const id = deleting?.id;
    if (!id || busy) return;
    busy = true;
    deleteError = '';
    try {
      await deleteCronJob(id);
      deleting = null;
      if (editor?.id === id) editor = null;
      await onChanged();
    } catch (e) {
      deleteError = e.message ?? String(e);
    } finally {
      busy = false;
    }
  }

  // A job is named by when it runs; its instruction can be long.
  function jobName(job) {
    return eventTimeLabel(job.event_edge, job.event_offset_minutes);
  }

  function jobMenu(job) {
    const blocked = busy || serverUnavailable;
    return {
      label: t('calendar.jobs.actions', { name: jobName(job) }),
      items: [
        ...(job.session_id && onOpenSession
          ? [
              {
                id: 'session',
                label: t('calendar.jobs.openSession'),
                group: 'open',
                onSelect: () =>
                  autosaveContext.requestTransition(() =>
                    onOpenSession(job.target, job.session_id),
                  ),
              },
            ]
          : []),
        {
          id: 'edit',
          label: t('common.edit'),
          group: 'open',
          disabled: blocked,
          onSelect: () => begin(job),
        },
        {
          id: 'delete',
          label: t('calendar.jobs.delete'),
          danger: true,
          group: 'delete',
          disabled: blocked,
          onSelect: () => requestRemove(job),
        },
      ],
    };
  }

  function openMenu(job, event) {
    event.preventDefault();
    menu = { ...contextMenuAnchor(event), ...jobMenu(job) };
  }

  // When the job is due for the shown occurrence: the time alone on the
  // occurrence's own day, else with its date.
  function dueText(job) {
    const due = occurrence ? eventJobDueAt(job, occurrence) : null;
    if (!due) return '';
    const sameDay =
      dayInZone(due) === dayInZone(new Date(occurrence.start_utc));
    return new Intl.DateTimeFormat(activeLocaleTag(), {
      timeZone,
      ...(sameDay ? {} : { weekday: 'short', day: 'numeric', month: 'short' }),
      hour: 'numeric',
      minute: '2-digit',
    }).format(due);
  }

  function dayInZone(date) {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(date);
  }

  // The next Run and the last result, in the server zone.
  function runText(job) {
    const parts = [
      job.next_fire_at
        ? t('calendar.jobs.next', {
            time: compactTimestamp(job.next_fire_at, timeZone),
          })
        : t('calendar.jobs.noNext'),
    ];
    if (job.last_outcome) {
      const at = job.last_completed_at || job.last_fired_at;
      parts.push(
        at
          ? t('calendar.jobs.lastAt', {
              outcome: outcomeLabel(job.last_outcome),
              time: compactTimestamp(at, timeZone),
            })
          : t('calendar.jobs.last', {
              outcome: outcomeLabel(job.last_outcome),
            }),
      );
    }
    return parts.join(' · ');
  }

  // A row's details card: the whole instruction and how the job runs.
  function jobDetails(job) {
    return {
      title: eventTimeLabel(job.event_edge, job.event_offset_minutes),
      text: job.prompt,
      rows: [
        { label: t('calendar.jobs.agent'), value: agentLabel(job.target) },
        { label: t('calendar.jobs.runs'), value: runText(job) },
      ],
    };
  }
</script>

<section class="calendar-jobs" aria-labelledby="calendar-jobs-heading">
  <div class="calendar-jobs-heading">
    <h3 id="calendar-jobs-heading">{t('calendar.jobs.heading')}</h3>
    <InfoHint text={t('calendar.jobs.help')} />
  </div>
  {#if jobsError}
    <Banner variant="error">{t('calendar.jobs.loadError')} {jobsError}</Banner>
  {:else if shownJobs.length === 0}
    <p class="calendar-jobs-empty">{t('calendar.jobs.empty')}</p>
  {/if}
  {#if shownJobs.length > 0}
    <ul class="calendar-job-list">
      {#each shownJobs as job (job.id)}
        {@const due = dueText(job)}
        <li
          class="calendar-job-row"
          class:calendar-job-row--inactive={job.status !== 'active'}
          data-testid={`calendar-job-${job.id}`}
        >
          <button
            type="button"
            class="calendar-job-main"
            use:tooltip={() => jobDetails(job)}
            onclick={() => {
              if (!busy && !serverUnavailable) begin(job);
            }}
            oncontextmenu={(event) => openMenu(job, event)}
            onkeydown={(event) => {
              if (isContextMenuKey(event)) openMenu(job, event);
            }}
          >
            <span class="calendar-job-summary">
              <strong
                >{eventTimeLabel(
                  job.event_edge,
                  job.event_offset_minutes,
                )}</strong
              >
              {#if due}<span class="calendar-job-due">{due}</span>{/if}
            </span>
            <span class="calendar-job-agent"
              >{agentLabel(job.target)} · {job.prompt}</span
            >
            {#if job.last_error}
              <span class="calendar-job-error">
                {t('calendar.jobs.error', { reason: job.last_error })}
              </span>
            {/if}
          </button>
          <StatusChip variant={statusChipVariant(job)}
            >{statusLabel(job.status)}</StatusChip
          >
          <Button
            variant="tertiary"
            icon
            class="calendar-job-menu"
            ariaLabel={t('calendar.jobs.actions', { name: jobName(job) })}
            aria-haspopup="menu"
            onClick={(event) => openMenu(job, event)}
            ><svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="currentColor"
              aria-hidden="true"
              ><circle cx="3.5" cy="8" r="1.25" /><circle
                cx="8"
                cy="8"
                r="1.25"
              /><circle cx="12.5" cy="8" r="1.25" /></svg
            ></Button
          >
        </li>
      {/each}
    </ul>
  {/if}
  {#if catalogError}
    <p class="calendar-jobs-notice" role="status">{catalogError}</p>
  {/if}
  <div>
    <Button
      variant="tertiary"
      class="calendar-jobs-add"
      disabled={busy || serverUnavailable || editor !== null}
      onClick={() => begin()}
      ><svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"
        ><path
          d="M8 3.25v9.5M3.25 8h9.5"
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
        /></svg
      >{t('calendar.jobs.add')}</Button
    >
  </div>
</section>

<ContextMenu {menu} onClose={() => (menu = null)} />

<!-- The dialogs leave the event's card, which floats on its own layer. -->
{#if editor || deleting}
  <div class="calendar-job-dialogs" use:portal>
    {#if editor}
      <Modal
        title={editor.id
          ? t('calendar.jobs.editTitle')
          : t('calendar.jobs.add')}
        description={occurrence?.recurring
          ? t('calendar.jobs.leadSeries', { title: occurrence?.title ?? '' })
          : t('calendar.jobs.lead', { title: occurrence?.title ?? '' })}
        size="md"
        class="calendar-job-dialog"
        onClose={close}
      >
        {#snippet body()}
          <form
            class="calendar-job-editor"
            onsubmit={(event) => {
              event.preventDefault();
              if (editor.id) autosave.participant.runSave('manual');
              else save();
            }}
          >
            <div class="modal-body">
              <FormField
                label={t('calendar.jobs.agent')}
                controlId="calendar-job-target"
              >
                <Dropdown
                  id="calendar-job-target"
                  value={editor.target}
                  options={targetOptions}
                  placeholder={t('calendar.jobs.chooseAgent')}
                  ariaLabel={t('calendar.jobs.agent')}
                  disabled={busy}
                  onValueChange={(next) => {
                    if (next === editor.target) return;
                    editor.target = next;
                    editor.session = '';
                    loadSessions();
                  }}
                />
              </FormField>
              <FormField
                label={t('calendar.jobs.session')}
                controlId="calendar-job-session"
              >
                <Dropdown
                  id="calendar-job-session"
                  value={editor.session}
                  options={sessionOptions}
                  ariaLabel={t('calendar.jobs.session')}
                  disabled={busy || sessionsLoading}
                  onValueChange={(next) => (editor.session = next)}
                />
                {#snippet actions()}
                  {#if sessionCursor}
                    <Button
                      variant="tertiary"
                      disabled={sessionsLoading}
                      onClick={() => loadSessions(true)}
                      >{t('calendar.jobs.moreSessions')}</Button
                    >
                  {/if}
                {/snippet}
              </FormField>
              <div class="calendar-job-timing">
                <FormField
                  label={t('cron.eventTime.when')}
                  controlId="calendar-job-direction"
                >
                  <div class="calendar-job-pair">
                    <Dropdown
                      id="calendar-job-direction"
                      value={editor.event_direction}
                      options={directionOptions}
                      ariaLabel={t('cron.eventTime.when')}
                      disabled={busy}
                      onValueChange={(next) => (editor.event_direction = next)}
                    />
                    <Dropdown
                      id="calendar-job-edge"
                      value={editor.event_edge}
                      options={edgeOptions}
                      ariaLabel={t('cron.eventTime.edge')}
                      disabled={busy}
                      onValueChange={(next) => (editor.event_edge = next)}
                    />
                  </div>
                </FormField>
                {#if editor.event_direction !== 'at'}
                  <FormField
                    label={t('cron.eventTime.offset')}
                    controlId="calendar-job-amount"
                  >
                    <div class="calendar-job-pair">
                      <TextField
                        id="calendar-job-amount"
                        class="calendar-job-amount"
                        type="number"
                        min="1"
                        value={editor.event_amount}
                        ariaLabel={t('cron.eventTime.amount')}
                        disabled={busy}
                        onInput={(value) => (editor.event_amount = value)}
                      />
                      <Dropdown
                        id="calendar-job-unit"
                        value={editor.event_unit}
                        options={unitOptions}
                        ariaLabel={t('cron.eventTime.unit')}
                        disabled={busy}
                        onValueChange={(next) => (editor.event_unit = next)}
                      />
                    </div>
                  </FormField>
                {/if}
              </div>
              <FormField
                label={t('calendar.jobs.instruction')}
                controlId="calendar-job-prompt"
                ><TextArea
                  id="calendar-job-prompt"
                  value={editor.prompt}
                  onInput={(value) => (editor.prompt = value)}
                  rows={4}
                  placeholder={t('calendar.jobs.instructionPlaceholder')}
                /></FormField
              >
              {#if error}<Banner variant="error">{error}</Banner>{/if}
            </div>
            <div class="modal-footer">
              {#if editor.id}
                <div class="modal-footer__start">
                  <SaveStatus
                    {saving}
                    pending={autosave.participant.hasChanges()}
                    onClick={() => autosave.participant.runSave('manual')}
                  />
                </div>
                <Button variant="primary" onClick={close}
                  >{t('calendar.jobs.done')}</Button
                >
              {:else}
                <Button variant="secondary" disabled={busy} onClick={close}>
                  {t('common.cancel')}
                </Button>
                <Button
                  variant="primary"
                  type="submit"
                  loading={busy}
                  disabled={serverUnavailable}
                >
                  {t('calendar.jobs.create')}
                </Button>
              {/if}
            </div>
          </form>
        {/snippet}
      </Modal>
    {/if}
    {#if deleting}
      <ConfirmDialog
        title={t('calendar.jobs.deleteTitle', { name: jobName(deleting) })}
        body={t('calendar.jobs.deleteBody')}
        confirmLabel={t('common.delete')}
        {busy}
        error={deleteError}
        onConfirm={removeNow}
        onCancel={() => (deleting = null)}
      />
    {/if}
  </div>
{/if}
