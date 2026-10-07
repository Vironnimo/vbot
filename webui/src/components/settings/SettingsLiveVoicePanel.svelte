<script>
  import { onMount, untrack } from 'svelte';

  import AgentModelSettings from '../agents/AgentModelSettings.svelte';
  import { formatSkillTime } from '../skills/skillRecords.js';
  import ToolAccessEditor from '../tools/ToolAccessEditor.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import SettingsSpecializedModelsPanel from './SettingsSpecializedModelsPanel.svelte';
  import { createBuiltinAgentEditor } from './builtinAgentEditor.svelte.js';
  import {
    listConnections,
    listModels,
    listSessions,
    listTools,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    LIVE_BACKEND_AGENT_ID,
    LIVE_VOICE_AGENT_ID,
  } from '$lib/liveAgents.js';

  // Live voice: the `live_voice` Task Model binding (the voice model and
  // its options, among them who answers its requests), the Tools of the
  // built-in voice Agent, the Model and Tools of the built-in vBot backend
  // Agent (both saved with `agent.update`), and the recent calls, each
  // opening the voice Session that recorded it.

  const noop = () => {};
  const CALL_LIMIT = 10;

  let {
    settings = null,
    onCommit = noop,
    onError = noop,
    onOpenSession = noop,
    modelsRefreshToken = 0,
    agentsRefreshToken = 0,
    sessionsRefreshToken = 0,
  } = $props();

  const voiceAgent = createBuiltinAgentEditor({
    agentId: LIVE_VOICE_AGENT_ID,
    builtin: 'live_voice',
    onError: (message) => onError(message),
  });
  const backendAgent = createBuiltinAgentEditor({
    agentId: LIVE_BACKEND_AGENT_ID,
    builtin: 'live_backend',
    onError: (message) => onError(message),
  });

  let availableModels = $state([]);
  let availableConnections = $state([]);
  // Null until the Tool catalog arrives.
  let tools = $state(null);
  let toolsError = $state('');
  // Null until the first list arrives.
  let calls = $state(null);
  let callsError = $state('');
  let callsVersion = 0;
  let disposed = false;

  onMount(() => {
    void loadModelCatalogs();
    void loadAgents();
    void loadCalls();
    return () => {
      disposed = true;
    };
  });

  watchToken(
    () => modelsRefreshToken,
    () => void loadModelCatalogs(),
  );
  watchToken(
    () => agentsRefreshToken,
    () => void loadAgents(),
  );
  watchToken(
    () => sessionsRefreshToken,
    () => void loadCalls(),
  );

  // Runs `reload` when the token changes after mount.
  function watchToken(read, reload) {
    let last = null;
    $effect(() => {
      const token = read();
      if (last === null) {
        last = token;
        return;
      }
      if (token !== last) {
        last = token;
        untrack(reload);
      }
    });
  }

  async function loadModelCatalogs() {
    try {
      const [modelsResult, connectionsResult] = await Promise.all([
        listModels(),
        listConnections(),
      ]);
      if (disposed) return;
      availableModels = modelsResult.models;
      availableConnections = connectionsResult.connections;
    } catch (error) {
      if (!disposed)
        onError(`${t('settings.models.loadError')} ${error.message}`);
    }
  }

  // The Live Agents with the Tool catalog their editors list.
  async function loadAgents() {
    void voiceAgent.load();
    void backendAgent.load();
    try {
      const result = await listTools();
      if (disposed) return;
      tools = Array.isArray(result?.tools) ? result.tools : [];
      toolsError = '';
    } catch (error) {
      if (!disposed) toolsError = error?.message || String(error);
    }
  }

  async function loadCalls() {
    const current = ++callsVersion;
    try {
      const result = await listSessions(LIVE_VOICE_AGENT_ID, {
        limit: CALL_LIMIT,
      });
      if (disposed || current !== callsVersion) return;
      calls = Array.isArray(result?.sessions)
        ? result.sessions.filter(
            (session) => typeof session?.id === 'string' && session.id,
          )
        : [];
      callsError = '';
    } catch (error) {
      if (!disposed && current === callsVersion)
        callsError = error?.message || String(error);
    }
  }

  function callTitle(session) {
    return (
      session.title || session.auto_title || formatSkillTime(session.created_at)
    );
  }

  function saveNow() {
    void voiceAgent.participant.runSave('manual');
    void backendAgent.participant.runSave('manual');
  }
