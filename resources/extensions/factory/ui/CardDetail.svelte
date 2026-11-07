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
  import { createRecordEditor } from './recordEditor.svelte.js';
  import {
    acceptLabel,
    cardActions,
    cardWorkflow,
    editableCard,
    eventLabel,
    stageKindLabel,
    stageText,
    stateLabel,
    queuedActionLabel,
    landingStatusLabel,
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
  const landing = $derived(card.integration_landing);
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
  const stages = $derived(workflow?.stages ?? []);
  const workStages = $derived(stages.filter((stage) => stage.kind === 'work'));
  // Send back without a choice returns to the work stage the current review
  // judges, or to the current work stage.
  const sendBackDefault = $derived.by(() => {
    const current = stages[card.stage?.index ?? 0];
    const id = current?.kind === 'review' ? current.target : current?.id;
    return stages.find((stage) => stage.id === id) ?? workStages[0];
  });
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
  let sendBackStage = $state('');
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
    sendBackStage = '';
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
              ...(sendBackStage ? { stage: sendBackStage } : {}),
            },
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

  function stageStatus(index) {
    if (card.state === 'done') return 'done';
    if (!card.stage) return 'pending';
    if (index < card.stage.index) return 'done';
    if (card.state === 'discarded') return 'pending';
    if (index > card.stage.index) return 'pending';
    return card.state === 'waiting' ? 'waiting' : 'current';
  }

  function stageStatusLabel(status) {
    switch (status) {
      case 'done':
        return t('factory.stageStatus.done');
      case 'current':
        return t('factory.stageStatus.current');
      case 'waiting':
        return t('factory.stageStatus.waiting');
      default:
        return t('factory.stageStatus.pending');
    }
  }

  function outputKindLabel(output) {
    switch (output.kind) {
      case 'review':
        return t('factory.output.review');
      case 'integration_review':
        return t('factory.integrationReview');
      default:
        return output.result
          ? t('factory.output.document')
          : t('factory.output.changes');
    }
  }

  // Lane and pass tell outputs of the same stage apart.
  function outputTitle(output) {
    const lanes =
      stages.find((stage) => stage.id === output.stage_id)?.lanes?.length ?? 0;
    const parts = [output.stage_name];
    if (lanes > 1 || output.lane > 0)
      parts.push(t('factory.laneNumber', { lane: output.lane + 1 }));
    if (output.pass > 1)
      parts.push(t('factory.passNumber', { pass: output.pass }));
    return parts.join(' · ');
  }

  // The newest document of each stage stays open; earlier passes fold.
  const latestDocuments = $derived(
    new Set(
      Object.values(
        Object.fromEntries(
          outputs
            .filter((output) => output.result)
            .map((output) => [`${output.stage_id}:${output.lane}`, output.id]),
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

  {#if landing}
    <Banner
      variant="warn"
      role="status"
      aria-labelledby="factory-landing-heading"
      class="factory-landing-notice"
      data-integration-landing={landing.status}
    >
      <div class="factory-landing-content">
        <h3 id="factory-landing-heading">
          {landingStatusLabel(landing.status)}
        </h3>
        <p class="factory-prose">{t('factory.landing.alreadyLanded')}</p>
        <p class="factory-prose">{t('factory.landing.noAutomaticRevert')}</p>
        {#if landing.base_sha && landing.head_sha !== card.head_sha}
          <p class="factory-help" data-landing-historical>
            {t('factory.landing.historical')}
          </p>
        {/if}
        {#if landing.landing_sha}
          <dl class="factory-facts" data-landing-commit>
            <div>
              <dt>{t('factory.landing.commit')}</dt>
              <dd class="factory-mono">
                {landing.landing_sha}<CopyButton
                  text={landing.landing_sha}
                  label={t('factory.landing.copyCommit')}
                />
              </dd>
            </div>
          </dl>
        {/if}
        {#if landing.extra_paths?.length}
          <div class="factory-landing-paths" data-landing-extra-paths>
            <h4>
              {t('factory.landing.extraPaths')}<CopyButton
                text={landing.extra_paths.join('\n')}
                label={t('factory.landing.copyExtraPaths')}
              />
            </h4>
            <ul class="factory-mono">
              {#each landing.extra_paths as path, index (index)}<li>
                  {path}
                </li>{/each}
            </ul>
          </div>
        {/if}
        {#if landing.merge_paths?.length}
          <div class="factory-landing-paths" data-landing-merge-paths>
            <h4>
              {t('factory.landing.mergePaths')}<CopyButton
                text={landing.merge_paths.join('\n')}
                label={t('factory.landing.copyMergePaths')}
              />
            </h4>
            <p class="factory-help">{t('factory.landing.mergeHelp')}</p>
            <ul class="factory-mono">
              {#each landing.merge_paths as path, index (index)}<li>
                  {path}
                </li>{/each}
            </ul>
          </div>
        {/if}
      </div>
    </Banner>
  {/if}

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
  {:else if card.state === 'waiting' && card.runtime?.note}
    <section class="factory-section factory-decision">
      <h3>{t('factory.waitingNote')}</h3>
      <p class="factory-prose">{card.runtime.note}</p>
    </section>
  {/if}

  {#if card.recommendation || (card.state === 'waiting' && card.waiting_reason === 'no_agreement')}
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
      <ol class="factory-stage-progress">
        {#each stages as stage, index (stage.id)}
          {@const status = stageStatus(index)}
          <li
            data-stage-status={status}
            aria-current={status === 'current' || status === 'waiting'
              ? 'step'
              : undefined}
          >
            <span class="factory-stage-marker" aria-hidden="true"
              >{status === 'done' ? '✓' : index + 1}</span
            >
            <span class="factory-stage-label"
              ><strong
                >{card.stage?.index === index
                  ? stageText(card.stage)
                  : stage.name}</strong
              ><span class="factory-help"
                >{stageKindLabel(stage)} · {stageStatusLabel(status)}</span
              ></span
            >
          </li>
        {/each}
      </ol>
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
              data-output-stage={output.stage_id}
            >
              <header>
                <h4>{outputTitle(output)}</h4>
                {#if output.kind === 'work'}<span class="factory-subtle"
                    >{outputKindLabel(output)}</span
                  >{:else}<span
                    class="factory-verdict"
                    data-verdict={output.verdict}
                    >{outputKindLabel(output)} · {output.verdict === 'approve'
                      ? t('factory.reviewApproved')
                      : t('factory.reviewChanges')}</span
                  >{/if}
              </header>
              {#if output.kind === 'work'}
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
                {#if output.branch || output.head_sha}
                  <dl class="factory-facts" data-output-change>
                    <div>
                      <dt>{t('factory.branch')}</dt>
                      <dd class="factory-mono">{output.branch || '—'}</dd>
                    </div>
                    <div>
                      <dt>{t('factory.base')}</dt>
                      <dd class="factory-mono">{output.base || '—'}</dd>
                    </div>
                    {#if output.base_sha || output.head_sha}<div>
                        <dt>{t('factory.commitRange')}</dt>
                        <dd class="factory-mono">
                          {output.base_sha}..{output.head_sha}<CopyButton
                            text={`${output.base_sha}..${output.head_sha}`}
                          />
                        </dd>
                      </div>{/if}
                  </dl>
                {/if}
                {#if output.checks}<h5>{t('factory.checks')}</h5>
                  <p class="factory-prose">{output.checks}</p>{/if}
              {:else}
                {#if output.recommendation}<p class="factory-prose">
                    {output.recommendation}
                  </p>{/if}
                <h5>{t('factory.findings')}</h5>
                <ul class="factory-findings">
                  {#each output.findings ?? [] as finding, index (index)}<li>
                      {typeof finding === 'string'
                        ? finding
                        : (finding.text ??
                          finding.description ??
                          JSON.stringify(finding))}
                    </li>{:else}<li class="factory-help">
                      {t('factory.noFindings')}
                    </li>{/each}
                </ul>
                {#if output.head_sha}<p class="factory-help factory-mono">
                    {output.head_sha}
                  </p>{/if}
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
  >
    {#snippet bodyExtra()}
      {#if confirm === 'discard' && landing}
        <Banner variant="warn" role="status" data-landing-discard-warning
          >{t('factory.landing.discardWarning')}</Banner
        >
      {/if}
    {/snippet}
  </ConfirmDialog>
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
        {#if compose === 'send_back' && workStages.length > 1}<div
            class="factory-field"
          >
            <label
              id="factory-send-back-stage-label"
              for="factory-send-back-stage">{t('factory.sendBackStage')}</label
            ><Dropdown
              id="factory-send-back-stage"
              ariaLabelledby="factory-send-back-stage-label"
              value={sendBackStage}
              options={[
                {
                  value: '',
                  label: t('factory.sendBackDefault', {
                    stage: sendBackDefault?.name ?? '',
                  }),
                },
                ...workStages.map((stage) => ({
                  value: stage.id,
                  label: stage.name,
                })),
              ]}
              disabled={Boolean(model.pending)}
              onOpenChange={model.pickerOpen}
              onValueChange={(value) => (sendBackStage = value)}
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
  .factory-output .factory-document,
  .factory-output .factory-facts {
    margin-top: 12px;
  }
  .factory-verdict[data-verdict='approve'] {
    color: var(--green, var(--accent));
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
  .factory-stage-progress {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 10px 22px;
  }
  .factory-stage-progress li {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    font-size: 12px;
  }
  .factory-stage-label {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .factory-stage-label .factory-help {
    margin: 0;
    font-size: 11px;
  }
  .factory-stage-marker {
    display: inline-grid;
    place-items: center;
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    border: 1px solid var(--border);
    color: var(--text-med);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
  }
  [data-stage-status='done'] .factory-stage-marker {
    background: var(--surface-3);
    color: var(--text-hi);
  }
  [data-stage-status='current'] .factory-stage-marker,
  [data-stage-status='waiting'] .factory-stage-marker {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 14%, var(--surface-1));
    color: var(--text-hi);
  }
  [data-stage-status='pending'] .factory-stage-label strong {
    color: var(--text-med);
    font-weight: 500;
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
  :global(.factory-landing-notice) {
    margin-bottom: 18px;
  }
  .factory-landing-content {
    width: 100%;
    min-width: 0;
  }
  .factory-landing-content h3 {
    margin: 0 0 10px;
    font-size: 14px;
  }
  .factory-landing-content p + p {
    margin-top: 8px;
  }
  .factory-landing-paths {
    margin-top: 18px;
  }
  .factory-landing-paths h4 {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0 0 6px;
    font-size: 12px;
  }
  .factory-landing-paths ul {
    max-height: 240px;
    overflow: auto;
    margin: 8px 0 0;
    padding-left: 20px;
    font-size: 12px;
    line-height: 1.7;
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
