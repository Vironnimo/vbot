<script>
  // The Agent jobs of one calendar event: the Cron jobs bound to it, each
  // with when it runs relative to the event, its Agent, instruction and
  // state, and a form to add, change or delete one.
  import { onMount } from 'svelte';
  import Button from './ui/Button.svelte';
  import Banner from './ui/Banner.svelte';
  import FormField from './ui/FormField.svelte';
  import TextField from './ui/TextField.svelte';
  import TextArea from './ui/TextArea.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import InfoHint from './ui/InfoHint.svelte';
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
  let editor = $state(null);
  let error = $state('');
  let busy = $state(false);
  let deleting = $state('');
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
      error = failure
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
    error = '';
    loadSessions();
  }

  function cancel() {
    editor = null;
    sessionRequest += 1;
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
    if (!editor || busy) return;
    const eventTime = eventTimeText(editor);
    if (!editor.target || !editor.prompt.trim() || !eventTime) {
      error = t('calendar.jobs.required');
      return;
    }
    const fields = {
      agent_id: editor.target,
      prompt: editor.prompt.trim(),
      event_time: eventTime,
    };
    busy = true;
    error = '';
    try {
      if (editor.id) {
        await updateCronJob({
          id: editor.id,
          ...fields,
          session_id: editor.session || null,
        });
      } else {
        await createCronJob({
          ...fields,
          schedule_type: 'event',
          event_id: eventId,
          ...(editor.session ? { session_id: editor.session } : {}),
        });
      }
      editor = null;
      await onChanged();
    } catch (e) {
      error = e.message ?? String(e);
    } finally {
      busy = false;
    }
  }

  async function remove(id) {
    busy = true;
    error = '';
    try {
      await deleteCronJob(id);
      deleting = '';
      await onChanged();
    } catch (e) {
      error = e.message ?? String(e);
    } finally {
      busy = false;
    }
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
      hour: '2-digit',
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
</script>

<section class="calendar-jobs" aria-label={t('calendar.jobs.heading')}>
  <div class="calendar-jobs-heading">
    <h3>{t('calendar.jobs.heading')}</h3>
    <InfoHint text={t('calendar.jobs.help')} />
    <Button
      variant="secondary"
      disabled={busy || serverUnavailable || editor !== null}
      onClick={() => begin()}>{t('calendar.jobs.add')}</Button
    >
  </div>
  {#if occurrence?.recurring}
    <p class="calendar-detail-meta">
      {t('calendar.jobs.series')}
    </p>
  {/if}
  {#if jobsError}
    <Banner variant="error">{t('calendar.jobs.loadError')} {jobsError}</Banner>
  {:else if shownJobs.length === 0 && !editor}
    <p class="calendar-detail-meta">
      {t('calendar.jobs.empty')}
    </p>
  {/if}
  {#each shownJobs as job (job.id)}
    <div class="calendar-job-row" data-testid={`calendar-job-${job.id}`}>
      <div class="calendar-job-summary">
        <strong
          >{eventTimeLabel(job.event_edge, job.event_offset_minutes)}</strong
        >
        {#if dueText(job)}<span class="calendar-job-due">{dueText(job)}</span
          >{/if}
        <span>{agentLabel(job.target)}</span>
        <StatusChip variant={statusChipVariant(job)}
          >{statusLabel(job.status)}</StatusChip
        >
      </div>
      <p class="calendar-job-prompt">{job.prompt}</p>
      <p class="calendar-detail-meta">{runText(job)}</p>
      {#if job.last_error}
        <p class="calendar-detail-meta calendar-job-error">
          {t('calendar.jobs.error', { reason: job.last_error })}
        </p>
      {/if}
      <div class="calendar-job-controls">
        {#if job.session_id && onOpenSession}
          <Button
            variant="secondary"
            onClick={() => onOpenSession(job.target, job.session_id)}
            >{t('calendar.jobs.openSession')}</Button
          >
        {/if}
        <Button
          variant="secondary"
          disabled={busy || serverUnavailable || editor !== null}
          onClick={() => begin(job)}>{t('common.edit')}</Button
        >
        <Button
          variant="danger"
          disabled={busy || serverUnavailable}
          onClick={() => (deleting = job.id)}>{t('common.delete')}</Button
        >
      </div>
      {#if deleting === job.id}
        <div class="calendar-job-controls">
          <span>{t('calendar.jobs.deleteConfirm')}</span>
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => remove(job.id)}>{t('common.delete')}</Button
          >
          <Button variant="secondary" onClick={() => (deleting = '')}
            >{t('common.cancel')}</Button
          >
        </div>
      {/if}
    </div>
  {/each}
  {#if editor}
    <form
      class="calendar-job-editor"
      onsubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      <div class="calendar-form-row">
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
          {#if sessionCursor}<Button
              variant="secondary"
              disabled={sessionsLoading}
              onClick={() => loadSessions(true)}
              >{t('calendar.jobs.moreSessions')}</Button
            >{/if}
        </FormField>
      </div>
      <div class="calendar-job-timing">
        <FormField
          label={t('cron.eventTime.when')}
          controlId="calendar-job-direction"
        >
          <Dropdown
            id="calendar-job-direction"
            value={editor.event_direction}
            options={directionOptions}
            ariaLabel={t('cron.eventTime.when')}
            disabled={busy}
            onValueChange={(next) => (editor.event_direction = next)}
          />
        </FormField>
        {#if editor.event_direction !== 'at'}
          <FormField
            label={t('cron.eventTime.amount')}
            controlId="calendar-job-amount"
            ><TextField
              id="calendar-job-amount"
              type="number"
              min="1"
              value={editor.event_amount}
              onInput={(value) => (editor.event_amount = value)}
            /></FormField
          >
          <FormField
            label={t('cron.eventTime.unit')}
            controlId="calendar-job-unit"
            ><Dropdown
              id="calendar-job-unit"
              value={editor.event_unit}
              options={unitOptions}
              ariaLabel={t('cron.eventTime.unit')}
              disabled={busy}
              onValueChange={(next) => (editor.event_unit = next)}
            /></FormField
          >
        {/if}
        <FormField
          label={t('cron.eventTime.edge')}
          controlId="calendar-job-edge"
          ><Dropdown
            id="calendar-job-edge"
            value={editor.event_edge}
            options={edgeOptions}
            ariaLabel={t('cron.eventTime.edge')}
            disabled={busy}
            onValueChange={(next) => (editor.event_edge = next)}
          /></FormField
        >
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
      <div class="calendar-job-controls">
        <Button
          variant="primary"
          disabled={busy || serverUnavailable}
          onClick={save}>{t('common.save')}</Button
        ><Button variant="secondary" disabled={busy} onClick={cancel}
          >{t('common.cancel')}</Button
        >
      </div>
    </form>
  {/if}
  {#if error}<Banner variant="error">{error}</Banner>{/if}
</section>
