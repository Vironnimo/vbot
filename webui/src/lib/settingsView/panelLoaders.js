// Stable loader identities share imports across retained sections and search.
const loadWakewordVoiceSettings = () =>
  import('../../components/WakewordVoiceSettings.svelte');
const loadTranscriptionAudioSettings = () =>
  import('../../components/voice/TranscriptionAudioSettings.svelte');
const loadArchiveEntriesPanel = () =>
  import('../../components/archive/ArchiveEntriesPanel.svelte');
const loadDesktopConnectionSettings = () =>
  import('../../components/settings/DesktopConnectionSettings.svelte');
const loadDesktopComputerSharing = () =>
  import('../../components/settings/DesktopComputerSharing.svelte');
const loadDesktopLiveVoiceShortcut = () =>
  import('../../components/settings/DesktopLiveVoiceShortcut.svelte');
const loadDesktopMicrophoneSettings = () =>
  import('../../components/settings/DesktopMicrophoneSettings.svelte');
const loadDesktopDictationSettings = () =>
  import('../../components/settings/DesktopDictationSettings.svelte');
const loadSettingsAppearancePanel = () =>
  import('../../components/settings/SettingsAppearancePanel.svelte');
const loadSettingsChannelsPanel = () =>
  import('../../components/settings/SettingsChannelsPanel.svelte');
const loadSettingsCredentialsPanel = () =>
  import('../../components/settings/SettingsCredentialsPanel.svelte');
const loadSettingsAgentSecretsPanel = () =>
  import('../../components/settings/SettingsAgentSecretsPanel.svelte');
const loadSettingsDebugPanel = () =>
  import('../../components/settings/SettingsDebugPanel.svelte');
const loadDebugView = () => import('../../components/DebugView.svelte');
const loadSettingsArchivePanel = () =>
  import('../../components/settings/SettingsArchivePanel.svelte');
const loadSettingsExtensionsPanel = () =>
  import('../../components/settings/SettingsExtensionsPanel.svelte');
const loadSettingsGeneralPanel = () =>
  import('../../components/settings/SettingsGeneralPanel.svelte');
const loadSettingsAccessPanel = () =>
  import('../../components/settings/SettingsAccessPanel.svelte');
const loadSettingsLibrarianPanel = () =>
  import('../../components/settings/SettingsLibrarianPanel.svelte');
const loadSettingsLiveVoicePanel = () =>
  import('../../components/settings/SettingsLiveVoicePanel.svelte');
const loadSettingsNotificationsPanel = () =>
  import('../../components/settings/SettingsNotificationsPanel.svelte');
const loadSettingsProvidersPanel = () =>
  import('../../components/settings/SettingsProvidersPanel.svelte');
const loadSettingsRecallPanel = () =>
  import('../../components/settings/SettingsRecallPanel.svelte');
const loadSettingsReflectionPanel = () =>
  import('../../components/settings/SettingsReflectionPanel.svelte');
const loadSettingsSessionTitlesPanel = () =>
  import('../../components/settings/SettingsSessionTitlesPanel.svelte');
const loadSettingsSpecializedModelsPanel = () =>
  import('../../components/settings/SettingsSpecializedModelsPanel.svelte');
const loadSettingsSubAgentsPanel = () =>
  import('../../components/settings/SettingsSubAgentsPanel.svelte');
const loadSettingsWebFetchPanel = () =>
  import('../../components/settings/SettingsWebFetchPanel.svelte');
const loadSettingsWebSearchPanel = () =>
  import('../../components/settings/SettingsWebSearchPanel.svelte');

export const settingsPanelLoaders = {
  preferences: loadSettingsGeneralPanel,
  providers: loadSettingsProvidersPanel,
  channels: loadSettingsChannelsPanel,
  extensions: loadSettingsExtensionsPanel,
  credentials: loadSettingsCredentialsPanel,
  agent_secrets: loadSettingsAgentSecretsPanel,
  live_voice_model: loadSettingsLiveVoicePanel,
  speech_models: loadSettingsSpecializedModelsPanel,
  media_models: loadSettingsSpecializedModelsPanel,
  decision_model: loadSettingsSpecializedModelsPanel,
  session_titles: loadSettingsSessionTitlesPanel,
  recall: loadSettingsRecallPanel,
  voice_controls: loadWakewordVoiceSettings,
  transcription_audio: loadTranscriptionAudioSettings,
  web_fetch: loadSettingsWebFetchPanel,
  web_search: loadSettingsWebSearchPanel,
  subagents: loadSettingsSubAgentsPanel,
  reflection: loadSettingsReflectionPanel,
  librarian: loadSettingsLibrarianPanel,
  notifications: loadSettingsNotificationsPanel,
  appearance: loadSettingsAppearancePanel,
  archive_retention: loadSettingsArchivePanel,
  archive_entries: loadArchiveEntriesPanel,
  debug_capture: loadSettingsDebugPanel,
  debug_traces: loadDebugView,
  server: loadSettingsGeneralPanel,
  access: loadSettingsAccessPanel,
  desktop_connection: loadDesktopConnectionSettings,
  computer_sharing: loadDesktopComputerSharing,
  live_voice_shortcut: loadDesktopLiveVoiceShortcut,
  microphone: loadDesktopMicrophoneSettings,
  desktop_dictation: loadDesktopDictationSettings,
};
