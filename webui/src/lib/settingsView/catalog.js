import { t } from '$lib/i18n.js';

// Navigation follows user tasks; editor components do not define pages.
const sections = [
  {
    id: 'appearance',
    label: () => t('settings.appearance.title'),
  },
  {
    id: 'session_titles',
    label: () => t('settings.sessionTitles.title'),
  },
  {
    id: 'preferences',
    label: () => t('settings.preferences.title'),
  },
  {
    id: 'notifications',
    label: () => t('settings.notifications.title'),
  },
  {
    id: 'providers',
    label: () => t('settings.providers.title'),
  },
  {
    id: 'voice_controls',
    label: () => t('settings.sections.wakeword'),
  },
  {
    id: 'transcription_audio',
    label: () => t('settings.sections.transcriptionAudio'),
  },
  {
    id: 'speech_models',
    label: () => t('settings.sections.speechModels'),
  },
  {
    id: 'live_voice_model',
    label: () => t('settings.sections.liveVoice'),
  },
  {
    id: 'live_voice_shortcut',
    label: () => t('settings.sections.liveVoiceShortcut'),
  },
  {
    id: 'microphone',
    label: () => t('settings.sections.microphone'),
  },
  {
    id: 'desktop_dictation',
    label: () => t('settings.sections.desktopDictation'),
  },
  {
    id: 'recall',
    label: () => t('settings.sections.recall'),
  },
  {
    id: 'reflection',
    label: () => t('settings.reflection.title'),
  },
  {
    id: 'librarian',
    label: () => t('settings.librarian.title'),
  },
  {
    id: 'web_search',
    label: () => t('settings.webSearch.title'),
  },
  { id: 'web_fetch', label: () => t('settings.webFetch.title') },
  {
    id: 'media_models',
    label: () => t('settings.sections.mediaModels'),
  },
  {
    id: 'decision_model',
    label: () => t('settings.sections.decisionModel'),
  },
  {
    id: 'subagents',
    label: () => t('settings.sections.delegation'),
  },
  { id: 'channels', label: () => t('settings.channels.title') },
  {
    id: 'extensions',
    label: () => t('settings.extensions.title'),
  },
  {
    id: 'credentials',
    label: () => t('settings.credentials.title'),
  },
  {
    id: 'agent_secrets',
    label: () => t('settings.agentSecrets.title'),
  },
  { id: 'server', label: () => t('settings.sections.server') },
  { id: 'access', label: () => t('settings.sections.access') },
  {
    id: 'desktop_connection',
    label: () => t('settings.desktop.connection.title'),
  },
  { id: 'computer_sharing', label: () => t('computers.sharingTitle') },
  {
    id: 'archive_retention',
    label: () => t('settings.archive.retentionTitle'),
  },
  { id: 'archive_entries', label: () => t('archive.listLabel') },
  { id: 'debug_capture', label: () => t('debug.captureAndStorage') },
  { id: 'debug_traces', label: () => t('debug.traceList') },
];
export const panelById = new Map(
  sections.map((section) => [section.id, section]),
);
export const modelTasksBySection = {
  speech_models: ['speech_to_text', 'text_to_speech'],
  media_models: [
    'image_understanding',
    'image_generation',
    'video_generation',
    'music_generation',
  ],
  decision_model: ['decision'],
};
export function settingsPages(desktopCapabilities) {
  return [
    {
      id: 'general',
      label: () => t('settings.pages.general'),
      description: () => t('settings.pages.generalDescription'),
      // Everyday display first; the time zone and the setup guide are
      // rarely revisited.
      sections: [
        'appearance',
        'session_titles',
        'notifications',
        'preferences',
      ],
    },
    {
      id: 'providers',
      label: () => t('settings.providers.title'),
      description: () => t('settings.pages.providersDescription'),
      sections: ['providers'],
    },
    {
      id: 'voice',
      label: () => t('settings.voice.title'),
      description: () => t('settings.pages.voiceDescription'),
      // Models first, then the Desktop: its microphone, the Live voice
      // shortcut right after the Live voice Model, dictation and the wakeword.
      // The rarely changed recording format comes last.
      sections: [
        'speech_models',
        'live_voice_model',
        ...(desktopCapabilities?.microphone ? ['microphone'] : []),
        ...(desktopCapabilities?.liveHotkey ? ['live_voice_shortcut'] : []),
        ...(desktopCapabilities?.dictation ? ['desktop_dictation'] : []),
        'voice_controls',
        'transcription_audio',
      ],
    },
    {
      id: 'memory',
      label: () => t('settings.pages.memory'),
      description: () => t('settings.pages.memoryDescription'),
      sections: ['reflection', 'librarian', 'recall'],
    },
    {
      id: 'tools',
      label: () => t('settings.pages.tools'),
      description: () => t('settings.pages.toolsDescription'),
      sections: [
        'web_search',
        'web_fetch',
        'media_models',
        'decision_model',
        'subagents',
      ],
    },
    {
      id: 'integrations',
      label: () => t('settings.pages.integrations'),
      description: () => t('settings.pages.integrationsDescription'),
      // Named credentials last: they are rarely managed on their own.
      sections: ['channels', 'extensions', 'credentials'],
    },
    {
      id: 'secrets',
      label: () => t('settings.agentSecrets.title'),
      description: () => t('settings.pages.secretsDescription'),
      sections: ['agent_secrets'],
    },
    {
      id: 'archive',
      label: () => t('settings.archive.title'),
      description: () => t('settings.pages.archiveDescription'),
      // The retention period first, then the archived items; an item opens
      // as a sub-page of this page in place of everything else.
      sections: ['archive_retention', 'archive_entries'],
      subPages: 'archive_entries',
    },
    {
      id: 'system',
      label: () => t('settings.pages.system'),
      description: () => t('settings.pages.systemDescription'),
      sections: [
        'server',
        'access',
        ...(desktopCapabilities?.serverSelection ? ['desktop_connection'] : []),
        ...(desktopCapabilities?.computerSharing ? ['computer_sharing'] : []),
      ],
    },
    {
      id: 'debug',
      label: () => t('debug.title'),
      description: () => t('debug.inspectorSubtitle'),
      sections: ['debug_capture', 'debug_traces'],
      subPages: 'debug_traces',
    },
  ];
}
