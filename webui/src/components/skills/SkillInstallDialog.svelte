<script>
  import { onDestroy, untrack } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { installSkill, installSkillArchive } from '$lib/api.js';
  import Modal from '../ui/Modal.svelte';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';
  import FileDropZone from '../ui/FileDropZone.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import PathField from '../ui/PathField.svelte';
  import TabList from '../ui/TabList.svelte';
  import Dropdown from '../Dropdown.svelte';

  let {
    initialScope = 'global',
    scopeOptions = [],
    onClose,
    onInstalled,
  } = $props();
  let scope = $state(untrack(() => initialScope));
  let mode = $state('link');
  let source = $state('');
  let file = $state.raw(null);
  let packagePath = $state('');
  let revision = $state('');
  let picked = $state([]);
  let jobs = $state([]);
  let busy = $state(false);
  let disposed = false;
  let request;
  onDestroy(() => {
    disposed = true;
    request?.abort();
  });

  const archiveLimit = 64 * 1024 * 1024;
  const sources = $derived([
    ...new Set([...picked, source.trim()].filter(Boolean)),
  ]);
  const sourceReady = $derived(
    mode === 'file' ? file && file.size <= archiveLimit : sources.length > 0,
  );
  const completed = $derived(jobs.filter((job) => job.result));
  const actionLabel = $derived(
    mode === 'link' && sources.length > 1
      ? t('skills.install.batchAction', { count: sources.length })
      : t('skills.install.action'),
  );

  function choosePaths(paths) {
    picked = [...new Set([...picked, ...paths])];
    source = '';
    packagePath = '';
  }

  function chooseFile(next) {
    file = next;
    packagePath = '';
  }

  async function runJobs(pending) {
    if (busy || !pending.length) return;
    busy = true;
    request = new AbortController();
    try {
      for (const job of pending) {
        if (disposed) return;
        job.status = 'working';
        job.error = '';
        const params = { scope, return_choices: true };
        if (job.path) params.path = job.path;
        if (job.approval) {
          params.replace = true;
          params.expected_sha256 = job.approval;
          job.approval = '';
        }
        try {
          const options = { signal: request.signal };
          const result =
            mode === 'file'
              ? await installSkillArchive(file, params, options)
              : await installSkill(
                  {
                    ...params,
                    source: job.source,
                    ...(revision.trim() ? { ref: revision.trim() } : {}),
                  },
                  options,
                );
          if (disposed) return;
          job.status = result.operation;
          if (
            result.operation === 'confirm_replace' ||
            result.operation === 'candidates'
          ) {
            job.decision = result;
          } else {
            job.decision = null;
            job.result = result;
          }
        } catch (error) {
          if (disposed) return;
          job.status = 'failed';
          job.decision = null;
          job.error = error.message;
        }
      }
      if (!disposed && jobs.every((job) => job.result)) await finish();
    } finally {
      busy = false;
    }
  }

  function start() {
    if (!sourceReady || jobs.length) return;
    const inputs = mode === 'file' ? [file.name] : sources;
    jobs = inputs.map((source) => ({
      source,
      path: packagePath.trim(),
      status: 'queued',
      decision: null,
      result: null,
      error: '',
      approval: '',
    }));
    void runJobs(jobs);
  }

  function replace(job) {
    job.approval = job.decision.sha256;
    job.path = job.decision.package_path;
    void runJobs([job]);
  }

  function finish() {
    const results = completed.map((job) => job.result);
    return results.length ? onInstalled(results) : onClose();
  }
</script>

<Modal
  title={t('skills.install.title')}
  size="lg"
  closeDisabled={Boolean(busy)}
  {onClose}
