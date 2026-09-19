<script>
  import { onMount, onDestroy } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { useAutosaveContext } from '$lib/autosave.js';
  import {
    listDecisionExperiments,
    getDecisionExperiment,
    saveDecisionExperiment,
    deleteDecisionExperiment,
  } from '$lib/api.js';
  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import Modal from '../ui/Modal.svelte';
  import ExperimentEditor from './ExperimentEditor.svelte';
  import { experimentExample } from './examples.js';
  import './jev.css';

  let { onNavigateToSettingsPanel = () => {} } = $props();
  const transitions = useAutosaveContext();
  let experiments = $state([]);
  let experiment = $state(null);
  let available = $state(false);
  let loading = $state(true);
  let busy = $state(false);
  let query = $state('');
  let error = $state('');
  let deleting = $state(false);
  let selectionVersion = $state(0);
  let destroyed = false;
  let generation = 0;
  let filtered = $derived(
    experiments.filter((item) =>
      item.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
    ),
  );

  async function refresh() {
    try {
      const result = await listDecisionExperiments();
      if (destroyed) return;
      experiments = result.experiments;
      available = result.available;
      error = '';
      if (!experiment && experiments.length) await select(experiments[0].id);
    } catch (failure) {
      if (!destroyed) error = failure.message;
    } finally {
      loading = false;
    }
  }
  async function select(id) {
    return transitions.requestTransition(async () => {
      const current = ++generation;
      try {
        const result = await getDecisionExperiment(id);
        if (!destroyed && current === generation) {
          experiment = result;
          selectionVersion++;
          error = '';
        }
      } catch (failure) {
        if (!destroyed) error = failure.message;
      }
    });
  }
  async function create(kind) {
    return transitions.requestTransition(async () => {
      busy = true;
      try {
        const draft =
          kind === 'copy'
            ? JSON.parse(JSON.stringify(experiment.draft))
            : experimentExample(kind);
        if (kind === 'copy')
          draft.title = t('jev.copyTitle', '{title} (copy)', {
            title: draft.title,
          }).slice(0, 200);
        const created = await saveDecisionExperiment(draft);
        if (!destroyed) {
          experiment = created;
          await refresh();
        }
      } catch (failure) {
        if (!destroyed) error = failure.message;
      } finally {
        busy = false;
      }
    });
  }
  function saved(result) {
    experiment = result;
    experiments = experiments.map((item) =>
      item.id === result.id ? result : item,
    );
  }
  async function remove() {
    busy = true;
    try {
      await deleteDecisionExperiment(experiment.id, experiment.revision);
      experiment = null;
      deleting = false;
      await refresh();
    } catch (failure) {
      error = failure.message;
      deleting = false;
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    void refresh();
  });
  onDestroy(() => {
    destroyed = true;
    generation++;
  });
</script>

<div class="view active jev-view">
  <header class="jev-header">
    <div>
      <h2>Jev</h2>
      <p>
        {t(
          'jev.subtitle',
          'Ask focused questions. Inspect decisions. Connect actions.',
        )}
      </p>
    </div>
    <Button
      variant="tertiary"
      onClick={() => onNavigateToSettingsPanel('specialized_models')}
      >{t('jev.configure', 'Configure model')}</Button
    >
  </header>
  {#if !loading && !available}<div class="jev-notice">
      {t(
        'jev.configureHelp',
        'Select a Decision model in Specialized Models to evaluate questions or start a control. You can prepare experiments now.',
      )}
    </div>{/if}
  {#if error}<p class="jev-error" role="alert">{error}</p>{/if}
  <div class="jev-workspace">
    <aside class="jev-library" aria-label={t('jev.experiments', 'Experiments')}>
      <Button variant="primary" disabled={busy} onClick={() => create('blank')}
        >{t('jev.new', 'New experiment')}</Button
      >
      <TextField
        ariaLabel={t('jev.search', 'Search experiments')}
        placeholder={t('jev.search', 'Search experiments')}
        value={query}
        onInput={(value) => (query = value)}
      />
      {#if loading}<p role="status">{t('jev.loading', 'Loading…')}</p>{/if}
      <div class="jev-experiments">
        {#each filtered as item (item.id)}
          <Button
            variant={experiment?.id === item.id ? 'secondary' : 'tertiary'}
            aria-pressed={experiment?.id === item.id}
            onClick={() => select(item.id)}>{item.title}</Button
          >
        {/each}
      </div>
      <div class="jev-library-footer">
        <h3>{t('jev.examples', 'Start from an example')}</h3>
        <Button disabled={busy} onClick={() => create('triage')}
          >{t('jev.example.triage', 'Support triage')}</Button
        >
        <Button disabled={busy} onClick={() => create('routing')}
          >{t('jev.example.routing', 'Task requirements')}</Button
        >
        <Button variant="tertiary" onClick={refresh}
          >{t('jev.refresh', 'Refresh')}</Button
        >
      </div>
    </aside>
    <main class="jev-main">
      {#if experiment}
        <div class="jev-toolbar">
          <span>{t('jev.experiments', 'Experiments')} / {experiment.title}</span
          >
          <Button
            variant="tertiary"
            disabled={busy}
            onClick={() => create('copy')}
            >{t('jev.duplicate', 'Duplicate')}</Button
          >
          <Button
            variant="tertiary"
            onClick={() =>
              transitions.requestTransition(() => (deleting = true))}
            >{t('jev.delete', 'Delete')}</Button
          >
        </div>
        {#key `${experiment.id}:${selectionVersion}`}<ExperimentEditor
            {experiment}
            onSaved={saved}
            onReload={() => select(experiment.id)}
            {available}
          />{/key}
      {:else if !loading}
        <div class="jev-welcome">
          <div class="jev-eyebrow">
            {t('jev.playground', 'Decision playground')}
          </div>
          <h2>{t('jev.welcome', 'Give Jev something to decide.')}</h2>
          <p>
            {t(
              'jev.welcomeHelp',
              'Supply text or JSON and define choices, a rating scale, or a yes/no question. Every evaluation keeps its input, answers, model and usage for comparison.',
            )}
          </p>
          <p>
            {t(
              'jev.controlIntro',
              'For application control, connect a state-reading command and named actions. Jev chooses an action; vBot runs the command you assigned to it.',
            )}
          </p>
          <Button variant="primary" onClick={() => create('triage')}
            >{t('jev.tryExample', 'Open support triage example')}</Button
          >
        </div>
      {/if}
    </main>
  </div>
</div>
{#if deleting}
  <Modal
    title={t('jev.deleteTitle', 'Delete experiment?')}
    closeDisabled={busy}
    onClose={() => (deleting = false)}
  >
    {#snippet body()}<p class="jev-dialog-body">
        {t(
          'jev.deleteHelp',
          'This removes the experiment and all its evaluation history. Active work must be stopped first.',
        )}
      </p>{/snippet}
    {#snippet footer()}<Button
        disabled={busy}
        onClick={() => (deleting = false)}
        >{t('common.cancel', 'Cancel')}</Button
      ><Button variant="danger" disabled={busy} onClick={remove}
        >{t('jev.delete', 'Delete')}</Button
      >{/snippet}
  </Modal>
{/if}
