import { t } from './i18n.js';
import { panelById } from './settingsView/catalog.js';

export function contributionSurfaceOptions() {
  return [
    { value: 'shell.status', label: t('contributions.shellStatus') },
    { value: 'shell.sidebar', label: t('contributions.sidebar') },
    { value: 'chat.header', label: t('contributions.header') },
    { value: 'chat.composer.actions', label: t('contributions.actions') },
    { value: 'chat.composer.context', label: t('contributions.context') },
    { value: 'chat.composer.footer', label: t('contributions.footer') },
    {
      value: 'chat.composer.footer.start',
      label: t('contributions.footerStart'),
    },
    { value: 'chat.composer.footer.end', label: t('contributions.footerEnd') },
    { value: 'chat.timeline', label: t('contributions.timeline') },
    { value: 'chat.tool-result', label: t('contributions.toolResult') },
    { value: 'chat.message', label: t('contributions.message') },
    { value: 'chat.message.actions', label: t('contributions.messageActions') },
    { value: 'chat.sessions', label: t('contributions.sessions') },
    { value: 'chat.session.item', label: t('contributions.sessionItem') },
    { value: 'agent.editor.sections', label: t('contributions.agentEditor') },
    {
      value: 'project.editor.sections',
      label: t('contributions.projectEditor'),
    },
    { value: 'settings.sections', label: t('contributions.settings') },
  ];
}

// Metadata for native contributions exists before any view is mounted. The
// mounted registry may refine dynamic labels, but opening preferences first
// must still let the user configure every native surface.
const BUILTIN_ENTRIES = {
  'shell.status': [
    ['core:status.live-voice', () => t('contributions.liveVoice'), 10],
    ['core:status.voice', () => t('contributions.voiceStatus'), 20],
    ['core:status.connection', () => t('contributions.connection'), 30],
  ],
  'shell.sidebar': [
    ['core:sidebar.navigation', () => t('navigation.sections'), 10],
  ],
  'chat.header': [
    ['core:chat-header.area', () => t('chat.title'), 10],
    ['core:chat-header.search', () => t('chat.search.title'), 15],
    ['core:chat-header.info', () => t('chat.activity.open'), 20],
  ],
  'chat.composer.actions': [
    ['core:composer.microphone', () => t('chat.voice.startRecording'), 10],
    ['core:composer.attachment', () => t('chat.attachment.addFile'), 20],
    ['core:composer.stop', () => t('chat.cancelRun'), 90],
    ['core:composer.send', () => t('chat.sendMessage'), 100],
  ],
  'chat.composer.context': [
    ['core:composer.usage', () => t('chat.contextRingLabel'), 10],
  ],
  'chat.composer.footer.start': [
    [
      'core:session-settings.project',
      () => t('chat.sessionSettings.project'),
      10,
    ],
    ['core:session-settings.agent', () => t('chat.sessionSettings.agent'), 20],
    [
      'core:session-settings.permissions',
      () => t('permissions.session.access'),
      30,
    ],
    ['core:session-settings.more', () => t('chat.sessionSettings.more'), 40],
    [
      'core:session-settings.performance',
      () => t('chat.sessionSettings.speed'),
      50,
    ],
  ],
  'chat.composer.footer.end': [
    ['core:session-settings.save', () => t('common.save'), 10],
    [
      'core:session-settings.fast',
      () => t('chat.sessionSettings.fastMode'),
      20,
    ],
    ['core:session-settings.model', () => t('chat.sessionSettings.model'), 30],
    [
      'core:session-settings.effort',
      () => t('chat.sessionSettings.thinkingEffort'),
      40,
    ],
  ],
  'chat.timeline': [['core:chat.timeline', () => t('chat.title'), 10]],
  'chat.tool-result': [
    ['core:chat.tool-result', () => t('chat.toolResultLabel'), 10],
  ],
  'chat.message': [['core:chat.message.content', () => t('chat.title'), 10]],
  'chat.message.actions': [
    ['core:chat.message.copy', () => t('common.copy'), 10],
    ['core:chat.message.edit', () => t('chat.editMessage'), 20],
  ],
  'chat.sessions': [
    ['core:chat.sessions.controls', () => t('sessions.title'), 10],
    ['core:chat.sessions.new', () => t('chat.newSession'), 20],
  ],
  'chat.session.item': [
    ['core:chat.session.select', () => t('sessions.title'), 10],
    ['core:chat.session.actions', () => t('sessions.actions'), 20],
  ],
  'agent.editor.sections': [
    ['core:agent.editor.overview', () => t('agents.detail.identity'), 10],
    ['core:agent.editor.behavior', () => t('agents.contextMemory'), 20],
    ['core:agent.editor.access', () => t('agents.form.toolAccess'), 30],
    ['core:agent.editor.details', () => t('agents.storageDetails'), 40],
  ],
  'project.editor.sections': [
    ['core:project.editor.overview', () => t('projects.repositorySection'), 10],
    ['core:project.editor.context', () => t('projects.part.context'), 20],
    ['core:project.editor.team', () => t('projects.part.team'), 30],
  ],
};

export function builtinContributionEntries(surface) {
  if (surface === 'settings.sections') {
    return [...panelById.values()].map((panel) => ({
      id: `core:settings.${panel.id}`,
      title: panel.label(),
      order: 0,
    }));
  }
  return (BUILTIN_ENTRIES[surface] ?? []).map(([id, label, order]) => ({
    id,
    title: label(),
    order,
  }));
}
