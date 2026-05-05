const DEFAULT_LOCALE = 'en';

export const englishCatalog = Object.freeze({
  'app.title': 'vBot',
  'app.eyebrow': 'Local agent harness',
  'app.subtitle': 'Chat with local-first agents through the vBot server.',
  'app.loading': 'Loading vBot…',
  'app.ready': 'Ready',
  'app.offline': 'Server connection unavailable',
  'app.serverStatus': 'Server status',
  'app.serverReady': 'server: ready',

  'navigation.primary': 'Primary navigation',
  'navigation.sections': 'Sections',
  'navigation.chat': 'Chat',
  'navigation.agents': 'Agents',
  'navigation.systemPrompt': 'System Prompt',
  'navigation.settings': 'Settings',

  'common.cancel': 'Cancel',
  'common.close': 'Close',
  'common.create': 'Create',
  'common.delete': 'Delete',
  'common.edit': 'Edit',
  'common.loading': 'Loading…',
  'common.new': 'New',
  'common.optional': 'Optional',
  'common.refresh': 'Refresh',
  'common.remove': 'Remove',
  'common.retry': 'Retry',
  'common.save': 'Save',
  'common.saving': 'Saving…',
  'common.send': 'Send',
  'common.unknown': 'Unknown',

  'loading.initial': 'Preparing the WebUI…',
  'loading.agents': 'Loading agents…',
  'loading.history': 'Loading chat history…',
  'loading.sending': 'Sending message…',
  'loading.cancelling': 'Cancelling run…',
  'loading.reconnecting': 'Reconnecting…',

  'errors.generic': 'Something went wrong. Try again.',
  'errors.network':
    'Network request failed. Check that the vBot server is running.',
  'errors.rpc': 'The server rejected the request.',
  'errors.notFound': 'The requested item was not found.',
  'errors.validation': 'Check the highlighted fields and try again.',
  'errors.streamClosed': 'The live stream closed before the run finished.',
  'errors.activeRun': 'That chat already has an active run.',
  'errors.minimumAgents': 'At least one agent must remain.',
  'errors.unknownMethod': 'The requested server method is not available.',

  'placeholders.status': 'Foundation placeholder',
  'placeholders.previewLabel': 'Upcoming view preview',
  'placeholders.chat.description':
    'Agent chat will appear here once the chat view is wired.',
  'placeholders.agents.description':
    'Agent creation, editing, and deletion controls are coming next.',
  'placeholders.systemPrompt.description':
    'Editable prompt pieces will be managed from this space later.',
  'placeholders.settings.description':
    'Runtime and WebUI settings placeholders live here for now.',

  'chat.title': 'Chat',
  'chat.subtitle': 'Select an agent and continue its active session.',
  'chat.selectAgent': 'Select agent',
  'chat.session': 'Session',
  'chat.noAgentSelected': 'Choose an agent to start chatting.',
  'chat.noAgents': 'No agents are available yet.',
  'chat.newSession': 'New Session',
  'chat.newSessionBlocked':
    'A new session can be started after the current run finishes.',
  'chat.historyEmpty': 'No messages yet. Send the first message to this agent.',
  'chat.composerLabel': 'Message',
  'chat.composerPlaceholder': 'Ask this agent to do something…',
  'chat.sendMessage': 'Send message',
  'chat.queueMessage': 'Queue message',
  'chat.messageQueued': 'Message queued for the next run.',
  'chat.cancelRun': 'Cancel run',
  'chat.cancelRunDescription': 'Stop the active run as soon as possible.',
  'chat.streamConnecting': 'Connecting to live run…',
  'chat.streamConnected': 'Live run connected',
  'chat.streamDisconnected': 'Live run disconnected',
  'chat.historyLoadError': 'Chat history could not be loaded.',
  'chat.sendError': 'Message could not be sent.',
  'chat.cancelError': 'Run could not be cancelled.',
  'chat.sessionCreateError': 'New session could not be created.',
  'chat.role.user': 'You',
  'chat.role.assistant': 'Assistant',
  'chat.role.system': 'System',
  'chat.event.thinking': 'Thinking',
  'chat.event.toolStarted': 'Tool started',
  'chat.event.toolResult': 'Tool result',
  'chat.event.args': 'Args',
  'chat.event.result': 'Result',
  'chat.event.running': 'running…',
  'chat.event.done': 'done',
  'chat.event.assistantOutput': 'Assistant output',
  'chat.event.completed': 'Run completed',
  'chat.event.failed': 'Run failed',
  'chat.event.cancelled': 'Run cancelled',
  'chat.runStatus.idle': 'Idle',
  'chat.runStatus.running': 'Running',
  'chat.runStatus.queued': 'Queued',
  'chat.runStatus.completed': 'Completed',
  'chat.runStatus.failed': 'Failed',
  'chat.runStatus.cancelling': 'Cancelling',
  'chat.runStatus.cancelled': 'Cancelled',

  'queue.title': 'Queued messages',
  'queue.empty': 'No queued messages.',
  'queue.pending': 'Waiting for the active run to finish.',
  'queue.removeMessage': 'Remove queued message',
  'queue.nextMessage': 'Next queued message',
  'queue.count': '{count} queued',

  'cancel.title': 'Cancel active run?',
  'cancel.description':
    'Cancellation is best-effort. Output already shown will remain visible.',
  'cancel.confirm': 'Cancel run',
  'cancel.cancelling': 'Cancelling run…',
  'cancel.cancelled': 'Run cancelled',

  'agents.title': 'Agents',
  'agents.subtitle':
    'Create and maintain the agent configurations used by chat.',
  'agents.listTitle': 'Available agents',
  'agents.loading': 'Loading agents…',
  'agents.empty': 'No agents found.',
  'agents.create': 'Create Agent',
  'agents.createDescription':
    'Define a file-backed agent configuration for chat.',
  'agents.edit': 'Edit Agent',
  'agents.delete': 'Delete Agent',
  'agents.deleteDisabledMinimum': 'The last remaining agent cannot be deleted.',
  'agents.deleteConfirmTitle': 'Delete {name}?',
  'agents.deleteConfirmMessage':
    'The agent will be archived and its sessions will remain on disk.',
  'agents.created': 'Agent created.',
  'agents.updated': 'Agent updated.',
  'agents.deleted': 'Agent deleted.',
  'agents.loadError': 'Agents could not be loaded.',
  'agents.saveError': 'Agent could not be saved.',
  'agents.deleteError': 'Agent could not be deleted.',
  'agents.noModel': 'No model',
  'agents.idLabel': 'id',
  'agents.group.identity': 'Identity',
  'agents.group.model': 'Model',
  'agents.group.access': 'Access',
  'agents.group.workspace': 'Workspace',
  'agents.form.id': 'Agent ID',
  'agents.form.name': 'Name',
  'agents.form.model': 'Model',
  'agents.form.fallbackModel': 'Fallback model',
  'agents.form.workspace': 'Workspace',
  'agents.form.temperature': 'Temperature',
  'agents.form.thinkingEffort': 'Thinking effort',
  'agents.form.allowedTools': 'Allowed tools',
  'agents.form.allowedSkills': 'Allowed skills',
  'agents.form.idHelp': 'Agent IDs are immutable after creation.',
  'agents.form.idPlaceholder': 'main-agent',
  'agents.form.namePlaceholder': 'Main Agent',
  'agents.form.modelPlaceholder': 'provider/model-id',
  'agents.form.thinkingPlaceholder': 'medium',
  'agents.form.listHelp': 'Enter one item per line.',
  'agents.form.submitCreate': 'Create agent',
  'agents.form.submitUpdate': 'Save changes',
  'agents.form.required': 'This field is required.',

  'systemPrompt.title': 'System Prompt',
  'systemPrompt.subtitle':
    'Review how prompt pieces will be managed in a later phase.',
  'systemPrompt.comingSoon': 'Editable prompt pieces are coming soon.',
  'systemPrompt.description':
    'The current minimal WebUI uses the server-managed system prompt.',
  'systemPrompt.pieces': 'Prompt pieces',

  'settings.title': 'Settings',
  'settings.subtitle': 'Runtime and WebUI settings will live here.',
  'settings.comingSoon': 'Settings controls are coming soon.',
  'settings.serverStatus': 'Server status',
  'settings.modelDefaults': 'Model defaults',
  'settings.preferences': 'Preferences',
});

const catalogs = Object.freeze({
  [DEFAULT_LOCALE]: englishCatalog,
});

let activeLocale = DEFAULT_LOCALE;

function hasText(value) {
  return typeof value === 'string' && value.length > 0;
}

function interpolate(template, values) {
  if (!values) {
    return template;
  }

  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (match, name) => {
    if (!Object.prototype.hasOwnProperty.call(values, name)) {
      return match;
    }

    return String(values[name]);
  });
}

export function t(key, fallback, values) {
  const catalog = catalogs[activeLocale] ?? catalogs[DEFAULT_LOCALE];
  const translation = catalog[key] ?? catalogs[DEFAULT_LOCALE][key];
  const template = hasText(translation)
    ? translation
    : hasText(fallback)
      ? fallback
      : key;

  return interpolate(template, values);
}

export function init(locale = DEFAULT_LOCALE) {
  activeLocale = catalogs[locale] ? locale : DEFAULT_LOCALE;

  return activeLocale;
}
