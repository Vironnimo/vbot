<script>
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import CompactionPolicyEditor from '../compaction/CompactionPolicyEditor.svelte';
  import {
    AGENT_FORM_MODE_EDIT,
    AGENT_FORM_MODE_CREATE,
    AGENT_MEMORY_PROMPT_MODES,
  } from '$lib/agentForm.js';
  import Dropdown from '../Dropdown.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import MemoryEntriesEditor from '../memory/MemoryEntriesEditor.svelte';
  let {
    agent,
    onNavigateToSettingsPanel,
    memoriesRefreshToken,
    formValues = $bindable(),
    formMode,
    handleCustomPromptToggle,
    navigateToAgentPrompt,
    showAgentToast,
    viewErrorMessage,
  } = $props();

  let memoryPromptOptions = $derived(
    AGENT_MEMORY_PROMPT_MODES.map((option) => ({
      value: option,
      label: t(`agents.form.memoryPromptModeOption.${option}`),
    })),
  );

  function setOwnCompactionPolicy(enabled) {
    formValues.compaction_policy = enabled
      ? $state.snapshot(agent?.effective_compaction_policy ?? {})
      : null;
  }
</script>

<div class="agents-view__part" id="agent-detail-panel-behavior">
  <section class="s-section" aria-labelledby="agent-section-context">
    <header class="s-section__head">
      <h3 class="s-section__title" id="agent-section-context">
        {t('agents.contextMemory')}
      </h3>
    </header>
    <div class="s-section__body">
      <div class="s-group agents-view__memory-group">
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('agents.form.customSystemPrompt')}
            </div>
            <div class="s-row-desc">
              {t('agents.form.customPromptDescription')}
            </div>
            {#if formMode === AGENT_FORM_MODE_EDIT && formValues.custom_system_prompt_enabled}
              <Button
                variant="tertiary"
                class="agents-view__row-link"
                onClick={navigateToAgentPrompt}
              >
                {t('agents.form.editAgentPrompt')}
              </Button>
            {/if}
          </div>
          <div class="s-row-control">
            <Toggle
              class="agents-view__prompt-toggle"
              checked={formValues.custom_system_prompt_enabled}
              ariaLabel={t('agents.form.customSystemPrompt')}
              disabled={formMode === AGENT_FORM_MODE_CREATE}
              onChange={(next) => {
                void handleCustomPromptToggle(next);
              }}
            />
          </div>
        </div>

        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="agent-memory-prompt-mode">
              {t('agents.form.memoryPromptMode')}
            </label>
            <div class="s-row-desc" id="agent-memory-prompt-mode-help">
              {t('agents.form.memoryModeHelp')}
            </div>
          </div>
          <div class="s-row-control">
            <Dropdown
              id="agent-memory-prompt-mode"
              value={formValues.memory_prompt_mode}
              options={memoryPromptOptions}
              ariaLabel={t('agents.form.memoryPromptMode')}
              ariaDescribedby="agent-memory-prompt-mode-help"
              triggerClass="agents-view__dropdown"
              listClass="agents-view__memory-list"
              onValueChange={(selectedValue) => {
                formValues.memory_prompt_mode = selectedValue;
              }}
            />
          </div>
        </div>

        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <div class="s-row-label">{t('memory.projectUsage')}</div>
            <div class="s-row-desc">{t('memory.projectUsageHelp')}</div>
          </div>
          <div class="s-row-control">
            <Toggle
              checked={formValues.project_memory_enabled}
              ariaLabel={t('memory.projectUsage')}
              onChange={(next) => (formValues.project_memory_enabled = next)}
            />
          </div>
        </div>

        {#if formMode === AGENT_FORM_MODE_EDIT}
          <div class="s-row s-row--stacked">
            <div class="s-row-info">
              <label class="s-row-label" for="agent-context-commands">
                {t('agents.form.contextCommands')}
              </label>
              <div class="s-row-desc" id="agent-context-commands-help">
                {t('agents.form.contextCommandsHelp')}
              </div>
            </div>
            <div class="s-row-control">
              <TextArea
                id="agent-context-commands"
                code
                autoGrow
                rows={2}
                value={formValues.context_commands}
                placeholder={t('agents.form.contextCommandsPlaceholder')}
                ariaLabel={t('agents.form.contextCommands')}
                aria-describedby="agent-context-commands-help"
                onInput={(next) => {
                  formValues.context_commands = next;
                }}
              />
            </div>
          </div>

          <MemoryEntriesEditor
            agentId={agent?.id ?? ''}
            activeScopes={formValues.memory_prompt_mode === 'agent_user'
              ? ['agent', 'user']
              : formValues.memory_prompt_mode === 'agent'
                ? ['agent']
                : []}
            refreshToken={memoriesRefreshToken}
            onToast={showAgentToast}
            {viewErrorMessage}
          />
        {/if}
      </div>

      <div class="s-group">
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('agents.form.librarian')}
              <InfoHint text={t('agents.form.librarianHelp')} />
            </div>
            <div class="s-row-desc">
              {t('agents.form.librarianDescription')}
            </div>
          </div>
          <div class="s-row-control">
            <Toggle
              class="agents-view__librarian-toggle"
              checked={formValues.librarian_enabled !== false}
              ariaLabel={t('agents.form.librarian')}
              onChange={(next) => (formValues.librarian_enabled = next)}
            />
          </div>
        </div>
      </div>

      <div class="s-group agents-view__compaction-group">
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('agents.customCompaction')}
            </div>
            <div class="s-row-desc">
              {t('agents.customCompactionHelp')}
            </div>
            <Button
              variant="tertiary"
              class="agents-view__row-link"
              onClick={() => onNavigateToSettingsPanel('compaction')}
              >{t('agents.shared.title')}</Button
            >
          </div>
          <div class="s-row-control">
            <Toggle
              checked={formValues.compaction_policy !== null}
              ariaLabel={t('compaction.scope.agentOwn')}
              onChange={setOwnCompactionPolicy}
            />
          </div>
        </div>
        {#if formValues.compaction_policy}
          <CompactionPolicyEditor
            layout="rows"
            value={formValues.compaction_policy}
            onChange={(next) => (formValues.compaction_policy = next)}
            idPrefix="agent-compaction"
          />
        {/if}
      </div>
    </div>
  </section>
</div>
