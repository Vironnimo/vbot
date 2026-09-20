<script>
  import { onDestroy, untrack } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { installSkill, installSkillArchive } from '$lib/api.js';
  import Modal from '../ui/Modal.svelte';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';
  import TextField from '../ui/TextField.svelte';
  import TabList from '../ui/TabList.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Dropdown from '../Dropdown.svelte';

  let {
    initialScope = 'global',
    scopeOptions = [],
    onClose,
    onInstalled,
    onLocations,
    onCreate,
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

  function chooseFile(event) {
    file = event.currentTarget.files?.[0] ?? null;
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
  class="skills-install-modal"
  closeDisabled={Boolean(busy)}
  {onClose}
>
  {#snippet body()}
    <div class="skills-modal-body skills-install-body">
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
          <div class="skills-field">
            <label class="skills-field-label" for="skill-install-source"
              >{t('skills.install.source')}</label
            >
            <TextField
              id="skill-install-source"
              value={source}
              disabled={Boolean(busy)}
              placeholder="https://…"
              onInput={(value) => {
                source = value;
                packagePath = '';
                resetPreview();
              }}
            />
            <p class="skills-field-help">{t('skills.install.linkHelp')}</p>
          </div>
        {:else}
          <div class="skills-field">
            <label class="skills-field-label" for="skill-install-file"
              >{t('skills.install.file')}</label
            >
            <input
              id="skill-install-file"
              class="skills-install-file"
              type="file"
              accept=".skill,.zip,.tar,.tar.gz,.tgz,.tar.bz2,.tbz2,.tar.xz,.txz"
              disabled={Boolean(busy)}
              onchange={chooseFile}
            />
            <p class="skills-field-help">{t('skills.install.fileHelp')}</p>
          </div>
        {/if}
      </div>
      <div class="skills-field">
        <span class="skills-field-label" id="skill-install-scope-label"
          >{t('skills.install.destination')}</span
        >
        <Dropdown
          ariaLabel={t('skills.install.destination')}
          value={scope}
          options={scopeOptions}
          disabled={Boolean(busy)}
          onValueChange={(value) => {
            scope = value;
            resetPreview();
          }}
        />
        <p class="skills-field-help">
          {t(
            scope === 'global'
              ? 'skills.createGlobalHelp'
              : 'skills.createPrivateHelp',
          )}
        </p>
      </div>
      <details class="skills-install-options">
        <summary>{t('skills.install.options')}</summary>
        <div class="skills-field">
          <label class="skills-field-label" for="skill-install-path"
            >{t('skills.install.path')}</label
          >
          <TextField
            id="skill-install-path"
            value={packagePath}
            disabled={Boolean(busy)}
            onInput={(value) => {
              packagePath = value;
              resetPreview();
            }}
          />
          <p class="skills-field-help">{t('skills.install.pathHelp')}</p>
        </div>
        {#if mode === 'link'}
          <div class="skills-field">
            <label class="skills-field-label" for="skill-install-ref"
              >{t('skills.install.ref')}</label
            >
            <TextField
              id="skill-install-ref"
              value={revision}
              disabled={Boolean(busy)}
              onInput={(value) => {
                revision = value;
                resetPreview();
              }}
            />
          </div>
        {/if}
      </details>
      {#if candidates.length}
        <div class="skills-field">
          <span class="skills-field-label">{t('skills.install.choose')}</span>
          <Dropdown
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
        </div>
      {/if}
      {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
      {#if busy}<Banner variant="info" role="status"
          >{t(`skills.install.${busy}`)}</Banner
        >{/if}
      {#if preview}
        <section
          class="skills-install-preview"
          aria-label={t('skills.install.preview')}
        >
          <h3>{preview.name}</h3>
          <p>{selected?.description}</p>
          <p class="skills-field-help">
            {t(
              preview.files === 1
                ? 'skills.install.summaryOne'
                : 'skills.install.summary',
              '',
              {
                count: preview.files,
                scope: scopeLabel,
              },
            )}
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
              <Toggle
                checked={replace}
                onChange={(value) => (replace = value)}
                disabled={Boolean(busy)}
                ariaLabel={t('skills.install.replace', '', {
                  name: preview.name,
                })}
              />
              <span
                >{t('skills.install.replace', '', { name: preview.name })}</span
              >
            </div>
            <p class="skills-field-help">{t('skills.install.replaceHelp')}</p>
          {/if}
        </section>
      {/if}
      <div class="skills-install-alternatives">
        <Button
          variant="tertiary"
          disabled={Boolean(busy)}
          onClick={onLocations}>{t('skills.install.locations')}</Button
        >
        <Button
          variant="tertiary"
          disabled={Boolean(busy)}
          onClick={() => onCreate(scope)}>{t('skills.createCustom')}</Button
        >
      </div>
    </div>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" disabled={Boolean(busy)} onClick={onClose}
      >{t('common.cancel', 'Cancel')}</Button
    >
    {#if preview}
      <Button
        variant="primary"
        disabled={Boolean(busy) || (needsReplace && !replace)}
        onClick={() => submit(false)}
        >{t(
          selected?.unchanged
            ? 'skills.install.show'
            : needsReplace
              ? 'skills.install.replaceAction'
              : 'skills.install.action',
        )}</Button
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
