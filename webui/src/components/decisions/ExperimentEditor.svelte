<script>
  import { onMount, onDestroy, untrack } from 'svelte';
  import Button from '../ui/Button.svelte';
  import TabList from '../ui/TabList.svelte';
  import TextField from '../ui/TextField.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import ControlEditor from './ControlEditor.svelte';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import QuestionEditor from './QuestionEditor.svelte';
  import DecisionResults from './DecisionResults.svelte';
  import { newQuestion } from './examples.js';
  import { t } from '$lib/i18n.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import {
    saveDecisionExperiment,
    getDecisionHistory,
    getDecisionResult,
    startDecisionEvaluation,
    cancelDecisionEvaluation,
  } from '$lib/api.js';

  let {
    experiment,
    onSaved = () => {},
    onReload = () => {},
    available = true,
  } = $props();
  const initial = untrack(() => experiment);
  const id = initial.id;
  let revision = $state(initial.revision);
  let draft = $state(JSON.parse(JSON.stringify(initial.draft)));
  const componentId = $props.id();
  let mode = $state(
    initial.draft.control && !initial.draft.questions.length
      ? 'control'
      : 'evaluate',
  );
  let jsonMode = $state(typeof initial.draft.state !== 'string');
  let stateText = $state(
    typeof initial.draft.state === 'string'
      ? initial.draft.state
      : JSON.stringify(initial.draft.state, null, 2),
  );
  let baseline = $state(
    untrack(() =>
      JSON.stringify({ draft: initial.draft, stateText, jsonMode }),
    ),
  );
  let error = $state('');
  let conflict = $state(false);
  let saved = $state(false);
  let saving = $state(false);
  let busy = $state(false);
  let panel = $state('setup');
  let panelChosen = false;
  let history = $state([]);
  let active = $derived(history.find((item) => item.status === 'running'));
  let before = $state(null);
  let selected = $state(null);
  let comparison = $state(null);
  let historyError = $state('');
  let destroyed = false;
  let pollTimer;
  let selectionGeneration = 0;
  let pendingStart = null;
  const snapshot = () => ({ draft, stateText, jsonMode });
  const hasChanges = () => JSON.stringify(snapshot()) !== baseline;
  const autosave = createDebouncedAutosave({
    getSnapshot: snapshot,
    hasChanges,
    save,
  });
  const transitions = useAutosaveContext();
  const unregister = transitions.register(autosave.participant);
  $effect(() => {
    if (hasChanges()) panelChosen = true;
    autosave.scheduleRun();
  });

  function payload() {
    const state = jsonMode ? JSON.parse(stateText) : stateText;
    if (jsonMode && (state === null || typeof state !== 'object'))
      throw new Error(
        t('jev.jsonStateError', 'JSON state must be an object or array.'),
      );
    return { ...JSON.parse(JSON.stringify(draft)), state };
  }

  async function save(reason) {
    if (!hasChanges()) {
      if (reason === 'manual') saved = true;
      return true;
    }
    const captured = JSON.stringify(snapshot());
    saving = true;
    try {
      const result = await saveDecisionExperiment(payload(), id, revision);
      revision = result.revision;
      baseline = captured;
      error = '';
      conflict = false;
      saved = true;
      onSaved(result);
      return true;
    } catch (failure) {
      error = failure.message;
      conflict = failure.code === 'conflict';
      return false;
    } finally {
      saving = false;
    }
  }

  async function loadHistory(append = false) {
    try {
      const result = await getDecisionHistory(id, append ? before : undefined);
      if (destroyed) return;
      history = append
        ? [...history, ...result.evaluations]
        : result.evaluations;
      before = result.next_before;
      historyError = '';
      if (!selected && history[0]) await inspect(history[0].id);
      if (history.some((item) => item.status === 'running')) schedulePoll();
    } catch (failure) {
      if (!destroyed) historyError = failure.message;
    }
  }

  function reloadSaved() {
    autosave.cancelPendingTimer();
    baseline = JSON.stringify(snapshot());
    void onReload();
  }

  async function inspect(evaluationId, compare = false) {
    const generation = ++selectionGeneration;
    try {
      const value = await getDecisionResult(evaluationId);
      if (destroyed || generation !== selectionGeneration) return;
      if (compare) comparison = value;
      else selected = value;
    } catch (failure) {
      if (!destroyed) historyError = failure.message;
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(
      async () => {
        const active = history.find((item) => item.status === 'running');
        if (!active || destroyed) return;
        try {
          const result = await getDecisionResult(active.id);
          if (destroyed) return;
          if (selected?.id === result.id) selected = result;
          if (comparison?.id === result.id) comparison = result;
          await loadHistory();
        } catch (failure) {
          if (!destroyed) historyError = failure.message;
        } finally {
          if (!destroyed && history.some((item) => item.status === 'running'))
            schedulePoll();
        }
      },
      historyError ? 3000 : 700,
    );
  }

  async function evaluate() {
    if (busy || !(await autosave.participant.flush())) return;
    busy = true;
    error = '';
    const requestKey = `${id}:${revision}:${mode}`;
    if (pendingStart?.key !== requestKey)
      pendingStart = { key: requestKey, id: crypto.randomUUID() };
    try {
      selected = await startDecisionEvaluation(
        id,
        revision,
        pendingStart.id,
        mode,
      );
      pendingStart = null;
      panel = 'results';
      await loadHistory();
    } catch (failure) {
      error = failure.message;
    } finally {
      busy = false;
    }
  }

  async function cancel() {
    const active = history.find((item) => item.status === 'running');
    if (!active) return;
    busy = true;
    try {
      const result = await cancelDecisionEvaluation(active.id);
      if (selected?.id === result.id) selected = result;
      await loadHistory();
    } catch (failure) {
      error = failure.message;
    } finally {
      busy = false;
    }
  }

  function addQuestion(type) {
    let index = draft.questions.length + 1;
    while (draft.questions.some((q) => q.id === `question_${index}`)) index++;
    draft.questions = [
      ...draft.questions,
      newQuestion(type, `question_${index}`),
    ];
  }

  function restore() {
    panel = 'setup';
    mode = selected.snapshot.mode;
    const source = selected.snapshot;
    draft = {
      title: source.title,
      state: source.state,
      questions: JSON.parse(JSON.stringify(source.questions)),
      ...(source.control
        ? { control: JSON.parse(JSON.stringify(source.control)) }
        : {}),
    };
    jsonMode = typeof source.state !== 'string';
    stateText = jsonMode ? JSON.stringify(source.state, null, 2) : source.state;
  }

  onMount(() => {
    void loadHistory().then(() => {
      if (!destroyed && !panelChosen && !hasChanges() && history.length)
        panel = 'results';
    });
  });
  onDestroy(() => {
    destroyed = true;
    selectionGeneration++;
    clearTimeout(pollTimer);
    autosave.cancelPendingTimer();
    unregister();
  });
</script>

<div class="jev-editor">
  <div class="jev-editor-bar">
    <TabList
      idPrefix={`${componentId}-panels`}
      ariaLabel={t('jev.experimentSections', 'Experiment sections')}
      items={[
        { id: 'setup', label: t('jev.setup', 'Setup') },
        { id: 'results', label: t('jev.results', 'Results') },
      ]}
      value={panel}
      onChange={(value) => {
        panelChosen = true;
        panel = value;
      }}
    />
    <div class="jev-actions">
      {#if active}
        <Button disabled={busy} onClick={cancel}
          >{(selected?.id === active.id ? selected.snapshot.mode : mode) ===
          'control'
            ? t('jev.stopControl', 'Stop control')
            : t('jev.cancel', 'Cancel evaluation')}</Button
        >
      {:else}
        <Button
          variant="primary"
          disabled={busy || !available}
          onClick={evaluate}
          >{mode === 'control'
            ? t('jev.startControl', 'Start control')
            : panel === 'results'
              ? t('jev.evaluateSetup', 'Evaluate setup')
              : t('jev.evaluate', 'Evaluate')}</Button
        >
      {/if}
    </div>
  </div>
  {#if error}<p class="jev-error" role="alert">{error}</p>{/if}
  {#if conflict}<Button onClick={reloadSaved}
      >{t(
        'jev.reloadSaved',
        'Discard local edits and reload saved version',
      )}</Button
    >{/if}
  <div
    class="jev-inputs"
    role="tabpanel"
    id={`${componentId}-panels-panel-setup`}
    aria-labelledby={`${componentId}-panels-tab-setup`}
    hidden={panel !== 'setup'}
    tabindex="0"
  >
    <div class="jev-field">
      <label for={`${componentId}-title`}
        >{t('jev.title', 'Experiment title')}</label
      ><TextField
        id={`${componentId}-title`}
        value={draft.title}
        onInput={(value) => (draft.title = value)}
      />
    </div>
    <div class="jev-actions">
      <Button
        aria-pressed={mode === 'evaluate'}
        variant={mode === 'evaluate' ? 'secondary' : 'tertiary'}
        onClick={() => (mode = 'evaluate')}
        >{t('jev.questions', 'Questions')}</Button
      >
      <Button
        aria-pressed={mode === 'control'}
        variant={mode === 'control' ? 'secondary' : 'tertiary'}
        onClick={() => {
          mode = 'control';
          draft.control ??= {
            instructions: '',
            observe: { argv: [''], cwd: '' },
            actions: {
              act: { description: '', command: { argv: [''], cwd: '' } },
              wait: { description: 'No action is needed yet.', command: null },
            },
            interval_ms: 0,
            max_steps: 100,
            timeout_seconds: 10,
          };
        }}>{t('jev.control', 'Application control')}</Button
      >
    </div>
    {#if mode === 'control'}
      <ControlEditor
        control={draft.control}
        onChange={(value) => (draft.control = value)}
      />
    {:else}
      <div class="jev-row">
        <h3>
          <label for={`${componentId}-state`}
            >{t('jev.stateInput', 'State · What Jev reads')}</label
          >
        </h3>
        <div class="jev-actions">
          <Button
            variant={jsonMode ? 'tertiary' : 'secondary'}
            aria-pressed={!jsonMode}
            onClick={() => (jsonMode = false)}>{t('jev.text', 'Text')}</Button
          >
          <Button
            variant={jsonMode ? 'secondary' : 'tertiary'}
            aria-pressed={jsonMode}
            onClick={() => (jsonMode = true)}>{t('jev.json', 'JSON')}</Button
          >
        </div>
      </div>
      <p class="jev-help">
        {t(
          'jev.stateHelp',
          'The text or data to evaluate. Every question below refers to this same state. Edit it to try a different situation.',
        )}
      </p>
      <TextArea
        id={`${componentId}-state`}
        ariaLabel={t('jev.state', 'State')}
        class="jev-state-input"
        value={stateText}
        rows={6}
        code={jsonMode}
        onInput={(value) => (stateText = value)}
      />
      <h3>{t('jev.questions', 'Questions')}</h3>
      <p class="jev-help">
        {t(
          'jev.questionsAboutState',
          'Each question is answered independently using the state above.',
        )}
      </p>
      {#each draft.questions as question, index (index)}
        <QuestionEditor
          {question}
          {index}
          onChange={(value) =>
            (draft.questions = draft.questions.map((q, i) =>
              i === index ? value : q,
            ))}
          onRemove={() =>
            (draft.questions = draft.questions.filter((_, i) => i !== index))}
        />
      {/each}
      <div class="jev-actions">
        <Button onClick={() => addQuestion('choice')}
          >+ {t('jev.choice', 'Choice')}</Button
        >
        <Button onClick={() => addQuestion('score')}
          >+ {t('jev.score', 'Score')}</Button
        >
        <Button onClick={() => addQuestion('noul')}
          >+ {t('jev.noul', 'Yes / no · Noul')}</Button
        >
      </div>
    {/if}
    <div class="jev-row jev-submit">
      <span class="jev-help" role="status"
        >{saving
          ? t('jev.saving', 'Saving…')
          : !hasChanges() && saved
            ? t('jev.saved', 'Saved')
            : ''}</span
      >
      <Button
        variant="tertiary"
        onClick={() => autosave.participant.runSave('manual', { force: true })}
        >{t('jev.save', 'Save')}</Button
      >
    </div>
  </div>
  <div
    class="jev-history"
    role="tabpanel"
    id={`${componentId}-panels-panel-results`}
    aria-labelledby={`${componentId}-panels-tab-results`}
    hidden={panel !== 'results'}
    tabindex="0"
  >
    {#if historyError}<p role="alert">{historyError}</p>
      <Button onClick={() => loadHistory()}>{t('common.retry', 'Retry')}</Button
      >{/if}
    {#if !history.length && !historyError}<div class="jev-empty">
        {t(
          'jev.noResults',
          'Evaluate your questions to see their answers here.',
        )}
      </div>{/if}
    {#if history.length}<details class="jev-history-picker">
        <summary
          >{t('jev.history', 'Evaluation history')} · {history.length}</summary
        >
        <p class="jev-help">
          {t(
            'jev.historyHelp',
            'Each result keeps its original input and model. Compare two results, or reuse a previous input.',
          )}
        </p>
        <div class="jev-history-list">
          {#each history as item (item.id)}
            <div class="jev-row">
              <Button
                variant={selected?.id === item.id ? 'secondary' : 'tertiary'}
                onClick={() => inspect(item.id)}
                >{formatDateTimeInApplicationZone(item.created_at, undefined, {
                  dateStyle: 'short',
                  timeStyle: 'medium',
                })} · {t(`jev.status.${item.status}`, item.status)}</Button
              >
              <Button
                variant="tertiary"
                disabled={selected?.id === item.id}
                onClick={() => inspect(item.id, true)}
                >{t('jev.compare', 'Compare')}</Button
              >
            </div>
          {/each}
          {#if before}<Button
              variant="tertiary"
              onClick={() => loadHistory(true)}
              >{t('jev.loadMore', 'Load older results')}</Button
            >{/if}
        </div>
      </details>{/if}
    {#if selected}<div class="jev-actions">
        <Button variant="tertiary" onClick={restore}
          >{t('jev.reuse', 'Reuse this input')}</Button
        >{#if comparison}<Button
            variant="tertiary"
            onClick={() => (comparison = null)}
            >{t('jev.closeCompare', 'Close comparison')}</Button
          >{/if}
      </div>{/if}
    {#if comparison && selected && JSON.stringify(comparison.snapshot.questions) !== JSON.stringify(selected.snapshot.questions)}<p
        class="jev-help"
      >
        {t(
          'jev.changedQuestions',
          'These evaluations use different questions or criteria. Compare each result against its own input.',
        )}
      </p>{/if}
    <div class:jev-comparison={comparison !== null}>
      <DecisionResults record={selected} />{#if comparison}<DecisionResults
          record={comparison}
        />{/if}
    </div>
  </div>
</div>
