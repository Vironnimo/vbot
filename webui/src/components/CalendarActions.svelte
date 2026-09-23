<script>
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
    addCalendarAction,
    updateCalendarAction,
    deleteCalendarAction,
    listAgents,
    listProjects,
    showProject,
    listSessions,
  } from '$lib/api.js';
  import {
    buildAgentTargetOptions,
    createAgentTargetCatalogLoader,
  } from '$lib/agentTargetOptions.js';

  let {
    eventId,
    occurrenceStart,
    recurring = false,
    actions = [],
    executions = [],
    timeZone = 'UTC',
    serverUnavailable = false,
    onChanged = () => {},
    onOpenSession = null,
  } = $props();
  let options = $state([]);
  let sessions = $state([]);
  let sessionCursor = $state(null);
  let sessionsLoading = $state(false);
  let sessionRequest = 0;
  let editor = $state(null);
  let error = $state('');
  let busy = $state(false);
  let deleting = $state('');
  let eventActions = $derived(
    actions.filter((action) => action.event_id === eventId),
  );
  // A stored target or Session that is no longer listed stays selectable under
  // its raw id, so opening an older action never silently changes it.
  let targetOptions = $derived(
    editor?.target && !options.some((option) => option.value === editor.target)
      ? [{ value: editor.target, label: editor.target }, ...options]
      : options,
  );
  let sessionOptions = $derived([
    {
      value: '',
      label: t('calendar.actions.newSession', 'New Session for each execution'),
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
  let directionOptions = $derived([
    { value: '-', label: t('calendar.actions.before', 'Before') },
    { value: 'at', label: t('calendar.actions.at', 'At') },
    { value: '+', label: t('calendar.actions.after', 'After') },
  ]);
  let unitOptions = $derived([
    { value: 'm', label: t('calendar.actions.minutes', 'minutes') },
    { value: 'h', label: t('calendar.actions.hours', 'hours') },
    { value: 'd', label: t('calendar.actions.days', 'days') },
  ]);
  let anchorOptions = $derived([
    { value: 'start', label: t('calendar.actions.start', 'Start') },
    { value: 'end', label: t('calendar.actions.end', 'End') },
  ]);

  const targetCatalog = createAgentTargetCatalogLoader({
    listAgents,
    listProjects,
    showProject,
  });
  onMount(() => {
    async function loadTargets() {
      const catalog = await targetCatalog.load();
      if (!catalog) return;
      options = buildAgentTargetOptions(catalog.agents, catalog.projectTeams);
      const failure = catalog.agentError ?? catalog.projectError;
      error = failure
        ? (failure.message ?? String(failure))
        : catalog.failedProjects.length
          ? t(
              'calendar.actions.targetsPartial',
              'Some Project Agent targets could not be loaded.',
            )
          : '';
    }
    loadTargets();
    return () => {
      targetCatalog.dispose();
      sessionRequest += 1;
    };
  });

  function begin(action = null) {
    const match = /^(start|end)(?:\s*([+-])\s*(\d+)([mhd]))?$/.exec(
      action?.when ?? 'start - 1h',
    );
    editor = {
      id: action?.id ?? '',
      target: action?.target ?? options[0]?.value ?? '',
      prompt: action?.prompt ?? '',
      session: action?.session ?? '',
      anchor: match?.[1] ?? 'start',
      direction: match?.[2] ?? 'at',
      amount: Number(match?.[3] ?? 1),
      unit: match?.[4] ?? 'h',
    };
    error = '';
    loadSessions();
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
    if (!editor.target || !editor.prompt.trim()) {
      error = t(
        'calendar.actions.required',
        'Choose an agent and enter an instruction.',
      );
      return;
    }
    const when =
      editor.direction === 'at'
        ? editor.anchor
        : `${editor.anchor} ${editor.direction} ${editor.amount}${editor.unit}`;
    const payload = {
      id: editor.id || eventId,
      when,
      prompt: editor.prompt,
      target: editor.target,
      session: editor.session || null,
    };
    busy = true;
    error = '';
    try {
      if (editor.id) await updateCalendarAction(payload);
      else await addCalendarAction(payload);
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
      await deleteCalendarAction(id);
      deleting = '';
      await onChanged();
    } catch (e) {
      error = e.message ?? String(e);
    } finally {
      busy = false;
    }
  }

  function timingLabel(when) {
    const match = /^(start|end)(?:\s*([+-])\s*(\d+)([mhd]))?$/.exec(when);
    if (!match) return when;
    const anchor =
      match[1] === 'start'
        ? t('calendar.actions.start', 'Start')
        : t('calendar.actions.end', 'End');
    if (!match[2])
      return t('calendar.actions.atAnchor', 'At {anchor}', { anchor });
    const singular = Number(match[3]) === 1;
    const unit = {
      m: singular
        ? t('calendar.actions.minute', 'minute')
        : t('calendar.actions.minutes', 'minutes'),
      h: singular
        ? t('calendar.actions.hour', 'hour')
        : t('calendar.actions.hours', 'hours'),
      d: singular
        ? t('calendar.actions.day', 'day')
        : t('calendar.actions.days', 'days'),
    }[match[4]];
    return t(
      match[2] === '-'
        ? 'calendar.actions.beforeAnchor'
        : 'calendar.actions.afterAnchor',
      match[2] === '-'
        ? '{amount} {unit} before {anchor}'
        : '{amount} {unit} after {anchor}',
      { amount: match[3], unit, anchor },
    );
  }

  function statusLabel(status) {
    const labels = {
      pending: 'Scheduled',
      claimed: 'Waiting',
      running: 'Running',
      completed: 'Completed',
      failed: 'Failed',
      cancelled: 'Cancelled',
      interrupted: 'Interrupted',
      missed: 'Missed',
    };
    return t(`calendar.actions.status.${status}`, labels[status] ?? status);
  }

  function timestamp(value) {
    return new Intl.DateTimeFormat(activeLocaleTag(), {
      timeZone,
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(new Date(value));
  }
</script>

<section
  class="calendar-actions"
  aria-label={t('calendar.actions.heading', 'Agent actions')}
>
  <div class="calendar-actions-heading">
    <h3>{t('calendar.actions.heading', 'Agent actions')}</h3>
    <InfoHint
      text={t(
        'calendar.actions.help',
        'Actions follow this event. Each execution gets a new Session unless you select an existing one. Preparations expire at event start, actions during the event at its end, later actions one hour after their scheduled time.',
      )}
    />
    <Button
      variant="secondary"
      disabled={busy || serverUnavailable || editor !== null}
      onClick={() => begin()}>{t('calendar.actions.add', 'Add action')}</Button
    >
  </div>
  {#if recurring}
    <p class="calendar-detail-meta">
      {t(
        'calendar.actions.series',
        'These actions apply to every occurrence in the series.',
      )}
    </p>
  {/if}
  {#if eventActions.length === 0 && !editor}
    <p class="calendar-detail-meta">
      {t('calendar.actions.empty', 'No agent actions attached.')}
    </p>
  {/if}
  {#each eventActions as action (action.id)}
    {@const execution = executions.find(
      (item) =>
        item.action_id === action.id &&
        (!recurring || item.occurrence_start === occurrenceStart),
    )}
    <div class="calendar-action-row">
      <div class="calendar-action-summary">
        <strong>{timingLabel(action.when)}</strong>
        <span
          >{options.find((option) => option.value === action.target)?.label ??
            action.target}</span
        >
        {#if execution}<StatusChip
            variant={execution.status === 'completed'
              ? 'success'
              : ['failed', 'interrupted', 'missed'].includes(execution.status)
                ? 'warn'
                : 'neutral'}>{statusLabel(execution.status)}</StatusChip
          >{/if}
      </div>
      <p class="calendar-action-prompt">{action.prompt}</p>
      {#if execution}
        <p class="calendar-detail-meta">
          {t('calendar.actions.scheduled', 'Scheduled: {time}', {
            time: timestamp(execution.scheduled_at),
          })}
        </p>
        {#if ['pending', 'claimed', 'missed'].includes(execution.status)}
          <p class="calendar-detail-meta">
            {t('calendar.actions.expires', 'Latest start: {time}', {
              time: timestamp(execution.expires_at),
            })}
          </p>
        {/if}
      {/if}
      <div class="calendar-action-controls">
        {#if execution?.session && execution?.run_id && onOpenSession}
          <Button
            variant="secondary"
            onClick={() => onOpenSession(execution.target, execution.session)}
            >{t('calendar.actions.openSession', 'Open Session')}</Button
          >
        {/if}
        <Button
          variant="secondary"
          disabled={busy || serverUnavailable || editor !== null}
          onClick={() => begin(action)}>{t('common.edit', 'Edit')}</Button
        >
        <Button
          variant="danger"
          disabled={busy || serverUnavailable}
          onClick={() => (deleting = action.id)}
          >{t('common.delete', 'Delete')}</Button
        >
      </div>
      {#if deleting === action.id}
        <div class="calendar-action-controls">
          <span
            >{t(
              'calendar.actions.deleteConfirm',
              'Remove this action from the event?',
            )}</span
          >
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => remove(action.id)}
            >{t('common.delete', 'Delete')}</Button
          >
          <Button variant="secondary" onClick={() => (deleting = '')}
            >{t('common.cancel', 'Cancel')}</Button
          >
        </div>
      {/if}
    </div>
  {/each}
  {#if editor}
    <form
      class="calendar-action-editor"
      onsubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      <div class="calendar-form-row">
        <FormField
          label={t('calendar.actions.agent', 'Agent')}
          controlId="calendar-action-target"
        >
          <Dropdown
            id="calendar-action-target"
            value={editor.target}
            options={targetOptions}
            placeholder={t('calendar.actions.chooseAgent', 'Choose an agent')}
            ariaLabel={t('calendar.actions.agent', 'Agent')}
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
          label={t('calendar.actions.session', 'Session')}
          controlId="calendar-action-session"
        >
          <Dropdown
            id="calendar-action-session"
            value={editor.session}
            options={sessionOptions}
            ariaLabel={t('calendar.actions.session', 'Session')}
            disabled={busy || sessionsLoading}
            onValueChange={(next) => (editor.session = next)}
          />
          {#if sessionCursor}<Button
              variant="secondary"
              disabled={sessionsLoading}
              onClick={() => loadSessions(true)}
              >{t(
                'calendar.actions.moreSessions',
                'Load more Sessions',
              )}</Button
            >{/if}
        </FormField>
      </div>
      <div class="calendar-action-timing">
        <FormField
          label={t('calendar.actions.timing', 'When')}
          controlId="calendar-action-direction"
        >
          <Dropdown
            id="calendar-action-direction"
            value={editor.direction}
            options={directionOptions}
            ariaLabel={t('calendar.actions.timing', 'When')}
            disabled={busy}
            onValueChange={(next) => (editor.direction = next)}
          />
        </FormField>
        {#if editor.direction !== 'at'}
          <FormField
            label={t('calendar.actions.amount', 'Amount')}
            controlId="calendar-action-amount"
            ><TextField
              id="calendar-action-amount"
              type="number"
              min="1"
              value={editor.amount}
              onInput={(value) => (editor.amount = Number(value))}
            /></FormField
          >
          <FormField
            label={t('calendar.actions.unit', 'Unit')}
            controlId="calendar-action-unit"
            ><Dropdown
              id="calendar-action-unit"
              value={editor.unit}
              options={unitOptions}
              ariaLabel={t('calendar.actions.unit', 'Unit')}
              disabled={busy}
              onValueChange={(next) => (editor.unit = next)}
            /></FormField
          >
        {/if}
        <FormField
          label={t('calendar.actions.reference', 'Event')}
          controlId="calendar-action-anchor"
          ><Dropdown
            id="calendar-action-anchor"
            value={editor.anchor}
            options={anchorOptions}
            ariaLabel={t('calendar.actions.reference', 'Event')}
            disabled={busy}
            onValueChange={(next) => (editor.anchor = next)}
          /></FormField
        >
      </div>
      <FormField
        label={t('calendar.actions.instruction', 'Instruction')}
        controlId="calendar-action-prompt"
        ><TextArea
          id="calendar-action-prompt"
          value={editor.prompt}
          onInput={(value) => (editor.prompt = value)}
          rows={4}
        /></FormField
      >
      <div class="calendar-action-controls">
        <Button
          variant="primary"
          disabled={busy || serverUnavailable}
          onClick={save}>{t('common.save', 'Save')}</Button
        ><Button
          variant="secondary"
          disabled={busy}
          onClick={() => {
            editor = null;
            sessionRequest += 1;
          }}>{t('common.cancel', 'Cancel')}</Button
        >
      </div>
    </form>
  {/if}
  {#if error}<Banner variant="error">{error}</Banner>{/if}
</section>
