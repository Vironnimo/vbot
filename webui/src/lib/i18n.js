const DEFAULT_LOCALE = 'en';

export const englishCatalog = Object.freeze({
  'app.title': 'vBot',

  'navigation.primary': 'Primary navigation',
  'navigation.sections': 'Sections',
  'navigation.chat': 'Chat',
  'navigation.agents': 'Agents',
  'navigation.projects': 'Projects',
  'navigation.cron': 'Schedules',
  'navigation.systemPrompt': 'System Prompt',
  'navigation.settings': 'Settings',
  'navigation.logs': 'Logs',
  'navigation.statistics': 'Statistics',
  'navigation.debug': 'Debug',
  'nav.section.work': 'Work',
  'nav.section.configure': 'Configure',
  'nav.section.insights': 'Insights',

  'common.alreadySaved': 'Already saved',
  'common.add': 'Add',
  'common.back': 'Back',
  'common.cancel': 'Cancel',
  'common.clear': 'Clear',
  'common.close': 'Close',
  'common.confirm': 'Confirm',
  'common.copy': 'Copy',
  'common.copied': 'Copied',
  'common.create': 'Create',
  'common.delete': 'Delete',
  'common.edit': 'Edit',
  'common.loading': 'Loading…',
  'common.moreInfo': 'More information',
  'common.refresh': 'Refresh',
  'common.remove': 'Remove',
  'common.reset': 'Reset',
  'common.retry': 'Retry',
  'common.discard': 'Discard',
  'common.save': 'Save',
  'common.saved': 'Saved',
  'common.saving': 'Saving…',
  'common.unknown': 'Unknown',

  'autosave.transitionFailureTitle': 'Changes could not be saved',
  'autosave.transitionFailureBody':
    'Your changes are still open. Try saving again, or discard them and continue.',
  'autosave.discardAndContinue': 'Discard and continue',

  'inherit.option': 'Inherited: {value} (global default)',
  'inherit.optionNotConfigured': 'Inherit (not configured)',
  'inherit.optionProviderDefault': 'Inherit (provider default)',
  'inherit.hint': 'Inherited: {value} (global default)',
  'inherit.hintProviderDefault':
    'Provider default — nothing is set here or in the global defaults.',
  'inherit.resetToInherit': 'Reset to inherited value',
  'inherit.editGlobalDefaults': 'Edit global defaults',

  'loading.agents': 'Loading agents…',
  'loading.history': 'Loading chat history…',

  'errors.generic': 'Something went wrong. Try again.',
  'errors.validation': 'Check the highlighted fields and try again.',
  'errors.streamClosed': 'The live stream closed before the run finished.',
  'errors.minimumAgents': 'At least one agent must remain.',

  'chat.title': 'Chat',
  'chat.selectAgent': 'Select agent',
  'chat.noAgentSelected': 'Choose an agent to start chatting.',
  'chat.noAgents': 'No agents are available yet.',
  'chat.newSession': 'New session',
  'chat.newSessionBlocked':
    'A new session can be started after the current run finishes.',
  'chat.historyEmpty': 'No messages yet. Send the first message to this agent.',
  'chat.composerLabel': 'Message',
  'chat.composerPlaceholder':
    'Ask this agent to do something… (/ for commands, $ for skills, @ for files)',
  'chat.sendMessage': 'Send message',
  'chat.queueMessage': 'Queue message',
  'chat.copyAnswer': 'Copy answer',
  'chat.answerCopied': 'Answer copied',
  'chat.copyUserMessage': 'Copy message',
  'chat.userMessageCopied': 'Message copied',
  'chat.copyReasoning': 'Copy thinking',
  'chat.reasoningCopied': 'Thinking copied',
  'chat.copyToolField': 'Copy {label}',
  'chat.copyCode': 'Copy code',
  'chat.codeCopied': 'Code copied',
  'chat.codeLanguagePlain': 'text',
  'chat.copyCommandOutput': 'Copy command output',
  'chat.commandOutputCopied': 'Command output copied',
  'chat.attachment.addFile': 'Add file',
  'chat.attachment.uploading': 'Uploading…',
  'chat.attachment.uploadFailed': 'Attachment upload failed.',
  'chat.attachment.remove': 'Remove attachment',
  'chat.attachment.preview': 'Preview attachment',
  'chat.attachment.fileLabel': 'Attached file',
  'chat.image.alt': 'Image',
  'chat.image.zoomIn': 'Click to view full size',
  'chat.image.zoomOut': 'Click to fit',
  'chat.voice.startRecording': 'Start voice input',
  'chat.voice.stopRecording': 'Stop recording',
  'chat.voice.startFailed': 'Microphone recording could not start.',
  'chat.voice.transcriptionFailed': 'Speech transcription failed.',
  'chat.cancelRun': 'Cancel run',
  'chat.cancelToolCall': 'Cancel',
  'chat.cancelToolCallAria': 'Cancel running tool call',
  'chat.cancelSubAgent': 'Cancel',
  'chat.cancelSubAgentAria': 'Cancel running sub-agent',
  'chat.historyLoadError': 'Chat history could not be loaded.',
  'chat.sendError': 'Message could not be sent.',
  'chat.skillsLoadError': 'Skill suggestions could not be loaded.',
  'chat.cancelError': 'Run could not be cancelled.',
  'chat.sessionCreateError': 'New session could not be created.',
  'chat.noProvider.title': 'Connect a provider to start',
  'chat.noProvider.hint':
    'No provider is connected yet. Connect one before choosing a model.',
  'chat.noProvider.action': 'Connect a provider',
  'chat.noModel.title': 'Pick a model to start',
  'chat.noModel.hint':
    'This agent has no model yet. Choose one to send messages.',
  'chat.noModel.action': 'Choose a model',
  'chat.role.user': 'You',
  'chat.role.assistant': 'Assistant',
  'chat.role.system': 'System',
  'chat.role.userAvatar': 'Y',
  'chat.role.assistantAvatar': 'A',
  'chat.role.systemAvatar': 'S',
  'chat.event.thinking': 'Thinking',
  'chat.event.toolStarted': 'Tool started',
  'chat.event.toolResult': 'Tool result',
  'chat.modelFallbackActivated': 'Switched to {model}',
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
  'chat.agentActivity.idle': '{name}: Idle',
  'chat.agentActivity.running': '{name}: Running',
  'chat.agentActivity.unread': '{name}: Unread result',
  'chat.today': 'Today',
  'chat.historyEmptyTitle': 'No messages yet',
  'chat.toolArgs': 'Args',
  'chat.toolPendingName': 'tool',
  'chat.toolCancelled': 'cancelled',
  'chat.toolResultLabel': 'Result',
  'chat.toolNoData': '—',
  'chat.runIterations': '{count} iter',
  'chat.runDurationSeconds': '{seconds}s',
  'chat.toolDurationSeconds': '{seconds}s',
  'chat.tokenBadge': '{tokens} / {context} tok',
  'chat.tokenBadgeEstimated': '~{tokens} / {context} tok',
  'chat.tokenBadgeNoContext': '{tokens} tok',
  'chat.tokenBadgeEstimatedNoContext': '~{tokens} tok',
  'chat.tokenBadgeNoUsage': '— / {context} tok',
  'chat.tokenTooltipLastTurn': 'Last turn',
  'chat.tokenTooltipInput': 'Input: {tokens} tok',
  'chat.tokenTooltipCacheRead': '  · read from cache: {tokens}',
  'chat.tokenTooltipCacheReadPct': '  · read from cache: {tokens} ({percent}%)',
  'chat.tokenTooltipCacheWrite': '  · newly written to cache: {tokens}',
  'chat.tokenTooltipUncached': '  · uncached: {tokens}',
  'chat.tokenTooltipOutput': 'Output: {tokens} tok',
  'chat.tokenTooltipEstimated': 'Estimated (provider sent no usage data)',
  'chat.tokenTooltipSession': 'Session ({turns} measured turns)',
  'chat.tokenTooltipSessionAvgCacheRead':
    'Avg cache read per turn: {tokens} tok',
  'chat.tokenTooltipSessionEstimatedTurns': '{count} estimated turns excluded',
  'chat.subagent.label': 'Sub-agent',
  'chat.subagent.starting': 'starting',
  'chat.subagent.loadingResult': 'loading result…',
  'chat.subagent.viewSession': 'view session',
  'chat.subagentSessionNotice': 'Viewing a sub-agent session',
  'chat.subagentSessionParentHint':
    'Messages here continue this sub-agent session. Return to the parent session when you are done.',
  'chat.returnToParentSession': 'Return to parent session',
  'chat.subagentSessionHint':
    'Messages here continue this sub-agent session. Return to the current agent session when you are done.',
  'chat.returnToCurrentSession': 'Return to current session',
  'chat.runError': 'Run failed.',
  'chat.errorDetails': 'Details',
  'chat.compacted': 'Context compacted',
  'chat.takenOver': 'Taken over by {from} → {to}',
  'chat.takenOverGeneric': 'Session taken over',
  'chat.transientCard.label': 'Command output',
  'chat.project.none': 'No project selected',
  'chat.personalBarLabel': 'Personal',
  'chat.personalBarHint':
    'Your personal agents — available with or without a project.',
  'chat.project.selectAria': 'Select project',
  'chat.project.teamBarHint': 'Agents discovered in this project’s repository.',
  'chat.project.teamLabel': 'Project team',
  'chat.project.teamEmpty': 'This project has no agents yet.',
  'chat.project.loadError': 'The project team could not be loaded.',
  'chat.project.sessionError': 'The project agent session could not be opened.',
  'chat.project.scanBanner':
    'This project’s scan found issues. Some agents may not work as expected.',
  'chat.project.scanBannerCount':
    'This project’s scan found {count} issues. Some agents may not work as expected.',
  'chat.project.scanBannerLink': 'Review in Projects',

  'sessions.title': 'Sessions',
  'sessions.hide': 'Hide sessions',
  'sessions.loading': 'Loading sessions…',
  'chat.sessions.emptyTitle': 'No sessions yet',
  'sessions.no_sessions': 'No sessions found for this agent.',
  'sessions.unreadCompletion': 'Unread',
  'sessions.unreadCompletionHint': 'This Session has an unread result.',
  'sessions.fork': 'Fork',
  'sessions.forkHint':
    'A copy of another session. Background reflection and /reflect review a conversation in a fork so the original session stays untouched.',
  'sessions.subagentHint':
    'A session run by a sub-agent working on behalf of a parent session. The parent is shown below.',
  'sessions.last_active': 'Last active',
  'sessions.link_channel_id': 'Channel ID',
  'sessions.platform_telegram': 'Telegram',
  'sessions.source_channel': 'Source channel',
  'sessions.subagent_parent': 'Parent',
  'sessions.actions': 'Session actions',
  'sessions.delete_confirm_title': 'Delete session',
  'sessions.rename': 'Rename',
  'sessions.rename_label': 'Rename session',
  'sessions.rename_placeholder': 'Session name',
  'sessions.rename_error': 'The session could not be renamed.',
  'sessions.compactionPolicy': 'Compaction Policy',
  'sessions.compactionOverride': 'Session override',
  'sessions.compactionOverrideDescription':
    'When disabled, this Session follows later Agent or global Policy changes.',
  'sessions.compactionSaveError': 'The Compaction Policy could not be saved.',

  'skillAutocomplete.label': 'Skill suggestions',
  'skillAutocomplete.eyebrow.commandsAndSkills': 'commands & skills',
  'skillAutocomplete.eyebrow.skills': 'skills',
  'skillAutocomplete.noDescription': 'No description available',

  'fileAutocomplete.label': 'File suggestions',
  'fileAutocomplete.eyebrow': 'files',
  'fileAutocomplete.truncated': 'list truncated — keep typing',

  'chat.fileMention.label': 'Mentioned file',
  'chat.fileMention.tooLarge': 'too large to attach — referenced by path',
  'chat.fileMention.notText': 'not a text file — referenced by path',
  'chat.fileMention.missing': 'file was not found at send time',

  'queue.title': 'Queued messages',
  'queue.pending': 'Waiting for the active run to finish.',
  'queue.removeMessage': 'Remove queued message',
  'queue.count': '{count} queued',
  'queue.editMessage': 'Edit queued message',
  'queue.saveEdit': 'Save',
  'queue.cancelEdit': 'Cancel',
  'queue.editError': 'Queued message could not be edited.',
  'queue.removeError': 'Queued message could not be removed.',
  'queue.syncError': 'Queued messages could not be synced.',
  'queue.restartDiscardedOne':
    '1 queued message was discarded because the server restarted.',
  'queue.restartDiscardedMany':
    '{count} queued messages were discarded because the server restarted.',

  'cancel.cancelling': 'Cancelling run…',

  'agents.title': 'Agents',
  'agents.loading': 'Loading agents…',
  'agents.empty': 'No agents found.',
  'agents.create': 'Create agent',
  'agents.delete': 'Delete agent',
  'agents.deleteDisabledMinimum': 'The last remaining agent cannot be deleted.',
  'agents.created': 'Agent created.',
  'agents.updated': 'Agent updated.',
  'agents.deleted': 'Agent deleted.',
  'agents.loadError': 'Agents could not be loaded.',
  'agents.saveError': 'Agent could not be saved.',
  'agents.deleteError': 'Agent could not be deleted.',
  'agents.order.handle': 'Reorder {name} (use arrow keys)',
  'agents.order.announcement': 'Moved {name} to position {position} of {total}',
  'agents.order.saveError': 'Agent order could not be saved.',
  'agents.form.id': 'Agent ID',
  'agents.form.name': 'Name',
  'agents.form.model': 'Model',
  'agents.form.fallbackModel': 'Fallback model',
  'agents.form.workspace': 'Workspace',
  'agents.form.temperature': 'Temperature',
  'agents.form.thinkingEffort': 'Thinking effort',
  'agents.form.allowedTools': 'Allowed tools',
  'agents.form.allowedSkills': 'Allowed skills',
  'agents.form.customSystemPrompt': 'Custom system prompt',
  'agents.form.customPromptHelp':
    'Gives this agent its own editable copy of the system prompt. Edit it in the System Prompt tab by selecting this agent as the scope. Turning this off keeps the customized blocks but stops using them.',
  'agents.form.memoryPromptMode': 'Memory',
  'agents.form.memoryPromptModeHelp':
    'Which memory notes are shown to the model: the agent’s own notes (MEMORY.md), or additionally what it knows about you (USER.md).',
  'agents.form.memoryModeHelp':
    'Which memory files are pinned into the System Prompt. The memory tool follows this setting — it is available to the agent unless this is off.',
  'agents.form.fallbackModelHelp':
    'Used automatically when the primary model fails or is unavailable.',
  'agents.form.temperatureHelp':
    'Sampling randomness, typically 0–2. Leave empty to use the default.',
  'agents.form.thinkingEffortHelp':
    'How much internal reasoning the model may spend before answering. Leave at — for the default.',
  'agents.form.wildcardNote':
    'Currently all are allowed, including ones added in the future. Turning any single item off switches to a fixed list.',
  'agents.form.idHelp': 'Agent IDs are immutable after creation.',
  'agents.form.modelPlaceholder': 'Default (no model selected)',
  'agents.form.fallbackModelPlaceholder': 'None',
  'agents.form.modelUnavailableOption': 'Unavailable / custom: {model}',
  'agents.form.modelUnavailableConnectionOption':
    'Unavailable / custom: {model} ({connection})',
  'models.filter.noTools': 'no tool calling',
  'models.filter.unreachable': 'service not running',
  'models.filter.belowMinContext': 'below 32k context',
  'models.filter.contextUnknown': 'context unknown',
  'models.filter.showAll': 'Show all models ({count} hidden)',
  'models.filter.showSuitable': 'Show only suitable models',
  'agents.form.editAgentPrompt': "Edit this agent's prompt",
  'agents.form.thinkingEffortOption.none': 'none',
  'agents.form.thinkingEffortOption.minimal': 'minimal',
  'agents.form.thinkingEffortOption.low': 'low',
  'agents.form.thinkingEffortOption.medium': 'medium',
  'agents.form.thinkingEffortOption.high': 'high',
  'agents.form.thinkingEffortOption.xhigh': 'xhigh',
  'agents.form.thinkingEffortOption.max': 'max',
  'agents.form.thinkingEffortUnsupported':
    'This model does not support reasoning.',
  'agents.form.memoryPromptModeOption.off': 'Off',
  'agents.form.memoryPromptModeOption.agent': 'Agent notes (MEMORY.md)',
  'agents.form.memoryPromptModeOption.agent_user':
    'Agent + user notes (MEMORY.md + USER.md)',
  'agents.form.workspaceAssignedByServer':
    'Workspace is assigned by the server when the agent is created.',
  'agents.form.workspaceEditableHelp':
    "Home of this agent's identity and memory files (SOUL.md, USER.md, MEMORY.md); the memory tool works here. File tools follow the session's working directory instead — the project repository in project sessions.",
  'agents.form.workspaceSetToDefault': 'Set to default',
  'agents.form.project': 'Project',
  'agents.form.noProject': 'No project',
  'agents.form.projectHelp':
    'Where relative file and shell work runs. Workspace remains the identity and memory home.',
  'agents.form.projectLoadError': 'Projects could not be loaded.',
  'agents.form.projectUnavailableHelp':
    'The saved selection is preserved. Project editing is unavailable until the catalog reloads.',
  'agents.form.unavailableProject': 'Unavailable project',
  'agents.workspaceMove.title': 'Change Workspace?',
  'agents.workspaceMove.body':
    'Choose whether to copy SOUL.md, USER.md, and MEMORY.md into the new Workspace. Source files remain in place; existing destination versions are backed up before replacement.',
  'agents.workspaceMove.copy': 'Copy files',
  'agents.workspaceMove.dontCopy': "Don't copy",
  'agents.form.submitCreate': 'Create agent',
  'agents.form.submitUpdate': 'Save changes',
  'agents.form.required': 'This field is required.',
  'agents.detail.newSubtitle': 'id assigned at creation',
  'agents.detail.idValue': 'id: {id}',
  'agents.detail.identity': 'Identity',
  'agents.detail.model': 'Model',
  'agents.detail.systemPrompt': 'System Prompt',
  'agents.detail.memory': 'Memory',
  'agents.detail.access': 'Access',
  'agents.detail.metadata': 'Metadata',
  'agents.detail.sessionId': 'Current session ID',
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

  // Shared toggle-chip allow-list (tools/skills) — Agent editor + Projects.
  'access.searchPlaceholder': 'Filter…',
  'access.count': '{on} / {total} on',
  'access.allOn': 'all on',
  'access.allOff': 'all off',
  'access.noMatches': 'No matches.',
  'access.toggle': 'Toggle {name}',
  'access.lockedAuto': 'auto',

  'agents.tools.memoryFollowsActive':
    'Follows the Memory setting — currently available.',
  'agents.tools.memoryFollowsOff':
    'Follows the Memory setting — currently unavailable (Memory is off).',
  'agents.tools.notReadyBadge': 'Currently unavailable',
  'agents.tools.openExtensions': 'Open Extensions',
  'agents.confirmDisableCustomPrompt.title': 'Disable custom system prompt?',
  'agents.confirmDisableCustomPrompt.body':
    'This agent has customized prompt blocks. They will be kept, but the agent stops using them and follows the Default scope again. Re-enabling brings them back.',
  'agents.confirmDisableCustomPrompt.confirm': 'Disable custom prompt',

  'cron.eyebrow': 'Scheduled automation',
  'cron.title': 'Scheduled Runs',
  'cron.subtitle':
    'Manage recurring and one-time Agent Runs, including completed and missed history.',
  'cron.noAgents': 'Create an agent before adding cron jobs.',
  'cron.loading': 'Loading cron jobs…',
  'cron.emptyTitle': 'No scheduled runs yet',
  'cron.emptyListSubtitle': 'Use Add to create a recurring or one-time Run.',
  'cron.emptySubtitle':
    'Create a recurring or one-time Run. Every fire gets a fresh Session unless you choose an existing one.',
  'cron.list.ariaLabel': 'Scheduled Runs',
  'cron.status.active': 'Active',
  'cron.status.paused': 'Paused',
  'cron.status.completed': 'Completed',
  'cron.status.failed': 'Failed',
  'cron.status.missed': 'Missed',
  'cron.notAvailable': '—',
  'cron.systemDefault': 'System default',
  'cron.actions.enableJob': 'Enable job {id}',
  'cron.actions.disableJob': 'Disable job {id}',
  'cron.actions.deleteJob': 'Delete job {id}',
  'cron.actions.enabled': 'Enabled',
  'cron.detail.createTitle': 'Create Scheduled Run',
  'cron.detail.editTitle': 'Edit Scheduled Run',
  'cron.detail.status': 'Status',
  'cron.detail.lastFired': 'Last fired',
  'cron.detail.lastAttempt': 'Last attempt',
  'cron.detail.lastCompleted': 'Last completed',
  'cron.detail.nextFire': 'Next fire',
  'cron.detail.lastOutcome': 'Last outcome',
  'cron.detail.lastRun': 'Last Run',
  'cron.detail.consecutiveFailures': 'consecutive failures',
  'cron.outcome.success': 'Succeeded',
  'cron.outcome.failed': 'Failed',
  'cron.outcome.cancelled': 'Cancelled',
  'cron.outcome.missed': 'Missed',
  'cron.outcome.unknown': 'Outcome unknown after restart',
  'cron.form.name': 'Name',
  'cron.form.namePlaceholder': 'Morning news digest',
  'cron.form.agent': 'Agent',
  'cron.form.agentPlaceholder': 'Select an agent',
  'cron.form.agentGroup.identity': 'Identity agents',
  'cron.form.agentGroup.project': 'Project agents',
  'cron.form.prompt': 'Prompt',
  'cron.form.promptPlaceholder': 'Describe the run to schedule…',
  'cron.form.scheduleType': 'Schedule type',
  'cron.form.scheduleType.cron': 'Cron',
  'cron.form.scheduleType.once': 'Once',
  'cron.form.preset': 'Schedule preset',
  'cron.presets.custom': 'Custom',
  'cron.presets.every15Minutes': 'Every 15 minutes',
  'cron.presets.hourly': 'Every hour',
  'cron.presets.dailyMorning': 'Every day at 9:00',
  'cron.presets.weekdayMornings': 'Weekdays at 9:00',
  'cron.presets.mondayMornings': 'Mondays at 9:00',
  'cron.presets.monthlyFirst': 'Monthly on the 1st at 9:00',
  'cron.form.cronExpression': 'Cron expression',
  'cron.form.cronExpressionPlaceholder': '0 9 * * 1-5',
  'cron.form.runAt': 'Run at',
  'cron.form.sessionId': 'Session ID',
  'cron.form.sessionIdPlaceholder': 'Optional',
  'cron.form.sessionIdHelp':
    'Optional: run inside one fixed, existing Session owned by the target. Leave empty to create a fresh Session for every fire.',
  'cron.form.cronExpressionHelp':
    'Exactly five space-separated fields: minute, hour, day of month, month, weekday. The minimum cadence is one minute; seconds are not supported.\n\nExample: 0 9 * * 1-5 runs at 09:00 on weekdays. * matches any value; ranges (1-5) and lists (1,3,5) work in every field.',
  'cron.deleteConfirmTitle': 'Delete Scheduled Run',
  'cron.deleteConfirm': 'Delete this job permanently? It will no longer run.',
  'cron.discardConfirmTitle': 'Discard unsaved changes?',
  'cron.discardConfirm':
    'Your edits have not been saved. Discard them and continue?',
  'cron.errors.loadJobs': 'Cron jobs could not be loaded.',
  'cron.errors.loadAgents': 'Agents could not be loaded for cron jobs.',
  'cron.errors.save': 'Cron job could not be saved.',
  'cron.errors.delete': 'Cron job could not be deleted.',
  'cron.errors.toggle': 'Cron job status could not be updated.',
  'cron.errors.missingRequired':
    'Name, agent, prompt, and schedule details are required.',
  'cron.messages.created': 'Cron job created.',
  'cron.messages.updated': 'Cron job updated.',
  'cron.messages.deleted': 'Cron job deleted.',
  'cron.messages.enabled': 'Cron job enabled.',
  'cron.messages.disabled': 'Cron job disabled.',

  'projects.eyebrow': 'Project workspaces',
  'projects.title': 'Projects',
  'projects.subtitle':
    'Add a repository as a project to discover its team and chat with project agents. Adding a project also scans its repo for issues.',
  'projects.loading': 'Loading projects…',
  'projects.loadError': 'Projects could not be loaded.',
  'projects.emptyTitle': 'No projects yet',
  'projects.emptySubtitle':
    'Add a repository path below to create your first project.',
  'projects.add.title': 'Add project',
  'projects.add.subtitle':
    'Enter the path to a repository on this machine. The folder must already exist; vBot reads it but never writes to it.',
  'projects.add.cwd': 'Repository path',
  'projects.add.cwdPlaceholder': 'C:/path/to/repository',
  'projects.add.cwdHelp':
    'The folder must exist. The project is created immediately and then scanned — you can remove it again afterwards.',
  'projects.add.displayName': 'Display name',
  'projects.add.displayNamePlaceholder':
    'Optional — defaults to the folder name',
  'projects.add.submit': 'Add project',
  'projects.add.submitting': 'Adding project…',
  'projects.add.missingCwd': 'Enter a repository path to add a project.',
  'projects.add.error': 'Project could not be added.',
  'projects.add.success': 'Project added.',
  'projects.add.format': 'Source format',
  'projects.add.formatHelp':
    'This repository carries both ecosystems. Pick which one this project uses — its agents and skills come only from that one. You can switch later in the project settings.',
  'projects.add.formatCounts': '{agents} agents · {skills} skills',
  'projects.add.formatDetected': 'Detected: {format}',
  'projects.add.claudeMdSuggestion':
    'Load {path} as a project file. The repository has no AGENTS.md; the file is loaded as-is into project agent prompts.',
  'projects.add.claudeMdSuggestionLabel': 'Load CLAUDE.md as a project file',
  'projects.format.opencode': 'OpenCode',
  'projects.format.claude': 'Claude Code',
  'projects.list.title': 'Your projects',
  'projects.manage.displayName': 'Display name',
  'projects.manage.sourceFormat': 'Source format',
  'projects.manage.sourceFormatHelp':
    'Where this project’s agents and skills come from. Switching re-derives the team and skills from the other ecosystem’s directories; sessions are kept.',
  'projects.manage.defaultAgent': 'Default agent',
  'projects.manage.defaultAgentHelp':
    'The team agent preselected when you open this project in Chat.',
  'projects.manage.defaultModelHelp':
    'Used by team agents that do not declare their own model. Resolution order: per-agent override → the agent’s own value → this project default → the global default.',
  'projects.manage.defaultTemperatureHelp':
    'Used by team agents that do not set their own temperature. Same resolution order as the default model.',
  'projects.manage.defaultThinkingEffortHelp':
    'Used by team agents that do not set their own thinking effort. Same resolution order as the default model.',
  'projects.manage.defaultAgentEmpty': 'No project default',
  'projects.manage.defaultAgentUnavailable': '{agentId} (not in team)',
  'projects.manage.defaultModel': 'Default model',
  'projects.manage.defaultModelEmpty': 'No project default',
  'projects.manage.defaultTemperature': 'Default temperature',
  'projects.manage.defaultThinkingEffort': 'Default thinking effort',
  'projects.manage.noThinkingEffort': 'No project default',
  'projects.manage.providerThinkingEffortDefault': '— (provider default)',
  'projects.manage.modelSearchPlaceholder': 'Filter models…',
  'projects.manage.modelSearchEmpty': 'No models match',
  'projects.manage.autoLoad': 'Auto-load files',
  'projects.manage.autoLoadPlaceholder': 'Add a file path…',
  'projects.manage.autoLoadAdd': 'Add',
  'projects.manage.autoLoadRemove': 'Remove {file}',
  'projects.manage.autoLoadEmpty': 'No auto-load files',
  'projects.manage.allowedTools': 'Tool whitelist',
  'projects.manage.allowedToolsHelp':
    'The maximum tools this project’s agents may use. An individual agent may use fewer through its own permissions.',
  'projects.manage.resetDefaults': 'Reset to defaults',
  'projects.manage.toggleTool': 'Toggle tool {name}',
  'projects.manage.toolsEmpty': 'No tools available',
  'projects.manage.unavailableToolHint':
    'This stored Tool Whitelist entry is not currently registered for Projects. Turn it off to remove the permission, or leave it on so the permission returns with the Tool.',
  'projects.manage.allowedSkills': 'Skill whitelist',
  'projects.manage.allowedSkillsHelp':
    'Project skills are active by default; bundled and global skills are opt-in.',
  'projects.manage.projectSkills': 'Project skills',
  'projects.manage.bundledSkills': 'Bundled skills',
  'projects.manage.globalSkills': 'Global skills',
  'projects.manage.toggleSkill': 'Toggle skill {name}',
  'projects.manage.skillsEmpty': 'No skills available',
  'projects.manage.save': 'Save changes',
  'projects.manage.saving': 'Saving…',
  'projects.manage.saveError': 'Project changes could not be saved.',
  'projects.manage.saveSuccess': 'Project updated.',
  'projects.remove': 'Remove',
  'projects.remove.confirmTitle': 'Remove project',
  'projects.remove.rootedAgentsBody':
    'Removing {name} clears it from every affected Rooted Agent and resets those Agents to their Default Workspace. Their Sessions and history stay unchanged. The repository and old Workspace files are never touched.',
  'projects.remove.copyIdentityFiles':
    'Copy SOUL.md, USER.md, and MEMORY.md to affected Default Workspaces',
  'projects.remove.copyIdentityFilesHelp':
    'When enabled, existing destination versions are backed up before replacement. One choice applies to every affected Agent.',
  'projects.remove.successOneAgent':
    'Project removed. 1 Agent was reset; identity files {copyState}.',
  'projects.remove.successManyAgents':
    'Project removed. {count} Agents were reset; identity files {copyState}.',
  'projects.remove.filesCopied': 'were copied',
  'projects.remove.filesNotCopied': 'were not copied',
  'projects.remove.confirm':
    'Remove project {name}? The project is archived and can be restored; the repository on disk is never touched.',
  'projects.remove.error': 'Project could not be removed.',
  'projects.remove.success': 'Project removed.',
  'projects.remove.busy':
    'This project has an active or queued run and cannot be removed right now.',
  'projects.remove.inUse':
    'A cron job points at one of this project’s agents, so it cannot be removed. Remove or retarget the cron job first.',
  'projects.detail.sectionSettings': 'Project settings',
  'projects.detail.sectionAutoLoad': 'Auto-load files',
  'projects.detail.autoLoadInfo':
    'These files are embedded into the system prompt of every session in this project — the agent always sees their full content, with higher weight than normal chat history, and they are never dropped or summarized by context compaction.\n\nPaths are relative to the project folder (absolute paths also work), files load in list order, and missing files are skipped. When an outside Identity Agent explicitly loads the project with the project Tool, the same files are returned as Project Context.',
  'projects.detail.sectionTeam': 'Team',
  'projects.detail.teamInfo':
    'Agents discovered live in the project repository — where they are read from depends on the source format. The list is re-derived on open and re-scan; the repository is the source of truth, so vBot never copies or edits these agents.',
  'projects.detail.sectionTools': 'Tools',
  'projects.detail.sectionSkills': 'Skills',
  'projects.detail.empty': 'Select a project to view and edit it.',
  'projects.team.title': 'Team',
  'projects.repository.rescan': 'Rescan repository',
  'projects.repository.rescanning': 'Scanning…',
  'projects.team.empty':
    'No agents discovered in this repository yet. An empty project is valid — add agent files to the repo to build a team.',
  'projects.team.noModel': 'No model',
  'projects.team.effectiveModel': 'Model',
  'projects.team.effectiveTemperature': 'Temperature',
  'projects.team.effectiveThinkingEffort': 'Thinking effort',
  'projects.team.valueNotConfigured': 'not configured',
  'projects.team.valueProviderDefault': 'provider default',
  'projects.team.fromSource': 'from {source}',
  'projects.team.sourceOverride': 'override',
  'projects.team.sourceAgentFile': 'agent file (repo)',
  'projects.team.sourceProjectDefault': 'project default',
  'projects.team.sourceGlobalDefault': 'global default',
  'projects.team.overrideLabel': 'Override',
  'projects.team.setOverride': 'Set override',
  'projects.team.clearOverride': 'Clear override',
  'projects.team.overrideSaved': 'Override saved.',
  'projects.team.overrideCleared': 'Override cleared.',
  'projects.team.overrideError': 'The override could not be saved.',
  'projects.team.overrideClearError': 'The override could not be cleared.',
  'projects.team.overrideHelp':
    'An override replaces the agent file and all defaults for this agent in this project. The model override can also be set with /model in chat.',
  'projects.team.overrideModelPlaceholder': 'No override',
  'projects.team.overrideTemperaturePlaceholder': 'e.g. 0.7',
  'projects.team.deniedTools': 'Denied by the agent file: {tools}',
  'projects.team.deniedToolsNone':
    'No tool denials — follows the project tool whitelist.',
  'projects.team.toolsFollowWhitelist':
    'All other tools follow the project tool whitelist.',
  'projects.team.sourceFile': 'Source: {path} ({format})',
  'projects.team.toggleExpand': 'Toggle {agent} details',
  'projects.report.title': 'Scan report',
  'projects.report.findingCount': '{count} issues found',
  'projects.report.group.slug_collision': 'Name collisions',
  'projects.report.group.unslugifiable_name': 'Unusable agent names',
  'projects.report.group.bad_model': 'Unconfigured models',
  'projects.report.group.orphan': 'Orphaned pointers',
  'projects.report.group.unavailable_tool': 'Unavailable tools',
  'projects.report.finding.agent': 'Agent {agentId}',
  'projects.report.finding.source': 'Source: {source}',
  'projects.rePoint.title': 'Repository not found',
  'projects.rePoint.description':
    'The repository folder for this project no longer exists. Point it at the new location to restore the project.',
  'projects.rePoint.cwd': 'New repository path',
  'projects.rePoint.cwdPlaceholder': 'C:/path/to/repository',
  'projects.rePoint.submit': 'Re-point',
  'projects.rePoint.submitting': 'Re-pointing…',
  'projects.rePoint.missingCwd': 'Enter the new repository path.',
  'projects.rePoint.error': 'The project could not be re-pointed.',
  'projects.rePoint.success': 'Project re-pointed.',

  'systemPrompt.title': 'System Prompt',
  'systemPrompt.eyebrow': 'Prompt assembly',
  'systemPrompt.subtitle':
    'Inspect, order, and preview the blocks that compose every agent’s system prompt.',
  'systemPrompt.scope.label': 'Prompt scope',
  'systemPrompt.scope.default': 'Default',
  'systemPrompt.fragmentEditor.save': 'Save',
  'systemPrompt.fragmentEditor.reset': 'Reset',
  'systemPrompt.fragmentEditor.dirtyIndicator': 'unsaved',
  'systemPrompt.fragmentEditor.modifiedIndicator': 'modified',
  'systemPrompt.fragmentEditor.modifiedHint':
    'Edited — differs from the built-in default.',
  'systemPrompt.fragmentEditor.resetConfirmTitle': 'Reset block',
  'systemPrompt.fragmentEditor.resetConfirm':
    'Reset this block to its default? This cannot be undone.',
  'systemPrompt.fragmentEditor.resetAgentConfirm':
    'Reset this Agent block to the current Default content? This cannot be undone.',
  'systemPrompt.preview.heading': 'Preview for',
  'systemPrompt.preview.copy': 'Copy',
  'systemPrompt.preview.tokenCount': '~{count} tokens',
  'systemPrompt.preview.tokenBreakdown':
    '~{prompt} prompt + ~{tools} tools = ~{total} tokens',
  'systemPrompt.preview.tokenBreakdownHint':
    'Estimated. Tools = the {count} tool definitions sent to the provider with every request alongside the system prompt.',
  'systemPrompt.preview.agentLabel': 'Agent',
  'systemPrompt.preview.agentGroup.identity': 'Identity agents',
  'systemPrompt.preview.agentGroup.project': 'Project agents',
  'systemPrompt.preview.empty': 'Select an agent to preview its system prompt.',
  'systemPrompt.error.loadFailed': 'Failed to load prompt data',
  'systemPrompt.error.saveFailed': 'Failed to save',
  'systemPrompt.error.resetFailed': 'Failed to reset',
  'systemPrompt.error.previewFailed': 'Failed to load preview',
  'systemPrompt.error.copyFailed': 'Failed to copy',
  'systemPrompt.error.layoutFailed': 'Failed to save layout',
  'systemPrompt.blockList.guide.label': 'How it works',
  'systemPrompt.blockList.guide.title':
    'These blocks become the System Prompt.',
  'systemPrompt.blockList.guide.assemblyLabel': 'Assembly',
  'systemPrompt.blockList.guide.assembly':
    'Blocks are read from top to bottom. Drag to reorder them, use the switches to include or exclude them, and edit their content directly.',
  'systemPrompt.blockList.guide.scopeLabel': 'Scope',
  'systemPrompt.blockList.guide.scope':
    'Default applies to every Agent. Enable “Custom system prompt” in Agents to create an Agent-specific scope here.',
  'systemPrompt.blockList.newBlock': 'New block',
  'systemPrompt.blockList.newBlockPrompt':
    'Name for the new block (letters, digits, “-” or “_”):',
  'systemPrompt.blockList.invalidSlug':
    'Invalid name — use letters, digits, “-” or “_”, starting with a letter or digit.',
  'systemPrompt.blockList.createFailed':
    'Failed to create block. The slug may be invalid or already used.',
  'systemPrompt.blockList.removeConfirmTitle': 'Remove block',
  'systemPrompt.blockList.removeConfirm':
    'Remove this custom block? This cannot be undone.',
  'systemPrompt.blockList.removeFailed': 'Failed to remove block',
  'systemPrompt.blockList.resetLayout': 'Reset order & visibility',
  'systemPrompt.blockList.resetLayoutConfirmTitle': 'Reset layout',
  'systemPrompt.blockList.resetLayoutConfirm':
    'Reset block order and visibility to the default? This cannot be undone.',
  'systemPrompt.blockList.customBadge': 'custom',
  'systemPrompt.blockList.dataBadge': 'auto',
  'systemPrompt.blockList.dataHint':
    'Generated content — rebuilt automatically, not editable.',
  'systemPrompt.blockList.inheritedBadge': 'inherited',
  'systemPrompt.blockList.inheritedHint':
    'Inherited from the Default scope — editing creates an override.',
  'systemPrompt.blockList.dataLabel': 'Generated content (read-only)',
  'systemPrompt.blockList.dataEmpty': 'No content for the current scope.',
  'systemPrompt.blockList.showPreview': 'Show preview',
  'systemPrompt.blockList.hidePreview': 'Hide preview',
  'systemPrompt.blockList.empty': 'No prompt blocks for this scope.',
  'systemPrompt.blockList.toggleAria': 'Toggle {id}',
  'systemPrompt.blockList.reorderHandle': 'Reorder {id} (use arrow keys)',
  'systemPrompt.blockList.reorderAnnouncement':
    'Moved to position {position} of {total}',
  'systemPrompt.blockList.ownerHint.always': 'Always included.',
  'systemPrompt.blockList.ownerHint.tool':
    'Included only while the {name} tool is active.',
  'systemPrompt.blockList.ownerHint.extension':
    'Included only while the {name} extension is active.',
  'systemPrompt.blockList.ownerHint.memory':
    'Included only while the memory tool is on.',
  'systemPrompt.blockList.ownerHint.channel':
    'Included only while the agent has an active channel.',

  'settings.title': 'Settings',
  'settings.loading': 'Loading settings…',
  'settings.loadError': 'Settings could not be loaded.',
  'settings.saveError': 'Settings could not be saved.',
  'settings.sections': 'Settings sections',
  'settings.groups.connect': 'Connect',
  'settings.groups.models': 'Models',
  'settings.groups.behavior': 'Behavior',
  'settings.groups.desktop': 'Desktop app',
  'settings.groups.system': 'System',
  'settings.search.placeholder': 'Search settings…',
  'settings.search.label': 'Search settings',
  'settings.search.matches': 'Matches: {count}',
  'settings.search.noMatches': 'No settings match your search.',
  'settings.desktop.connection.title': 'Connection',
  'settings.desktop.connection.subtitle':
    'Choose which vBot server this Desktop app connects to.',
  'settings.desktop.connection.savedTitle': 'Saved servers',
  'settings.desktop.connection.savedDescription':
    'The active server supplies this WebUI. Switching reloads the Desktop app without moving Sessions or Runs.',
  'settings.desktop.connection.loading': 'Loading saved servers…',
  'settings.desktop.connection.loadError': 'Saved servers could not be loaded.',
  'settings.desktop.connection.emptyTitle': 'No saved servers',
  'settings.desktop.connection.emptyDescription':
    'Add a server below to make it available for this Desktop app.',
  'settings.desktop.connection.active': 'Connected',
  'settings.desktop.connection.connect': 'Connect',
  'settings.desktop.connection.connecting': 'Connecting…',
  'settings.desktop.connection.connectError':
    'The Desktop app could not connect to that server.',
  'settings.desktop.connection.addTitle': 'Add server',
  'settings.desktop.connection.addDescription':
    'Save a local or remote vBot server for this Windows app.',
  'settings.desktop.connection.host': 'Host',
  'settings.desktop.connection.hostRequired': 'Enter a server host.',
  'settings.desktop.connection.port': 'Port',
  'settings.desktop.connection.portInvalid':
    'Enter a port between 1 and 65535.',
  'settings.desktop.connection.label': 'Label (optional)',
  'settings.desktop.connection.labelPlaceholder': 'Home server',
  'settings.desktop.connection.addAction': 'Add server',
  'settings.desktop.connection.addSuccess': 'Server saved.',
  'settings.desktop.connection.addError': 'Server could not be saved.',
  'settings.desktop.connection.removeSuccess': 'Server removed.',
  'settings.desktop.connection.removeError': 'Server could not be removed.',
  'settings.desktop.switchModalTitle': 'Switch server',
  'settings.general.title': 'Server info',
  'settings.general.subtitle':
    'Server address, data directory, and connected clients.',
  'settings.general.serverHost': 'Server host',
  'settings.general.serverHostDescription':
    'Address and port the vBot server listens on.',
  'settings.general.dataDirectory': 'Data directory',
  'settings.general.dataDirectoryDescription':
    'Root path for agents, sessions, and workspace files.',
  'settings.general.setupGuide': 'Setup guide',
  'settings.general.setupGuideDescription':
    'Reopen the guided first-run setup to connect a provider and assign a model.',
  'settings.general.setupGuideAction': 'Open setup guide',
  'settings.general.clients.title': 'Connected clients',
  'settings.general.clients.description':
    'App windows currently connected to this server (browser tabs and the Desktop app).',
  'settings.general.clients.loading': 'Loading connected clients…',
  'settings.general.clients.empty': 'No app windows connected.',
  'settings.general.clients.loadError':
    'Connected clients could not be loaded.',
  'settings.general.clients.thisWindow': 'This window',
  'settings.general.clients.connectedAt': 'Connected {time}',
  'settings.general.clients.accessor.browser': 'Browser',
  'settings.general.clients.accessor.desktop': 'Desktop',
  'settings.general.clients.accessor.unknown': 'Unknown',
  'settings.general.clients.status.connected': 'Connected',
  'settings.defaults.title': 'Agent defaults',
  'settings.defaults.subtitle':
    'Model, temperature, and thinking effort used when an agent or project leaves them unset — shown there as "Inherited: … (global default)".',
  'settings.defaults.model': 'Model',
  'settings.defaults.modelDescription': 'Used when an agent model is empty.',
  'settings.defaults.fallbackModel': 'Fallback model',
  'settings.defaults.fallbackModelDescription':
    'Used when an agent fallback model is empty.',
  'settings.defaults.temperature': 'Temperature',
  'settings.defaults.temperatureDescription':
    'Used when an agent temperature is unset.',
  'settings.defaults.thinkingEffort': 'Thinking effort',
  'settings.defaults.thinkingEffortDescription':
    'Used when an agent thinking effort is unset.',
  'settings.defaults.noThinkingEffort': '— (no default)',
  'settings.defaults.saveSuccess': 'Agent defaults updated.',
  'settings.skills.title': 'Skills',
  'settings.skills.subtitle': 'Manage skill files and skill scan directories.',
  'settings.skills.defaultDirectory': 'Default skill directory',
  'settings.skills.defaultDirectoryDescription':
    'Always scanned from the vBot data directory and kept read-only here.',
  'settings.skills.extraDirectories': 'Additional skill directories',
  'settings.skills.extraDirectoriesDescription':
    'Extra folders scanned for skills as part of the global library — their skills are available to every agent. Useful for keeping a skill collection outside the vBot data directory.',
  'settings.skills.pathPlaceholder': 'C:/path/to/skills',
  'settings.skills.addDirectory': 'Add directory',
  'settings.skills.removeDirectory': 'Remove skill directory {path}',
  'settings.skills.emptyDirectories':
    'No additional skill directories configured.',
  'settings.skills.saveSuccess': 'Skill directories updated.',
  'settings.skills.manageLabel': 'Manage skills',
  'settings.skills.manageDescription':
    'View, create, edit, and delete skills in your global library or an agent’s private home.',
  'settings.skills.scopeLabel': 'Skill scope',
  'settings.skills.scopeGlobal': 'Global skills',
  'settings.skills.scopeAgent': '{name} (private)',
  'settings.skills.loadError': 'Skills could not be loaded.',
  'settings.skills.empty': 'No skills in this scope yet.',
  'settings.skills.newSkill': 'New skill',
  'settings.skills.newSkillHelp':
    'A skill is a Markdown playbook: a header with a name and a short description, followed by the instructions.\n\nThe description matters most — it is what the agent reads to decide when to apply the skill, so state clearly what task it is for.',
  'settings.skills.nameLabel': 'Skill name',
  'settings.skills.contentLabel': 'SKILL.md content',
  'settings.skills.namePlaceholder': 'skill-name',
  'settings.skills.contentPlaceholder':
    '---\nname: skill-name\ndescription: When to use this skill.\n---\n\n# Overview',
  'settings.skills.create': 'Create skill',
  'settings.skills.created': 'Skill created.',
  'settings.skills.createError': 'Skill could not be created.',
  'settings.skills.saved': 'Skill saved.',
  'settings.skills.contentSaveError': 'Skill could not be saved.',
  'settings.skills.deleted': 'Skill deleted.',
  'settings.skills.deleteError': 'Skill could not be deleted.',
  'settings.skills.deleteConfirmTitle': 'Delete skill',
  'settings.skills.deleteConfirm':
    'Delete skill “{name}” permanently? The skill file is removed from disk.',
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
    'Maximum wait time for foreground sub-agent calls before they fail.',
  'settings.subagents.saveSuccess': 'Sub-agent settings updated.',
  'settings.reflection.title': 'Reflection',
  'settings.reflection.subtitle':
    'Automatic background self-review that saves durable memory and skill updates from finished conversations.',
  'settings.reflection.enabled': 'Enable background reflection',
  'settings.reflection.enabledDescription':
    'After a run finishes, the agent periodically reviews the conversation in a forked session and saves durable memory and skill updates. The original conversation is never touched.',
  'settings.reflection.memoryInterval': 'Memory review interval (turns)',
  'settings.reflection.memoryIntervalDescription':
    'A memory review becomes due after this many of your messages in a conversation.',
  'settings.reflection.skillInterval': 'Skill review interval (tool calls)',
  'settings.reflection.skillIntervalDescription':
    'A skill review becomes due after this many tool calls in a conversation.',
  'settings.reflection.saveSuccess': 'Reflection settings updated.',
  'settings.compaction.title': 'Compaction',
  'settings.compaction.subtitle':
    'Choose when Context is compacted and how the next checkpoint is assembled.',
  'compaction.enabled': 'Automatic compaction',
  'compaction.enabledDescription':
    'Evaluate this Policy after safe, completed Model steps.',
  'compaction.trigger.label': 'Trigger',
  'compaction.trigger.contextRatio': 'Context window ratio',
  'compaction.trigger.inputTokens': 'Absolute input tokens',
  'compaction.trigger.threshold': 'Context ratio',
  'compaction.trigger.tokens': 'Input tokens',
  'compaction.strategy.label': 'Strategy',
  'compaction.strategy.summaryTail': 'Summary + verbatim tail',
  'compaction.strategy.continuation': 'Cache-preserving continuation',
  'compaction.strategy.tailTokens': 'Verbatim tail tokens',
  'compaction.strategy.summaryModel': 'Summary model',
  'compaction.strategy.activeModel': 'Active Model',
  'compaction.strategy.continuationDescription':
    'Reuses the active Model request prefix and turns one text response directly into the next checkpoint.',
  'settings.compaction.auto': 'Auto-compact',
  'settings.compaction.autoDescription':
    'When the conversation reaches the threshold, older messages are automatically summarized; the summary plus the most recent messages stay in context.',
  'settings.compaction.threshold': 'Threshold',
  'settings.compaction.thresholdDescription':
    'Fraction of the context window that triggers compaction, between 0 and 1 — e.g. 0.8 compacts when the context is 80% full.',
  'settings.compaction.tailTokens': 'Tail tokens',
  'settings.compaction.tailTokensDescription':
    'Amount of recent conversation that is always kept word-for-word instead of summarized, measured in tokens.',
  'settings.compaction.summaryModel': 'Summary model',
  'settings.compaction.summaryModelPlaceholder': 'Active agent model',
  'settings.compaction.summaryModelDescription':
    'Model used for summarization. Leave blank to use the active agent model. This binding is independent of agent and project defaults.',
  'settings.compaction.saved': 'Compaction settings saved.',
  'settings.recall.title': 'Recall',
  'settings.recall.subtitle': 'How agents search past conversations.',
  'settings.recall.backend': 'Recall backend',
  'settings.recall.backendDescription':
    'How the session search looks through stored conversations.',
  'settings.recall.backends.jsonl_scan':
    'Simple scan — exact keyword match, no index',
  'settings.recall.backends.sqlite_fts':
    'Full-text search — fast keyword search with an index',
  'settings.recall.backends.vector':
    'Semantic — finds matches by meaning, needs an embedding model',
  'settings.recall.vectorHint':
    'Semantic search requires an embedding model — configure it under Specialized Models.',
  'settings.recall.saveSuccess': 'Recall backend updated.',
  'settings.webSearch.title': 'Web Search',
  'settings.webSearch.subtitle': 'Provider used by the web_search tool.',
  'settings.webSearch.provider': 'Search provider',
  'settings.webSearch.providerDescription':
    'Provider used whenever an agent calls web_search.',
  'settings.webSearch.providers.brave': 'Brave Search',
  'settings.webSearch.providers.searxng': 'SearXNG',
  'settings.webSearch.defaultCount': 'Default result count',
  'settings.webSearch.defaultCountDescription':
    'Number of results a web_search call returns when the agent does not ask for a specific count (1-20).',
  'settings.webSearch.searxngBaseUrl': 'SearXNG base URL',
  'settings.webSearch.searxngBaseUrlDescription':
    'Address of the SearXNG instance to use. SearXNG is a self-hosted metasearch engine — you need to run one yourself or point this at a reachable instance.',
  'settings.webSearch.searxngBaseUrlPlaceholder': 'http://localhost:8888',
  'settings.webSearch.braveKeyHint':
    'Brave Search requires an API key: set BRAVE_API_KEY in the .env file in the vBot data directory. Without it, every web search fails.',
  'settings.webSearch.saveSuccess': 'Web search settings updated.',
  'settings.specializedModels.title': 'Specialized Models',
  'settings.specializedModels.subtitle':
    'Task-specific model bindings for speech, image, and embedding tools. These bindings are independent of agent and project defaults.',
  'settings.specializedModels.loading': 'Loading specialized model targets…',
  'settings.specializedModels.loadError':
    'Specialized model targets could not be loaded.',
  'settings.specializedModels.optionsLoadError':
    'Model options could not be loaded.',
  'settings.specializedModels.saveSuccess':
    'Specialized model bindings updated.',
  'settings.specializedModels.speechToText': 'Speech to text',
  'settings.specializedModels.speechToTextDescription':
    'Used by the chat microphone transcription flow.',
  'settings.specializedModels.textToSpeech': 'Text to speech',
  'settings.specializedModels.textToSpeechDescription':
    'Used by the agent text_to_speech tool.',
  'settings.specializedModels.imageUnderstanding': 'Image understanding',
  'settings.specializedModels.imageUnderstandingDescription':
    'Used by analyze_image when the active agent route cannot accept images.',
  'settings.specializedModels.imageGeneration': 'Image generation',
  'settings.specializedModels.imageGenerationDescription':
    'Used for image generation requests.',
  'settings.specializedModels.embeddingModel': 'Embedding model',
  'settings.specializedModels.embeddingModelDescription':
    'Turns text into numeric vectors for meaning-based search. Required when Recall is set to Semantic.',
  'settings.specializedModels.noTarget': 'Not configured',
  'settings.specializedModels.customTarget': 'Custom target: {target}',
  'settings.specializedModels.noOptions':
    'This target has no configurable options.',
  'settings.specializedModels.optionsAria': 'Options for {task}',
  'settings.specializedModels.jsonPlaceholder':
    'e.g. [{"text":"hello","bbox":[[0,0],[1,0],[1,1],[0,1]]}]',
  'settings.specializedModels.jsonInvalid': 'Invalid JSON',

  'settings.providers.title': 'Providers',
  'settings.providers.subtitle': 'Connected providers and their credentials.',
  'settings.providers.noneConnected':
    'No providers connected yet. Add one to make its models available.',
  'settings.providers.description.credentialKey':
    'Credential key: {credentialKey}.',
  'settings.providers.description.baseUrl': 'Endpoint: {baseUrl}.',
  'settings.providers.description.modelCount': '{count} models available.',
  'settings.providers.description.none':
    'Provider metadata is not available yet.',
  'settings.providers.refreshModels': 'Update Model DB',
  'settings.providers.refreshModelsHint':
    'Fetches the current model lists from your connected providers and the public model catalog. Run it when a provider ships new models — your hand-maintained overrides are never touched.',
  'settings.providers.refreshingModels': 'Updating…',
  'settings.providers.refreshSuccess':
    'Model DB updated: {providerCount} providers, {count} models available.',
  'settings.providers.refreshError': 'Model DB could not be updated.',
  'settings.providers.refreshPartial':
    'Some providers could not be reached and were skipped: {providers}.',
  'settings.providers.connect': 'Connect',
  'settings.providers.disconnect': 'Disconnect',
  'settings.providers.connected': 'Connected',
  'settings.providers.disabledChip': 'Disabled',
  'settings.providers.notReachableChip': 'Not reachable',
  'settings.providers.enable': 'Enable',
  'settings.providers.enableAria': 'Enable connection {id}',
  'settings.providers.disable': 'Disable',
  'settings.providers.disableAria': 'Disable connection {id}',
  'settings.providers.detailsAria': 'Details for {id}',
  'settings.providers.disabledDescription':
    'Disabled — not probed and offering no models until you enable it.',
  'settings.providers.enabledReachableToast':
    '{connection} enabled — endpoint reachable, model catalog refreshed.',
  'settings.providers.enabledUnreachableToast':
    '{connection} enabled, but the endpoint is not reachable. Start the service and its models appear automatically.',
  'settings.providers.disabledToast': '{connection} disabled.',
  'settings.providers.toggleError': 'Provider connection could not be updated.',
  'settings.providers.connectError':
    'Provider connection could not be started.',
  'settings.providers.disconnectError':
    'Provider connection could not be disconnected.',
  'settings.providers.apiKeyDescription':
    'Static credential configured from environment or data directory.',
  'settings.providers.oauthDescription':
    'OAuth device authorization managed by the provider.',
  'settings.providers.oauthTokenDescription':
    'OAuth token configured from environment or data directory.',
  'settings.providers.keylessDescription':
    'No key required — this endpoint is keyless.',
  'settings.providers.localContext.title': 'Local model context',
  'settings.providers.localContext.description':
    'The context window vBot budgets against and requests from the local server per call. Empty uses the default (32k, capped at the model max).',
  'settings.providers.localContext.inputLabel': 'Context window for {model}',
  'settings.providers.localContext.maxHint': 'model max {max}',
  'settings.providers.localContext.invalidValue':
    'Context window must be a positive whole number',
  'settings.providers.openrouter.title': 'Routing',
  'settings.providers.openrouter.description':
    'Control which upstream providers OpenRouter may use. vBot sends a stable Session identifier so OpenRouter can apply Sticky Routing.',
  'settings.providers.openrouter.stabilityHint':
    'Sticky Routing is best effort. To prevent provider switches, allow one exact endpoint and turn provider fallbacks off.',
  'settings.providers.openrouter.scopeLabel': 'Scope',
  'settings.providers.openrouter.scopeHelp':
    'Global routing applies to every OpenRouter model unless that model has an override.',
  'settings.providers.openrouter.globalScope': 'Global routing',
  'settings.providers.openrouter.modelSearch': 'Find an OpenRouter model…',
  'settings.providers.openrouter.modelOverride': 'Model override',
  'settings.providers.openrouter.modelOverrideOn':
    'This model has its own routing policy. Global blocks still apply.',
  'settings.providers.openrouter.modelOverrideOff':
    'This model inherits the global routing policy.',
  'settings.providers.openrouter.modelOverrideAria':
    'Use a routing override for {model}',
  'settings.providers.openrouter.modeLabel': 'Routing mode',
  'settings.providers.openrouter.mode.automatic':
    'Automatic (OpenRouter managed)',
  'settings.providers.openrouter.mode.allowed': 'Only allowed providers',
  'settings.providers.openrouter.mode.ordered': 'Preferred provider order',
  'settings.providers.openrouter.orderWarning':
    'A manual provider order overrides OpenRouter Sticky Routing. OpenRouter tries the listed providers first, but automatic cache affinity is disabled.',
  'settings.providers.openrouter.preferredProviders': 'Provider priority',
  'settings.providers.openrouter.allowedProviders': 'Allowed providers',
  'settings.providers.openrouter.blockedProviders': 'Blocked providers',
  'settings.providers.openrouter.blockedProvidersModel':
    'Additionally blocked for this model',
  'settings.providers.openrouter.addProvider': 'Add provider…',
  'settings.providers.openrouter.blockProvider': 'Block provider…',
  'settings.providers.openrouter.providerSearch': 'Find a provider…',
  'settings.providers.openrouter.customProvider': 'Custom provider slug',
  'settings.providers.openrouter.customProviderHelp':
    'Use an exact endpoint tag such as google-vertex/europe when it is not in the fetched list.',
  'settings.providers.openrouter.customProviderPlaceholder':
    'google-vertex/europe',
  'settings.providers.openrouter.block': 'Block',
  'settings.providers.openrouter.select': 'Select',
  'settings.providers.openrouter.moveUp': 'Move {provider} up',
  'settings.providers.openrouter.moveDown': 'Move {provider} down',
  'settings.providers.openrouter.removeProvider': 'Remove {provider}',
  'settings.providers.openrouter.unblockProvider': 'Unblock {provider}',
  'settings.providers.openrouter.invalidSlug':
    'Enter a valid OpenRouter provider slug.',
  'settings.providers.openrouter.providerRequired':
    '{scope} needs at least one provider for this routing mode.',
  'settings.providers.openrouter.providerConflict':
    '{provider} is both selected and blocked in {scope}.',
  'settings.providers.openrouter.fallbacks': 'Provider fallbacks',
  'settings.providers.openrouter.fallbacksHelp':
    'When disabled, OpenRouter returns an error instead of trying a backup provider when the primary is unavailable.',
  'settings.providers.openrouter.fallbacksAria':
    'Allow OpenRouter provider fallbacks',
  'settings.providers.openrouter.save': 'Save routing',
  'settings.providers.openrouter.saved': 'OpenRouter routing settings saved.',
  'settings.providers.openrouter.saveError':
    'OpenRouter routing settings could not be saved.',
  'settings.providers.device_flow.title': 'Connect {provider}',
  'settings.providers.device_flow.instructions':
    'Enter this code at the link below:',
  'settings.providers.device_flow.copy_aria': 'Copy device code {code}',
  'settings.providers.device_flow.copied': 'Copied',
  'settings.providers.device_flow.copy_success': 'Device code copied.',
  'settings.providers.device_flow.copy_error':
    'Device code could not be copied.',
  'settings.providers.device_flow.waiting':
    'Waiting for {provider} authorization…',
  'settings.providers.device_flow.success_toast':
    '{provider} connected successfully',
  'settings.providers.device_flow.error_toast':
    'Authorization failed or timed out',
  'settings.providers.replaceKey': 'Replace key…',
  'settings.providers.accounts.defaultLabel': 'Default',
  'settings.providers.accounts.notUsable': 'Not usable',
  'settings.providers.accounts.source.processEnv': 'Process env',
  'settings.providers.accounts.source.dataDir': '.env file',
  'settings.providers.accounts.source.oauth': 'OAuth',
  'settings.providers.accounts.addButton': 'Add account…',
  'settings.providers.accounts.nameLabel': 'Account',
  'settings.providers.accounts.nameHint':
    'Optional name for this account. Only needed if you add more than one — otherwise leave it empty.',
  'settings.providers.accounts.invalidId':
    'Account names use 1–32 lowercase letters, digits, or underscores and start with a letter or digit.',
  'settings.providers.accounts.removeEnvHint':
    'This credential comes from the process environment and cannot be removed here.',
  'settings.providers.removeKeySuccess': 'API key removed.',
  'settings.providers.removeKeyError': 'API key could not be removed.',
  'settings.providers.removeKeyStillEnv':
    'Key removed, but the process environment still provides a credential.',
  'settings.providers.add.button': 'Add provider',
  'settings.providers.add.connectionButton': 'Add connection',
  'settings.providers.add.title': 'Add provider',
  'settings.providers.add.chooseProvider': 'Choose a provider to connect.',
  'settings.providers.add.chooseMethod': 'Choose how to connect {provider}.',
  'settings.providers.add.allConnected':
    'All available providers are already connected.',
  'settings.providers.add.methodApiKey': 'API key',
  'settings.providers.add.methodApiKeyDescription':
    'Paste a static API key; it is stored in the data directory.',
  'settings.providers.add.methodOAuth': 'Sign in (OAuth)',
  'settings.providers.add.methodOAuthDescription':
    'Authorize vBot through the provider account in a browser.',
  'settings.providers.add.apiKeyLabel': 'API key',
  'settings.providers.add.apiKeyPlaceholder': 'Paste the API key…',
  'settings.providers.add.apiKeyHint':
    'Stored as {credentialKey} in the data directory .env.',
  'settings.providers.add.saveKey': 'Save key',
  'settings.providers.add.keyError': 'API key could not be saved.',
  'settings.providers.add.oauthIntro':
    'Click Connect to begin. vBot then shows a code to enter at {provider} in your browser.',
  'settings.channels.title': 'Channels',
  'settings.channels.subtitle': 'Manage channel routing and runtime status.',
  'settings.channels.add': 'Add channel',
  'settings.channels.edit': 'Edit channel {id}',
  'settings.channels.enable': 'Enable',
  'settings.channels.enableAria': 'Enable channel {id}',
  'settings.channels.disable': 'Disable',
  'settings.channels.disableAria': 'Disable channel {id}',
  'settings.channels.delete': 'Delete channel {id}',
  'settings.channels.platform': 'Platform',
  'settings.channels.agent': 'Agent',
  'settings.channels.agent.placeholder': 'Select agent',
  'settings.channels.agent.none': 'No agents available',
  'settings.channels.dm_scope': 'DM scope',
  'settings.channels.dm_scope.per_conversation': 'Per conversation',
  'settings.channels.dm_scope.main': 'Main',
  'settings.channels.dm_scope.per_peer': 'Per peer',
  'settings.channels.dm_scope.per_account_channel_peer':
    'Per account + channel + peer',
  'settings.channels.token_env_var': 'Token env var',
  'settings.channels.token_env_var.help':
    'Name of the environment variable that holds the bot token. Set the variable itself in the .env file in the vBot data directory — only the name goes here.',
  'settings.channels.idHelp':
    'A name you choose for this channel. It cannot be changed after creation.',
  'settings.channels.dm_scope.help':
    'How direct messages are grouped into chat sessions:\n\nMain — all DMs share one session. Per peer — one session per person. Per conversation — one session per chat. Per account, channel & peer — one session per chat and person.\n\nGroup chats always share one session per group, regardless of this setting.',
  'settings.channels.allowed_chat_ids.help':
    'Comma-separated chat IDs allowed to talk to this channel. An empty list allows nobody. Messages from chats not on the list are rejected and appear on the channel card below with a one-click Allow.',
  'settings.channels.allowed_chat_ids': 'Allowed chat IDs (inbound)',
  'settings.channels.allowed_chat_ids.placeholder': '12345, -1009876543210',
  'settings.channels.allowed_chat_ids.none': 'None',
  'settings.channels.enabled': 'Enabled',
  'settings.channels.disabled': 'Disabled',
  'settings.channels.running': 'Running',
  'settings.channels.stopped': 'Stopped',
  'settings.channels.empty': 'No channels configured.',
  'settings.channels.delete_confirm_title': 'Delete channel',
  'settings.channels.delete_confirm':
    'Delete channel "{id}" permanently? vBot stops listening on it and its configuration is removed.',
  'settings.channels.createSuccess': 'Channel created.',
  'settings.channels.updateSuccess': 'Channel updated.',
  'settings.channels.enableSuccess': 'Channel enabled.',
  'settings.channels.disableSuccess': 'Channel disabled.',
  'settings.channels.deleteSuccess': 'Channel deleted.',
  'settings.extensions.title': 'Extensions',
  'settings.extensions.subtitle':
    'Loaded extensions and their capabilities. Toggles take effect immediately.',
  'settings.extensions.empty': 'No extensions discovered.',
  'settings.extensions.statusLoaded': 'Loaded',
  'settings.extensions.statusFailed': 'Failed',
  'settings.extensions.statusDisabled': 'Disabled',
  'settings.extensions.statusOverridden': 'Overridden',
  'settings.extensions.overriddenBy': 'Overridden by your copy at {path}',
  'settings.extensions.waiting': 'On, waiting for configuration',
  'settings.extensions.waitingFor': 'Waiting for: {fields}',
  'settings.extensions.enable': 'Enable',
  'settings.extensions.disable': 'Disable',
  'settings.extensions.enableAria': 'Enable extension {name}',
  'settings.extensions.disableAria': 'Disable extension {name}',
  'settings.extensions.enableSuccess': 'Extension enabled.',
  'settings.extensions.disableSuccess': 'Extension disabled.',
  'settings.extensions.error': 'Error',
  'settings.extensions.warning': 'Warning',
  'settings.extensions.hooks': 'Hooks',
  'settings.extensions.tools': 'Tools',
  'settings.extensions.recallBackends': 'Recall backends',
  'settings.extensions.startup': 'startup',
  'settings.extensions.shutdown': 'shutdown',
  'settings.extensions.config': 'Config (JSON)',
  'settings.extensions.configAria': 'Config for extension {name}',
  'settings.extensions.configToggleAria': 'Configuration for extension {name}',
  'settings.extensions.configInvalid': 'Config must be a JSON object.',
  'settings.extensions.saveConfig': 'Save config',
  'settings.extensions.saveSettings': 'Save settings',
  'settings.extensions.configSaveSuccess': 'Extension config saved.',
  'settings.extensions.fieldAria': '{label} for extension {name}',
  'settings.extensions.numberInvalid': 'Enter a valid number.',
  'settings.extensions.secretSet': 'Set',
  'settings.extensions.secretUnset': 'Not set',
  'settings.extensions.secretSave': 'Save',
  'settings.extensions.secretClear': 'Clear',
  'settings.extensions.secretPlaceholder': 'Enter a new value',
  'settings.extensions.secretAria': 'Secret {label} for extension {name}',
  'settings.extensions.secretSaved': 'Secret saved.',
  'settings.extensions.secretCleared': 'Secret cleared.',
  'settings.extensions.reload': 'Reload extensions',
  'settings.extensions.reloadSuccess': 'Extensions reloaded.',
  'settings.extensions.reloadHelp':
    'Rebuilds all extensions from disk — picks up code edits, new and removed extensions.',
  'settings.appearance.title': 'Appearance',
  'settings.appearance.subtitle': 'Language and chat reading width.',
  'settings.appearance.language': 'Language',
  'settings.appearance.languageDescription': 'Interface language.',
  'settings.appearance.chatWidth.label': 'Chat width',
  'settings.appearance.chatWidth.description':
    'Reading width of the chat column on wide screens.',
  'settings.appearance.chatWidth.comfortable': 'Comfortable',
  'settings.appearance.chatWidth.wide': 'Wide',
  'settings.appearance.chatWidth.full': 'Full width',
  'settings.appearance.saveSuccess': 'Appearance updated.',
  'settings.language.en': 'English',

  'logs.title': 'Logs',
  'logs.eyebrow': 'Daily log viewer',
  'logs.subtitle':
    'The application’s technical log, useful when diagnosing problems. Read one daily file at a time with filtering and live updates.',
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
  'logs.copyEntry': 'Copy log line',
  'logs.copied': 'Copied',
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

  'debug.eyebrow': 'Debug',
  'debug.title': 'Debug',
  'debug.subtitle':
    'Inspect captured provider requests and responses, and probe model endpoints.',
  'debug.status': 'Status',
  'debug.statusCount': 'Traces: {count} / {limit}',
  'debug.traceList': 'Traces',
  'debug.modelProbe': 'Model Probe',

  'debug.settings': 'Debug',
  'debug.enabled': 'Enable debug mode',
  'debug.traceLimit': 'Trace limit',
  'debug.localWarning':
    'Debug traces are stored locally. Provider requests and responses are captured in full, including raw prompt content sent to models. Secret values like API keys and tokens are automatically redacted.',

  'debug.request': 'Request',
  'debug.requestMethod': 'Method',
  'debug.requestUrl': 'URL',
  'debug.requestHeaders': 'Headers',
  'debug.requestBody': 'Body',
  'debug.response': 'Response',
  'debug.responseStatus': 'Status',
  'debug.responseHeaders': 'Headers',
  'debug.responseBody': 'Body',
  'debug.streamRaw': 'Raw',
  'debug.streamParsed': 'Parsed',

  'debug.metadata': 'Metadata',

  'debug.emptyState':
    'No traces captured yet. Enable debug mode in Settings and send a message to start recording provider requests and responses.',

  'debug.clearConfirm': 'Clear all traces? This cannot be undone.',

  'debug.modelProbe.run': 'Probe',
  'debug.modelProbe.provider': 'Provider',
  'debug.modelProbe.connection': 'Connection',

  'debug.modelProbe.modelCount': '{count} models',

  'debug.modelProbe.rawResponse': 'Raw Response',
  'debug.modelProbe.normalizedPreview': 'Normalized Preview',

  'debug.modelProbe.selectProvider': 'Select a provider',
  'debug.modelProbe.selectConnection': 'Select a connection',

  'debug.emptyHeader': 'No traces captured yet',
  'debug.expandRow': 'Expand row',
  'debug.collapseRow': 'Collapse row',

  'status.connected': 'Connected',
  'status.notReachable': 'Not reachable',
  'status.reconnecting': 'Reconnecting…',
  'status.connectionInterrupted': 'Connection interrupted',
  'status.connectionRestored': 'Connection restored',
  'status.serverUnavailableTitle': 'Server is not reachable',
  'status.serverUnavailableMessage':
    'vBot is trying to restore the connection automatically.',
  'status.serverUnavailableDetails':
    'The browser connection to the vBot server was interrupted. Features that need the server are temporarily unavailable.',
  'status.serverRestoredTitle': 'Server is reachable again',
  'status.serverRestoredMessage': 'The current view has been refreshed.',
  'status.retryNow': 'Retry now',
  'status.switchServer': 'Switch server',

  'settings.voice.title': 'Voice',
  'settings.voice.subtitle': 'Wakeword detection and voice command settings.',
  'settings.voice.enabled': 'Wakeword listening',
  'settings.voice.model': 'Wakeword model',
  'settings.voice.models': 'Wakeword phrases',
  'settings.voice.modelDescription':
    'Choose one or two phrases to listen for at the same time. Each model keeps its own sensitivity.',
  'settings.voice.modelBuiltIn': 'Built-in',
  'settings.voice.modelImported': 'Imported TFLite',
  'settings.voice.modelToggleAria': 'Listen for {name}',
  'settings.voice.modelLimit': '{count} of 2 wakeword models active',
  'settings.voice.importModel': 'Import TFLite model',
  'settings.voice.removeModel': 'Remove imported model',
  'settings.voice.importSuccessActive':
    'Wakeword model imported and activated.',
  'settings.voice.importSuccessInactive':
    'Wakeword model imported. Deactivate another model to use it.',
  'settings.voice.deleteConfirmTitle': 'Remove wakeword model',
  'settings.voice.deleteConfirm':
    'Remove “{name}” permanently from this Desktop? The TFLite file stored by vBot will be deleted.',
  'settings.voice.deleteSuccess': 'Wakeword model removed.',
  'settings.voice.microphone': 'Microphone',
  'settings.voice.sensitivity': 'Sensitivity',
  'settings.voice.targetAgent': 'Personal Agent',
  'settings.voice.targetAgentDescription':
    'The Personal Agent that receives spoken commands on this server. Project Agents and other servers use separate routing.',
  'settings.voice.sessionBehavior': 'Session',
  'settings.voice.sessionBehaviorActive': 'Use active session',
  'settings.voice.sessionBehaviorNew': 'New session each time',
  'settings.voice.state': 'Status',
  'settings.voice.privacyNote':
    'While listening is enabled, microphone audio is analyzed continuously on this device. Nothing is saved or sent before the wake phrase matches; the following command recording is sent to your configured vBot speech backend for transcription.',
  'settings.voice.saveSuccess': 'Voice settings updated.',
  'settings.voice.systemDefaultMic': 'System default',
  'settings.voice.systemAutomaticMic': 'Automatic selection',
  'settings.voice.compatibleMic': 'Compatible',
  'settings.voice.incompatibleMic': 'Unsupported format',
  'settings.voice.noAgent': '— (none)',
  'settings.voice.lessSensitive': 'Less sensitive',
  'settings.voice.moreSensitive': 'More sensitive',
  'settings.voice.desktopOnly':
    'Voice settings are only available in the vBot Desktop app. Open the Desktop app to configure wakeword detection and voice commands.',
  'settings.voice.mockWarning':
    'Voice is running in demo mode. State changes are simulated; no microphone is heard and no command is sent. Restart Desktop without --mock-wakeword for real detection.',
  'settings.voice.cancelPhrases':
    'Say “abbrechen” or “vergiss es” at the end of the same recording to discard the entire command before it starts a Run.',
  'settings.voice.error.serverUnreachable':
    'Voice could not reach the active server. Check the Desktop connection and try again.',
  'settings.voice.error.speechToTextUnconfigured':
    'Configure a Speech-to-text Model under Settings → Models before enabling wakeword listening.',
  'settings.voice.error.speechToTextUnavailable':
    'The configured Speech-to-text Model is not currently usable. Check its Provider connection or choose another Model under Settings → Models.',
  'settings.voice.error.speechToTextReadiness':
    'Voice could not verify the Speech-to-text configuration. Check the Desktop log and try again.',

  'voice.state.off': 'Disabled',
  'voice.state.starting': 'Starting',
  'voice.state.listening': 'Listening',
  'voice.state.wakewordDetected': 'Wakeword detected',
  'voice.state.recording': 'Recording',
  'voice.state.transcribing': 'Transcribing',
  'voice.state.sending': 'Sending',
  'voice.state.sent': 'Sent',
  'voice.state.cancelled': 'Cancelled',
  'voice.state.no_speech': 'No speech heard',
  'voice.state.transcription_failed': 'Not understood',
  'voice.state.processing': 'Processing',
  'voice.state.error': 'Voice error',

  'voice.mic.tooltip.off': 'Wakeword disabled',
  'voice.mic.tooltip.starting': 'Starting wakeword listening',
  'voice.mic.tooltip.listening': 'Listening for wakeword',
  'voice.mic.tooltip.detected': 'Wakeword detected',
  'voice.mic.tooltip.recording': 'Recording voice command',
  'voice.mic.tooltip.processing': 'Processing voice command',
  'voice.mic.tooltip.sent': 'Voice command sent',
  'voice.mic.tooltip.cancelled': 'Voice command cancelled',
  'voice.mic.tooltip.noSpeech': 'No speech heard',
  'voice.mic.tooltip.transcriptionFailed': 'Voice command was not understood',
  'voice.mic.tooltip.error': 'Voice error',
  'voice.toast.sentTitle': 'Voice command sent',
  'voice.toast.noSpeechTitle': 'No speech heard',
  'voice.toast.noSpeechMessage':
    'No command followed the wakeword. Try again and speak after the cue.',
  'voice.toast.transcriptionFailedTitle':
    'Voice command could not be transcribed',
  'voice.toast.transcriptionFailedMessage':
    'Check the Speech-to-text Model and the Desktop log, then try again.',
  'voice.toast.errorMessage':
    'Open Voice settings for details. The failure was written to the Desktop log.',

  'statistics.eyebrow': 'Usage & activity',
  'statistics.title': 'Statistics',
  'statistics.subtitle':
    'Aggregated on demand from your session history — no extra data is stored.',
  'statistics.loading': 'Loading statistics…',
  'statistics.loadError': 'Statistics could not be loaded.',
  'statistics.empty': 'No activity recorded yet.',
  'statistics.none': 'None',
  'statistics.generatedAt': 'Generated {time}',
  'statistics.estimatedBadge': '~ estimated',
  'statistics.estimatedHint':
    'Estimated tokens are approximated, not provider-reported, and are tracked separately from measured usage.',
  'statistics.derivedHint':
    'Derived from an in-run model change — not an authoritative fallback signal.',
  'statistics.subview.overview': 'Overview',
  'statistics.subview.usage': 'Usage',
  'statistics.subview.runs': 'Runs & errors',
  'statistics.subview.tools': 'Tools',
  'statistics.subview.skills': 'Skills',
  'statistics.granularity.label': 'Period',
  'statistics.granularity.day': 'Day',
  'statistics.granularity.week': 'Week',
  'statistics.granularity.month': 'Month',
  'statistics.status.completed': 'Completed',
  'statistics.status.failed': 'Failed',
  'statistics.status.cancelled': 'Cancelled',
  'statistics.role.system': 'System',
  'statistics.role.user': 'User',
  'statistics.role.assistant': 'Assistant',
  'statistics.role.tool': 'Tool',
  'statistics.role.note': 'System reminder',
  'statistics.role.error': 'Error',
  'statistics.role.compaction_checkpoint': 'Compaction',
  'statistics.role.run_summary': 'Run summary',
  'statistics.role.agent_takeover': 'Agent takeover',
  'statistics.overview.agents': 'Agents',
  'statistics.overview.sessions': 'Sessions',
  'statistics.overview.runs': 'Runs',
  'statistics.overview.chatMessages': 'Chat messages',
  'statistics.overview.chatMessagesHint':
    'Visible User messages and Assistant text. Thinking-only and Tool-call-only Model steps are excluded.',
  'statistics.overview.modelSteps': 'Model steps',
  'statistics.overview.modelStepsHint':
    'Every persisted Assistant response from a Model, including steps that only contain Thinking or request Tools.',
  'statistics.overview.toolCalls': 'Tool calls',
  'statistics.overview.runHealth': 'Run health',
  'statistics.overview.totalRuns': '{count} total Runs',
  'statistics.overview.completedLabel': 'completed',
  'statistics.overview.statusAria':
    '{completed} completed, {failed} failed, {cancelled} cancelled.',
  'statistics.overview.nonCompleted':
    '{count} Runs ({share}) did not complete.',
  'statistics.overview.facts': 'At a glance',
  'statistics.overview.avgDuration': 'Average run',
  'statistics.overview.medianDuration': 'Median run',
  'statistics.overview.lastActivity': 'Last activity',
  'statistics.overview.chatMessagesByRole': 'Visible chat messages by role',
  'statistics.overview.sessionRecords': 'Stored Session records',
  'statistics.overview.sessionRecordsHint':
    'Every persisted Session entry, including Chat messages and internal execution or context records.',
  'statistics.overview.activityReliability': 'Activity & reliability',
  'statistics.overview.activityWindow.day': 'Last 30 days',
  'statistics.overview.activityWindow.week': 'Last 16 weeks',
  'statistics.overview.activityWindow.month': 'Last 12 months',
  'statistics.overview.noActivityPeriod': 'No Runs in this period.',
  'statistics.overview.periodRuns': 'Runs',
  'statistics.overview.completionRate': 'Completion',
  'statistics.overview.peak': 'Peak',
  'statistics.overview.weekOf': 'Week of {date}',
  'statistics.overview.activityTooltip':
    '{period} · {runs} Runs · {completed} completed · {failed} failed · {cancelled} cancelled',
  'statistics.overview.activityAria':
    '{runs} Runs in this period; {completion} completed.',
  'statistics.overview.agentsTable': 'Per agent',
  'statistics.col.runs': 'Runs',
  'statistics.col.errors': 'Errors',
  'statistics.col.agent': 'Agent',
  'statistics.agent.projectBadgeTitle': 'Project: {project}',
  'statistics.col.sessions': 'Sessions',
  'statistics.col.session': 'Session',
  'statistics.col.lastActivity': 'Last activity',
  'statistics.col.provider': 'Provider',
  'statistics.col.model': 'Model',
  'statistics.col.tokens': 'Tokens',
  'statistics.col.share': 'Share',
  'statistics.col.avgDuration': 'Avg',
  'statistics.col.duration': 'Duration',
  'statistics.col.status': 'Status',
  'statistics.col.models': 'Models',
  'statistics.col.tool': 'Tool',
  'statistics.col.calls': 'Calls',
  'statistics.col.acceptedRate': 'Accepted',
  'statistics.col.rejectedRate': 'Rejected',
  'statistics.col.topRejection': 'Top rejection',
  'statistics.col.cacheHit': 'Cache hit',
  'statistics.col.turns': 'Turns',
  'statistics.col.input': 'Input',
  'statistics.col.cacheRead': 'Cache read',
  'statistics.col.hitRate': 'Hit rate',
  'statistics.col.time': 'Time',
  'statistics.col.previousInput': 'Prev. input',
  'statistics.col.skill': 'Skill',
  'statistics.col.origins': 'Origins',
  'statistics.col.offered': 'Offered',
  'statistics.col.activated': 'Activated',
  'statistics.col.usageRate': 'Offer conversion',
  'statistics.col.firstActivated': 'First activated',
  'statistics.col.lastActivated': 'Last activated',
  'statistics.usage.measuredTokens': 'Measured tokens (in / out)',
  'statistics.usage.estimatedTokens': 'Estimated tokens (in / out)',
  'statistics.usage.measuredTurns': 'Measured Model steps',
  'statistics.usage.estimatedTurns': 'Estimated Model steps',
  'statistics.usage.cacheRead': 'Cache read',
  'statistics.usage.cacheWrite': 'Cache write',
  'statistics.usage.cacheHitRate': 'Cache hit rate',
  'statistics.usage.cacheIntro':
    'Cache metrics track provider-side prompt caching. A higher hit rate can reduce billed input where the Provider discounts cache reads.',
  'statistics.usage.cacheHitHint':
    'Cache hit rate: tokens read from cache as a share of the input, over the turns that report cache data.',
  'statistics.usage.runAttributionHint':
    'Provider and Model Run counts mean “involved in this Run.” A fallback Run can appear in multiple rows, and Model duration is the full Run duration.',
  'statistics.usage.cacheSessions': 'Sessions with lowest cache hit rate',
  'statistics.usage.cacheEmpty': 'No cache-reporting activity yet.',
  'statistics.usage.cacheBreaks': 'Suspected cache breaks (derived)',
  'statistics.usage.cacheBreaksSummary':
    '{suspected} suspected breaks across {evaluated} evaluated continuation turns.',
  'statistics.usage.cacheBreaksHint':
    'A turn whose cache read fell far below the previous prompt although nothing legitimate explains a miss (new session, compaction, takeover, model switch, expired cache, or a tiny prompt are excluded). Best-effort heuristic, not authoritative.',
  'statistics.legend.measured': 'Measured tokens',
  'statistics.legend.estimated': 'Estimated tokens',
  'statistics.legend.cacheHit': 'Cache hit % (0–100)',
  'statistics.usage.providers': 'Providers',
  'statistics.usage.models': 'Models',
  'statistics.usage.dailyTokens': 'Tokens per period',
  'statistics.runs.count': 'Runs',
  'statistics.runs.average': 'Average',
  'statistics.runs.p50Hint':
    'Median — half of all runs finished within this time.',
  'statistics.runs.p90Hint': '90% of runs finished within this time.',
  'statistics.runs.p95Hint': '95% of runs finished within this time.',
  'statistics.runs.withTools': 'Runs with tools',
  'statistics.runs.openGroups': 'Open run groups',
  'statistics.runs.openGroupsHint':
    'Trailing turns with no completion record yet — interrupted, crashed, or still running. Best-effort, and counted apart from the finished runs above.',
  'statistics.runs.cancelRate': 'Cancel rate',
  'statistics.runs.failureRate': 'Failure rate',
  'statistics.runs.fallbackRuns': 'Fallback runs (derived)',
  'statistics.runs.avgToolsPerRun': 'Avg Tool calls / Run',
  'statistics.runs.avgAgentMessagesPerRun': 'Avg Agent messages / Run',
  'statistics.runs.avgAgentMessagesHint':
    'Visible Assistant text per recorded Run, including intermediate status updates. Open Run groups are excluded.',
  'statistics.runs.avgModelStepsPerRun': 'Avg Model steps / Run',
  'statistics.runs.avgModelStepsHint':
    'All Assistant Model responses per recorded Run, including Thinking-only and Tool-call-only steps. Open Run groups are excluded.',
  'statistics.runs.longest': 'Longest runs',
  'statistics.errors.title': 'Errors',
  'statistics.errors.total': 'Total errors',
  'statistics.errors.byKind': 'By kind',
  'statistics.errors.byProvider': 'By provider',
  'statistics.errors.byAgent': 'By agent',
  'statistics.errors.byHour': 'By UTC hour',
  'statistics.errors.scopeHint':
    'These are persisted Run errors; Tool failures are reported under Tools. Provider and Model attribution uses the last preceding Assistant Model step and is therefore a proxy.',
  'statistics.tools.totalCalls': 'Tool calls',
  'statistics.tools.outcomeNote':
    'Accepted means the Tool returned ok:true. Rejected means it returned ok:false, including safe validation and guardrail rejections; a rejection does not by itself mean the Tool malfunctioned. Statistics never reads or includes Tool arguments.',
  'statistics.tools.perTool': 'Per tool',
  'statistics.tools.byAgent': 'Calls per agent',
  'statistics.tools.topSessions': 'Busiest sessions',
  'statistics.skills.total': 'Skills',
  'statistics.skills.used': 'Activated',
  'statistics.skills.offeredUnactivated': 'No offer conversion',
  'statistics.skills.withoutOfferData': 'No offer data',
  'statistics.skills.intro':
    'A Skill is offered when it appears in a Session catalog and activated when the Agent invokes it. “Offer conversion” counts only Sessions where both facts are recorded, so older Sessions without catalog metadata cannot inflate the rate.',
  'statistics.skills.perSkill': 'Per skill',
  'statistics.skills.empty': 'No skills in the current inventory.',
  'statistics.skills.neverUsedBadge': 'No offer conversion',
  'statistics.skills.neverUsedRowTitle':
    'No Session with recorded offer data also recorded an activation — a candidate to delete or improve.',
  'statistics.skills.noOfferDataBadge': 'No offer data',
  'statistics.skills.noOfferDataRowTitle':
    'No Session has recorded this Skill in its offered catalog yet, so there is not enough evidence to judge it.',
  'statistics.skills.byAgent': 'Activations per agent',
  'statistics.skills.origin.bundled': 'bundled',
  'statistics.skills.origin.global': 'global',
  'statistics.skills.origin.agent': 'agent: {detail}',
  'statistics.skills.origin.project': 'project: {detail}',
  'statistics.subview.limits': 'Limits',
  'statistics.limits.note':
    'Live subscription usage, updated every 10 seconds while this tab is visible — nothing is stored.',
  'statistics.limits.loading': 'Loading usage limits…',
  'statistics.limits.loadError': 'Usage limits could not be loaded.',
  'statistics.limits.empty': 'No subscription providers connected.',
  'statistics.limits.unavailable': 'Usage unavailable',
  'statistics.limits.usedPercent': '{percent}% used',
  'statistics.limits.resetsIn': 'Resets in {duration}',

  // First-run onboarding wizard.
  'onboarding.title': 'Set up vBot',
  'onboarding.dismiss': 'Skip for now',
  'onboarding.finishSetup': 'Finish setup',
  'onboarding.finishSetupHint': 'Connect an AI service to start chatting.',
  'onboarding.step.service.kicker': 'Step 1 of 2',
  'onboarding.step.service.title': 'Choose an AI service',
  'onboarding.step.service.subtitle':
    'vBot reaches AI models through a service. Pick one to connect — you can add more later in Settings.',
  'onboarding.hero.badge': 'Recommended to start',
  'onboarding.hero.title': 'OpenRouter',
  'onboarding.hero.description':
    'One account unlocks many models, including free ones — so you can reach a working chat at no cost, without an existing subscription.',
  'onboarding.hero.action': 'Connect OpenRouter',
  'onboarding.subscription.title': 'Already subscribed?',
  'onboarding.subscription.description':
    'Sign in with an existing subscription — no API key needed.',
  'onboarding.subscription.action': 'Sign in with {provider}',
  'onboarding.more.toggle': 'More services',
  'onboarding.more.description': 'Connect another provider with an API key.',
  'onboarding.more.action': 'Connect {provider}',
  'onboarding.step.model.kicker': 'Step 2 of 2',
  'onboarding.step.model.title': 'Choose a model',
  'onboarding.step.model.subtitle':
    'Pick the model this agent will use. You can change it anytime in Agents.',
  'onboarding.model.label': 'Model',
  'onboarding.model.placeholder': 'Select a model',
  'onboarding.model.searchPlaceholder': 'Filter models…',
  'onboarding.model.searchEmpty': 'No models match',
  'onboarding.model.loading': 'Loading models…',
  'onboarding.model.loadError': 'Models could not be loaded.',
  'onboarding.model.empty':
    'No models are available yet. Retry once the model list finishes updating.',
  'onboarding.model.retry': 'Retry',
  'onboarding.model.start': 'Start chatting',
  'onboarding.model.assignError': 'The model could not be assigned.',
  'onboarding.model.back': 'Choose a different service',
  'onboarding.provider.tip.openrouter':
    'Type free in the model search to list models you can use at no cost.',
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

// BCP 47 tag of the active UI language, for Intl formatters. Dates and times
// must follow the app language, not the browser/OS locale — a German OS must
// not inject German month names or comma decimals into the English UI.
export function activeLocaleTag() {
  return activeLocale;
}
