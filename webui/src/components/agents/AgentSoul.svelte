<script>
  // The Agent's SOUL.md in the Identity section: a rendered preview that
  // opens a docked editor with Write and Preview tabs. The Agent may rewrite
  // its own SOUL.md at any time, so the editor saves explicitly with the
  // revision it read; a conflict offers the current file instead of silently
  // overwriting it.
  import { untrack } from 'svelte';
  import { formatFileSize, t } from '$lib/i18n.js';
  import { getAgentSoul, setAgentSoul } from '$lib/api.js';
  import { normalizeSoulText, soulStats } from '$lib/agentSoul.js';
  import MarkdownContent from '../chat/MarkdownContent.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import Modal from '../ui/Modal.svelte';
  import TabList from '../ui/TabList.svelte';
  import TextArea from '../ui/TextArea.svelte';

  let {
    agentId,
    agentName = '',
    // Changes whenever the roster delivers this Agent again, so another
    // window's save shows here too.
    refreshKey = undefined,
  } = $props();

  // `{content, revision, path, prompt_limit_bytes}` as the server sent it.
  let soul = $state(null);
  let loadError = $state('');
  let editorOpen = $state(false);
  let draft = $state('');
  // The saved text and revision the draft started from.
  let baseline = $state('');
  let baseRevision = $state('');
  let tab = $state('write');
  let saving = $state(false);
  let saveError = $state('');
  // The file as it is now when a save found it changed: `{content, revision}`.
  let conflict = $state(null);
  let confirmDiscard = $state(false);
  let loadGeneration = 0;
  let previewElement = $state();
  // Only a preview cut off at its height fades out at the bottom.
  let previewClipped = $state(false);

  $effect(() => {
    void soul?.content;
    if (!previewElement) return;
    previewClipped =
      previewElement.scrollHeight > previewElement.clientHeight + 1;
  });

  let savedStats = $derived(soulStats(soul?.content));
  let draftStats = $derived(soulStats(draft));
  let dirty = $derived(
    editorOpen && normalizeSoulText(draft) !== normalizeSoulText(baseline),
  );
  let limitBytes = $derived(soul?.prompt_limit_bytes ?? 0);
  let tooLarge = $derived(limitBytes > 0 && draftStats.bytes > limitBytes);
  let editorTabs = $derived([
    { id: 'write', label: t('agents.soul.tabWrite') },
    { id: 'preview', label: t('agents.soul.tabPreview') },
  ]);

  $effect(() => {
    void agentId;
    void refreshKey;
    untrack(() => {
      // An open editor keeps its draft; it re-reads when it opens again.
      if (!editorOpen) void load();
    });
  });

  async function load() {
    const generation = ++loadGeneration;
    const requestedId = agentId;
    loadError = '';
    try {
      const result = await getAgentSoul(requestedId);
      if (generation !== loadGeneration) return;
      soul = result;
    } catch (error) {
      if (generation !== loadGeneration) return;
      soul = null;
      loadError = error?.message || t('errors.generic');
    }
  }

  async function openEditor() {
    await load();
    if (!soul) return;
    draft = normalizeSoulText(soul.content);
    baseline = draft;
    baseRevision = soul.revision;
    tab = 'write';
    saveError = '';
    conflict = null;
    editorOpen = true;
  }

  function closeEditor() {
    editorOpen = false;
    confirmDiscard = false;
    conflict = null;
    saveError = '';
  }

  function requestClose() {
    if (saving) return;
    if (dirty) {
      confirmDiscard = true;
      return;
    }
    closeEditor();
  }

  async function save({ overwrite = false } = {}) {
    if (saving || (!dirty && !overwrite)) return;
    saving = true;
    saveError = '';
    try {
      const result = await setAgentSoul(
        agentId,
        draft,
        overwrite && conflict ? conflict.revision : baseRevision,
      );
      soul = result;
      closeEditor();
    } catch (error) {
      const current = error?.details?.data;
      if (error?.code === 'soul_conflict' && current) {
        conflict = {
          content: normalizeSoulText(current.content),
          revision: current.revision,
        };
      } else {
        saveError = error?.message || t('errors.generic');
      }
    } finally {
      saving = false;
    }
  }

  // Drops the user's edits for the file as it is now.
  function takeCurrentVersion() {
    if (!conflict) return;
    draft = conflict.content;
    baseline = conflict.content;
    baseRevision = conflict.revision;
    soul = { ...soul, content: conflict.content, revision: conflict.revision };
    conflict = null;
  }

  function onEditorKeydown(event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      void save();
    }
  }

  function statsText(stats) {
    const size = formatFileSize(stats.bytes);
    return stats.lines === 1
      ? t('agents.soul.statsOne', { size })
      : t('agents.soul.stats', { lines: stats.lines, size });
  }
</script>

