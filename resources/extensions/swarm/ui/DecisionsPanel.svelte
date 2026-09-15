<script>
  import { onMount, onDestroy, untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import MarkdownContent from '../../../../webui/src/components/chat/MarkdownContent.svelte';
  import { requestId } from './pagePresentation.js';
  import { createPageRefresh } from './pageRefresh.js';

  let { swarmId, client, contentLinks } = $props();
  let questions = $state([]),
    listNext = $state(null),
    query = $state(''),
    archived = $state(false);
  let detail = $state(null),
    positions = $state([]),
    positionsNext = $state(null),
    history = $state(null);
  let form = $state(null),
    baseline = '',
    error = $state(''),
    busy = $state(false),
    pending = $state.raw(null);
  let readGeneration = 0,
    listGeneration = 0,
    disposed = false,
    retry = null;
  const call = (args) =>
    client.operation('decisions', { swarm_id: swarmId, ...args });
  const dirty = () => form !== null && JSON.stringify(form) !== baseline;
  const backgroundRefresh = createPageRefresh(async () => {
    await Promise.all([
      refresh(),
      detail && !form && !busy
        ? readQuestion(detail.question.question_id)
        : undefined,
    ]);
  });
  const participant = {
    hasPending: () => dirty() || busy,
    flush: async () => !dirty() && !busy,
    runSave: async () => false,
  };
  $effect(() => {
    if (form) return untrack(() => client.registerAutosave(participant));
  });
  $effect(() => {
    dirty();
    client.notifyAutosave();
  });
  onMount(() => {
    void backgroundRefresh.run();
    return client.onInvalidation(backgroundRefresh.schedule);
  });
  onDestroy(() => {
    disposed = true;
    backgroundRefresh.destroy();
    readGeneration++;
    listGeneration++;
  });
  export function requestTransition(action) {
    if (dirty() || busy) {
      pending = action;
      return;
    }
    form = null;
    pending = null;
    return action();
  }
  export function openQuestion(id) {
    return requestTransition(() => readQuestion(id));
  }
  async function refresh(continuation) {
    const generation = ++listGeneration;
    try {
      const result = await call(
        continuation ?? {
          action: 'list',
          include_archived: archived,
          ...(query.trim() ? { query: query.trim() } : {}),
        },
      );
      if (disposed || generation !== listGeneration) return;
      questions = continuation
        ? [...questions, ...result.entries]
        : result.entries;
      listNext = result.next_call?.arguments;
    } catch (cause) {
      if (!disposed && generation === listGeneration) error = cause.message;
    }
  }
  async function readQuestion(id) {
    const generation = ++readGeneration;
    error = '';
    try {
      const result = await call({
        action: 'read',
        question_id: id,
        limit: 100,
      });
      if (disposed || generation !== readGeneration) return;
      detail = result;
      positions = result.positions;
      positionsNext = result.positions_next_call?.arguments;
      history = null;
    } catch (cause) {
      if (!disposed && generation === readGeneration) error = cause.message;
    }
  }
  function edit(action, option) {
    return requestTransition(() => {
      readGeneration++;
      form = {
        action,
        ...(action !== 'create'
          ? {
              question_id: detail.question.question_id,
              expected_revision: detail.question.revision,
            }
          : {}),
        ...(action === 'position'
          ? {
              position_revision: detail.position_revision,
              note: detail.my_position?.note ?? '',
              ...(option ? { option_id: option.option_id } : {}),
            }
          : {
              title:
                action === 'update'
                  ? detail.question.title
                  : (option?.title ?? ''),
              text:
                action === 'update'
                  ? detail.question.text
                  : (option?.text ?? ''),
              ...(option ? { option_id: option.option_id } : {}),
            }),
      };
      baseline = JSON.stringify(form);
      retry = null;
      error = '';
    });
  }
  async function mutate(args) {
    const fingerprint = JSON.stringify(args);
    if (retry?.fingerprint !== fingerprint)
      retry = { fingerprint, id: requestId() };
    busy = true;
    error = '';
    try {
      const result = await call({ ...args, request_id: retry.id });
      if (disposed) return;
      retry = null;
      form = null;
      pending = null;
      await readQuestion(result.question.question_id);
      await refresh();
    } catch (cause) {
      if (!disposed) error = cause.message;
    } finally {
      busy = false;
    }
  }
  async function morePositions() {
    const generation = readGeneration;
    try {
      const result = await call(positionsNext);
      if (disposed || generation !== readGeneration) return;
      positions = [...positions, ...result.entries];
      positionsNext = result.next_call?.arguments;
    } catch (cause) {
      if (!disposed) error = cause.message;
    }
  }
  async function loadHistory(continuation) {
    const generation = readGeneration;
    try {
      const result = await call(
        continuation ?? {
          action: 'history',
          question_id: detail.question.question_id,
        },
      );
      if (disposed || generation !== readGeneration) return;
      history = {
        ...result,
        entries: continuation
          ? [...history.entries, ...result.entries]
          : result.entries,
      };
    } catch (cause) {
      if (!disposed) error = cause.message;
    }
  }
  const optionTitle = (id) =>
    detail?.entries.find((option) => option.option_id === id)?.title ??
    t('swarm.decisions.undecided', 'No preferred option');
  function discard() {
    if (busy) return;
    form = null;
    error = '';
    const action = pending;
    pending = null;
    action?.();
  }
</script>

<div
  class="decisions-panel"
  role="tabpanel"
  tabindex="0"
  aria-label={t('swarm.decisions.title', 'Decisions')}
>
  <div class="decision-actions">
    <h3>{t('swarm.decisions.title', 'Decisions')}</h3>
    <Button onClick={() => edit('create')}
      >{t('swarm.decisions.new', 'New question')}</Button
    >
  </div>
  <p class="hint">
    {t(
      'swarm.decisions.help',
      'Explore alternatives and keep current positions together. Support counts do not declare a winner.',
    )}
  </p>
  {#if error}<Banner variant="error">{error}</Banner>{/if}
  {#if pending}<Banner
      >{t(
        'swarm.decisions.pending',
        'Submit or discard your draft before leaving.',
      )}<Button disabled={busy} onClick={discard}
        >{t(
          'swarm.decisions.discardContinue',
          'Discard draft and continue',
        )}</Button
      ></Banner
    >{/if}
  <div class="decision-columns">
    <aside>
      <form
        onkeydown={(event) => {
          if (event.key === 'Enter' && event.target.tagName === 'INPUT') {
            event.preventDefault();
            void refresh();
          }
        }}
        onsubmit={(event) => {
          event.preventDefault();
          void refresh();
        }}
      >
        <FormField
          controlId="decision-search"
          label={t('swarm.decisions.search', 'Search questions')}
          ><TextField
            id="decision-search"
            value={query}
            onInput={(value) => (query = value)}
          /></FormField
        >
        <Button onClick={() => refresh()} variant="tertiary"
          >{t('common.search', 'Search')}</Button
        >
      </form>
      <label
        ><input
          type="checkbox"
          bind:checked={archived}
          onchange={() => void refresh()}
        />{t('swarm.decisions.archived', 'Include archived questions')}</label
      >
      {#each questions as question (question.question_id)}<button
          class="question-link"
          class:selected={detail?.question.question_id === question.question_id}
          onclick={() => openQuestion(question.question_id)}
          >{question.title}{#if question.archived}<small
              >{t('swarm.decisions.closed', 'Archived')}</small
            >{/if}</button
        >{:else}<p>{t('swarm.decisions.empty', 'No questions yet.')}</p>{/each}
      {#if listNext}<Button variant="tertiary" onClick={() => refresh(listNext)}
          >{t('common.loadMore', 'Load more')}</Button
        >{/if}
    </aside>
    <article use:contentLinks>
      {#if form}
        <form
          onkeydown={(event) => {
            if (event.key === 'Enter' && event.target.tagName === 'INPUT') {
              event.preventDefault();
              if (!busy) void mutate({ ...form });
            }
          }}
          onsubmit={(event) => {
            event.preventDefault();
            void mutate({ ...form });
          }}
        >
          {#if form.action === 'position'}
            <h3>{t('swarm.decisions.yourPosition', 'Your position')}</h3>
            <p>{optionTitle(form.option_id)}</p>
            <FormField
              controlId="decision-note"
              label={t(
                'swarm.decisions.reason',
                'Reason, concern or condition (optional)',
              )}
              ><TextArea
                id="decision-note"
                disabled={busy}
                value={form.note}
                onInput={(value) => (form.note = value)}
                rows={5}
              /></FormField
            >
          {:else}
            <FormField
              controlId="decision-title"
              label={t('swarm.decisions.heading', 'Title')}
              ><TextField
                id="decision-title"
                disabled={busy}
                value={form.title}
                onInput={(value) => (form.title = value)}
              /></FormField
            >
            <FormField
              controlId="decision-text"
              label={t(
                'swarm.decisions.context',
                'Context and evidence (Markdown)',
              )}
              ><TextArea
                id="decision-text"
                disabled={busy}
                value={form.text}
                onInput={(value) => (form.text = value)}
                rows={6}
              /></FormField
            >
          {/if}
          <div class="decision-actions">
            <Button variant="tertiary" disabled={busy} onClick={discard}
              >{t('common.cancel', 'Cancel')}</Button
            ><Button onClick={() => mutate({ ...form })} loading={busy}
              >{t('swarm.decisions.submit', 'Submit')}</Button
            >
          </div>
        </form>
      {:else if detail}
        <h3>{detail.question.title}</h3>
        <p class="hint">
          {t('swarm.decisions.revision', 'Question version {revision}', {
            revision: detail.question.revision,
          })}
        </p>
        <MarkdownContent source={detail.question.text} class="msg-markdown" />
        <TextField
          ariaLabel={t('swarm.decisions.reference', 'Question reference')}
          value={detail.question.link}
          readonly
        />
        <div class="decision-actions">
          <Button
            variant="tertiary"
            disabled={busy}
            onClick={() => edit('update')}
            >{t('swarm.decisions.editQuestion', 'Edit question')}</Button
          >
          <Button
            variant="tertiary"
            disabled={busy}
            onClick={() => loadHistory()}
            >{t('swarm.decisions.history', 'History')}</Button
          >
          <Button
            variant="tertiary"
            disabled={busy}
            onClick={() =>
              mutate({
                action: 'reconsider',
                question_id: detail.question.question_id,
                expected_revision: detail.question.revision,
              })}>{t('swarm.decisions.reconsider', 'Reconsider')}</Button
          >
          <Button
            variant="tertiary"
            disabled={busy}
            onClick={() =>
              mutate({
                action: 'update',
                question_id: detail.question.question_id,
                expected_revision: detail.question.revision,
                archived: !detail.question.archived,
              })}
            >{detail.question.archived
              ? t('swarm.decisions.reopen', 'Reopen')
              : t('swarm.decisions.archive', 'Archive')}</Button
          >
        </div>
        {#if detail.question.archived}<Banner
            >{t(
              'swarm.decisions.archiveHelp',
              'Archived. This does not indicate collective agreement. Reopen to add alternatives or positions.',
            )}</Banner
          >{/if}
        {#if detail.participation.needs_review}<Banner
            >{t(
              'swarm.decisions.review',
              '{count} positions refer to an earlier question version.',
              { count: detail.participation.needs_review },
            )}</Banner
          >{/if}
        <div class="decision-actions">
          <h4>{t('swarm.decisions.options', 'Options')}</h4>
          <Button
            disabled={busy || detail.question.archived}
            onClick={() => edit('add_option')}
            >{t('swarm.decisions.addOption', 'Add option')}</Button
          >
        </div>
        {#each detail.entries as option (option.option_id)}
          <section class="decision-option">
            <h4>
              {option.title}{#if option.withdrawn}
                — {t('swarm.decisions.withdrawn', 'Withdrawn')}{/if}
            </h4>
            <MarkdownContent source={option.text} class="msg-markdown" />
            <p class="hint">
              {t(
                'swarm.decisions.support',
                '{count} support · {reviewed} reviewed the current version',
                { count: option.support, reviewed: option.reviewed_support },
              )}
            </p>
            <div class="decision-actions">
              <Button
                disabled={busy || option.withdrawn || detail.question.archived}
                onClick={() => edit('position', option)}
                >{t(
                  'swarm.decisions.supportAction',
                  'Support this option',
                )}</Button
              >
              <Button
                variant="tertiary"
                disabled={busy || detail.question.archived}
                onClick={() => edit('update_option', option)}
                >{t('swarm.decisions.editOption', 'Edit option')}</Button
              >
              <Button
                variant="tertiary"
                disabled={busy || detail.question.archived}
                onClick={() =>
                  mutate({
                    action: 'update_option',
                    question_id: detail.question.question_id,
                    expected_revision: detail.question.revision,
                    option_id: option.option_id,
                    withdrawn: !option.withdrawn,
                  })}
                >{option.withdrawn
                  ? t('swarm.decisions.restoreOption', 'Restore option')
                  : t(
                      'swarm.decisions.withdrawOption',
                      'Withdraw option',
                    )}</Button
              >
            </div>
          </section>
        {/each}
        <div class="decision-actions">
          <h4>{t('swarm.decisions.positions', 'Positions')}</h4>
          <Button
            variant="tertiary"
            disabled={busy || detail.question.archived}
            onClick={() => edit('position')}
            >{t(
              'swarm.decisions.expressConcern',
              'Add an undecided position or concern',
            )}</Button
          >
          {#if detail.my_position && !detail.my_position.withdrawn}<Button
              variant="tertiary"
              disabled={busy}
              onClick={() =>
                mutate({
                  action: 'withdraw',
                  question_id: detail.question.question_id,
                  position_revision: detail.position_revision,
                })}
              >{t(
                'swarm.decisions.withdrawPosition',
                'Withdraw my position',
              )}</Button
            >{/if}
        </div>
        <p class="hint">
          {t(
            'swarm.decisions.participation',
            '{count} participants have not stated a position.',
            { count: detail.participation.not_positioned },
          )}
        </p>
        {#each positions as position (position.author.id)}<section
            class="decision-position"
          >
            <strong>{position.author.name}</strong> · {position.withdrawn
              ? t('swarm.decisions.withdrawn', 'Withdrawn')
              : optionTitle(position.option_id)}
            {#if position.needs_review}<p class="hint">
                {t(
                  'swarm.decisions.earlierPosition',
                  'Based on question version {revision}; not yet confirmed for the current version.',
                  { revision: position.question_revision },
                )}
              </p>{/if}
            <MarkdownContent source={position.note} class="msg-markdown" />
          </section>{/each}
        {#if positionsNext}<Button variant="tertiary" onClick={morePositions}
            >{t('common.loadMore', 'Load more')}</Button
          >{/if}
        {#if history}<h4>{t('swarm.decisions.history', 'History')}</h4>
          {#each history.entries as event (event.event_id)}<section
              class="decision-position"
            >
              <strong>{event.author.name}</strong> · {t(
                `swarm.decisions.event.${event.action}`,
                {
                  create: 'Opened question',
                  update: 'Updated question',
                  add_option: 'Added option',
                  update_option: 'Updated option',
                  position: 'Stated position',
                  withdraw: 'Withdrew position',
                  reconsider: 'Reopened consideration',
                }[event.action],
              )}<MarkdownContent
                source={[
                  event.change.title,
                  event.change.text,
                  event.change.note,
                ]
                  .filter(Boolean)
                  .join('\n\n')}
                class="msg-markdown"
              />
            </section>{/each}{#if history.next_call}<Button
              variant="tertiary"
              onClick={() => loadHistory(history.next_call.arguments)}
              >{t('common.loadMore', 'Load more')}</Button
            >{/if}{/if}
      {:else}<p>
          {t(
            'swarm.decisions.choose',
            'Choose a question or open one for the group.',
          )}
        </p>{/if}
    </article>
  </div>
</div>

<style>
  .decisions-panel {
    padding: 1rem;
  }
  .decision-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin: 0.75rem 0;
  }
  .decision-actions h3,
  .decision-actions h4 {
    margin-right: auto;
  }
  .decision-columns {
    display: grid;
    grid-template-columns: minmax(180px, 28%) minmax(0, 1fr);
    gap: 1.5rem;
    margin-top: 1rem;
  }
  aside {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  article {
    min-width: 0;
  }
  .question-link {
    padding: 0.75rem;
    text-align: left;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-hi);
    background: var(--surface);
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  .question-link.selected {
    border-color: var(--accent);
  }
  .question-link small {
    display: block;
  }
  .decision-option,
  .decision-position {
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.8rem;
    margin: 0.7rem 0;
    overflow-wrap: anywhere;
  }
  .hint {
    color: var(--text-med);
    margin: 0.5rem 0;
  }
  label {
    display: flex;
    gap: 0.5rem;
  }
  @media (max-width: 650px) {
    .decision-columns {
      grid-template-columns: 1fr;
    }
  }
</style>
