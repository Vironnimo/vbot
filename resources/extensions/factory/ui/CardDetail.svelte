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
  import LazyContent from '../../../../webui/src/components/ui/LazyContent.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import SaveStatus from '../../../../webui/src/components/ui/SaveStatus.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import { createRecordEditor } from './recordEditor.svelte.js';
  import {
    cardActions,
    editableCard,
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
    failureText,
  } from './presentation.js';

  let { model, onNavigate } = $props();
  const card = $derived(model.detail.card);
  const actions = $derived(cardActions(card));
  const usage = $derived(usageSummary(model.detail.usage));
  const participants = $derived(model.detail.participants ?? []);
  const approvals = $derived(model.detail.approvals ?? []);
  const reviews = $derived(model.detail.reviews ?? []);
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
      return editableCard(card);
    },
    project: (record) => ({
      title: record.title ?? '',
      description: record.description ?? '',
    }),
    normalize: (draft) => ({
      title: draft.title.trim(),
      description: draft.description.trim(),
    }),
    validate: (draft) =>
      !editableCard(card)
        ? t('factory.editStopped')
        : !draft.title.trim()
          ? t('factory.titleRequired')
          : !draft.description.trim()
            ? t('factory.descriptionRequired')
            : '',
    save: async (values, revision, baseline) =>
      (
        await model.call('card.update', {
          id,
          expected_revision: revision,
          ...Object.fromEntries(
            Object.entries(values).filter(
              ([key, value]) => value !== baseline[key],
            ),
          ),
        })
      ).card,
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
          : { comment: comment.trim() },
      )
    )
      compose = '';
  }

  async function openReference(url) {
    try {
      await model.client.openLink(url);
    } catch (cause) {
      model.error = failureText(cause);
    }
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
        {card.runtime?.question || card.recommendation}
      </p>
    </section>
  {:else if card.state === 'waiting' && card.waiting_reason !== 'approved' && card.runtime?.note}
    <section class="factory-section factory-decision">
      <h3>{t('factory.waitingNote')}</h3>
      <p class="factory-prose">{card.runtime.note}</p>
    </section>
  {/if}

  {#if card.recommendation || card.waiting_reason === 'approved'}
    <section
      class="factory-section factory-recommendation"
      aria-labelledby="factory-recommendation-heading"
      data-recommendation
    >
      <h3 id="factory-recommendation-heading">{t('factory.recommendation')}</h3>
      <p class="factory-prose">
        {card.recommendation || t('factory.noRecommendation')}
      </p>
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
        >{t('factory.accept')}</Button
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
          <h3>{t('factory.workerSummary')}</h3>
          <p class="factory-prose">{card.summary}</p>
        </section>{/if}
      {#if card.checks}<section class="factory-section" data-checks>
          <h3>{t('factory.checks')}</h3>
          <p class="factory-prose">{card.checks}</p>
        </section>{/if}
      {#if card.branch || card.head_sha || card.integration_reference}
        <section class="factory-section" data-submission>
          <h3>{t('factory.submission')}</h3>
          <dl class="factory-facts">
            <div>
              <dt>{t('factory.branch')}</dt>
              <dd class="factory-mono">{card.branch || '—'}</dd>
            </div>
            <div>
              <dt>{t('factory.base')}</dt>
              <dd class="factory-mono">{card.base || '—'}</dd>
            </div>
            {#if card.base_sha || card.head_sha}<div>
                <dt>{t('factory.commitRange')}</dt>
                <dd class="factory-mono">
                  {card.base_sha}..{card.head_sha}<CopyButton
                    text={`${card.base_sha}..${card.head_sha}`}
                  />
                </dd>
              </div>{/if}
            {#if card.integration_reference}<div>
                <dt>{t('factory.integrationReference')}</dt>
                <dd>
                  {#if /^https?:\/\//i.test(card.integration_reference)}<Button
                      variant="tertiary"
                      onClick={() => openReference(card.integration_reference)}
                      >{card.integration_reference} ↗</Button
                    >{:else}<span class="factory-mono"
                      >{card.integration_reference}</span
                    >{/if}
                </dd>
              </div>{/if}
          </dl>
        </section>
      {/if}
      {#if reviews.length}
        <section
          class="factory-section"
          aria-labelledby="factory-reviews-heading"
        >
          <h3 id="factory-reviews-heading">{t('factory.reviews')}</h3>
          {#each reviews as review, index (`${review.round}:${review.kind}:${index}`)}
            <article class="factory-review" data-review-round={review.round}>
              <header>
                <h4>
                  {t('factory.reviewRound', {
                    round: review.round,
                  })}{#if review.kind === 'integration_review'}
                    · {t('factory.integrationReview')}{/if}
                </h4>
                <span
                  >{review.verdict === 'approve'
                    ? t('factory.reviewApproved')
                    : t('factory.reviewChanges')}</span
                >
              </header>
              {#if review.recommendation}<p class="factory-prose">
                  {review.recommendation}
                </p>{/if}
              <h5>{t('factory.findings')}</h5>
              <ul class="factory-findings">
                {#each review.findings ?? [] as finding, index (index)}<li>
                    {typeof finding === 'string'
                      ? finding
                      : (finding.text ??
                        finding.description ??
                        JSON.stringify(finding))}
                  </li>{:else}<li class="factory-help">
                    {t('factory.noFindings')}
                  </li>{/each}
              </ul>
              {#if review.reviewed_head_sha}<p
                  class="factory-help factory-mono"
                >
                  {review.reviewed_head_sha}
                </p>{/if}
              <time class="factory-help" datetime={review.created_at}
                >{timestamp(review.created_at)}</time
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
            <strong>{event.event.replaceAll('_', ' ')}</strong><span
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
  />
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
  .factory-recommendation {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 6%, var(--surface-1));
    padding: 24px;
  }
  .factory-recommendation :global(.factory-prose) {
    color: var(--text-hi);
    font-size: 15px;
    line-height: 1.8;
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
  .factory-review + .factory-review {
    border-top: 1px solid var(--border);
    margin-top: 20px;
    padding-top: 16px;
  }
  .factory-review header {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    justify-content: space-between;
    font-size: 12px;
    margin-bottom: 14px;
  }
  .factory-review h4 {
    margin: 0;
    font-size: 13px;
  }
  .factory-review h5 {
    margin: 16px 0 8px;
    font-size: 12px;
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
