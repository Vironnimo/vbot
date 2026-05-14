const DEFAULT_LOCALE = 'en';

export const englishCatalog = Object.freeze({
  'app.title': 'vBot',
  'app.eyebrow': 'Local agent harness',
  'app.subtitle': 'Chat with local-first agents through the vBot server.',
  'app.loading': 'Loading vBot…',
  'app.ready': 'Ready',
  'app.offline': 'Server connection unavailable',

  'navigation.primary': 'Primary navigation',
  'navigation.sections': 'Sections',
  'navigation.chat': 'Chat',
  'navigation.agents': 'Agents',
  'navigation.cron': 'Cron',
  'navigation.systemPrompt': 'System Prompt',
  'navigation.settings': 'Settings',
  'navigation.logs': 'Logs',

  'common.archive': 'Archive',
  'common.cancel': 'Cancel',
  'common.close': 'Close',
  'common.confirm': 'Confirm',
  'common.copy': 'Copy',
  'common.create': 'Create',
  'common.delete': 'Delete',
  'common.dismiss': 'Dismiss',
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
  'chat.noAgentSelected': 'Choose an agent to start chatting.',
  'chat.noAgents': 'No agents are available yet.',
  'chat.newSession': 'New Session',
  'chat.newSessionBlocked':
    'A new session can be started after the current run finishes.',
  'chat.historyEmpty': 'No messages yet. Send the first message to this agent.',
  'chat.composerLabel': 'Message',
  'chat.composerPlaceholder': 'Ask this agent to do something…',
  'chat.composer.placeholder': 'Enter message…',
  'chat.sendMessage': 'Send message',
  'chat.send': 'Send',
  'chat.queueMessage': 'Queue message',
  'chat.attachPlaceholder': 'Attachments are not available yet',
  'chat.messageQueued': 'Message queued for the next run.',
  'chat.cancelRun': 'Cancel run',
  'chat.cancelRunDescription': 'Stop the active run as soon as possible.',
  'chat.streamConnecting': 'Connecting to live run…',
  'chat.streamConnected': 'Live run connected',
  'chat.streamDisconnected': 'Live run disconnected',
  'chat.historyLoadError': 'Chat history could not be loaded.',
  'chat.sendError': 'Message could not be sent.',
  'chat.skillsLoadError': 'Skill suggestions could not be loaded.',
  'chat.cancelError': 'Run could not be cancelled.',
  'chat.sessionCreateError': 'New session could not be created.',
  'chat.role.user': 'You',
  'chat.role.assistant': 'Assistant',
  'chat.role.system': 'System',
  'chat.role.userAvatar': 'Y',
  'chat.role.assistantAvatar': 'A',
  'chat.role.systemAvatar': 'S',
  'chat.event.thinking': 'Thinking',
  'chat.event.toolStarted': 'Tool started',
  'chat.event.toolPreparing': 'Preparing tool',
  'chat.event.toolResult': 'Tool result',
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
  'chat.today': 'Today',
  'chat.historyEmptyTitle': 'No messages yet',
  'chat.empty.title': 'No messages yet',
  'chat.empty.subtitle': 'Send a message to start the conversation.',
  'chat.toolDone': 'done',
  'chat.toolSucceeded': 'succeeded',
  'chat.toolFailed': 'failed',
  'chat.toolArgs': 'Args',
  'chat.toolStatus': 'Status',
  'chat.toolPendingName': 'tool',
  'chat.toolPreparingArguments': 'preparing arguments',
  'chat.toolArgumentsHidden':
    'Arguments are streaming and will appear when ready.',
  'chat.toolResultLabel': 'Result',
  'chat.toolNoData': '—',
  'chat.runIterations': '{count} iter',
  'chat.runDurationSeconds': '{seconds}s',
  'chat.tokenBadge': '{tokens} / {context} tok',
  'chat.tokenBadgeEstimated': '~{tokens} / {context} tok',
  'chat.tokenBadgeNoContext': '{tokens} tok',
  'chat.tokenBadgeEstimatedNoContext': '~{tokens} tok',
  'chat.tokenBadgeNoUsage': '— / {context} tok',
  'chat.subagent.label': 'Sub-agent',
  'chat.subagent.viewSession': 'view session',
  'chat.subagent.running': 'running',
  'chat.subagent.resultStatus': 'Status: {status}',
  'chat.subagentSessionReadOnly': 'Viewing a sub-agent session',
  'chat.subagentSessionReadOnlyHint':
    'This historical session is read-only. Return to the current agent session to continue chatting.',
  'chat.returnToCurrentSession': 'Return to current session',

  'skillAutocomplete.label': 'Skill suggestions',
  'skillAutocomplete.eyebrow': 'skills',
  'skillAutocomplete.noDescription': 'No description available',

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
  'agents.form.listHelp': 'Enter one item per line.',
  'agents.form.modelPlaceholder': 'Default (no model selected)',
  'agents.form.fallbackModelPlaceholder': 'None',
  'agents.form.modelUnavailableOption': 'Unavailable / custom: {model}',
  'agents.form.modelUnavailableConnectionOption':
    'Unavailable / custom: {model} ({connection})',
  'agents.form.thinkingEffortDefault': 'Default',
  'agents.form.thinkingEffortOption.none': 'none',
  'agents.form.thinkingEffortOption.minimal': 'minimal',
  'agents.form.thinkingEffortOption.low': 'low',
  'agents.form.thinkingEffortOption.medium': 'medium',
  'agents.form.thinkingEffortOption.high': 'high',
  'agents.form.thinkingEffortOption.xhigh': 'xhigh',
  'agents.form.thinkingEffortOption.max': 'max',
  'agents.form.workspaceAssignedByServer':
    'Workspace is assigned by the server when the agent is created.',
  'agents.form.workspaceReadOnly': 'Workspace is read-only in this WebUI.',
  'agents.form.submitCreate': 'Create agent',
  'agents.form.submitUpdate': 'Save changes',
  'agents.form.required': 'This field is required.',
  'agents.detail.newSubtitle': 'id assigned at creation',
  'agents.detail.idValue': 'id: {id}',
  'agents.detail.identity': 'Identity',
  'agents.detail.model': 'Model',
  'agents.detail.fallbackStatus': 'Fallback',
  'agents.detail.thinkingStatus': 'Thinking',
  'agents.detail.access': 'Access',
  'agents.detail.session': 'Session',
  'agents.detail.sessionId': 'Session ID',
  'agents.detail.created': 'Created',
  'agents.detail.updated': 'Updated',
  'agents.emptyCreateHint': 'Create an agent to begin configuring chat access.',
  'agents.access.allOn': 'all on',
  'agents.access.allOff': 'all off',
  'agents.access.toggleTool': 'Toggle tool {name}',
  'agents.access.toggleSkill': 'Toggle skill {name}',
  'agents.access.descriptionLabel': '{description}',
  'agents.access.noSkills': 'No loadable skills are available.',
  'agents.access.skillWarnings': 'Warnings',
  'agents.access.invalidSkillsTitle': 'Unavailable skills',
  'agents.access.unknownSkillName': 'Unknown skill',
  'agents.access.notLoadable': 'not loadable',

  'cron.eyebrow': 'Scheduled automation',
  'cron.title': 'Cron',
  'cron.subtitle':
    'Manage scheduled agent runs. Completed jobs are hidden from this list.',
  'cron.newJob': 'New Job',
  'cron.noAgents': 'Create an agent before adding cron jobs.',
  'cron.loading': 'Loading cron jobs…',
  'cron.emptyTitle': 'No scheduled jobs',
  'cron.emptySubtitle':
    'Create a job to run an agent prompt on a schedule.',
  'cron.table.caption': 'Cron jobs',
  'cron.table.agent': 'Agent',
  'cron.table.prompt': 'Prompt',
  'cron.table.schedule': 'Schedule',
  'cron.table.timezone': 'Timezone',
  'cron.table.status': 'Status',
  'cron.table.lastFired': 'Last Fired',
  'cron.table.nextFire': 'Next Fire',
  'cron.table.actions': 'Actions',
  'cron.status.active': 'Active',
  'cron.status.paused': 'Paused',
  'cron.status.completed': 'Completed',
  'cron.notAvailable': '—',
  'cron.systemDefault': 'System default',
  'cron.actions.enable': 'Enable',
  'cron.actions.disable': 'Disable',
  'cron.actions.enableJob': 'Enable job {id}',
  'cron.actions.disableJob': 'Disable job {id}',
  'cron.actions.editJob': 'Edit job {id}',
  'cron.actions.deleteJob': 'Delete job {id}',
  'cron.modal.createTitle': 'Create Cron Job',
  'cron.modal.editTitle': 'Edit Cron Job',
  'cron.form.agent': 'Agent',
  'cron.form.agentPlaceholder': 'Select an agent',
  'cron.form.prompt': 'Prompt',
  'cron.form.promptPlaceholder': 'Describe the run to schedule…',
  'cron.form.scheduleType': 'Schedule type',
  'cron.form.scheduleType.cron': 'Cron',
  'cron.form.scheduleType.once': 'Once',
  'cron.form.cronExpression': 'Cron expression',
  'cron.form.cronExpressionPlaceholder': '0 9 * * 1-5',
  'cron.form.runAt': 'Run at',
  'cron.form.timezone': 'Timezone',
  'cron.form.timezonePlaceholder': 'System default',
  'cron.form.sessionId': 'Session ID',
  'cron.form.sessionIdPlaceholder': 'Optional',
  'cron.deleteConfirm': 'Delete cron job for agent {agentId}?',
  'cron.errors.loadJobs': 'Cron jobs could not be loaded.',
  'cron.errors.loadAgents': 'Agents could not be loaded for cron jobs.',
  'cron.errors.save': 'Cron job could not be saved.',
  'cron.errors.delete': 'Cron job could not be deleted.',
  'cron.errors.toggle': 'Cron job status could not be updated.',
  'cron.errors.missingRequired':
    'Agent, prompt, and schedule details are required.',
  'cron.messages.created': 'Cron job created.',
  'cron.messages.updated': 'Cron job updated.',
  'cron.messages.deleted': 'Cron job deleted.',
  'cron.messages.enabled': 'Cron job enabled.',
  'cron.messages.disabled': 'Cron job disabled.',

  'systemPrompt.title': 'System Prompt',
  'systemPrompt.subtitle':
    'Review how prompt pieces will be managed in a later phase.',
  'systemPrompt.comingSoon': 'Editable prompt pieces are coming soon.',
  'systemPrompt.description':
    'The current minimal WebUI uses the server-managed system prompt.',
  'systemPrompt.pieces': 'Prompt pieces',
  'systemPrompt.prototypePlaceholder': 'Placeholder — coming in a later phase.',

  'settings.title': 'Settings',
  'settings.subtitle': 'Runtime and WebUI settings will live here.',
  'settings.comingSoon': 'Settings controls are coming soon.',
  'settings.loading': 'Loading settings…',
  'settings.loadError': 'Settings could not be loaded.',
  'settings.saveError': 'Settings could not be saved.',
  'settings.serverStatus': 'Server status',
  'settings.modelDefaults': 'Model defaults',
  'settings.preferences': 'Preferences',
  'settings.sections': 'Settings sections',
  'settings.placeholder': 'Placeholder',
  'settings.general.title': 'General',
  'settings.general.subtitle': 'Bind address and application data directory.',
  'settings.general.serverHost': 'Server host',
  'settings.general.serverHostDescription':
    'Address and port the vBot server listens on.',
  'settings.general.serverHostPlaceholder':
    'Server host placeholder, not a detected runtime value',
  'settings.general.dataDirectory': 'Data directory',
  'settings.general.dataDirectoryDescription':
    'Root path for agents, sessions, and workspace files.',
  'settings.general.dataDirectoryPlaceholder':
    'Data directory placeholder, not a detected runtime value',
  'settings.skills.title': 'Skills',
  'settings.skills.subtitle':
    'Additional directories scanned for local skills.',
  'settings.skills.defaultDirectory': 'Default skill directory',
  'settings.skills.defaultDirectoryDescription':
    'Always scanned from the vBot data directory and kept read-only here.',
  'settings.skills.extraDirectories': 'Additional skill directories',
  'settings.skills.extraDirectoriesDescription':
    'Absolute or home-relative paths from settings.json skill_directories.',
  'settings.skills.pathPlaceholder': 'C:/path/to/skills',
  'settings.skills.addDirectory': 'Add directory',
  'settings.skills.removeDirectory': 'Remove skill directory {path}',
  'settings.skills.emptyDirectories':
    'No additional skill directories configured.',
  'settings.skills.saveSuccess': 'Skill directories updated.',
  'settings.subagents.title': 'Sub-Agents',
  'settings.subagents.subtitle':
    'Depth, fan-out, and timeout limits for spawned agent sessions.',
  'settings.subagents.maxDepth': 'Max sub-agent depth',
  'settings.subagents.maxDepthDescription':
    'Maximum nesting level allowed when sub-agents spawn their own sub-agents.',
  'settings.subagents.maxPerTurn': 'Max sub-agents per turn',
  'settings.subagents.maxPerTurnDescription':
    'Maximum number of sub-agent sessions one parent run may spawn.',
  'settings.subagents.timeoutMinutes': 'Timeout minutes',
  'settings.subagents.timeoutMinutesDescription':
    'Maximum wait time for blocking sub-agent calls before they fail.',
  'settings.subagents.saveSuccess': 'Sub-agent settings updated.',
  'settings.providers.title': 'Providers',
  'settings.providers.subtitle':
    'Credential status and endpoint metadata for available providers.',
  'settings.providers.empty': 'No providers are available.',
  'settings.providers.description.credentialKey':
    'Credential key: {credentialKey}.',
  'settings.providers.description.baseUrl': 'Endpoint: {baseUrl}.',
  'settings.providers.description.modelCount': '{count} models available.',
  'settings.providers.description.none':
    'Provider metadata is not available yet.',
  'settings.providers.status.configured': 'Configured',
  'settings.providers.status.missingCredentials': 'Missing credentials',
  'settings.providers.status.placeholder': 'Placeholder',
  'settings.providers.customEndpoint': 'Custom endpoint',
  'settings.providers.customEndpointDescription':
    'OpenAI-compatible custom endpoints remain placeholder-only in this phase.',
  'settings.providers.customEndpointStatus': 'Placeholder',
  'settings.providers.configure': 'Configure…',
  'settings.providers.refreshModels': 'Update Model DB',
  'settings.providers.refreshingModels': 'Updating…',
  'settings.providers.refreshSuccess':
    'Model DB updated: {providerCount} providers, {count} models available.',
  'settings.providers.refreshError': 'Model DB could not be updated.',
  'settings.providers.connect': 'Connect',
  'settings.providers.disconnect': 'Disconnect',
  'settings.providers.connected': 'Connected',
  'settings.providers.connectError':
    'Provider connection could not be started.',
  'settings.providers.disconnectError':
    'Provider connection could not be disconnected.',
  'settings.providers.apiKeyDescription':
    'Static credential configured from environment or data directory.',
  'settings.providers.oauthDescription':
    'OAuth device authorization managed by the provider.',
  'settings.providers.device_flow.eyebrow': 'OAuth',
  'settings.providers.device_flow.title': 'Connect GitHub Copilot',
  'settings.providers.device_flow.instructions':
    'Enter this code at the link below:',
  'settings.providers.device_flow.copy_aria': 'Copy device code {code}',
  'settings.providers.device_flow.copied': 'Copied',
  'settings.providers.device_flow.copy_success': 'Device code copied.',
  'settings.providers.device_flow.copy_error':
    'Device code could not be copied.',
  'settings.providers.device_flow.waiting':
    'Waiting for authorization in GitHub…',
  'settings.providers.device_flow.cancel': 'Cancel',
  'settings.providers.device_flow.success_toast':
    'GitHub Copilot connected successfully',
  'settings.providers.device_flow.error_toast':
    'Authorization failed or timed out',
  'settings.appearance.title': 'Appearance',
  'settings.appearance.subtitle': 'Language preference.',
  'settings.appearance.language': 'Language',
  'settings.appearance.languageDescription': 'Interface language.',
  'settings.appearance.saveSuccess': 'Language preference updated.',
  'settings.language.en': 'English',

  'logs.title': 'Logs',
  'logs.eyebrow': 'Daily log viewer',
  'logs.subtitle':
    'Read one daily log file at a time with local filtering and live append updates.',
  'logs.file': 'File',
  'logs.emptyOption': 'No log files',
  'logs.levelFilter': 'Level',
  'logs.sort': 'Order',
  'logs.sort.newest': 'Newest first',
  'logs.sort.oldest': 'Oldest first',
  'logs.level.all': 'All levels',
  'logs.level.info': 'INFO',
  'logs.level.warn': 'WARN',
  'logs.level.warning': 'WARNING',
  'logs.level.error': 'ERROR',
  'logs.level.unknown': 'UNKNOWN',
  'logs.search': 'Search',
  'logs.searchPlaceholder': 'Search timestamp, level, logger, or message…',
  'logs.resultsCount': '{count} visible entries',
  'logs.currentFile': 'Current file: {file}',
  'logs.entries': 'Log entries',
  'logs.loadingCatalog': 'Loading log files…',
  'logs.loadingFile': 'Loading log file…',
  'logs.emptyTitle': 'No log files yet',
  'logs.emptySubtitle':
    'Application logs will appear here after the server writes daily files.',
  'logs.fileEmptyTitle': 'This log file is empty',
  'logs.fileEmptySubtitle':
    'Live updates will appear here when the file grows.',
  'logs.noMatchesTitle': 'No entries match the current filters',
  'logs.noMatchesSubtitle': 'Try another level or broaden the search text.',
  'logs.catalogLoadError': 'Log files could not be loaded.',
  'logs.readError': 'Log file could not be loaded.',
  'logs.streamError': 'Live log updates failed.',
  'logs.stream.connecting': 'Connecting…',
  'logs.stream.connected': 'Live',
  'logs.stream.reconnecting': 'Reconnecting…',
  'logs.stream.error': 'Live update error',
  'logs.stream.idle': 'Idle',

  'app.serverStatus': 'Local UI placeholder',
  'app.statusPlaceholder': 'Local UI placeholder',

  'status.connected': 'Connected',
  'status.activeRun': 'active run',
  'status.medium': 'medium',
  'status.notReachable': 'Not reachable',
  'status.reconnecting': 'Reconnecting…',
  'status.inactive': 'Inactive',
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
