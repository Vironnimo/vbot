<script module>
  const loadSession = () => import('./CardSession.svelte');
</script>

<script>
  import { untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import ConfirmDialog from '../../../../webui/src/components/ui/ConfirmDialog.svelte';
  import CopyButton from '../../../../webui/src/components/ui/CopyButton.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import MarkdownContent from '../../../../webui/src/components/chat/MarkdownContent.svelte';
  import LazyContent from '../../../../webui/src/components/ui/LazyContent.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import SaveStatus from '../../../../webui/src/components/ui/SaveStatus.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import WorkflowDiagram from './WorkflowDiagram.svelte';
  import LimitsEditor from './LimitsEditor.svelte';
  import { limitsProblem, normalizeLimits } from './limits.js';
  import { createRecordEditor } from './recordEditor.svelte.js';
  import {
    acceptLabel,
    cardActions,
    cardWorkflow,
    editableCard,
    eventLabel,
    stepStatus,
    sendBackDefault,
    stateLabel,
    queuedActionLabel,
    waitingLabel,
    originLabel,
    projectName,
    participantId,
    participantName,
    usageSummary,
    timestamp,
    integer,
  } from './presentation.js';

  let { model, onNavigate } = $props();
  const card = $derived(model.detail.card);
  const actions = $derived(cardActions(card));
  const usage = $derived(usageSummary(model.detail.usage));
  const participants = $derived(model.detail.participants ?? []);
  const approvals = $derived(model.detail.approvals ?? []);
  const outputs = $derived(model.detail.outputs ?? []);
  // The started Card's own Workflow snapshot (with outputs and targets), else
  // the setup's Workflow it will start with.
  const workflow = $derived(
    card.runtime?.workflow ?? cardWorkflow(card, model.setups),
  );
  const steps = $derived(workflow?.steps ?? []);
  const defaultTarget = $derived(sendBackDefault(card, workflow));
  const activeRuns = $derived(
    (model.detail.runs ?? []).filter((run) =>
      ['pending', 'running', 'cancelling'].includes(run.status),
    ),
  );
  const id = untrack(() => card.id);
  let confirm = $state('');
  let compose = $state('');
  let comment = $state('');
  let commentError = $state('');
  let sendBackStep = $state('');
  let inspectedId = $state('');
  const inspected = $derived(
    participants.find(
      (participant) => participantId(participant) === inspectedId,
    ),
  );
  const editor = createRecordEditor({
    identity: id,
    client: untrack(() => model.client),
    get record() {
      return card;
    },
    get editable() {
      return editableCard(card) || card.state === 'waiting';
    },
    project: (record) => ({
      title: record.title ?? '',
      description: record.description ?? '',
      card_limits:
        record.runtime?.card_limits ?? record.runtime?.workflow?.limits ?? {},
      step_limits:
        record.runtime?.step_limits ??
        record.runtime?.workflow?.steps.find(
          (s) => s.id === record.runtime?.step,
        )?.limits ??
        {},
    }),
    normalize: (draft) => ({
      title: draft.title.trim(),
      description: draft.description.trim(),
      card_limits: normalizeLimits(draft.card_limits),
      step_limits: normalizeLimits(draft.step_limits),
    }),
    validate: (draft) => {
      if (card.state === 'waiting')
        return (
          limitsProblem(draft.card_limits) || limitsProblem(draft.step_limits)
        );
      return !editableCard(card)
        ? t('factory.editStopped')
        : !draft.title.trim()
          ? t('factory.titleRequired')
          : !draft.description.trim()
            ? t('factory.descriptionRequired')
            : '';
    },
    save: async (values, revision, baseline) => {
      const limits = card.state === 'waiting';
      return (
        await model.call(limits ? 'card.limits' : 'card.update', {
          id,
          expected_revision: revision,
          ...Object.fromEntries(
            Object.entries(values).filter(
              ([key, value]) =>
                (limits
                  ? ['card_limits', 'step_limits'].includes(key)
                  : ['title', 'description'].includes(key)) &&
                JSON.stringify(value) !== JSON.stringify(baseline[key]),
            ),
          ),
        })
      ).card;
    },
    read: async () => (await model.call('card.get', { id })).card,
    onSave: (saved) => model.acceptSavedCard(saved),
  });

  async function act(operation, args = {}) {
    if (!(await editor.flush())) return null;
    return model.mutate(operation, { id, ...args });
  }

  async function confirmAction() {
    if (
      (confirm === 'discard' && !actions.discard) ||
      (confirm === 'delete' && !actions.delete)
    )
      return;
    if (await act(`card.${confirm}`)) confirm = '';
  }

  function composeComment(kind) {
    compose = kind;
    comment = '';
    commentError = '';
    sendBackStep = '';
  }
  async function sendComment() {
    if (compose === 'answer' ? !actions.answer : !actions.sendBack) return;
    commentError = comment.trim() ? '' : t('factory.commentRequired');
    if (commentError) return;
    if (
      await act(
        `card.${compose}`,
        compose === 'answer'
          ? { text: comment.trim() }
          : {
              comment: comment.trim(),
              ...(sendBackStep ? { step: sendBackStep } : {}),
            },
      )
    )
      compose = '';
  }

  function approvalAddress(approval) {
    return (
      approval.address ??
      (approval.agent_id && approval.session_id
        ? {
            project_id: approval.project_id ?? null,
            agent_id: approval.agent_id,
            session_id: approval.session_id,
          }
        : participants.find(
            (participant) => participant.lifecycle_run_id === approval.run_id,
          )?.address)
    );
  }

  function outputTitle(output) {
    const name =
      output.step_name ||
      steps.find((step) => step.id === output.step)?.name ||
      output.step;
    return output.pass > 1
      ? `${name} · ${t('factory.passNumber', { pass: output.pass })}`
      : name;
  }

  // Keep only the latest document from each step expanded.
  const latestDocuments = $derived(
    new Set(
      Object.values(
        Object.fromEntries(
          outputs
            .filter((output) => output.result)
            .map((output) => [output.step, output.id]),
        ),
      ),
    ),
  );

  function eventText(event) {
    return (
      event.detail?.comment ??
      event.detail?.question ??
      event.detail?.note ??
      ''
    );
  }
</script>

<article class="factory-detail" data-card-id={card.id}>
  <header class="factory-detail-header">
    <Button variant="tertiary" onClick={() => onNavigate('')}
      >← {t('factory.board')}</Button
    >
    <span class="factory-subtle"
      >{projectName(model.catalog, card.project_id)}</span
    >
    <div class="factory-card-identity">
      <span class="factory-mono">{card.id}</span><CopyButton text={card.id} />
    </div>
    <h2>{card.title}</h2>
    <div class="factory-detail-badges">
      <span class="factory-state-badge" data-state={card.state}
        >{stateLabel(card.state)}</span
      >
      {#if card.state === 'waiting'}<span
          >{waitingLabel(card.waiting_reason)}</span
        >{/if}
      {#if card.queued_action}<span
          >{queuedActionLabel(card.queued_action)}</span
        >{/if}
      <span class="factory-subtle">{t('factory.origin')}:</span>
      {#if card.origin_session?.agent_id && card.origin_session?.session_id}<Button
          variant="tertiary"
          ariaLabel={t('factory.provenance')}
          onClick={() => model.openSession(card.origin_session)}
          >{originLabel(card.origin)} ↗</Button
        >{:else}<span class="factory-origin-badge"
          >{originLabel(card.origin)}</span
        >{/if}
    </div>
    {#if card.origin_run}<p class="factory-help">
        {t('factory.originRun')}:
        <span class="factory-mono">{card.origin_run}</span>
      </p>{/if}
  </header>

  {#if approvals.length}
    <Banner variant="warn" role="status" class="factory-permission-notice">
      <strong>{t('factory.permissionWaiting')}</strong>
      {#each approvals as approval, index (`${approval.run_id}:${approval.session_id ?? ''}:${index}`)}
        {@const address = approvalAddress(approval)}
        {#if address}<Button
            variant="secondary"
            onClick={() => model.openSession(address)}
            >{t('factory.reviewPermission')}</Button
          >{/if}
      {/each}
    </Banner>
  {/if}

  {#if card.state === 'waiting' && card.waiting_reason === 'blocked'}
    <section
      class="factory-section factory-decision"
      aria-labelledby="factory-question"
    >
      <h3 id="factory-question">{t('factory.question')}</h3>
      <p class="factory-prose">
        {card.runtime?.question}
      </p>
    </section>
  {:else if card.state === 'waiting' && card.runtime?.note}
    <section class="factory-section factory-decision">
      <h3>{t('factory.waitingNote')}</h3>
      <p class="factory-prose">{card.runtime.note}</p>
    </section>
  {/if}

  <div
    class="factory-actions factory-card-actions"
    aria-label={t('factory.title')}
  >
    {#if actions.approve}<Button
        variant="primary"
        disabled={Boolean(model.pending)}
        onClick={() => act('card.approve_proposal')}
        >{t('factory.approveProposal')}</Button
      >{/if}
    {#if actions.accept}<Button
        variant="primary"
        loading={model.pending === 'card.accept'}
        disabled={Boolean(model.pending)}
        onClick={() => act('card.accept', { expected_revision: card.revision })}
        >{acceptLabel(card)}</Button
      >{/if}
    {#if actions.answer}<Button
        variant="primary"
        disabled={Boolean(model.pending)}
        onClick={() => composeComment('answer')}>{t('factory.answer')}</Button
      >{/if}
    {#if actions.retry}<Button
        variant="primary"
        disabled={Boolean(model.pending)}
        loading={model.pending === 'card.retry'}
        onClick={() => act('card.retry')}>{t('factory.retry')}</Button
      >{/if}
    {#if actions.sendBack}<Button
        variant="secondary"
        disabled={Boolean(model.pending)}
        onClick={() => composeComment('send_back')}
        >{t('factory.sendBack')}</Button
      >{/if}
    {#if actions.discard}<Button
        variant="tertiary"
        disabled={Boolean(model.pending)}
        onClick={() => (confirm = 'discard')}>{t('factory.discard')}</Button
      >{/if}
    {#if actions.delete}<Button
        variant="tertiary"
        disabled={Boolean(model.pending)}
        onClick={() => (confirm = 'delete')}>{t('factory.delete')}</Button
      >{/if}
  </div>

  {#if workflow}
    <section
      class="factory-section factory-workflow-progress"
      aria-labelledby="factory-workflow-heading"
      data-workflow-progress={workflow.id}
    >
      <h3 id="factory-workflow-heading">
        {t('factory.workflowHeading', { name: workflow.name })}
      </h3>
      <WorkflowDiagram {workflow} status={(step) => stepStatus(card, step)} />
    </section>
  {/if}

  <div class="factory-detail-grid">
    <div class="factory-detail-main">
      <section
        class="factory-section factory-objective"
        aria-labelledby="factory-objective-title"
      >
        <h3 id="factory-objective-title">{t('factory.description')}</h3>
        {#if editableCard(card) || editor.hasChanges()}
          <div class="factory-form">
            <div class="factory-field">
              <label for="factory-card-title">{t('factory.cardTitle')}</label
              ><TextField
                id="factory-card-title"
                maxlength="200"
                value={editor.draft.title}
                disabled={!editableCard(card)}
                onInput={(value) => (editor.draft.title = value)}
              />
            </div>
            <TextArea
              id="factory-card-description"
              ariaLabel={t('factory.description')}
              maxlength="20000"
              value={editor.draft.description}
              rows={5}
              disabled={!editableCard(card)}
              onInput={(value) => (editor.draft.description = value)}
            />
            {#if !editableCard(card)}<Banner variant="warn"
                >{t('factory.editStopped')}<Button
                  variant="tertiary"
                  onClick={editor.reset}>{t('factory.discardDraft')}</Button
                ></Banner
              >{/if}
            {#if editor.notice}<Banner variant="warn" role="status"
                >{editor.notice}</Banner
              >{/if}
            {#if editor.error}<Banner variant="error" role="alert"
                >{editor.error}</Banner
              >{/if}
            <div class="factory-card-save">
              <SaveStatus
                saving={editor.saving}
                pending={editor.hasChanges()}
                onClick={editor.save}
              />
            </div>
          </div>
        {:else}<p class="factory-prose">{card.description}</p>{/if}
      </section>
      {#if card.summary}<section class="factory-section">
          <h3>{t('factory.latestSummary')}</h3>
          <p class="factory-prose">{card.summary}</p>
        </section>{/if}
      {#if outputs.length}
        <section
          class="factory-section"
          aria-labelledby="factory-outputs-heading"
        >
          <h3 id="factory-outputs-heading">{t('factory.outputs')}</h3>
          {#each outputs as output (output.id)}
            <article
              class="factory-output"
              data-output-kind={output.kind}
              data-output-step={output.step}
            >
              <header>
                <h4>{outputTitle(output)}</h4>
                {#if output.kind === 'changes'}<span
                    class="factory-verdict"
                    data-verdict="changes_requested"
                    >{t('factory.reviewChanges')}</span
                  >{/if}
              </header>
              {#if output.summary}<p class="factory-prose">
                  {output.summary}
                </p>{/if}
              {#if output.result}
                <details
                  class="factory-document"
                  open={latestDocuments.has(output.id)}
                >
                  <summary>{t('factory.document')}</summary>
                  <MarkdownContent
                    source={output.result}
                    class="msg-markdown md-document"
                  />
                </details>
              {/if}
              {#if output.kind === 'changes'}
                {#if output.target}<p class="factory-help">
                    {t('factory.returnedTo', {
                      step:
                        steps.find((step) => step.id === output.target)?.name ??
                        output.target,
                    })}
                  </p>{/if}
                <h5>{t('factory.findings')}</h5>
                <ul class="factory-findings">
                  {#each output.findings ?? [] as finding, index (index)}<li>
                      {finding}
                    </li>{/each}
                </ul>
              {/if}
              <time class="factory-help" datetime={output.created_at}
                >{timestamp(output.created_at)}</time
              >
            </article>
          {/each}
        </section>
      {/if}
      {#if model.detail.comments?.length}<section class="factory-section">
          <h3>{t('factory.comments')}</h3>
          {#each model.detail.comments as entry, index (index)}<article
              class="factory-comment"
            >
              <p class="factory-prose">{entry.text}</p>
              <time class="factory-help" datetime={entry.at}
                >{timestamp(entry.at)}</time
              >
            </article>{/each}
        </section>{/if}
    </div>
    <aside class="factory-detail-aside">
      {#if card.runtime?.workflow}
        <section class="factory-section" aria-label={t('factory.limit.title')}>
          <h3>{t('factory.limit.title')}</h3>
          <LimitsEditor
            bind:value={editor.draft.card_limits}
            prefix="factory-card-limit"
            label={t('factory.limit.card')}
            disabled={card.state !== 'waiting'}
            usage={model.detail.budget?.card.usage}
          />
          <LimitsEditor
            bind:value={editor.draft.step_limits}
            prefix="factory-step-limit"
            label={t('factory.limit.step')}
            disabled={card.state !== 'waiting'}
            usage={model.detail.budget?.step.usage}
          />
          <p class="factory-help">{t('factory.limit.help')}</p>
          {#if card.state === 'waiting'}
            <p class="factory-help">{t('factory.limit.resume')}</p>
            <SaveStatus
              saving={editor.saving}
              pending={editor.hasChanges()}
              onClick={editor.save}
            />
            {#if editor.error}<Banner variant="error" role="alert"
                >{editor.error}</Banner
              >{/if}
            {#if editor.notice}<Banner variant="warn" role="status"
                >{editor.notice}</Banner
              >{/if}
          {/if}
        </section>
      {/if}
      <section class="factory-section" data-usage>
        <h3>{t('factory.cost')}</h3>
        <strong class="factory-cost">{usage.cost}</strong>
        <dl class="factory-facts">
          <div>
            <dt>{t('factory.tokens')}</dt>
            <dd>{integer(usage.tokens)}</dd>
          </div>
          <div>
            <dt>{t('factory.requests')}</dt>
            <dd>{integer(usage.requests)}</dd>
          </div>
          <div>
            <dt>{t('factory.toolCalls')}</dt>
            <dd>{integer(usage.tools)}</dd>
          </div>
        </dl>
      </section>
      <section class="factory-section" data-live-status>
        <h3>{t('factory.runStatus')}</h3>
        {#each activeRuns as run (run.run_id)}<p class="factory-live-status">
            <span class="factory-live-dot"></span><span
              >{participantName(
                participants.find(
                  (participant) =>
                    participantId(participant) === run.participant_id,
                ) ?? { participant_id: run.participant_id },
              )} · {run.status}</span
            >
          </p>{:else}<p class="factory-help">
            {t('factory.noActiveRun')}
          </p>{/each}
      </section>
      <section
        class="factory-section"
        aria-labelledby="factory-sessions-heading"
      >
        <h3 id="factory-sessions-heading">{t('factory.sessions')}</h3>
        <div class="factory-session-list">
          {#each participants as participant (participantId(participant))}<div
              class="factory-session-choice"
            >
              <div>
                <strong>{participantName(participant)}</strong
                >{#if participant.run_active}<span class="factory-live-label"
                    >{t('factory.live')}</span
                  >{/if}
                <p class="factory-help factory-mono">{participant.model}</p>
              </div>
              <Button
                variant={inspectedId === participantId(participant)
                  ? 'secondary'
                  : 'tertiary'}
                aria-expanded={inspectedId === participantId(participant)}
                onClick={() =>
                  (inspectedId =
                    inspectedId === participantId(participant)
                      ? ''
                      : participantId(participant))}
                >{t('factory.openSession')}</Button
              >
            </div>{:else}<p class="factory-help">
              {t('factory.noSessions')}
            </p>{/each}
        </div>
      </section>
    </aside>
  </div>
  {#if inspected}
    {#key `${card.group_id}:${inspectedId}`}<LazyContent load={loadSession}
        >{#snippet children(Component)}<Component
            client={model.client}
            groupId={card.group_id}
            participant={inspected}
            onError={(message) => (model.error = message)}
            onClose={() => (inspectedId = '')}
          />{/snippet}</LazyContent
      >{/key}
  {/if}
  <section class="factory-section" aria-labelledby="factory-timeline-heading">
    <h3 id="factory-timeline-heading">{t('factory.timeline')}</h3>
    <ol class="factory-event-list">
      {#each model.detail.events ?? [] as event (event.id)}<li>
          <time datetime={event.at}>{timestamp(event.at)}</time>
          <div>
            <strong>{eventLabel(event.event)}</strong><span
              class="factory-help"
            >
              · {event.actor}</span
            >{#if event.detail?.state}<p class="factory-help">
                {stateLabel(event.detail.previous_state)} → {stateLabel(
                  event.detail.state,
                )}
              </p>{/if}{#if eventText(event)}<p class="factory-prose">
                {eventText(event)}
              </p>{/if}
          </div>
        </li>{:else}<li class="factory-help">{t('factory.noEvents')}</li>{/each}
    </ol>
  </section>
</article>

{#if confirm}
  <ConfirmDialog
    title={confirm === 'discard'
      ? t('factory.discardTitle', { title: card.title })
      : t('factory.deleteTitle', { title: card.title })}
    body={confirm === 'discard'
      ? t('factory.discardHelp')
      : t('factory.deleteHelp')}
    confirmLabel={confirm === 'discard'
      ? t('factory.discard')
      : t('factory.delete')}
    busy={Boolean(model.pending)}
    error={model.error}
    onConfirm={confirmAction}
    onCancel={() => (confirm = '')}
  ></ConfirmDialog>
{/if}
{#if compose}
  <Modal
    title={compose === 'answer'
      ? t('factory.answer')
      : t('factory.sendBackTitle')}
    closeDisabled={Boolean(model.pending)}
    onClose={() => (compose = '')}
  >
    {#snippet body()}<div class="modal-body factory-form">
        <div class="factory-field">
          <label for="factory-response"
            >{compose === 'answer'
              ? t('factory.answerLabel')
              : t('factory.comment')}</label
          ><TextArea
            id="factory-response"
            value={comment}
            rows={5}
            maxlength="20000"
            disabled={Boolean(model.pending)}
            onInput={(value) => (comment = value)}
          />
        </div>
        {#if compose === 'send_back' && steps.length > 1}<div
            class="factory-field"
          >
            <label
              id="factory-send-back-step-label"
              for="factory-send-back-step">{t('factory.sendBackStep')}</label
            ><Dropdown
              id="factory-send-back-step"
              ariaLabelledby="factory-send-back-step-label"
              value={sendBackStep}
              options={[
                {
                  value: '',
                  label: t('factory.sendBackDefault', {
                    step: defaultTarget?.name ?? '',
                  }),
                },
                ...steps.map((step) => ({
                  value: step.id,
                  label: step.name,
                })),
              ]}
              disabled={Boolean(model.pending)}
              onOpenChange={model.pickerOpen}
              onValueChange={(value) => (sendBackStep = value)}
            />
          </div>{/if}
        {#if commentError || model.error}<Banner variant="error" role="alert"
            >{commentError || model.error}</Banner
          >{/if}
      </div>{/snippet}
    {#snippet footer()}<Button
        variant="secondary"
        disabled={Boolean(model.pending)}
        onClick={() => (compose = '')}>{t('common.cancel')}</Button
      ><Button
        variant="primary"
        loading={Boolean(model.pending)}
        disabled={compose === 'answer' ? !actions.answer : !actions.sendBack}
        onClick={sendComment}
        >{compose === 'answer'
          ? t('factory.answer')
          : t('factory.sendBack')}</Button
      >{/snippet}
  </Modal>
{/if}

<style>
  .factory-detail {
    max-width: 1320px;
    margin: 0 auto;
  }
  .factory-detail-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px 18px;
    margin-bottom: 24px;
  }
  .factory-detail-header h2 {
    width: 100%;
    font-size: 25px;
    margin: 8px 0 4px;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }
  .factory-detail-badges {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    width: 100%;
    font-size: 12px;
  }
  .factory-card-identity {
    display: flex;
    gap: 4px;
    align-items: center;
    color: var(--text-lo);
    font-size: 11px;
    margin-left: auto;
  }
  .factory-state-badge {
    padding: 5px 9px;
    border-radius: 5px;
    background: var(--surface-3);
    font-weight: 600;
  }
  .factory-state-badge[data-state='waiting'] {
    color: var(--accent);
  }
  .factory-decision {
    border-left: 3px solid var(--accent);
  }
  .factory-card-actions {
    margin: 8px 0 26px;
  }
  .factory-detail-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(260px, 340px);
    gap: 24px;
  }
  .factory-facts {
    display: flex;
    flex-direction: column;
    gap: 14px;
    font-size: 12px;
    margin: 14px 0 0;
  }
  .factory-facts > div {
    display: flex;
    gap: 16px;
    justify-content: space-between;
    align-items: start;
  }
  .factory-facts dt {
    color: var(--text-med);
    flex-shrink: 0;
  }
  .factory-facts dd {
    display: flex;
    gap: 6px;
    margin: 0;
    text-align: right;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .factory-cost {
    font-size: 23px;
    font-weight: 550;
    font-variant-numeric: tabular-nums;
  }
  .factory-output + .factory-output {
    border-top: 1px solid var(--border);
    margin-top: 20px;
    padding-top: 16px;
  }
  .factory-output header {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    justify-content: space-between;
    font-size: 12px;
    margin-bottom: 14px;
  }
  .factory-output h4 {
    margin: 0;
    font-size: 13px;
  }
  .factory-output h5 {
    margin: 16px 0 8px;
    font-size: 12px;
  }
  .factory-output .factory-document {
    margin-top: 12px;
  }
  .factory-verdict[data-verdict='changes_requested'] {
    color: var(--amber);
  }
  .factory-document {
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-2);
    padding: 10px 14px;
  }
  .factory-document summary {
    cursor: pointer;
    font-size: 12px;
    font-weight: 550;
  }
  .factory-document[open] summary {
    margin-bottom: 10px;
  }
  .factory-findings {
    padding-left: 20px;
    line-height: 1.7;
    color: var(--text-med);
  }
  .factory-comment + .factory-comment {
    margin-top: 18px;
  }
  .factory-session-list {
    display: grid;
    gap: 18px;
  }
  .factory-session-choice {
    font-size: 12px;
    min-width: 0;
  }
  .factory-session-choice > div {
    margin-bottom: 6px;
  }
  .factory-live-label {
    color: var(--accent);
    font-size: 10px;
    margin-left: 8px;
  }
  .factory-live-status {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    margin: 0 0 8px;
  }
  .factory-live-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent);
    flex-shrink: 0;
  }
  .factory-card-save {
    display: flex;
    justify-content: end;
  }
  .factory-event-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 18px;
  }
  .factory-event-list li {
    display: grid;
    grid-template-columns: 170px minmax(0, 1fr);
    gap: 18px;
    font-size: 12px;
  }
  .factory-event-list time {
    color: var(--text-lo);
    font-variant-numeric: tabular-nums;
  }
  .factory-event-list strong {
    text-transform: capitalize;
    font-weight: 550;
  }
  .factory-event-list .factory-prose {
    margin-top: 6px;
  }
  :global(.factory-permission-notice) {
    margin-bottom: 18px;
  }
  @media (max-width: 850px) {
    .factory-detail-grid {
      grid-template-columns: 1fr;
      gap: 0;
    }
    .factory-detail-aside {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
    }
  }
  @media (max-width: 540px) {
    .factory-event-list li {
      grid-template-columns: 1fr;
      gap: 6px;
    }
    .factory-facts > div {
      flex-direction: column;
      gap: 6px;
    }
    .factory-facts dd {
      text-align: left;
    }
  }
</style>
