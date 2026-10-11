<script>
  // The Instructions section of the Swarm editor: the Swarm's own
  // instructions, the additional System Prompt blocks and the reminders its
  // participants receive, and a preview of the combined System Prompt for
  // one formation row. It edits the editor's bound draft; `snapshot()` and
  // `payload()` read the editor's current draft, so the preview covers
  // unsaved changes, and `previewable()` resolves whether that draft can be
  // previewed (the editor shows its problems otherwise).
  import { SvelteSet } from 'svelte/reactivity';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import InfoHint from '../../../../webui/src/components/ui/InfoHint.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import Toggle from '../../../../webui/src/components/ui/Toggle.svelte';
  import ToolDefinitionsPanel from '../../../../webui/src/components/ToolDefinitionsPanel.svelte';
  import { participantCount } from './pagePresentation.js';
  import { createProfilePromptPreview } from './profilePromptPreview.svelte.js';

  let {
    draft = $bindable(),
    catalog = {},
    bridgeClient,
    snapshot,
    payload,
    previewable,
  } = $props();

  const preview = createProfilePromptPreview({
    get snapshot() {
      return snapshot;
    },
    get bridgeClient() {
      return bridgeClient;
    },
    get profilePayload() {
      return payload;
    },
    get ready() {
      return previewable;
    },
  });
  // Blocks and reminders whose text is shown.
  const revealed = new SvelteSet();
  let previewOpen = $state(false);

  const promptBlocks = $derived(
    (catalog.prompt_blocks ?? []).filter(
      (block) => block.id !== 'core:agent_body',
    ),
  );
  // Saved blocks the catalog no longer offers stay listed until turned off.
  const missingBlocks = $derived(
    draft.prompt_blocks.filter(
      (id) => !promptBlocks.some((block) => block.id === id),
    ),
  );
  const reminderTitles = $derived({
    delivery: t('swarm.profile.reminderDelivery'),
    resume: t('swarm.profile.reminderResume'),
  });
  const reminders = $derived(
    Object.entries(catalog.reminder_texts ?? {}).map(([event, text]) => ({
      event,
      text,
      title: reminderTitles[event] ?? event,
      named: event in reminderTitles,
    })),
  );
  const formationOptions = $derived(
    draft.participants.map((row, index) => ({
      value: index,
      label: row.model || t('swarm.profile.noModel'),
      secondaryLabel: participantCount(Number(row.count) || 0),
    })),
  );
  const previewTools = $derived(
    (preview.preview?.tools ?? []).map((definition) => ({ definition })),
  );

  // A removed formation row cannot stay the previewed one.
  $effect(() => {
    if (preview.previewFormation >= draft.participants.length)
      preview.previewFormation = draft.participants.length - 1;
  });

  function setPromptBlock(id, enabled) {
    draft.prompt_blocks = enabled
      ? [...new Set([...draft.prompt_blocks, id])]
      : draft.prompt_blocks.filter((item) => item !== id);
  }

  function toggleRevealed(key) {
    if (revealed.has(key)) revealed.delete(key);
    else revealed.add(key);
  }
</script>

