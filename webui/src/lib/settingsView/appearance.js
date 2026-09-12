export const SETTINGS_LAYOUT_CLASS = 'settings-layout view active';

// Presence rows for the General panel "Connected clients" list. Pure: passes
// the registry fields through and flags the caller's own row by matching the
// client-minted connection id (an empty own id matches nothing).
export function buildClientPresenceRows(clients, ownConnectionId) {
  if (!Array.isArray(clients)) {
    return [];
  }
  const ownId = typeof ownConnectionId === 'string' ? ownConnectionId : '';
  return clients.map((client) => {
    const connectionId =
      typeof client?.connection_id === 'string' ? client.connection_id : '';
    return {
      id: typeof client?.id === 'string' ? client.id : '',
      connectionId,
      accessor: typeof client?.accessor === 'string' ? client.accessor : '',
      browser: typeof client?.browser === 'string' ? client.browser : '',
      os: typeof client?.os === 'string' ? client.os : '',
      connectedAt:
        typeof client?.connected_at === 'string' ? client.connected_at : '',
      status: typeof client?.status === 'string' ? client.status : '',
      isOwn: ownId.length > 0 && connectionId === ownId,
    };
  });
}

export const TRANSCRIPTION_AUDIO_PROFILES = Object.freeze([
  'compatibility',
  'high_quality',
  'custom',
]);

export const TRANSCRIPTION_AUDIO_FORMATS = Object.freeze(['wav', 'flac']);

export const TRANSCRIPTION_AUDIO_SAMPLE_RATES = Object.freeze([
  16000, 24000, 48000,
]);

const TRANSCRIPTION_AUDIO_PRESETS = Object.freeze({
  compatibility: Object.freeze({ format: 'wav', sample_rate_hz: 16000 }),
  high_quality: Object.freeze({ format: 'flac', sample_rate_hz: 48000 }),
});

const DEFAULT_TRANSCRIPTION_AUDIO = Object.freeze({
  profile: 'compatibility',
  ...TRANSCRIPTION_AUDIO_PRESETS.compatibility,
});

export function normalizeTranscriptionAudio(settings) {
  const source = settings?.speech?.transcription_audio;
  const profile = TRANSCRIPTION_AUDIO_PROFILES.includes(source?.profile)
    ? source.profile
    : DEFAULT_TRANSCRIPTION_AUDIO.profile;
  const preset = TRANSCRIPTION_AUDIO_PRESETS[profile];
  if (preset) {
    return { profile, ...preset };
  }

  const format = TRANSCRIPTION_AUDIO_FORMATS.includes(source?.format)
    ? source.format
    : DEFAULT_TRANSCRIPTION_AUDIO.format;
  const sampleRate = Number(source?.sample_rate_hz);
  const sample_rate_hz = TRANSCRIPTION_AUDIO_SAMPLE_RATES.includes(sampleRate)
    ? sampleRate
    : DEFAULT_TRANSCRIPTION_AUDIO.sample_rate_hz;
  return { profile, format, sample_rate_hz };
}

export function buildTranscriptionAudioSettingsPayload(audio) {
  return {
    speech: {
      transcription_audio: normalizeTranscriptionAudio({
        speech: { transcription_audio: audio },
      }),
    },
  };
}

export function buildLanguageOptions(appearance) {
  const availableLanguages = Array.isArray(appearance?.available_languages)
    ? appearance.available_languages
    : [];
  const languageIds =
    availableLanguages.length > 0
      ? availableLanguages
      : appearance?.language
        ? [appearance.language]
        : ['en'];

  return languageIds.map((languageId) => ({
    id: languageId,
    labelKey: `settings.language.${languageId}`,
    labelFallback: languageId,
  }));
}

export function formatServerHost(server, translate) {
  if (
    typeof server?.listen_host === 'string' &&
    server.listen_host.length > 0 &&
    Number.isFinite(server.listen_port)
  ) {
    return `${server.listen_host}:${server.listen_port}`;
  }

  return translate('common.unknown', 'Unknown');
}

export function getDataDirectoryValue(settings, translate) {
  return (
    settings?.general?.data_directory ?? translate('common.unknown', 'Unknown')
  );
}

export function getDefaultSkillDirectoryValue(settings, translate) {
  return (
    settings?.skills?.default_directory ??
    settings?.general?.default_skill_directory ??
    translate('common.unknown', 'Unknown')
  );
}

export function getSkillDirectories(settings) {
  return Array.isArray(settings?.skills?.directories)
    ? normalizeSkillDirectories(settings.skills.directories)
    : [];
}

function normalizeSkillDirectories(directories) {
  if (!Array.isArray(directories)) {
    return [];
  }

  return directories
    .map((directory) =>
      directory === null || directory === undefined
        ? ''
        : String(directory).trim(),
    )
    .filter((directory) => directory.length > 0);
}

export function createSkillDirectoriesUpdatePayload(directories) {
  return {
    skills: {
      directories: normalizeSkillDirectories(directories),
    },
  };
}

export function getPersistedLanguageId(settings) {
  return settings?.appearance?.language ?? '';
}

// Chat reading-column width preference (mirrors the backend
// SUPPORTED_APPEARANCE_CHAT_WIDTHS / DEFAULT_APPEARANCE_CHAT_WIDTH).
export const CHAT_WIDTH_OPTIONS = ['comfortable', 'wide', 'full'];

export const DEFAULT_CHAT_WIDTH = 'comfortable';

export const CHAT_WORKING_MODE_OPTIONS = ['normal', 'compact'];

export const DEFAULT_CHAT_WORKING_MODE = 'normal';

export function getPersistedChatWidth(settings) {
  const value = settings?.appearance?.chat_width;
  return CHAT_WIDTH_OPTIONS.includes(value) ? value : DEFAULT_CHAT_WIDTH;
}

export function buildChatWidthOptions() {
  return CHAT_WIDTH_OPTIONS.map((id) => ({
    id,
    labelKey: `settings.appearance.chatWidth.${id}`,
    labelFallback: id,
  }));
}

export function getPersistedChatWorkingMode(settings) {
  const value = settings?.appearance?.chat_working_mode;
  return CHAT_WORKING_MODE_OPTIONS.includes(value)
    ? value
    : DEFAULT_CHAT_WORKING_MODE;
}

export function buildChatWorkingModeOptions() {
  return CHAT_WORKING_MODE_OPTIONS.map((id) => ({
    id,
    labelKey: `settings.appearance.chatWorkingMode.${id}`,
    labelFallback: id,
  }));
}

// The appearance section is normalized as a whole on the backend (a missing
// field resets to its default), so all controls always save together.
export function isAppearanceSaveDisabled({
  loading,
  saving,
  selectedLanguageId,
  selectedChatWidth,
  selectedChatWorkingMode,
  persistedLanguageId,
  persistedChatWidth,
  persistedChatWorkingMode,
}) {
  if (loading || saving || selectedLanguageId.length === 0) {
    return true;
  }
  return (
    selectedLanguageId === persistedLanguageId &&
    selectedChatWidth === persistedChatWidth &&
    selectedChatWorkingMode === persistedChatWorkingMode
  );
}

export function createAppearanceUpdatePayload({
  language,
  chatWidth,
  chatWorkingMode,
}) {
  return {
    appearance: {
      language,
      chat_width: chatWidth,
      chat_working_mode: chatWorkingMode,
    },
  };
}