<div class="s-row s-row--stacked agent-soul">
  <div class="agent-soul__head">
    <div class="s-row-info">
      <span class="s-row-label" id="agent-soul-label">
        {t('agents.soul.title')}
      </span>
      <div class="s-row-desc" id="agent-soul-help">
        {t('agents.soul.help')}
      </div>
    </div>
    {#if soul?.content.trim()}
      <Button
        variant="secondary"
        aria-describedby="agent-soul-help"
        onClick={openEditor}
      >
        {t('agents.soul.edit')}
      </Button>
    {/if}
  </div>

  {#if loadError}
    <Banner variant="error" role="alert">
      {t('agents.soul.loadError', { error: loadError })}
      <Button variant="secondary" onClick={load}>{t('common.retry')}</Button>
    </Banner>
  {:else if !soul}
    <p class="agent-soul__placeholder">{t('agents.soul.loading')}</p>
  {:else if !soul.content.trim()}
    <div class="agent-soul__empty">
      <p>{t('agents.soul.empty')}</p>
      <Button variant="secondary" onClick={openEditor}>
        {t('agents.soul.write')}
      </Button>
    </div>
  {:else}
    <div class="agent-soul__card">
      <div
        class="agent-soul__preview"
        class:agent-soul__preview--clipped={previewClipped}
        bind:this={previewElement}
        inert
      >
        <MarkdownContent
          class="msg-markdown md-document agent-soul__markdown"
          source={soul.content}
        />
      </div>
      <!-- The whole card opens the editor for the pointer; keyboard users
           reach it through the Edit button above. -->
      <button
        type="button"
        class="agent-soul__open"
        tabindex="-1"
        aria-hidden="true"
        onclick={openEditor}
      ></button>
    </div>
    <p class="agent-soul__meta">{statsText(savedStats)}</p>
  {/if}
</div>

{#if editorOpen}
  <Modal
    title={t('agents.soul.editorTitle', { name: agentName || agentId })}
    description={t('agents.soul.editorDescription')}
    size="xl"
    variant="sheet"
    class="agent-soul-editor"
    closeDisabled={saving}
    onClose={requestClose}
  >
    {#snippet body()}
      <div class="modal-body agent-soul-editor__body">
        {#if conflict}
          <Banner variant="warn" role="alert">
            {t('agents.soul.conflict')}
            <div class="agent-soul-editor__banner-actions">
              <Button variant="secondary" onClick={takeCurrentVersion}>
                {t('agents.soul.conflictLoad')}
              </Button>
              <Button
                variant="tertiary"
                disabled={saving}
                onClick={() => save({ overwrite: true })}
              >
                {t('agents.soul.conflictOverwrite')}
              </Button>
            </div>
          </Banner>
        {/if}
        {#if saveError}
          <Banner variant="error" role="alert">
            {t('agents.soul.saveError', { error: saveError })}
          </Banner>
        {/if}
        {#if tooLarge}
          <Banner variant="warn">
            {t('agents.soul.tooLarge', { limit: formatFileSize(limitBytes) })}
          </Banner>
        {/if}
        <TabList
          items={editorTabs}
          value={tab}
          appearance="segmented"
          density="compact"
          idPrefix="agent-soul"
          ariaLabel={t('agents.soul.viewLabel')}
          onChange={(next) => (tab = next)}
        />
        <div
          class="agent-soul-editor__panel"
          role="tabpanel"
          id={`agent-soul-panel-${tab}`}
          aria-labelledby={`agent-soul-tab-${tab}`}
        >
          {#if tab === 'write'}
            <TextArea
              id="agent-soul-content"
              class="agent-soul-editor__text"
              code
              rows="22"
              value={draft}
              ariaLabel={t('agents.soul.contentLabel')}
              placeholder={t('agents.soul.placeholder')}
              disabled={saving}
              onInput={(next) => (draft = next)}
              onkeydown={onEditorKeydown}
            />
          {:else if draft.trim()}
            <MarkdownContent
              class="msg-markdown md-document agent-soul-editor__preview"
              source={draft}
            />
          {:else}
            <p class="agent-soul__placeholder">
              {t('agents.soul.previewEmpty')}
            </p>
          {/if}
        </div>
      </div>
    {/snippet}
    {#snippet footer()}
      <div class="modal-footer__start agent-soul-editor__status">
        <span>{statsText(draftStats)}</span>
        {#if dirty}
          <span class="agent-soul-editor__dirty"
            >{t('agents.soul.unsaved')}</span
          >
        {/if}
      </div>
      <Button variant="secondary" disabled={saving} onClick={requestClose}>
        {t('common.cancel')}
      </Button>
      <Button
        variant="primary"
        disabled={!dirty || saving || Boolean(conflict)}
        onClick={() => save()}
      >
        {saving ? t('agents.soul.saving') : t('common.save')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if confirmDiscard}
  <ConfirmDialog
    title={t('agents.soul.discardTitle')}
    body={t('agents.soul.discardMessage')}
    confirmLabel={t('agents.soul.discard')}
    onConfirm={closeEditor}
    onCancel={() => (confirmDiscard = false)}
  />
{/if}
