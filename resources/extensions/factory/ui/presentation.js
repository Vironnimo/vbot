import { t, activeLocaleTag } from '../../../../webui/src/lib/i18n.js';
import { formatDateTimeInApplicationZone } from '../../../../webui/src/lib/dateTimePrefs.svelte.js';

export const copy = (value) => JSON.parse(JSON.stringify(value));
export function failureText(cause) {
  switch (cause?.code) {
    case 'factory_unavailable':
      return t('factory.error.unavailable');
    case 'project_not_enabled':
      return t('factory.error.projectDisabled');
    case 'project_not_found':
      return t('factory.error.projectMissing');
    // Setup validation names the Workflow and step at fault; only the
    // server knows which Agents and Models are available to it.
    case 'invalid_workflow':
      return cause?.message || t('factory.error.invalidWorkflow');
    case 'profile_not_found':
      return cause?.message || t('factory.error.profileMissing');
    case 'model_not_found':
      return cause?.message || t('factory.error.modelMissing');
    case 'model_unresolved':
      return cause?.message || t('factory.error.modelUnresolved');
    case 'invalid_effort':
      return cause?.message || t('factory.error.invalidEffort');
    case 'workflow_not_found':
      return t('factory.error.workflowMissing');
    case 'invalid_step':
      return cause?.message || t('factory.error.invalidStep');
    case 'setup_not_found':
      return t('factory.error.setupMissing');
    case 'setup_in_use':
      return t('factory.error.setupInUse');
    case 'card_not_found':
      return t('factory.cardMissing');
    case 'card_not_final':
      return t('factory.error.cardNotFinal');
    case 'revision_conflict':
    case 'factory_conflict':
    case 'stale_revision':
      return t('factory.error.conflict');
    case 'phase_queued':
      return t('factory.error.phaseQueued');
    case 'invalid_state':
      return t('factory.error.stateChanged');
    case 'not_acceptable':
      return t('factory.error.notAcceptable');
    case 'not_blocked':
      return t('factory.error.notBlocked');
    case 'not_retryable':
      return t('factory.error.notRetryable');
    case 'invalid_position':
      return t('factory.error.invalidPosition');
    case 'missing_field':
    case 'missing_text':
      return t('factory.error.required');
    case 'text_too_long':
      return t('factory.error.textTooLong');
    case 'unknown_operation':
    case 'invalid_action':
      return t('factory.error.unknownAction');
    default:
      return cause?.message || t('factory.requestFailed');
  }
}
export const editableCard = (card) =>
  ['proposal', 'queued'].includes(card?.state) && !card?.queued_action;
export const finalCard = (card) => ['done', 'discarded'].includes(card?.state);
export const participantId = (participant) =>
  participant.participant_id ?? participant.id;
export const projectName = (catalog, id) =>
  catalog.projects?.find((project) => project.id === id)?.name ?? id;

export function stateLabel(state) {
  switch (state) {
    case 'proposal':
      return t('factory.state.proposal');
    case 'queued':
      return t('factory.state.queued');
    case 'working':
      return t('factory.state.working');
    case 'waiting':
      return t('factory.state.waiting');
    case 'done':
      return t('factory.state.done');
    case 'discarded':
      return t('factory.state.discarded');
    default:
      return state ?? '';
  }
}

export function waitingLabel(reason) {
  switch (reason) {
    case 'checkpoint':
      return t('factory.reason.checkpoint');
    case 'finished':
      return t('factory.reason.finished');
    case 'no_agreement':
      return t('factory.reason.noAgreement');
    case 'blocked':
      return t('factory.reason.blocked');
    case 'budget_exhausted':
      return t('factory.reason.budget');
    case 'run_failed':
      return t('factory.reason.runFailed');
    case 'restart':
      return t('factory.reason.restart');
    default:
      return reason ?? '';
  }
}

export const ACCEPTABLE_REASONS = ['checkpoint', 'finished', 'no_agreement'];

// What accepting does depends on why the Card waits: start the step that
// waits for approval, overrule a step out of rounds, or finish the Card.
export function acceptLabel(card) {
  switch (card?.waiting_reason) {
    case 'checkpoint':
      return t('factory.acceptCheckpoint', { step: card.step?.name ?? '' });
    case 'no_agreement':
      return t('factory.acceptAnyway');
    default:
      return t('factory.acceptDone');
  }
}

export function queuedActionLabel(action) {
  switch (action) {
    case 'accept':
      return t('factory.queued.accept');
    case 'send_back':
      return t('factory.queued.sendBack');
    case 'retry':
      return t('factory.queued.retry');
    default:
      return '';
  }
}

export function originLabel(origin) {
  switch (origin) {
    case 'operator':
      return t('factory.origin.operator');
    case 'user':
      return t('factory.origin.user');
    case 'automation':
      return t('factory.origin.automation');
    case 'agent':
      return t('factory.origin.agent');
    default:
      return origin ?? '';
  }
}

export const participantName = (participant) =>
  participant.display_name || participantId(participant) || '';

// A Card's Workflow: its snapshot once started, else the setup's Workflow
// it will start with.
export function cardWorkflow(card, setups) {
  if (card?.workflow) return card.workflow;
  const setup = setups?.find((entry) => entry.project_id === card?.project_id);
  const workflows = setup?.workflows ?? [];
  return (
    workflows.find((workflow) => workflow.id === card?.workflow_id) ??
    workflows.find((workflow) => workflow.id === setup?.default_workflow) ??
    null
  );
}