<section class="s-section" aria-labelledby="swarm-section-instructions">
  <header class="s-section__head">
    <h3 class="s-section__title" id="swarm-section-instructions">
      {t('swarm.profile.instructionsHeading')}
    </h3>
  </header>
  <p class="s-section__desc">{t('swarm.profile.instructionsHelp')}</p>
  <div class="s-section__body">
    <div class="s-group">
      <div class="s-row s-row--stacked swarm-profile-editor__prompt-row">
        <label class="s-row-label" for="swarm-instructions">
          {t('swarm.profile.instructions')}
        </label>
        <TextArea
          id="swarm-instructions"
          class="swarm-profile-editor__prompt"
          rows={12}
          spellcheck="false"
          value={draft.instructions}
          onInput={(value) => (draft.instructions = value)}
        />
      </div>
    </div>

    <div class="s-subhead">
      <h4 class="s-subhead__title">
        {t('swarm.profile.promptContributions')}
      </h4>
      <p class="s-subhead__desc">{t('swarm.profile.promptSelectionHelp')}</p>
    </div>
    <div class="s-group" data-testid="swarm-prompt-blocks">
      {#each promptBlocks as block, index (block.id)}
        {@const title = preview.blockTitle(block.id)}
        {@const contentId = `swarm-block-${index}`}
        {@const open = revealed.has(block.id)}
        {@const detail = preview.previewCurrent
          ? preview.preview.blocks.find((item) => item.id === block.id)
          : null}
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <button
              type="button"
              class="swarm-profile-editor__reveal"
              aria-expanded={open}
              aria-controls={contentId}
              onclick={() => toggleRevealed(block.id)}
            >
              <span
                class="disclosure-chevron"
                class:disclosure-chevron--open={open}
                aria-hidden="true"
              ></span>
              <span
                class="s-row-label"
                class:swarm-profile-editor__mono={title === block.id}
                >{title}</span
              >
            </button>
          </div>
          <div class="s-row-control">
            <Toggle
              checked={draft.prompt_blocks.includes(block.id)}
              ariaLabel={title}
              onChange={(enabled) => setPromptBlock(block.id, enabled)}
            />
          </div>
        </div>
        <div
          class="s-group__block swarm-profile-editor__content"
          id={contentId}
          hidden={!open}
        >
          {#if detail}
            {#if !detail.active}
              <p class="swarm-profile-editor__note">
                {t('swarm.profile.blockInactive')}
              </p>
            {/if}
            {#if detail.text}
              <pre class="swarm-profile-editor__text">{detail.text}</pre>
            {/if}
          {:else if block.text !== undefined}
            <pre class="swarm-profile-editor__text">{block.text}</pre>
          {:else}
            <p class="swarm-profile-editor__note">
              {t('swarm.profile.dynamicPreview')}
            </p>
          {/if}
        </div>
      {/each}
      {#each missingBlocks as id (id)}
        {@const title = preview.blockTitle(id)}
        <div class="s-row s-row--compact">
          <div class="s-row-info swarm-profile-editor__indented">
            <span
              class="s-row-label"
              class:swarm-profile-editor__mono={title === id}>{title}</span
            >
            <div class="s-row-desc">{t('swarm.profile.blockUnavailable')}</div>
          </div>
          <div class="s-row-control">
            <Toggle
              checked={true}
              ariaLabel={title}
              onChange={(enabled) => setPromptBlock(id, enabled)}
            />
          </div>
        </div>
      {/each}
    </div>

    {#if reminders.length}
      <div class="s-subhead">
        <h4 class="s-subhead__title swarm-profile-editor__label">
          {t('swarm.profile.reminders')}
          <InfoHint text={t('swarm.profile.reminderInfo')} />
        </h4>
        <p class="s-subhead__desc">{t('swarm.profile.reminderHelp')}</p>
      </div>
      <div class="s-group" data-testid="swarm-reminders">
        {#each reminders as reminder (reminder.event)}
          {@const contentId = `swarm-reminder-${reminder.event}`}
          {@const open = revealed.has(contentId)}
          <div class="s-row s-row--compact">
            <div class="s-row-info">
              <button
                type="button"
                class="swarm-profile-editor__reveal"
                aria-expanded={open}
                aria-controls={contentId}
                onclick={() => toggleRevealed(contentId)}
              >
                <span
                  class="disclosure-chevron"
                  class:disclosure-chevron--open={open}
                  aria-hidden="true"
                ></span>
                <span
                  class="s-row-label"
                  class:swarm-profile-editor__mono={!reminder.named}
                  >{reminder.title}</span
                >
              </button>
            </div>
            <div class="s-row-control">
              <Toggle
                checked={Boolean(draft.reminders?.[reminder.event])}
                ariaLabel={reminder.title}
                onChange={(value) =>
                  (draft.reminders = {
                    ...draft.reminders,
                    [reminder.event]: value,
                  })}
              />
            </div>
          </div>
          <div
            class="s-group__block swarm-profile-editor__content"
            id={contentId}
            hidden={!open}
          >
            <pre class="swarm-profile-editor__text">{reminder.text}</pre>
          </div>
        {/each}
      </div>
    {/if}

    <div class="s-group">
      <button
        type="button"
        class="s-row s-row--compact s-disclosure-row"
        aria-expanded={previewOpen}
        aria-controls="swarm-preview"
        onclick={() => (previewOpen = !previewOpen)}
      >
        <span class="s-row-label" id="swarm-preview-label">
          <span
            class="disclosure-chevron"
            class:disclosure-chevron--open={previewOpen}
            aria-hidden="true"
          ></span>
          {t('swarm.profile.promptPreview')}
        </span>
      </button>
      <div
        class="s-group__rows"
        id="swarm-preview"
        role="group"
        aria-labelledby="swarm-preview-label"
        hidden={!previewOpen}
      >
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="swarm-preview-model">
              {t('swarm.profile.model')}
            </label>
            <div class="s-row-desc" id="swarm-preview-help">
              {t('swarm.profile.previewHelp')}
            </div>
          </div>
          <div class="s-row-control">
            <Dropdown
              id="swarm-preview-model"
              value={preview.previewFormation}
              options={formationOptions}
              ariaLabel={t('swarm.profile.model')}
              ariaDescribedby="swarm-preview-help"
              onValueChange={(value) => (preview.previewFormation = value)}
            />
          </div>
        </div>
        <div class="swarm-profile-editor__preview">
          <div>
            <Button
              variant="secondary"
              loading={preview.previewBusy}
              onClick={preview.inspectPrompt}
              >{t('swarm.profile.generatePreview')}</Button
            >
          </div>
          {#if preview.previewError}
            <Banner variant="error" role="alert">{preview.previewError}</Banner>
          {/if}
          {#if preview.preview && !preview.previewCurrent}
            <Banner>{t('swarm.profile.previewStale')}</Banner>
          {/if}
          {#if preview.previewCurrent}
            <pre
              class="swarm-profile-editor__preview-text"
              data-testid="swarm-prompt-preview">{preview.preview.text}</pre>
            <h4 class="s-subhead__title">{t('swarm.profile.nativeTools')}</h4>
            <ToolDefinitionsPanel
              tools={previewTools}
              onToast={({ title, variant }) =>
                bridgeClient.toast(title, variant)}
            />
          {/if}
        </div>
      </div>
    </div>
  </div>
</section>