</script>

<SettingsSpecializedModelsPanel
  taskTypes={['live_voice']}
  showTaskLabels={false}
  {settings}
  {onCommit}
  {onError}
  {modelsRefreshToken}
/>

<!-- The binding editor's save state sits on the section heading; the Agent
     settings below keep their own after them, so their footer is not a
     direct child of the section body. -->
<div>
  <div class="s-subhead">
    <h4 class="s-subhead__title">{t('settings.liveVoice.voiceTools')}</h4>
    <p class="s-subhead__desc">
      {t('settings.liveVoice.voiceToolsDescription')}
    </p>
    <p class="s-subhead__desc">{t('settings.liveVoice.voiceToolsHint')}</p>
  </div>
  {@render agentTools(voiceAgent)}

  <div class="s-subhead">
    <h4 class="s-subhead__title">{t('settings.liveVoice.backend')}</h4>
    <p class="s-subhead__desc">{t('settings.liveVoice.backendDescription')}</p>
  </div>
  {#if backendAgent.agent}
    <AgentModelSettings
      bind:formValues={backendAgent.form}
      {availableModels}
      {availableConnections}
      formErrors={backendAgent.formErrors}
      fieldError={backendAgent.fieldError}
      inheritSource={backendAgent.inheritSource}
      inheritDisplayValue={backendAgent.inheritDisplayValue}
      idPrefix="settings-live-backend"
    />
    <div class="s-subhead">
      <h4 class="s-subhead__title">{t('settings.liveVoice.backendTools')}</h4>
    </div>
  {/if}
  {@render agentTools(backendAgent)}

  <div class="s-footer">
    <SaveStatus
      saving={voiceAgent.saving || backendAgent.saving}
      pending={voiceAgent.participant.hasChanges() ||
        backendAgent.participant.hasChanges()}
      onClick={saveNow}
    />
  </div>
</div>

<div class="s-subhead">
  <h4 class="s-subhead__title">{t('settings.liveVoice.calls')}</h4>
  <p class="s-subhead__desc">{t('settings.liveVoice.callsDescription')}</p>
</div>

{#if callsError}
  <Banner variant="error"
    >{t('settings.liveVoice.callsError')} {callsError}</Banner
  >
{:else if calls === null}
  <Banner variant="neutral">{t('settings.liveVoice.callsLoading')}</Banner>
{:else if calls.length === 0}
  <EmptyState
    density="compact"
    description={t('settings.liveVoice.callsEmpty')}
  />
{:else}
  <div class="s-group">
    {#each calls as call (call.id)}
      <div class="s-row s-row--compact" data-live-call={call.id}>
        <div class="s-row-info">
          <div class="s-row-label">{callTitle(call)}</div>
          {#if call.has_active_run}
            <div class="s-row-desc">{t('settings.liveVoice.callRunning')}</div>
          {/if}
        </div>
        <div class="s-row-control">
          <Button
            variant="secondary"
            ariaLabel={t('settings.liveVoice.openSessionLabel', {
              title: callTitle(call),
            })}
            onClick={() => onOpenSession(LIVE_VOICE_AGENT_ID, call.id)}
            >{t('settings.liveVoice.openSession')}</Button
          >
        </div>
      </div>
    {/each}
  </div>
{/if}

<!-- One Live Agent's Tools: only the configurable ones, the Live call Tools
     among them, because its policy is its whole Tool set. -->
{#snippet agentTools(editor)}
  {#if editor.loadError}
    <Banner variant="error"
      >{t('settings.liveVoice.agentLoadError')} {editor.loadError}</Banner
    >
  {:else if toolsError}
    <Banner variant="error"
      >{t('settings.liveVoice.toolsLoadError')} {toolsError}</Banner
    >
  {:else if editor.agent && tools}
    <ToolAccessEditor
      liveCall
      value={editor.form.tool_access}
      {tools}
      onChange={(next) => (editor.form.tool_access = next)}
    />
  {:else if editor.loaded && !editor.agent}
    <Banner variant="warn">{t('settings.liveVoice.agentUnavailable')}</Banner>
  {/if}
{/snippet}
