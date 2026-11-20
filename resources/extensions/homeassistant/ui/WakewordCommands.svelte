<script>
  import { onMount, untrack } from 'svelte';
  import { createExtensionPageClient } from '@vbot/extension-sdk/client';
  import { init, t } from '@vbot/extension-sdk/lib/i18n.js';
  import { applyExtensionTheme } from '@vbot/extension-sdk/lib/extensionTheme.js';
  import { provideNavigation } from '@vbot/extension-sdk/lib/navigation.svelte.js';
  import { createDebouncedAutosave } from '@vbot/extension-sdk/lib/autosave.js';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import ConfirmDialog from '@vbot/extension-sdk/ui/ConfirmDialog.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import Modal from '@vbot/extension-sdk/ui/Modal.svelte';
  import SaveStatus from '@vbot/extension-sdk/ui/SaveStatus.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import { commandDraft, draftCommand, toolFields } from './commands.js';

  let { bridgeClient = null } = $props();
  const client = untrack(() => bridgeClient ?? createExtensionPageClient());
  provideNavigation({ registerLayer: client.registerLayer });
  const toolOptions = Object.keys(toolFields).map((value) => ({
    value,
    label: value,
  }));

  let commands = $state(null);
  let decisionLimit = $state(null);
  let readError = $state('');
  let editError = $state('');
  let original = $state(null);
  let draft = $state(null);
  let baseline = $state('');
  let removal = $state(null);
  let saving = $state(false);
  let ready = $state(false);
  let loadVersion = 0;
  let refreshAfterSave = false;

  function edit(command) {
    original = command;
    draft = commandDraft(command);
    baseline = JSON.stringify(draft);
    editError = '';
  }

  const snapshot = () => JSON.stringify(draft);
  const hasChanges = () =>
    Boolean(original && draft && snapshot() !== baseline);
  const autosave = createDebouncedAutosave({
    getSnapshot: snapshot,
    hasChanges,
    save: persist,
  });
  $effect(() => {
    if (hasChanges() && !saving) autosave.scheduleRun();
    if (ready) client.notifyAutosave();
    return autosave.cancelPendingTimer;
  });

  async function refresh() {
    const version = ++loadVersion;
    try {
      const result = await client.operation('wakeword_commands');
      if (version !== loadVersion) return;
      commands = result.commands;
      decisionLimit = result.decision_command_limit;
      readError = '';
      if (draft && original && !saving && !hasChanges()) {
        const current = commands.find(
          (item) => item.phrase === original.phrase,
        );
        if (current) edit(current);
        else draft = null;
      }
    } catch (cause) {
      if (version === loadVersion) readError = cause.message;
    }
  }

  onMount(() => {
    function context(next) {
      if (!next) return;
      document.documentElement.lang = init(next.locale);
      applyExtensionTheme(next.theme);
      if (!ready) {
        ready = true;
        void refresh();
        void client
          .setSearchIndex([
            {
              id: 'commands',
              label: t('haWakeword.title'),
              terms: t('haWakeword.aliases'),
            },
          ])
          .catch(() => {});
      }
    }
    const stopContext = client.onContext(context);
    const stopChanges = client.onInvalidation(({ change }) => {
      if (change && change.resource !== 'wakeword_commands') return;
      if (saving) refreshAfterSave = true;
      else void refresh();
    });
    const stopSearch = client.onSearch(() =>
      document.getElementById('add-command')?.focus(),
    );
    const stopAutosave = client.registerAutosave(autosave.participant);
    context(client.context);
    return () => {
      ++loadVersion;
      stopContext();
      stopChanges();
      stopSearch();
      stopAutosave();
      client.dispose();
    };
  });

  async function write(previous, command) {
    const result = await client.operation('set_wakeword_command', {
      previous: $state.snapshot(previous),
      command: $state.snapshot(command),
    });
    ++loadVersion;
    commands = result.commands;
    decisionLimit = result.decision_command_limit;
    readError = '';
  }

  function finishSaving() {
    saving = false;
    if (refreshAfterSave) {
      refreshAfterSave = false;
      void refresh();
    }
  }

  async function persist() {
    const submitted = snapshot();
    const creating = original === null;
    editError = '';
    saving = true;
    try {
      const command = draftCommand(JSON.parse(submitted), original);
      await write(original, command);
      if (creating) draft = null;
      else {
        original = commands.find((item) => item.phrase === command.phrase);
        // Preserve keystrokes made during this save and their pending state.
        baseline = submitted;
      }
      return true;
    } catch (cause) {
      editError = cause.message;
      return false;
    } finally {
      finishSaving();
    }
  }

  async function closeEditor() {
    if (!original || (await autosave.participant.flush())) draft = null;
  }

  async function remove() {
    saving = true;
    editError = '';
    try {
      await write(removal, null);
      removal = null;
    } catch (cause) {
      editError = cause.message;
    } finally {
      finishSaving();
    }
  }
</script>