>
  {#snippet body()}
    <div class="modal-body">
      {#if !jobs.length}
        <TabList
          ariaLabel={t('skills.install.sourceType')}
          idPrefix="skill-install"
          items={[
            {
              id: 'link',
              label: t('skills.install.linkTab'),
              panelId: 'skill-install-source-panel',
            },
            {
              id: 'file',
              label: t('skills.install.fileTab'),
              panelId: 'skill-install-source-panel',
            },
          ]}
          value={mode}
          onChange={(value) => {
            if (!busy) {
              mode = value;
              file = null;
              packagePath = '';
              picked = [];
              source = '';
            }
          }}
        />
        <div
          id="skill-install-source-panel"
          role="tabpanel"
          aria-labelledby={`skill-install-tab-${mode}`}
        >
          {#if mode === 'link'}
            <FormField
              controlId="skill-install-source"
              label={t('skills.install.source')}
              help={t('skills.install.linkHelp')}
            >
              {#snippet children(field)}
                <!-- Completes only typed absolute paths, never links. -->
                <PathField
                  id={field.controlId}
                  mode="any"
                  value={source}
                  rememberLocation="skill-install-source"
                  onSelectMany={choosePaths}
                  browseLabel={t('pathPicker.browse')}
                  placeholder="https://…"
                  aria-describedby={field.describedBy}
                  onInput={(value) => {
                    source = value;
                    packagePath = '';
                  }}
                />
              {/snippet}
            </FormField>
            {#if picked.length}
              <ul class="skill-install-sources">
                {#each picked as path (path)}
                  <li>
                    <span>{path}</span><Button
                      variant="tertiary"
                      icon
                      ariaLabel={t('skills.install.removeSource', { path })}
                      onClick={() =>
                        (picked = picked.filter((item) => item !== path))}
                      >×</Button
                    >
                  </li>
                {/each}
              </ul>
            {/if}
          {:else}
            <FormField
              controlId="skill-install-file"
              label={t('skills.install.file')}
            >
              {#snippet children(field)}
                <FileDropZone
                  id={field.controlId}
                  accept=".skill,.zip,.tar,.tar.gz,.tgz,.tar.bz2,.tbz2,.tar.xz,.txz"
                  {file}
                  hint={t('skills.install.fileHelp')}
                  invalid={Boolean(file && file.size > archiveLimit)}
                  ariaLabelledby={field.labelId}
                  onFile={chooseFile}
                />
              {/snippet}
            </FormField>
            {#if file?.size > archiveLimit}<Banner variant="error" role="alert"
                >{t('skills.install.tooLarge')}</Banner
              >{/if}
          {/if}
        </div>
        <FormField
          controlId="skill-install-scope"
          label={t('skills.install.destination')}
          help={scope === 'global'
            ? t('skills.createGlobalHelp')
            : t('skills.createPrivateHelp')}
        >
          {#snippet children(field)}
            <Dropdown
              id={field.controlId}
              ariaLabel={t('skills.install.destination')}
              ariaDescribedby={field.describedBy}
              value={scope}
              options={scopeOptions}
              onValueChange={(value) => (scope = value)}
            />
          {/snippet}
        </FormField>
        <details class="skills-install-options">
          <summary>{t('skills.install.options')}</summary>
          <div class="skills-install-options__fields">
            <FormField
              controlId="skill-install-path"
              label={t('skills.install.path')}
              help={t('skills.install.pathHelp')}
            >
              {#snippet children(field)}
                <TextField
                  id={field.controlId}
                  value={packagePath}
                  code
                  aria-describedby={field.describedBy}
                  onInput={(value) => (packagePath = value)}
                />
              {/snippet}
            </FormField>
            {#if mode === 'link'}
              <FormField
                controlId="skill-install-ref"
                label={t('skills.install.ref')}
              >
                <TextField
                  id="skill-install-ref"
                  value={revision}
                  code
                  onInput={(value) => (revision = value)}
                />
              </FormField>
            {/if}
          </div>
        </details>
      {:else}
        <div class="skill-install-jobs" aria-live="polite">
          {#each jobs as job, index (job.source)}
            <section class="skill-install-job">
              <strong
                >{job.result?.name || job.decision?.name || job.source}</strong
              >
              {#if job.decision?.name && jobs.length > 1}<small
                  >{job.source}</small
                >{/if}
              {#if job.status === 'working'}<p>
                  {t('skills.install.installing')}
                </p>
              {:else if job.status === 'queued'}<p>
                  {t('skills.install.queued')}
                </p>
              {:else if job.result}<p>
                  {job.status === 'unchanged'
                    ? t('skills.install.unchanged')
                    : t('skills.install.success', { name: job.result.name })}
                </p>
              {:else if job.status === 'confirm_replace'}
                <p>
                  {t('skills.install.conflict', { name: job.decision.name })}
                </p>
                <p>{t('skills.install.replaceHelp')}</p>
                <Button
                  variant="secondary"
                  disabled={busy}
                  onClick={() => replace(job)}
                  >{t('skills.install.replaceAction')}</Button
                >
              {:else if job.status === 'candidates'}
                <FormField
                  controlId={`skill-candidate-${index}`}
                  label={t('skills.install.choose')}
                >
                  {#snippet children(field)}
                    <Dropdown
                      id={field.controlId}
                      ariaLabel={t('skills.install.choose')}
                      value=""
                      disabled={busy}
                      options={job.decision.candidates.map((candidate) => ({
                        value: candidate.path,
                        label: candidate.name + ' · ' + candidate.path,
                      }))}
                      onValueChange={(path) => {
                        job.path = path;
                        void runJobs([job]);
                      }}
                    />
                  {/snippet}
                </FormField>
              {:else if job.error}
                <Banner variant="error" role="alert">{job.error}</Banner>
                <Button
                  variant="secondary"
                  disabled={busy}
                  onClick={() => runJobs([job])}>{t('common.retry')}</Button
                >
              {/if}
            </section>
          {/each}
        </div>
      {/if}
    </div>
  {/snippet}
  {#snippet footer()}
    {#if !jobs.length}
      <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button
      >
      <Button variant="primary" disabled={!sourceReady} onClick={start}
        >{actionLabel}</Button
      >
    {:else}
      {#if !completed.length}
        <Button variant="secondary" disabled={busy} onClick={() => (jobs = [])}
          >{t('skills.install.changeSource')}</Button
        >
      {/if}
      <Button variant="secondary" disabled={busy} onClick={finish}
        >{completed.length ? t('common.close') : t('common.cancel')}</Button
      >
    {/if}
  {/snippet}
</Modal>

<style>
  .skill-install-sources {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .skill-install-sources li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .skill-install-sources span,
  .skill-install-job strong,
  .skill-install-job small {
    overflow-wrap: anywhere;
  }
  .skill-install-job {
    display: grid;
    gap: 6px;
    justify-items: start;
    padding-block: 10px;
  }
  .skill-install-job + .skill-install-job {
    border-top: 1px solid var(--border);
  }
  .skill-install-job p {
    margin: 0;
    color: var(--text-med);
  }
</style>
