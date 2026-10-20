<script>
  import { onDestroy, untrack } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { installSkill, installSkillArchive } from '$lib/api.js';
  import Modal from '../ui/Modal.svelte';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
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
  let preview = $state(null);
  let candidates = $state([]);
  let replace = $state(false);
  let busy = $state('');
  let error = $state('');
  let disposed = false;
  let request;
  onDestroy(() => {
    disposed = true;
    request?.abort();
  });

  const archiveLimit = 64 * 1024 * 1024;
  let sourceReady = $derived(
    mode === 'file'
      ? file && file.size <= archiveLimit
      : Boolean(source.trim()),
  );
  let selected = $derived(preview?.candidates?.[0]);
  let needsReplace = $derived(
    Boolean(selected?.exists && !selected?.unchanged),
  );
  let scopeLabel = $derived(
    scopeOptions.find((option) => option.value === scope)?.label || scope,
  );

  function resetPreview(clearCandidates = true) {
    preview = null;
    replace = false;
    error = '';
    if (clearCandidates) candidates = [];
  }

  function chooseFile(next) {
    file = next;
    packagePath = '';
    resetPreview();
    if (file?.size > archiveLimit) error = t('skills.install.tooLarge');
  }

  async function submit(dryRun) {
    if (
      busy ||
      !sourceReady ||
      (!dryRun && (!preview || (needsReplace && !replace)))
    )
      return;
    busy = dryRun ? 'checking' : 'installing';
    error = '';
    request = new AbortController();
    const params = { scope, dry_run: dryRun };
    const path = dryRun ? packagePath.trim() : preview.package_path;
    if (path) params.path = path;
    if (!dryRun) {
      params.expected_sha256 = preview.sha256;
      params.replace = needsReplace && replace;
    }
    try {
      const result =
        mode === 'file'
          ? await installSkillArchive(file, params, { signal: request.signal })
          : await installSkill(
              {
                ...params,
                source: source.trim(),
                ...(revision.trim() ? { ref: revision.trim() } : {}),
              },
              { signal: request.signal },
            );
      if (disposed) return;
      if (dryRun) {
        replace = false;
        if (result.operation === 'candidates') {
          candidates = result.candidates;
          preview = null;
        } else {
          preview = result;
        }
      } else {
        await onInstalled(result);
      }
    } catch (failure) {
      if (!disposed) {
        error = failure.message;
        preview = null;
        replace = false;
      }
    } finally {
      if (!disposed) busy = '';
    }
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
      <TabList
        ariaLabel={t('skills.install.sourceType')}
        idPrefix="skill-install"
        items={[
          {
            id: 'link',
            label: t('skills.install.linkTab'),
            panelId: 'skill-install-source-panel',
            disabled: Boolean(busy),
          },
          {
            id: 'file',
            label: t('skills.install.fileTab'),
            panelId: 'skill-install-source-panel',
            disabled: Boolean(busy),
          },
        ]}
        value={mode}
        onChange={(value) => {
          if (!busy) {
            mode = value;
            file = null;
            packagePath = '';
            resetPreview();
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
                disabled={Boolean(busy)}
                placeholder="https://…"
                aria-describedby={field.describedBy}
                onInput={(value) => {
                  source = value;
                  packagePath = '';
                  resetPreview();
                }}
              />
            {/snippet}
          </FormField>
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
                disabled={Boolean(busy)}
                invalid={Boolean(file && file.size > archiveLimit)}
                ariaLabelledby={field.labelId}
                onFile={chooseFile}
              />
            {/snippet}
          </FormField>
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
            disabled={Boolean(busy)}
            onValueChange={(value) => {
              scope = value;
              resetPreview();
            }}
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
                disabled={Boolean(busy)}
                aria-describedby={field.describedBy}
                onInput={(value) => {
                  packagePath = value;
                  resetPreview();
                }}
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
                disabled={Boolean(busy)}
                onInput={(value) => {
                  revision = value;
                  resetPreview();
                }}
              />
            </FormField>
          {/if}
        </div>
      </details>
      {#if candidates.length}
        <FormField
          controlId="skill-install-candidate"
          label={t('skills.install.choose')}
        >
          <Dropdown
            id="skill-install-candidate"
            ariaLabel={t('skills.install.choose')}
            value={packagePath}
            disabled={Boolean(busy)}
            options={candidates.map((candidate) => ({
              value: candidate.path,
              label: `${candidate.name} · ${candidate.path}`,
            }))}
            onValueChange={(value) => {
              packagePath = value;
              resetPreview(false);
              void submit(true);
            }}
          />
        </FormField>
      {/if}
      {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
      {#if busy}<Banner variant="info" role="status"
          >{busy === 'installing'
            ? t('skills.install.installing')
            : t('skills.install.checking')}</Banner
        >{/if}
      {#if preview}
        <section
          class="skills-install-preview"
          aria-label={t('skills.install.preview')}
        >
          <h3>{preview.name}</h3>
          {#if selected?.description}<p>{selected.description}</p>{/if}
          <p class="skills-install-preview__meta">
            {preview.files === 1
              ? t('skills.install.summaryOne', { scope: scopeLabel })
              : t('skills.install.summary', {
                  count: preview.files,
                  scope: scopeLabel,
                })}
          </p>
          {#if preview.warnings?.length}
            <Banner variant="warn"
              ><ul>
                {#each preview.warnings as warning, index (index)}<li>
                    {warning}
                  </li>{/each}
              </ul></Banner
            >
          {/if}
          {#if selected?.unchanged}
            <Banner variant="info">{t('skills.install.unchanged')}</Banner>
          {:else if needsReplace}
            <div class="skills-install-replace">
              <Checkbox
                checked={replace}
                disabled={Boolean(busy)}
                aria-describedby="skill-install-replace-help"
                onChange={(value) => (replace = value)}
                >{t('skills.install.replace', { name: preview.name })}</Checkbox
              >
              <p
                id="skill-install-replace-help"
                class="skills-install-preview__meta"
              >
                {t('skills.install.replaceHelp')}
              </p>
            </div>
          {/if}
        </section>
      {/if}
    </div>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" disabled={Boolean(busy)} onClick={onClose}
      >{t('common.cancel')}</Button
    >
    {#if preview}
      <Button
        variant="primary"
        disabled={Boolean(busy) || (needsReplace && !replace)}
        onClick={() => submit(false)}
        >{selected?.unchanged
          ? t('skills.install.show')
          : needsReplace
            ? t('skills.install.replaceAction')
            : t('skills.install.action')}</Button
      >
    {:else}
      <Button
        variant="primary"
        disabled={Boolean(busy) || !sourceReady}
        onClick={() => submit(true)}>{t('skills.install.check')}</Button
      >
    {/if}
  {/snippet}
</Modal>
