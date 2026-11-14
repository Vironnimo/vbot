import { t, activeLocaleTag } from '../../../../webui/src/lib/i18n.js';
import { formatDateTimeInApplicationZone } from '../../../../webui/src/lib/dateTimePrefs.svelte.js';

export const copy = (value) => JSON.parse(JSON.stringify(value));
export function failureText(cause) {
  switch (cause?.code) {
    case 'factory_unavailable':
      return t('factory.error.unavailable');
    case 'project_not_found':
      return t('factory.error.projectMissing');
    // Workflow validation names the step at fault; only the server knows
    // which Agents and Models are available to it.
    case 'invalid_workflow':
    case 'invalid_schedule':
    case 'invalid_limits':
    case 'workflow_name_taken':
    case 'project_mismatch':
    case 'too_deep':
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
    case 'workflow_in_use':
      return cause?.message || t('factory.error.workflowInUse');
    case 'trigger_not_found':
      return t('factory.error.triggerMissing');
    case 'triggers_unavailable':
      return t('factory.triggersUnavailable');
    case 'invalid_step':
      return cause?.message || t('factory.error.invalidStep');
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
    case 'wrong_state':
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
    case 'upgraded':
      return t('factory.reason.upgraded');
    case 'child_failed':
      return t('factory.reason.childFailed');
    case 'workflow_missing':
      return t('factory.reason.workflowMissing');
    case 'project_unresolved':
      return t('factory.reason.projectUnresolved');
    default:
      return reason ?? '';
  }
}

// Why a step waits for the user, and what the user can do about it.
const STEP_ACCEPT = ['checkpoint', 'no_agreement'];
const STEP_RETRY = [
  'run_failed',
  'restart',
  'budget_exhausted',
  'upgraded',
  'child_failed',
];

// What accepting a step does: start the step that waits for approval, or
// overrule a step that ran out of rounds.
export const stepAcceptLabel = (step) =>
  step.reason === 'checkpoint'
    ? t('factory.acceptCheckpoint', { step: step.name })
    : t('factory.acceptAnyway');

export function queuedActionLabel(action) {
  switch (action) {
    case 'accept':
      return t('factory.queued.accept');
    case 'send_back':
      return t('factory.queued.sendBack');
    case 'retry':
      return t('factory.queued.retry');
    case 'answer':
    case 'resume':
      return t('factory.event.queuedForSlot');
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
    case 'trigger':
      return t('factory.origin.trigger');
    case 'workflow':
      return t('factory.origin.workflow');
    default:
      return origin ?? '';
  }
}

export const participantName = (participant) =>
  participant.display_name || participantId(participant) || '';

// A Card's Workflow: its snapshot once started, else the Workflow it will
// start with.
export function cardWorkflow(card, workflows = []) {
  if (card?.workflow) return card.workflow;
  return (
    workflows.find((workflow) => workflow.id === card?.workflow_id) ?? null
  );
}

// Each step of a started Card with its state in this Card's run.
export function cardSteps(card) {
  return (card?.workflow?.steps ?? []).map((step) => {
    const state = card.steps?.[step.id] ?? {};
    const status = state.status ?? 'pending';
    return {
      ...step,
      ...state,
      status,
      reason: status === 'approval' ? 'checkpoint' : (state.reason ?? ''),
    };
  });
}

// The steps that work or wait right now.
export const activeSteps = (card) =>
  cardSteps(card).filter((step) =>
    ['running', 'child', 'waiting', 'approval'].includes(step.status),
  );

// The steps that wait for the user, each with what the user can do.
export const waitingSteps = (card) =>
  card?.state === 'waiting'
    ? cardSteps(card)
        .filter((step) => ['waiting', 'approval'].includes(step.status))
        .map((step) => ({
          ...step,
          accept: STEP_ACCEPT.includes(step.reason),
          answer: step.reason === 'blocked',
          retry: STEP_RETRY.includes(step.reason),
        }))
    : [];

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
    case 'child_finished':
      return t('factory.event.childFinished');
    case 'child_discarded':
      return t('factory.event.childDiscarded');
    case 'limits_updated':
      return t('factory.event.limitsUpdated');
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

// The Card's own actions; a step that waits for the user has its own
// (`waitingSteps`). A blocked Card could not start or go on as a whole, such
// as when its Workflow is gone; Retry starts it again.
export function cardActions(card) {
  const waiting = card?.state === 'waiting';
  const steps = waitingSteps(card);
  const blocked =
    waiting &&
    card.waiting_reason !== 'finished' &&
    !steps.some((step) => step.reason === card.waiting_reason);
  return {
    approve: card?.state === 'proposal',
    accept: waiting && card.waiting_reason === 'finished',
    retry: blocked,
    sendBack: waiting && !blocked,
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
// the first waiting step, else the last step nothing comes after, or the
// step that one sends work back to.
export function sendBackDefault(card, workflow) {
  const steps = workflow?.steps ?? [];
  const current =
    steps.find((step) => card?.steps?.[step.id]?.status === 'waiting') ??
    steps
      .filter((step) => !steps.some((other) => other.after?.includes(step.id)))
      .at(-1);
  const id = current?.returns_to || current?.id;
  return steps.find((step) => step.id === id) ?? null;
}

// A step's place in a Card's run, for the flow diagram.
export function stepStatus(card, step) {
  if (card?.state === 'done') return 'done';
  return card?.steps?.[step.id]?.status ?? 'pending';
}