<main>
  <header class="s-group-toolbar">
    <h3>{t('haWakeword.title')}</h3>
    <Button
      id="add-command"
      variant="secondary"
      disabled={!commands}
      onClick={() => edit(null)}>{t('haWakeword.add')}</Button
    >
  </header>
  {#if readError}
    <Banner variant="error" role="alert">
      {readError}
      <Button variant="secondary" onClick={refresh}>{t('common.retry')}</Button>
    </Banner>
  {:else if !commands}
    <p>{t('common.loading')}</p>
  {:else if commands.length === 0}
    <p>{t('haWakeword.empty')}</p>
  {/if}
  {#if decisionLimit != null && commands?.length > decisionLimit}
    <Banner variant="warn"
      >{t('haWakeword.decisionLimit', { limit: decisionLimit })}</Banner
    >
  {/if}
  {#if commands?.length}
    <div class="s-group">
      {#each commands as command (command.phrase)}
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label">{command.phrase}</div>
            <code class="s-row-desc">{command.tool}</code>
          </div>
          <div class="s-row-control">
            <Button
              variant="tertiary"
              ariaLabel={t('haWakeword.edit', { phrase: command.phrase })}
              onClick={() => edit(command)}>{t('common.edit')}</Button
            >
            <Button
              variant="tertiary"
              onClick={() => {
                removal = command;
                editError = '';
              }}>{t('common.remove')}</Button
            >
          </div>
        </div>
      {/each}
    </div>
  {/if}
</main>

{#if draft}
  <Modal
    title={original
      ? t('haWakeword.edit', { phrase: original.phrase })
      : t('haWakeword.add')}
    closeDisabled={saving && !original}
    onClose={closeEditor}
  >
    {#snippet body()}
      <div class="modal-body">
        <div class="fields">
          <label for="phrase">{t('haWakeword.phrase')}</label>
          <TextField
            id="phrase"
            aria-describedby="wording-help"
            required
            value={draft.phrase}
            disabled={saving && !original}
            onInput={(value) => (draft.phrase = value)}
          />
          <label for="aliases">{t('haWakeword.aliases')}</label>
          <TextArea
            id="aliases"
            aria-describedby="wording-help"
            rows={2}
            value={draft.aliases}
            disabled={saving && !original}
            onInput={(value) => (draft.aliases = value)}
          />
          <small id="wording-help">{t('haWakeword.wordingHelp')}</small>
          <label for="tool" id="tool-label">{t('haWakeword.tool')}</label>
          <Dropdown
            id="tool"
            ariaLabelledby="tool-label"
            value={draft.tool}
            options={toolOptions}
            disabled={saving && !original}
            onValueChange={(value) => {
              if (value !== draft.tool)
                draft = { ...draft, tool: value, values: {}, extra: '' };
            }}
          />
          {#each toolFields[draft.tool] as field (field.key)}
            <label for={'arg-' + field.key}>{field.label()}</label>
            <TextField
              id={'arg-' + field.key}
              value={draft.values[field.key] ?? ''}
              placeholder={field.example}
              disabled={saving && !original}
              onInput={(value) => (draft.values[field.key] = value)}
            />
          {/each}
          <label for="extra">{t('haWakeword.extra')}</label>
          <TextArea
            id="extra"
            rows={2}
            value={draft.extra}
            placeholder={'{}'}
            disabled={saving && !original}
            onInput={(value) => (draft.extra = value)}
          />
        </div>
        {#if editError}<Banner variant="error" role="alert">{editError}</Banner
          >{/if}
        {#if original}
          <SaveStatus
            {saving}
            pending={hasChanges()}
            onClick={() => autosave.participant.runSave('manual')}
          />
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      {#if original}
        {#if editError && !saving}
          <Button
            variant="tertiary"
            onClick={() => {
              draft = null;
              void refresh();
            }}>{t('haWakeword.discard')}</Button
          >
        {/if}
        <Button variant="secondary" onClick={closeEditor}
          >{t('common.close')}</Button
        >
      {:else}
        <Button variant="secondary" disabled={saving} onClick={closeEditor}
          >{t('common.cancel')}</Button
        >
        <Button variant="primary" loading={saving} onClick={persist}
          >{t('common.create')}</Button
        >
      {/if}
    {/snippet}
  </Modal>
{/if}

{#if removal}
  <ConfirmDialog
    title={t('haWakeword.removeTitle', { phrase: removal.phrase })}
    body={t('haWakeword.removeBody')}
    confirmLabel={t('common.remove')}
    busy={saving}
    error={editError}
    onCancel={() => (removal = null)}
    onConfirm={remove}
  />
{/if}

<style>
  main {
    height: 100%;
    overflow: auto;
    padding: 12px;
  }
  h3,
  p {
    margin: 0;
  }
  h3 {
    font-size: 1rem;
  }
  p {
    color: var(--text-med);
  }
  .fields {
    display: grid;
    grid-template-columns: minmax(8rem, 1fr) 2fr;
    gap: 10px 14px;
    align-items: baseline;
  }
  #wording-help {
    grid-column: -2 / -1;
    color: var(--text-med);
  }
  @media (max-width: 440px) {
    .fields {
      grid-template-columns: minmax(0, 1fr);
      gap: 6px;
    }
  }
</style>