export function eventLabel(event) {
  switch (event) {
    case 'created':
      return t('factory.event.created');
    case 'updated':
      return t('factory.event.updated');
    case 'moved':
      return t('factory.event.moved');
    case 'proposal_approved':
      return t('factory.event.proposalApproved');
    case 'queued_for_slot':
      return t('factory.event.queuedForSlot');
    case 'started':
      return t('factory.event.started');
    case 'run_admitted':
      return t('factory.event.runAdmitted');
    case 'run_observed':
      return t('factory.event.runObserved');
    case 'delivery_acknowledged':
      return t('factory.event.deliveryAcknowledged');
    case 'submitted':
      return t('factory.event.submitted');
    case 'changes_requested':
      return t('factory.event.changesRequested');
    case 'checkpoint':
      return t('factory.event.checkpoint');
    case 'finished':
      return t('factory.event.finished');
    case 'no_agreement':
      return t('factory.event.noAgreement');
    case 'blocked':
      return t('factory.event.blocked');
    case 'accepted':
      return t('factory.event.accepted');
    case 'answered':
      return t('factory.event.answered');
    case 'sent_back':
      return t('factory.event.sentBack');
    case 'retried':
      return t('factory.event.retried');
    case 'retry_consumed':
      return t('factory.event.retryConsumed');
    case 'retry_reconciled':
      return t('factory.event.retryReconciled');
    case 'nudged':
      return t('factory.event.nudged');
    case 'restarted':
      return t('factory.event.restarted');
    case 'restart':
      return t('factory.event.restart');
    case 'recovered_initial':
      return t('factory.event.recoveredInitial');
    case 'run_failed':
      return t('factory.event.runFailed');
    case 'admission_failed':
      return t('factory.event.admissionFailed');
    case 'budget_exhausted':
      return t('factory.event.budgetExhausted');
    case 'discarded':
      return t('factory.event.discarded');
    case 'deletion_requested':
      return t('factory.event.deletionRequested');
    case 'pending_dropped':
      return t('factory.event.pendingDropped');
    case 'cleanup_failed':
      return t('factory.event.cleanupFailed');
    case 'workflow_upgrade':
      return t('factory.event.workflowUpgrade');
    default:
      return (event ?? '').replaceAll('_', ' ');
  }
}

export function boardColumns(cards, showDiscarded = false) {
  return [
    {
      id: 'proposals',
      label: t('factory.column.proposals'),
      states: ['proposal'],
    },
    { id: 'backlog', label: t('factory.column.backlog'), states: ['queued'] },
    {
      id: 'progress',
      label: t('factory.column.progress'),
      states: ['working'],
    },
    { id: 'waiting', label: t('factory.column.waiting'), states: ['waiting'] },
    {
      id: 'done',
      label: t('factory.column.done'),
      states: showDiscarded ? ['done', 'discarded'] : ['done'],
    },
  ]
    .map((column) => ({
      ...column,
      cards: cards.filter((card) => column.states.includes(card.state)),
    }))
    .filter((column) => column.id !== 'proposals' || column.cards.length > 0);
}

export function cardActions(card) {
  const waiting = card?.state === 'waiting';
  return {
    approve: card?.state === 'proposal',
    accept: waiting && ACCEPTABLE_REASONS.includes(card.waiting_reason),
    answer: waiting && card.waiting_reason === 'blocked',
    retry:
      waiting &&
      ['run_failed', 'restart', 'budget_exhausted'].includes(
        card.waiting_reason,
      ),
    sendBack: waiting && card.waiting_reason !== 'blocked',
    discard: Boolean(card) && !finalCard(card),
    delete: finalCard(card),
  };
}

export function timestamp(value) {
  return value
    ? formatDateTimeInApplicationZone(value, activeLocaleTag(), {
        dateStyle: 'medium',
        timeStyle: 'short',
      })
    : '';
}

export function integer(value) {
  return Number.isFinite(value)
    ? new Intl.NumberFormat(activeLocaleTag()).format(value)
    : '—';
}

export function usageSummary(usage = {}) {
  const totals = usage.activity?.totals ?? usage.totals ?? usage;
  const cost =
    usage.cost?.total ??
    usage.cost?.amount ??
    usage.cost_usd ??
    totals.cost_usd ??
    totals.cost;
  return {
    tokens:
      totals.total_tokens ??
      (Number.isFinite(totals.input_tokens) ||
      Number.isFinite(totals.output_tokens)
        ? (totals.input_tokens ?? 0) + (totals.output_tokens ?? 0)
        : null),
    requests: totals.calls ?? usage.requests ?? totals.requests,
    tools: usage.activity?.tool_calls ?? usage.tool_calls,
    cost: Number.isFinite(cost)
      ? new Intl.NumberFormat(activeLocaleTag(), {
          style: 'currency',
          currency: usage.cost?.currency ?? 'USD',
          maximumFractionDigits: 4,
        }).format(cost)
      : t('factory.costUnavailable'),
  };
}

// Where a send-back without a chosen step goes, as the server decides it:
// the current step, or the last finished one, or the step either of them
// sends work back to.
export function sendBackDefault(card, workflow) {
  const steps = workflow?.steps ?? [];
  const done = card?.done_steps ?? [];
  let current = steps.find((step) => step.id === card?.step?.id);
  if (!current || !done.includes(current.id))
    current = steps.filter((step) => done.includes(step.id)).at(-1) ?? steps[0];
  const id = current?.returns_to || current?.id;
  return steps.find((step) => step.id === id) ?? null;
}

// A step's place in a Card's run, for the flow diagram.
export function stepStatus(card, step) {
  if (card?.state === 'done' || card?.done_steps?.includes(step.id))
    return 'done';
  if (card?.step?.id !== step.id || card?.state === 'discarded')
    return 'pending';
  return card.state === 'waiting' ? 'waiting' : 'current';
}
