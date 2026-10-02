export default Object.freeze({
  'extensions.pageUnavailable': 'This Extension page is unavailable.',
  'extensions.inputWaiting': '{count} Extension requests need your response.',
  'extensions.reviewInput': 'Review request',
  'extensions.inputTitle': 'Request from {name}',
  'extensions.signInHelp':
    'Open the sign-in page and sign in. vBot completes the sign-in when the browser returns to it. If the browser shows an error page instead, paste its complete address below.',
  'extensions.redirectUrl': 'Redirected address',
  'extensions.sendResponse': 'Send response',
  'extensions.declineInput': 'Decline',
  'extensions.declineHelp': 'Refuse the request.',
  'extensions.cancelInput': 'Cancel request',
  'extensions.cancelHelp': 'End the request without an answer.',
  'extensions.noChoice': 'No choice',
  'extensions.inputTimeZone': 'Time in {zone}.',
  'extensions.inputRequired': 'Enter a value.',
  'extensions.inputChoicesMin': 'Choose at least {count}.',
  'extensions.inputChoicesMax': 'Choose at most {count}.',
  'extensions.inputJson': 'Enter a valid JSON value.',
  'extensions.inputNumber': 'Enter a number.',
  'extensions.inputInteger': 'Enter a whole number.',
  'extensions.inputMinimum': 'Enter {minimum} or more.',
  'extensions.inputMaximum': 'Enter {maximum} or less.',
  'extensions.inputMinLength': 'Enter at least {count} characters.',
  'extensions.inputMaxLength': 'Enter at most {count} characters.',
  'extensions.inputEmail': 'Enter an email address.',
  'extensions.inputUri':
    'Enter a complete address, such as https://example.com.',
  'extensions.inputDate': 'Enter a valid date.',
  'extensions.inputDateTime': 'Enter a valid date and time.',
  'extensions.urlRequest':
    '{name} asks you to open a page in your browser. Check where the address leads before you open it.',
  'extensions.urlConsent':
    'Opening the page accepts the request. Decline refuses it.',
  'extensions.urlAddress': 'Full address',
  'extensions.urlOpensOn': 'Opens on',
  'extensions.urlInsecure':
    'This address does not use HTTPS: others on the network can read or change the page.',
  'extensions.urlInternational':
    'This address contains international characters (xn--), which can imitate the name of another site.',
  'extensions.urlCredentials':
    'This address has text before an @ sign, which can look like a site name. The page opens on the site named under Opens on.',
  'extensions.urlIpAddress':
    'This address names a numeric IP address instead of a site name.',
  'extensions.urlInvalid':
    'The requested address is not a web address and cannot be opened.',
  'extensions.openPage': 'Open page',
  'extensions.openSignIn': 'Open sign-in page',
  'extensions.openFailed':
    'The page could not be opened. Copy the address into your browser instead.',
  'settings.providers.opencode.sharedKey':
    'This Account key is shared by OpenCode Go and Zen. Replacing or removing it affects both. Each connection can be enabled separately.',
  'settings.providers.opencode.sharedKeyShort':
    'Shared by OpenCode Go and Zen.',
  'settings.providers.opencode.goHelp':
    'Uses your OpenCode Go subscription. OpenCode may charge Zen credits if you enabled Use balance in your OpenCode account.',
  'settings.providers.opencode.zenHelp':
    'Paid Models use Zen credits. Free Models are restricted to the OpenCode app and cannot be used in vBot, even with another key.',
  'settings.providers.opencode.removeKey': 'Remove shared key',
  'settings.pages.general': 'General',
  'settings.pages.generalDescription':
    'Display, Session titles, notifications, time zone, and setup.',
  'settings.pages.providersDescription':
    'Connect the services and local runtimes that supply your Models.',
  'settings.pages.voiceDescription':
    'Speaking, listening, live conversations, and voice activation.',
  'settings.pages.memory': 'Memory',
  'settings.pages.memoryDescription':
    'How Agents learn from conversations and find past ones.',
  'settings.pages.tools': 'Tools',
  'settings.pages.toolsDescription':
    'How Agents search and read the web, create media, and delegate work.',
  'settings.pages.integrations': 'Integrations',
  'settings.pages.integrationsDescription':
    'Messaging Channels, Extensions, and MCP connections.',
  'settings.pages.archiveDescription':
    'Restore deleted Agents, Projects and Sessions or delete them for good, and choose how long vBot keeps them.',
  'settings.pages.system': 'System',
  'settings.pages.systemDescription':
    'Server information, connections, and diagnostics.',
  'settings.archive.title': 'Archive',
  'settings.archive.retentionTitle': 'Automatic deletion',
  'settings.archive.automatic': 'Delete archived items automatically',
  'settings.archive.automaticDescription':
    'Deleted Agents, Projects and Sessions wait in the Archive, where you can restore them. When this is off, they stay until you delete them there. Files from older vBot versions, items that may hold folders of your own and items vBot found without a record of when they were archived are never deleted automatically.',
  'settings.archive.days': 'Days in the Archive',
  'settings.archive.daysDescription':
    '1 to 3650 days, counted from when an item was archived. A lower value deletes older items at the next hourly check.',
  'settings.sections.wakeword': 'Wakeword',
  'settings.sections.transcriptionAudio': 'Transcription audio',
  'settings.sections.speechModels': 'Speech models',
  'settings.sections.liveVoice': 'Live voice',
  'settings.sections.liveVoiceShortcut': 'Live voice shortcut',
  'settings.liveShortcut.enabled': 'Global shortcut',
  'settings.liveShortcut.enabledAria': 'Enable the Live voice shortcut',
  'settings.liveShortcut.description':
    'Starts or stops Live voice, even while another app is in front.',
  'settings.liveShortcut.combination': 'Key combination',
  'settings.liveShortcut.combinationHelp':
    'Combine a letter, digit, function key, or Space with Ctrl, Alt, Shift, or Win. F13 to F24 also work alone.\n\nClick the combination, then press the new keys. Escape cancels.',
  'settings.liveShortcut.combinationHelpAria': 'About the key combination',
  'settings.liveShortcut.captureHint':
    'Press the new key combination. Escape cancels.',
  'settings.liveShortcut.capturing': 'Press keys…',
  'settings.liveShortcut.capturingAria': 'Recording a new key combination',
  'settings.liveShortcut.changeAria':
    'Change the key combination, currently {combination}',
  'settings.liveShortcut.space': 'Space',
  'settings.liveShortcut.loadError':
    'The Desktop app did not return the shortcut settings.',
  'settings.liveShortcut.unsupported':
    'Global shortcuts are available in the vBot Desktop app on Windows.',
  'settings.liveShortcut.error.inUse':
    'Another app already uses this key combination. Choose a different one.',
  'settings.liveShortcut.error.invalid':
    'This key combination cannot be used. Combine a letter, digit, function key, or Space with Ctrl, Alt, Shift, or Win.',
  'settings.liveShortcut.error.failed':
    'Windows could not register the shortcut. Choose another key combination or restart the Desktop app.',
  'settings.sections.recall': 'Conversation search',
  'settings.sections.mediaModels': 'Images, video & music',
  'settings.sections.evaluation': 'Evaluation',
  'settings.sections.delegation': 'Sub-Agent limits',
  'settings.agentShortcut.hint':
    'The chat Model, Thinking, and Compaction are configured in',
  'settings.specializedModels.resetOptions': 'Reset options',
  'settings.search.resultCount': 'Matching settings: {count}',
  'settings.agentShortcut.search':
    'Agents → Shared defaults · Model, Thinking, fallbacks and Compaction',
  'settings.title': 'Settings',
  'settings.loading': 'Loading settings…',
  'settings.loadError': 'Settings could not be loaded.',
  'settings.saveError': 'Settings could not be saved.',
  'settings.saveConflict':
    'Some of these settings were changed elsewhere while you edited. The editor now shows the saved values.',
  'settings.sections': 'Settings sections',
  'settings.preferences.title': 'Region & setup',
  'settings.search.results': 'Search results',
  'settings.search.placeholder': 'Search settings…',
  'settings.search.label': 'Search settings',
  'settings.search.noMatches': 'No settings match your search.',
  'settings.search.location': '{page} › {section}',
  'settings.desktop.connection.title': 'Connection',
  'settings.desktop.connection.count': '{count} saved',
  'settings.desktop.connection.help':
    'This Desktop app shows the WebUI of the connected server. Connect switches it to another saved server and reloads it; Sessions and Runs stay on the server that holds them.\n\nSaved servers are remembered only by the Desktop app on this computer.',
  'settings.desktop.connection.loading': 'Loading saved servers…',
  'settings.desktop.connection.loadError': 'Saved servers could not be loaded.',
  'settings.desktop.connection.emptyTitle': 'No saved servers',
  'settings.desktop.connection.emptyDescription':
    'Save a local or remote vBot server to switch this Desktop app to it.',
  'settings.desktop.connection.active': 'Connected',
  'settings.desktop.connection.connect': 'Connect',
  'settings.desktop.connection.connecting': 'Connecting…',
  'settings.desktop.connection.connectError':
    'The Desktop app could not connect to that server.',
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
  'settings.sections.server': 'Server',
  'settings.general.version': 'Version',
  'settings.general.versionRelease': 'Release',
  'settings.general.copyVersion': 'Copy version',
  'settings.general.serverHost': 'Server host',
  'settings.general.dataDirectory': 'Data directory',
  'settings.general.dataDirectoryHelp':
    'The folder on the computer running the vBot server that holds its settings, Agents, Sessions, and logs.',
  'settings.general.copyDataDirectory': 'Copy data directory',
  'settings.general.timezone': 'Time zone',
  'settings.general.timezoneHelp':
    'vBot shows every date and time in this time zone. Agents, Schedules, and Calendar events use it for the current time.\n\nIt starts as the time zone of the computer running the vBot server.',
  'settings.general.timezoneSearch': 'Search time zones…',
  'settings.general.keepAwake': 'Keep computer awake',
  'settings.general.keepAwakeHelp':
    'Keeps the computer running the vBot server from going to sleep on its own, so Channels such as Telegram and scheduled work stay reachable. You can still put it to sleep yourself.\n\nOnly Windows supports this; other systems ignore it.',
  'settings.general.setupGuide': 'Setup guide',
  'settings.general.setupGuideDescription':
    'Connect a Provider and choose a Model step by step.',
  'settings.general.setupGuideOpen': 'Open',
  'settings.general.setupGuideAction': 'Open setup guide',
  'settings.general.clients.title': 'Connected clients',
  'settings.general.clients.loading': 'Loading connected clients…',
  'settings.general.clients.empty': 'No apps connected.',
  'settings.general.clients.loadError':
    'Connected clients could not be loaded.',
  'settings.general.clients.thisWindow': 'This window',
  'settings.general.clients.connectedAt': 'Connected {time}',
  'settings.general.clients.accessor.browser': 'Browser',
  'settings.general.clients.accessor.desktop': 'Desktop',
  'settings.general.clients.accessor.tray': 'vBot tray',
  'settings.general.clients.accessor.unknown': 'Unknown',
  'settings.defaults.model': 'Model',
  'settings.defaults.modelDescription': 'Used when an agent model is empty.',
  'settings.defaults.fallbackModels': 'Fallback models',
  'settings.defaults.fallbackModelDescription':
    'Used when an agent fallback chain is empty.',
  'settings.defaults.temperature': 'Temperature',
  'settings.defaults.temperatureDescription':
    'Used when an agent temperature is unset.',
  'settings.defaults.thinkingEffort': 'Thinking effort',
  'settings.defaults.thinkingEffortDescription':
    'Used when an agent thinking effort is unset.',
  'settings.defaults.thinkingEffortHelp':
    'How much internal reasoning the Model may spend before answering. Every Agent and Project without its own Thinking effort uses this value. With either — option, vBot sends no effort and the Provider decides.',
  'settings.defaults.temperatureHelp':
    'Sampling randomness, typically 0–2. Every Agent and Project without its own temperature uses this value. When empty, a Model’s recommended temperature applies if its catalog entry has one, otherwise the Provider default.',
  'settings.defaults.noThinkingEffort': '— (no default)',
  'settings.defaults.providerThinkingEffortDefault': '— (provider default)',
  'settings.defaults.noModelDefault': '— (no default)',
  'settings.defaults.noFallbackModelDefault': '— (no default)',
  'settings.skills.defaultDirectory': 'Default skill directory',
  'settings.skills.defaultDirectoryDescription':
    'Always scanned from the vBot data directory and kept read-only here.',
  'settings.skills.extraDirectories': 'Additional skill directories',
  'settings.skills.extraDirectoriesDescription':
    'Enter a folder on the computer running vBot that contains Skill folders with a SKILL.md file. Skills stay in that folder and join the global library; each Agent’s Skill selection still applies.',
  'settings.skills.pathPlaceholder': 'C:/path/to/skills',
  'settings.skills.addDirectory': 'Add directory',
  'settings.skills.removeDirectory': 'Remove skill directory {path}',
  'settings.skills.emptyDirectories':
    'No additional skill directories configured.',
  'settings.skills.scopeGlobal': 'Global skills',
  'settings.skills.scopeAgent': '{name} (private)',
  'settings.skills.newSkill': 'New skill',
  'settings.skills.nameLabel': 'Skill name',
  'settings.skills.contentLabel': 'SKILL.md content',
  'settings.skills.namePlaceholder': 'skill-name',
  'settings.skills.create': 'Create skill',
  'settings.skills.created': 'Skill created.',
  'settings.skills.createError': 'Skill could not be created.',
  'settings.skills.saved': 'Skill saved.',
  'settings.skills.contentSaveError': 'Skill could not be saved.',
  'settings.skills.deleted': 'Skill moved to Archived.',
  'settings.skills.deleteError': 'Skill could not be deleted.',
  'settings.skills.deleteConfirmTitle': 'Delete skill',
  'settings.subagents.maxDepth': 'Maximum nesting depth',
  'settings.subagents.maxDepthHelp':
    'Sub-Agents can start Sub-Agents of their own. This sets how many levels deep that can go; at the limit, a Sub-Agent has to do the work itself. Default: 4.',
  'settings.subagents.maxPerTurn': 'Maximum Sub-Agents per Run',
  'settings.subagents.maxPerTurnHelp':
    'The most Sub-Agents an Agent may start during one Run. Further requests in that Run are refused, and the Agent is told to wait for results or do the work itself. Default: 8.',
  'settings.subagents.timeoutMinutes': 'Nested Sub-Agent timeout',
  'settings.subagents.timeoutMinutesDescription':
    'Minutes a Sub-Agent waits for its own Sub-Agent.',
  'settings.subagents.timeoutMinutesHelp':
    'When a Sub-Agent starts a Sub-Agent of its own, it waits for the result. After this many minutes the nested Sub-Agent is cancelled and reported as failed. Default: 60.\n\nSub-Agents started directly by the Agent you talk to run in the background and have no time limit.',
  'settings.reflection.title': 'Reflection',
  'settings.reflection.enabled': 'Background reflection',
  'settings.reflection.enabledDescription':
    'Agents review finished conversations to update Memory and Skills.',
  'settings.reflection.enabledHelp':
    'From time to time after a Run, the Agent reviews the conversation in a separate copy and saves lasting facts to Memory and reusable procedures as Skills. The original conversation is never changed.\n\nReviews are ordinary Runs with the Agent’s own Model, so they use tokens. Only Agents that can use the memory Tool are reviewed; Sub-Agent conversations are skipped.\n\nEach review is kept as its own Session, so you can see what it changed. Type /reflect in a chat to start a review yourself.',
  'settings.reflection.memoryInterval': 'Memory review interval',
  'settings.reflection.memoryIntervalDescription':
    'Your messages per conversation between Memory reviews.',
  'settings.reflection.memoryIntervalHelp':
    'A Memory review becomes due after this many completed Runs in one conversation, usually one per message you send. When the Agent saves to Memory on its own, the count starts over. Default: 10.',
  'settings.reflection.skillInterval': 'Skill review interval',
  'settings.reflection.skillIntervalDescription':
    'Agent steps per conversation between Skill reviews.',
  'settings.reflection.skillIntervalHelp':
    'A Skill review becomes due after this many Agent steps in one conversation. Every Model response is one step, including each round of Tool calls, so one message can add several steps. When the Agent edits a Skill on its own, the count starts over. Default: 10.',
  'settings.notifications.title': 'Desktop notifications',
  'settings.notifications.intro': 'Shown by the vBot tray app on Windows.',
  'settings.notifications.help':
    'The vBot tray app of an installed vBot on Windows shows these notifications.\n\nRun completed and Run failed cover Agent Runs in your Sessions, not Sub-Agent, Channel, or other background work. They are skipped while a vBot window already shows that Session.\n\nAutomation failed covers Schedules and Calendar actions that fail. Update finished reports how a vBot update ended, successfully or not. Server stopped appears when the server the tray started stops unexpectedly.',
  'settings.notifications.runCompleted': 'Run completed',
  'settings.notifications.runFailed': 'Run failed',
  'settings.notifications.automationFailed': 'Automation failed',
  'settings.notifications.updateResult': 'Update finished',
  'settings.notifications.serverStopped': 'Server stopped',
  'settings.compaction.title': 'Compaction',
  'settings.compaction.summaryModelPlaceholder': 'Active agent model',
  'settings.recall.method': 'Search method',
  'settings.recall.methodHelp':
    'Agents look through earlier conversations with the session_search Tool.\n\nKeywords finds the words searched for, using a fast local index. Keywords and meaning also finds passages that say the same thing in other words, so a search for “vehicles” also finds “cars”. Meaning only ranks by meaning alone; it can miss exact names and numbers that keyword search finds.\n\nSearching by meaning needs an embedding model. vBot builds its search index in the background shortly after conversations and keeps it up to date. Until the index has caught up, searches return every keyword match plus the meaning matches among the passages indexed so far.\n\nExtensions can add search methods of their own.',
  'settings.recall.methodRecommended': 'Recommended',
  'settings.recall.methodExtension': 'A search method added by an Extension.',
  'settings.recall.backends.sqlite_fts': 'Keywords',
  'settings.recall.backends.hybrid': 'Keywords and meaning',
  'settings.recall.backends.vector': 'Meaning only',
  'settings.recall.backendDescriptions.sqlite_fts':
    'Finds conversations that contain the searched words.',
  'settings.recall.backendDescriptions.hybrid':
    'Also finds conversations that say the same thing in other words.',
  'settings.recall.backendDescriptions.vector':
    'Ranks by meaning alone; can miss exact names and numbers.',
  'settings.recall.model.loading': 'Loading embedding models…',
  'settings.recall.model.placeholder': 'Choose a model',
  'settings.recall.model.search': 'Search embedding models',
  'settings.recall.model.none': 'No embedding models found',
  'settings.recall.model.groupLocal': 'On this computer',
  'settings.recall.model.groupCloud': 'Cloud',
  'settings.recall.model.groupSaved': 'No longer offered',
  'settings.recall.model.free': 'Free',
  'settings.recall.model.price': '{price} / 1M tokens',
  'settings.recall.model.notInstalled': 'Not installed',
  'settings.recall.model.notInstalledSize': 'Not installed · {size}',
  'settings.recall.model.unavailable': 'Unavailable',
  'settings.recall.model.englishOnly': 'English only',
  'settings.recall.model.choose':
    'Choose a model to search by meaning. Connect a Provider that offers embedding models if the list is empty.',
  'settings.recall.model.chooseRecommended':
    'Choose a model to search by meaning. {model} is a good start.',
  'settings.recall.model.notOffered':
    'This model is no longer offered. Choose another one.',
  'settings.recall.model.installNeeded':
    'This model is not installed yet. Choose it again to install it.',
  'settings.recall.model.privacyLocal':
    'Runs on this computer, free. Conversation text stays here.',
  'settings.recall.model.privacyLocalRuntime':
    'Runs in {provider} on this computer. Conversation text stays here.',
  'settings.recall.model.privacyProvider':
    'Conversation text is sent to {provider} to build the search index.',
  'settings.recall.model.privacyProviderPrice':
    'Conversation text is sent to {provider} to build the search index, at {price} per 1M tokens.',
  'settings.recall.index': 'Search index',
  'settings.recall.indexHelp':
    'The passages of your conversations that the embedding model has turned into the search index. vBot adds new passages in the background shortly after each conversation and checks regularly for other changes.\n\nWaiting passages are not indexed yet. Skipped passages were rejected by the embedding model and stay out of the index until it is rebuilt. Spent counts the indexing since the model was chosen or the index was last rebuilt; costs are estimated from the model’s listed price when the Provider does not report them.',
  'settings.recall.status.loading': 'Checking the search index…',
  'settings.recall.status.loadError':
    'The search index status could not be loaded.',
  'settings.recall.status.empty': 'No passages to index yet',
  'settings.recall.status.complete': 'All passages indexed ({count})',
  'settings.recall.status.progress': 'Indexed {indexed} of {total} passages',
  'settings.recall.status.indexing': 'Indexing: {indexed} of {total} passages',
  'settings.recall.status.waiting': 'about {tokens} tokens waiting',
  'settings.recall.status.waitingCost':
    'about {tokens} tokens waiting (~{cost})',
  'settings.recall.status.skipped': '{count} skipped',
  'settings.recall.status.spent': '{cost} spent',
  'settings.recall.status.spentTokens': '{tokens} tokens spent',
  'settings.recall.status.retrying': 'Retrying {when}.',
  'settings.recall.status.nextAttempt': 'Next attempt {when}.',
  'settings.recall.indexError.generic': 'Indexing failed.',
  'settings.recall.indexError.provider_unavailable':
    'The embedding Provider is unreachable or failing for now.',
  'settings.recall.indexError.provider_rate_limited':
    'The embedding Provider is limiting requests.',
  'settings.recall.indexError.provider_auth':
    'The embedding Provider rejected the credentials. Check its connection under Providers.',
  'settings.recall.indexError.embedding_unusable':
    'The chosen embedding model cannot be used. Choose another model or check its Provider.',
  'settings.recall.indexError.embedding_model_unavailable':
    'The Provider does not offer the chosen embedding model. Choose another model.',
  'settings.recall.indexError.embedding_failed':
    'The embedding Provider returned an unusable response.',
  'settings.recall.indexError.provider_rejected':
    'The embedding Provider rejected some texts.',
  'settings.recall.indexError.context_overflow':
    'Some texts are longer than the embedding model accepts.',
  'settings.recall.indexError.index_unavailable':
    'The search index could not be read or written.',
  'settings.recall.indexError.space_unstable':
    'The embedding model kept changing during indexing.',
  'settings.recall.indexError.local_model_missing':
    'The local embedding model is not installed. Choose it under Embedding model to install it, or choose another model.',
  'settings.recall.indexError.local_engine_failed':
    'The local embedding model failed to run on this computer.',
  'settings.recall.status.etaSoon': 'less than a minute left',
  'settings.recall.status.eta': 'about {duration} left',
  'settings.recall.rebuild': 'Rebuild',
  'settings.recall.rebuildTitle': 'Rebuild the search index?',
  'settings.recall.rebuildBody':
    'Indexes every conversation again from scratch. This is only needed when search by meaning finds poor results; after a model change vBot rebuilds the index on its own. A cloud embedding model bills its Provider again for all conversation text.',
  'settings.recall.rebuildConfirm': 'Rebuild index',
  'settings.recall.rebuildError': 'The search index could not be rebuilt.',
  'settings.recall.modelOptions': 'Model options',
  'settings.localModel.state.checking': 'Checking whether it is installed…',
  'settings.localModel.state.missing': 'Not set up on this computer yet.',
  'settings.localModel.state.ready': 'Installed.',
  'settings.localModel.state.restart_required':
    'Installed. Restart the vBot server to use it.',
  'settings.localModel.dialogTitle': 'Install {model}',
  'settings.localModel.dialogIntro':
    'It runs on this computer, so conversation text never leaves it. vBot downloads it once.',
  'settings.localModel.downloadSize': '{size} download',
  'settings.localModel.license': '{license} license',
  'settings.localModel.progress': '{completed} of {total}',
  'settings.localModel.progressLabel': 'Download progress',
  'settings.activity.title': 'Background activity',
  'settings.activity.empty': 'No background activity.',
  'settings.activity.task.speech_to_text': 'Speech to text: {model}',
  'settings.activity.task.text_to_speech': 'Text to speech: {model}',
  'settings.activity.task.text_embedding': 'Conversation search: {model}',
  'settings.activity.whatsapp': 'WhatsApp support: {channel}',
  'settings.activity.whatsappFailed':
    'WhatsApp support could not be installed. Open the Channel to try again.',
  'settings.activity.recallIndex': 'Conversation search index',
  'settings.activity.indexing': 'Indexing conversations…',
  'settings.activity.retrying':
    'Indexing paused after a problem; it retries automatically.',
  'settings.activity.downloadingModel': 'Downloading the model…',
  'settings.activity.completed': 'Finished.',
  'settings.activity.progressLabel': 'Progress of {title}',
  'settings.activity.open': 'Open',
  'settings.activity.cancel': 'Cancel',
  'settings.activity.dismiss': 'Dismiss',
  'settings.activity.actionFailed':
    'The server could not be reached. Try again.',
  'settings.localModel.install': 'Install',
  'settings.localModel.installing': 'Installing…',
  'settings.localModel.retry': 'Try again',
  'settings.localModel.checkAgain': 'Check again',
  'settings.localModel.phase.checking': 'Checking this computer…',
  'settings.localModel.phase.queued':
    'Waiting for another installation to finish…',
  'settings.localModel.phase.python': 'Preparing the local model runtime…',
  'settings.localModel.phase.gpu': 'Preparing GPU support…',
  'settings.localModel.phase.downloading':
    'Downloading the local model runtime…',
  'settings.localModel.phase.installing': 'Installing the local model runtime…',
  'settings.localModel.phase.verifying': 'Checking the installation…',
  'settings.localModel.downloadingModel':
    'Downloading the model. You can close this dialog; the download continues.',
  'settings.localModel.error.connection':
    'The server could not be reached. Check again to see the installation status.',
  'settings.localModel.error.install_failed':
    'Installation failed. Check the server’s internet connection, free disk space and write permissions, then try again.',
  'settings.localModel.error.download_failed':
    'The model download failed. Check the server’s internet connection, then try again.',
  'settings.localModel.error.checksum_mismatch':
    'The downloaded model files were damaged. Try again.',
  'settings.localModel.error.insufficient_space':
    'This computer does not have enough free disk space for the model. Free some space, then try again.',
  'settings.localModel.error.verification_failed':
    'The installed model could not start. Try the installation again.',
  'settings.localModel.error.setup_unavailable':
    'Setup could not access the vBot installation. Check its files and write permissions, then try again.',
  'settings.localModel.error.timeout':
    'Installation took too long. Check the server’s internet connection, then try again.',
  'settings.localModel.error.interrupted':
    'Installation was interrupted. Try again to finish it.',
  'settings.webFetch.direct': 'Direct (no service)',
  'settings.webFetch.fallback': 'Only when direct reading fails',
  'settings.webFetch.prefer': 'Prefer this service',
  'settings.webFetch.provider': 'Extraction service',
  'settings.webFetch.description':
    'Optional service for sites that block bots or need JavaScript.',
  'settings.webFetch.providerHelp':
    'By default, vBot reads web pages itself when an Agent opens one (web_fetch Tool). An extraction service can read pages that block automated visitors or only show their content with JavaScript.\n\nThe service receives every URL it reads and may charge per page; free allowances and prices vary. Paging through or searching a page that was already read makes no new service request.',
  'settings.webFetch.mode': 'When to use it',
  'settings.webFetch.modeHelp':
    'Only when direct reading fails: vBot reads the page itself first and uses the service for blocked, failed or unreadable pages.\n\nPrefer this service: the service reads pages first; if it fails, vBot reads the page itself. Images and documents are always read directly first.',
  'settings.webFetch.cost':
    'The service receives the URLs it reads and may charge per page.',
  'settings.webFetch.pricing': 'Pricing',
  'settings.webFetch.title': 'Web page reading',
  'settings.webSearch.title': 'Web search',
  'settings.webSearch.provider': 'Search provider',
  'settings.webSearch.providerHelp':
    'The service Agents use when they search the web (web_search Tool). The choice applies to every Agent.\n\nDuckDuckGo needs no API key but may block frequent searches. SearXNG needs an instance you run or can reach. The other services need an API key and may charge per search.',
  'settings.webSearch.providers.brave': 'Brave Search',
  'settings.webSearch.providers.duckduckgo': 'DuckDuckGo',
  'settings.webSearch.providers.tavily': 'Tavily',
  'settings.webSearch.providers.exa': 'Exa',
  'settings.webSearch.providers.serper': 'Serper',
  'settings.webSearch.providers.firecrawl': 'Firecrawl',
  'settings.webSearch.providers.perplexity': 'Perplexity',
  'settings.webSearch.providers.searxng': 'SearXNG',
  'settings.webSearch.providers.parallel': 'Parallel',
  'settings.webSearch.defaultCount': 'Results per search',
  'settings.webSearch.defaultCountHelp':
    'How many results a web search returns when the Agent does not ask for a specific number: 1 to 20, default 12.\n\nMore results give the Agent more to choose from but take more room in its context.',
  'settings.webSearch.searxngBaseUrl': 'SearXNG URL',
  'settings.webSearch.searxngBaseUrlDescription':
    'A SearXNG instance you run yourself or can reach.',
  'settings.webSearch.searxngBaseUrlHelp':
    'SearXNG is a free, self-hosted metasearch engine; vBot does not include one. Enter the address of your own instance or of one you can reach.\n\nThe instance must allow JSON results (add json to search.formats in its settings.yml); otherwise every search fails with HTTP 403.',
  'settings.webSearch.searxngBaseUrlPlaceholder': 'http://localhost:8888',
  'settings.serviceKey.label': 'API key',
  'settings.serviceKey.help':
    'vBot keeps the key on its server and never shows it again. A key set in the server environment takes precedence over a saved one and can only be changed there.',
  'settings.serviceKey.helpFile':
    'Saved keys go in the .env file in {path}. Keys can also be added to that file by hand; restart the vBot server after editing it.',
  'settings.serviceKey.helpShared':
    'Web search and Web page reading use the same {variable} key, so a change here applies to both.',
  'settings.serviceKey.set': 'Set',
  'settings.serviceKey.optionSet': 'API key set',
  'settings.serviceKey.optionMissing': 'API key missing',
  'settings.serviceKey.missing': 'Missing',
  'settings.serviceKey.stateSaved':
    'Saved in the data directory as {variable}.',
  'settings.serviceKey.stateMissing':
    'This service needs an API key ({variable}).',
  'settings.serviceKey.stateEnvironment':
    'Set in the server environment ({variable}); change it there.',
  'settings.serviceKey.stateEnvironmentEmpty':
    '{variable} is empty in the server environment, which overrides a saved key. Set it there, or remove it and restart vBot.',
  'settings.serviceKey.replace': 'Replace',
  'settings.serviceKey.placeholder': 'Paste the API key…',
  'settings.serviceKey.inputLabel': 'New API key ({variable})',
  'settings.serviceKey.saveSuccess': 'API key saved.',
  'settings.serviceKey.removeSuccess': 'API key removed.',
  'settings.localSpeech.memoryTitle': 'Local speech memory',
  'settings.localSpeech.memoryLoaded': 'Loaded in memory',
  'settings.localSpeech.memoryEmpty': 'Not loaded',
  'settings.localSpeech.memoryBusy':
    'This model is busy. Unload becomes available when processing finishes.',
  'settings.localSpeech.memoryChecking': 'Checking loaded speech model…',
  'settings.localSpeech.memoryHelp':
    'Unload models individually to free RAM and GPU memory. Other models stay loaded. Downloads stay on disk for the next use.',
  'settings.localSpeech.unloadButton': 'Unload from memory',
  'settings.localSpeech.unloadAria': 'Unload {model} from memory',
  'settings.localSpeech.unloading': 'Releasing speech model memory…',
  'settings.localSpeech.memoryError':
    'Could not check speech memory. Reconnecting…',
  'settings.localSpeech.unloadError':
    'Could not unload the speech model. Try again.',
  'settings.localSpeech.ttsReady':
    'Installed on the vBot server and works offline. The first preview or Tool request loads the model into memory, which can take a moment.',
  'settings.localSpeech.ttsMissing':
    'Not installed on the vBot server yet. Installing downloads this voice model and the engine that runs it; afterwards it works offline.',
  'settings.localSpeech.phase.queued':
    'Waiting for another speech installation to finish…',
  'settings.localSpeech.phase.python':
    'Preparing the local speech environment…',
  'settings.localSpeech.previewText':
    'Hello! This is a preview of my local voice.',
  'settings.localSpeech.previewLabel': 'Text for voice preview',
  'settings.localSpeech.previewButton': 'Generate voice preview',
  'settings.localSpeech.cancelPreview': 'Cancel',
  'settings.localSpeech.previewAudio': 'Voice preview',
  'settings.localSpeech.previewFailed':
    'Voice preview failed. Please try again.',
  'settings.localSpeech.options.voice.label': 'Voice',
  'settings.localSpeech.options.instructions.label': 'Speaking instructions',
  'settings.localSpeech.options.instructions.help':
    'Optional style instructions, such as a calm or cheerful voice.',
  'settings.localSpeech.options.exaggeration.label': 'Expressiveness',
  'settings.localSpeech.options.cfg_weight.label': 'Guidance',
  'settings.localSpeech.ready':
    'Installed on the vBot server and works offline. The first transcription loads the model into memory, which can take a moment.',
  'settings.localSpeech.state.checking': 'Checking local speech support…',
  'settings.localSpeech.state.missing':
    'Not installed on the vBot server yet. Installing downloads this speech-to-text model and the engine that runs it; afterwards it works offline.',
  'settings.localSpeech.state.restart_required':
    'Installation complete. Restart the server to enable local speech recognition. Active Runs will be interrupted.',
  'settings.localSpeech.state.restarting':
    'Restarting the server. Reconnecting automatically…',
  'settings.localSpeech.installButton': 'Install',
  'settings.localSpeech.installingButton': 'Installing…',
  'settings.localSpeech.restartButton': 'Restart server',
  'settings.localSpeech.restartingButton': 'Restarting…',
  'settings.localSpeech.retry': 'Try again',
  'settings.localSpeech.checkAgain': 'Check again',
  'settings.localSpeech.phase.checking': 'Checking the server environment…',
  'settings.localSpeech.phase.gpu': 'Preparing GPU support…',
  'settings.localSpeech.phase.downloading':
    'Downloading the speech engine. This can take several minutes; you can leave this page.',
  'settings.localSpeech.phase.installing': 'Installing the speech engine…',
  'settings.localSpeech.phase.verifying':
    'Checking the installed speech engine…',
  'settings.localSpeech.downloadingModel':
    'Downloading the model. You can leave this page; the download continues.',
  'settings.localSpeech.error.connection':
    'The server could not be reached. Check the connection to see the current installation status.',
  'settings.localSpeech.error.install_failed':
    'Installation failed. Check the server’s internet connection, available storage and write permissions, then try again.',
  'settings.localSpeech.error.pip_unavailable':
    'The server’s Python package installer is unavailable. Repair the vBot installation, then try again.',
  'settings.localSpeech.error.setup_unavailable':
    'Setup could not access the vBot installation. Check its files and write permissions, then try again.',
  'settings.localSpeech.error.verification_failed':
    'The installed speech engine could not start. Try the installation again.',
  'settings.localSpeech.error.download_failed':
    'The model download failed. Check the server’s internet connection, then try again; the finished part is kept.',
  'settings.localSpeech.error.checksum_mismatch':
    'The downloaded model files were damaged. Try again.',
  'settings.localSpeech.error.insufficient_space':
    'The vBot server does not have enough free disk space for this model. Free some space, then try again.',
  'settings.localSpeech.error.gpu_unavailable':
    'The GPU could not be used after installation. Update the server’s graphics driver, then try again.',
  'settings.localSpeech.error.timeout':
    'Installation took too long. Check the server’s internet connection, then try again; completed downloads can be reused.',
  'settings.localSpeech.error.interrupted':
    'Installation was interrupted. Try again to finish setup.',
  'settings.localSpeech.error.setup_not_finished':
    'Finish the installation before restarting the server.',
  'settings.localSpeech.error.restart_unavailable':
    'The server could not restart automatically. Restart it using the application that started it.',
  'settings.localSpeech.error.restart_timeout':
    'The server has not reconnected yet. Check whether it is running, then check again.',
  'settings.localSpeech.options.device.label': 'Device',
  'settings.localSpeech.options.dtype.label': 'Precision',
  'settings.localSpeech.options.model_path.label': 'Model directory',
  'settings.localSpeech.options.model_path.help':
    'Optional directory on the vBot server with a Transformers model of this engine’s architecture, loaded instead of the installed model. Leave empty to use the installed model.',
  'settings.localSpeech.options.language.label': 'Language',
  'settings.localSpeech.options.language.help':
    'Leave empty for automatic detection, or enter a language code such as de or en.',
  'settings.localSpeech.options.prompt.label': 'Vocabulary and context',
  'settings.localSpeech.options.prompt.help':
    'Optional names or terminology to help recognize your recording.',
  'settings.localSpeech.choices.auto': 'Automatic',
  'settings.specializedModels.loading': 'Loading specialized model targets…',
  'settings.specializedModels.loadError':
    'Specialized model targets could not be loaded.',
  'settings.specializedModels.optionsLoadError':
    'Model options could not be loaded.',
  'settings.specializedModels.speechToText': 'Speech to text',
  'settings.specializedModels.speechToTextHelp':
    'Transcribes what you say into the Chat or Terminal microphone and the commands spoken after a wake phrase. Audio attachments are also transcribed with it when the Agent’s Model cannot take audio.\n\nThe recording format is set under Transcription audio at the end of this page.',
  'settings.specializedModels.textToSpeech': 'Text to speech',
  'settings.specializedModels.textToSpeechHelp':
    'Speaks text aloud when an Agent uses the text_to_speech Tool.',
  'settings.specializedModels.liveVoice': 'Live voice',
  'settings.specializedModels.liveVoiceModel': 'Voice model',
  'settings.specializedModels.liveVoiceDescription':
    'Live voice appears in the sidebar once a Model is chosen.',
  'settings.specializedModels.liveVoiceHelp':
    'The realtime Model you talk with in Live voice.\n\nSome voice Models hand work in the app to a backend Model. Choose it in the options that appear once the voice Model is set.\n\nThe Provider bills voice time, including pauses, and backend requests separately.',
  'settings.specializedModels.imageUnderstanding': 'Image understanding',
  'settings.specializedModels.imageUnderstandingDescription':
    'Describes images for Agents whose Model cannot see them.',
  'settings.specializedModels.imageUnderstandingHelp':
    'Used by the analyze_image Tool. Agents whose Model cannot see images can use it; Agents with vision only when it is enabled in their Tool settings.\n\nThe images are sent to this Model’s Provider.',
  'settings.specializedModels.imageGeneration': 'Image generation',
  'settings.specializedModels.imageGenerationHelp':
    'Used by the image_generation Tool. Agents can also edit existing images when this Model accepts images as input.\n\nThe options below apply to every request; an Agent can set only the aspect ratio and resolution per request.',
  'settings.specializedModels.videoGeneration': 'Video generation',
  'settings.specializedModels.videoGenerationHelp':
    'Used by the generate_video Tool.',
  'settings.specializedModels.musicGeneration': 'Music generation',
  'settings.specializedModels.musicGenerationHelp':
    'Used by the generate_music Tool.',
  'settings.specializedModels.embeddingModel': 'Embedding model',
  'settings.specializedModels.embeddingModelHelp':
    'Turns conversation text into the search index for searching by meaning, and embeds each search query. Models on this computer are listed first, then the embedding models your Providers offer; recommended ones come first in each group. A model on this computer that is not installed yet is installed once when you choose it.\n\nChanging the model rebuilds the index from all stored conversations. That takes a while and, with a cloud model, adds Provider usage.',
  'settings.specializedModels.noTarget': 'Not configured',
  'settings.specializedModels.customTarget': 'Custom target: {target}',
  'settings.specializedModels.aboutAria': 'About {name}',
  'settings.specializedModels.resetOptionsAria': 'Reset options for {task}',
  'settings.specializedModels.jsonPlaceholder':
    'e.g. [{"text":"hello","bbox":[[0,0],[1,0],[1,1],[0,1]]}]',
  'settings.specializedModels.jsonInvalid': 'Invalid JSON: {error}',
  'settings.specializedModels.decision': 'Decision model',
  'settings.specializedModels.decisionHelp':
    'Makes the structured judgments of the evaluate Tool and of Jev experiments.',
  'settings.providers.title': 'Providers',
  'settings.providers.noneConnected':
    'No providers connected yet. Add one to make its models available.',
  'settings.providers.modelCount': '{count} Models',
  'settings.providers.modelCountOne': '1 Model',
  'settings.providers.billingInfoAria': 'Billing for {provider}',
  'settings.providers.unreachableHint':
    'Start the local server; its Models appear automatically.',
  'settings.providers.endpoint': 'Endpoint',
  'settings.providers.modelDb.title': 'Model DB',
  'settings.providers.modelDb.description':
    'Fetch new Models after a Provider releases them.',
  'settings.providers.modelDb.help':
    'Fetches the current Model lists from your connected Providers and the public Model catalog. Run it when a Provider ships new Models.\n\nYour hand-maintained Model overrides are never changed.',
  'settings.providers.refreshModels': 'Update',
  'settings.providers.refreshModelsAria': 'Update Model DB',
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
  'settings.providers.connectedHint': 'Enabled and ready to send requests.',
  'settings.providers.notReachableChipHint':
    'vBot could not reach this Connection’s server at its last check.',
  'settings.providers.notUsableChipHint':
    'No account has a usable credential: a key is empty or a sign-in is no longer valid.',
  'settings.providers.disabledChipHint':
    'Turned off: vBot offers none of its Models and sends it no requests.',
  'settings.providers.enable': 'Enable',
  'settings.providers.enableAria': 'Enable connection {id}',
  'settings.providers.disable': 'Disable',
  'settings.providers.disableAria': 'Disable connection {id}',
  'settings.providers.detailsAria': 'Details for {id}',
  'settings.providers.disabledDescription':
    'Not used or probed until you enable it.',
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
  'settings.providers.oauthTokenHelp':
    'vBot reads this sign-in token from the process environment or the data directory .env file; it cannot be changed here.',
  'settings.providers.localContext.title': 'Context windows',
  'settings.providers.localContext.help':
    'The context window vBot budgets against and requests from the local server per call.\n\nLeave a field empty to use the default of 32k tokens, capped at the Model maximum.',
  'settings.providers.localContext.inputLabel': 'Context window for {model}',
  'settings.providers.localContext.maxHint': 'max {max}',
  'settings.providers.localContext.invalidValue':
    'Context window must be a positive whole number',
  'settings.providers.openrouter.title': 'Routing',
  'settings.providers.openrouter.help':
    'Choose which upstream providers OpenRouter may use. vBot sends a stable Session identifier so OpenRouter can apply Sticky Routing.\n\nSticky Routing is best effort. To prevent provider switches, allow one exact endpoint and turn provider fallbacks off.',
  'settings.providers.openrouter.helpAria': 'About OpenRouter routing',
  'settings.providers.openrouter.summary.automatic': 'Automatic',
  'settings.providers.openrouter.summary.allowed': 'Allowed providers only',
  'settings.providers.openrouter.summary.ordered': 'Preferred order',
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
    'A manual order overrides Sticky Routing and turns off automatic cache affinity.',
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
  'settings.providers.openrouter.fallbacksDescription':
    'When off, OpenRouter fails the request instead of using a backup.',
  'settings.providers.openrouter.fallbacksAria':
    'Allow OpenRouter provider fallbacks',
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
  'settings.providers.accounts.source.processEnv': 'Environment variable',
  'settings.providers.accounts.source.dataDir': 'Data directory .env',
  'settings.providers.accounts.source.oauth': 'OAuth sign-in',
  'settings.providers.accounts.variable': 'Variable',
  'settings.providers.accounts.sourceHint.processEnv':
    'Read from the environment of the vBot server process. Change or remove it where the server is started.',
  'settings.providers.accounts.sourceHint.dataDir':
    'Stored in the .env file of vBot’s data directory on the server.',
  'settings.providers.accounts.sourceHint.oauth':
    'Signed in with the Provider; vBot stores and refreshes the token.',
  'settings.providers.accounts.notUsableKey':
    'Its credential variable is empty.',
  'settings.providers.accounts.notUsableOAuth':
    'Its saved sign-in is no longer valid.',
  'settings.providers.accounts.addButton': 'Add account…',
  'settings.providers.accounts.nameLabel': 'Account',
  'settings.providers.accounts.nameHint':
    'Optional. Only needed when you add more than one account.',
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
  'settings.providers.add.localUnreachable':
    '{provider} was added, but is not reachable.',
  'settings.providers.add.localSuccess': '{provider} added successfully.',
  'settings.providers.add.localError': 'Provider could not be added.',
  'settings.providers.add.methodLocal': 'Local',
  'settings.providers.add.methodLocalDescription':
    'Connect to the local endpoint without credentials.',
  'settings.providers.add.localIntro':
    'Add the local endpoint. Models are discovered now and loaded only when used.',
  'settings.providers.add.localSubmit': 'Add provider',
  'settings.providers.custom.addButton': 'Add custom',
  'settings.providers.custom.addTitle': 'Add Custom Provider',
  'settings.providers.custom.editTitle': 'Edit Custom Provider',
  'settings.providers.custom.intro':
    'Connect an OpenAI-compatible endpoint you control and describe its Models.',
  'settings.providers.custom.id': 'Provider id',
  'settings.providers.custom.idHint':
    'Stable id used in Model references, for example local-ai.',
  'settings.providers.custom.name': 'Name',
  'settings.providers.custom.adapter': 'Adapter',
  'settings.providers.custom.adapterOpenAiCompatible': 'OpenAI compatible',
  'settings.providers.custom.auth': 'Authentication',
  'settings.providers.custom.authApiKey': 'Bearer API key',
  'settings.providers.custom.authNone': 'No API key',
  'settings.providers.custom.baseUrl': 'Endpoint URL',
  'settings.providers.custom.baseUrlHint':
    'Include the API prefix, for example /v1.',
  'settings.providers.custom.baseUrlPlaceholder': 'http://127.0.0.1:8080/v1',
  'settings.providers.custom.modelsEndpoint': 'Model discovery path',
  'settings.providers.custom.modelsEndpointHint':
    'Optional OpenAI-compatible path. Leave empty to use manual Models only.',
  'settings.providers.custom.modelsEndpointPlaceholder': '/models',
  'settings.providers.custom.apiKey': 'API key (optional)',
  'settings.providers.custom.replaceApiKey': 'Replace API key (optional)',
  'settings.providers.custom.apiKeyHint':
    'Stored in the data directory .env. Leave empty to keep the current key.',
  'settings.providers.custom.modelsTitle': 'Manual Models',
  'settings.providers.custom.modelsHint':
    'Manual facts override discovered Models with the same wire id.',
  'settings.providers.custom.addModel': 'Add Model',
  'settings.providers.custom.noModels':
    'No manual Models. Use discovery or add one here.',
  'settings.providers.custom.modelId': 'Wire id',
  'settings.providers.custom.modelNumber': 'Model {number}',
  'settings.providers.custom.modelName': 'Display name',
  'settings.providers.custom.contextWindow': 'Context window',
  'settings.providers.custom.maxOutput': 'Max output tokens',
  'settings.providers.custom.inputModalities': 'Input modalities',
  'settings.providers.custom.inputModalitiesHint':
    'Comma-separated: text, image, audio, file, video',
  'settings.providers.custom.outputModalities': 'Output modalities',
  'settings.providers.custom.outputModalitiesHint':
    'Comma-separated: text, image, speech, transcription, embeddings',
  'settings.providers.custom.taskTypes': 'Task types',
  'settings.providers.custom.taskTypesHint':
    'Optional comma-separated explicit task types',
  'settings.providers.custom.parameters': 'Supported parameters',
  'settings.providers.custom.parametersHint':
    'Optional comma-separated wire parameter names',
  'settings.providers.custom.voices': 'Supported voices',
  'settings.providers.custom.voicesHint': 'Optional comma-separated voice ids',
  'settings.providers.custom.tools': 'Tools',
  'settings.providers.custom.vision': 'Vision',
  'settings.providers.custom.jsonMode': 'JSON mode',
  'settings.providers.custom.reasoning': 'Reasoning',
  'settings.providers.custom.capabilityAria': '{capability} for {model}',
  'settings.providers.custom.validationId':
    'Provider id must use lowercase letters and digits in hyphen-separated segments.',
  'settings.providers.custom.validationName': 'Enter a Provider name.',
  'settings.providers.custom.validationBaseUrl':
    'Enter an absolute HTTP(S) endpoint URL.',
  'settings.providers.custom.validationModelId':
    'Every Model needs a wire id without "::".',
  'settings.providers.custom.validationDuplicateModel':
    'Model ids must be unique.',
  'settings.providers.custom.validationPositiveInteger':
    '{label} must be a positive whole number.',
  'settings.providers.custom.saved': 'Custom Provider saved.',
  'settings.providers.custom.saveError': 'Custom Provider could not be saved.',
  'settings.providers.custom.deleted': 'Custom Provider deleted.',
  'settings.providers.custom.deleteError':
    'Custom Provider could not be deleted.',
  'settings.providers.custom.deleteTitle': 'Delete Custom Provider?',
  'settings.providers.custom.deleteBody':
    'The Provider and its stored data-directory API keys are removed. Existing Model references are kept and become unavailable.',
  'settings.channels.title': 'Channels',
  'settings.providers.connectedCount': '{count} connected',
  'settings.channels.add': 'Add channel',
  'settings.channels.count': '{count} configured',
  'settings.channels.edit': 'Edit channel {id}',
  'settings.channels.enableAria': 'Enable channel {id}',
  'settings.channels.delete': 'Delete channel {id}',
  'settings.channels.id': 'Channel ID',
  'settings.channels.platform': 'Platform',
  'settings.channels.platform.telegram': 'Telegram',
  'settings.channels.platform.discord': 'Discord',
  'settings.channels.platform.slack': 'Slack',
  'settings.channels.platform.mattermost': 'Mattermost',
  'settings.channels.platform.whatsapp': 'WhatsApp',
  'settings.channels.platform.help':
    'Changing the platform of an existing Channel clears its own identity and group access, because user and group IDs from one platform mean nothing on another.',
  'settings.channels.agent': 'Agent',
  'settings.channels.agent.placeholder': 'Select agent',
  'settings.channels.agent.none': 'No agents available',
  'settings.channels.agent.required': 'Select an agent before saving.',
  'settings.channels.dm_scope': 'DM scope',
  'settings.channels.dm_scope.per_conversation': 'Per conversation',
  'settings.channels.dm_scope.main': 'Main',
  'settings.channels.dm_scope.per_peer': 'Per peer',
  'settings.channels.dm_scope.per_account_channel_peer':
    'Per account + channel + peer',
  'settings.channels.token_env_var': 'Token env var',
  'settings.channels.token_env_var.help':
    'Name of the environment variable that holds the bot token. Set the variable itself in the .env file in the vBot data directory — only the name goes here.',
  'settings.channels.idHelp': 'A name you choose; it cannot be changed later.',
  'settings.channels.dm_scope.help':
    'How direct messages are grouped into Sessions.\n\nPer conversation: one Session per chat.\n\nMain: all DMs share one Session.\n\nPer peer: one Session per person.\n\nPer account + channel + peer: one Session per chat and person.\n\nGroup chats always share one Session per group, regardless of this setting.',
  'settings.channels.allowed_chat_ids.help':
    'The chats that may send messages to this Channel, separated by commas. An empty list allows nobody. WhatsApp accepts only self, your own chat.\n\nMessages from other chats are rejected and listed under Blocked chats in the Channel row, where Allow adds the chat to this list.',
  'settings.channels.allowed_chat_ids': 'Allowed chat IDs',
  'settings.channels.allowed_chat_ids.description':
    'Separate IDs with commas. An empty list allows nobody.',
  'settings.channels.allowed_chat_ids.placeholder': '12345, -1009876543210',
  'settings.channels.running': 'Running',
  'settings.channels.stopped': 'Stopped',
  'settings.channels.failed': 'Failed',
  'settings.channels.empty': 'No Channels yet',
  'settings.channels.emptyHint':
    'Talk to your Agents from Telegram, Discord, Slack, Mattermost or WhatsApp.',
  'settings.channels.delete_confirm_title': 'Delete channel',
  'settings.channels.delete_confirm':
    'Delete channel "{id}" permanently? vBot stops listening on it and its configuration is removed.',
  'settings.channels.createSuccess': 'Channel created.',
  'settings.channels.enableSuccess': 'Channel enabled.',
  'settings.channels.disableSuccess': 'Channel disabled.',
  'settings.channels.deleteSuccess': 'Channel deleted.',
  'settings.channels.access.title': 'Group access',
  'settings.channels.access.help':
    'Groups where this Channel has seen messages, with the people who wrote there.\n\nIn a group, only admins can use commands and the Agent’s Tools. For members, the Agent can only search and read the web.\n\nThis is me marks your own account on this platform. It is an admin in every group and cannot be demoted.',
  'settings.channels.access.helpAria': 'About group access',
  'settings.channels.access.identity': 'Own identity',
  'settings.channels.access.identitySuccess': 'Own identity updated.',
  'settings.channels.access.roleSuccess': 'Group role updated.',
  'settings.channels.access.empty': 'No groups seen yet.',
  'settings.channels.access.group': 'Group',
  'settings.channels.access.noParticipants': 'No seen participants.',
  'settings.channels.access.admin': 'Admin',
  'settings.channels.access.member': 'Member',
  'settings.channels.access.me': 'Me',
  'settings.channels.access.thisIsMe': 'This is me',
  'settings.channels.access.thisIsMeAria': 'Use {name} as own identity',
  'settings.channels.access.makeAdmin': 'Make admin',
  'settings.channels.access.makeMember': 'Make member',
  'settings.channels.access.makeAdminAria': 'Make {name} an admin',
  'settings.channels.access.makeMemberAria': 'Make {name} a member',
  'settings.channels.denied.allowSuccess': 'Chat allowed.',
  'settings.channels.denied.group': 'Group',
  'settings.channels.denied.direct': 'Direct',
  'settings.channels.denied.title': 'Blocked chats',
  'settings.channels.denied.help':
    'Recent messages from chats that are not in Allowed chat IDs. Allow adds the chat to the list.\n\nThe list is kept only while the Channel runs.',
  'settings.channels.denied.helpAria': 'About blocked chats',
  'settings.channels.denied.allowAria': 'Allow chat {id}',
  'settings.channels.denied.allow': 'Allow',
  'settings.channels.app_token_env': 'App token env var',
  'settings.channels.app_token_help':
    'Slack needs a second token for Socket Mode. Enter the name of the variable holding the xapp token with connections:write permission.',
  'settings.channels.server_url': 'Mattermost server URL',
  'settings.channels.whatsapp.help':
    'Link your existing WhatsApp account and talk to vBot in your self chat: send a message to yourself to reach this Channel’s Agent. Other conversations cannot trigger Runs.\n\nThis uses an unofficial linked-device connection; WhatsApp may restrict the account.\n\nRequires Node.js 22 or newer on the vBot server.',
  'settings.channels.whatsapp.helpAria': 'About WhatsApp linking',
  'settings.channels.whatsapp.risk':
    'Unofficial connection; WhatsApp may restrict the account.',
  'settings.channels.whatsapp.notInstalled':
    'WhatsApp support is not installed yet.',
  'settings.channels.whatsapp.installing': 'Installing WhatsApp support…',
  'settings.channels.whatsapp.install': 'Install WhatsApp support',
  'settings.channels.whatsapp.connected': 'WhatsApp connected.',
  'settings.channels.whatsapp.scan':
    'In WhatsApp, open Settings → Linked devices → Link a device, then scan this QR code.',
  'settings.channels.whatsapp.qr': 'WhatsApp device linking QR code',
  'settings.channels.whatsapp.waiting':
    'Connect to show a QR code or restore your linked device.',
  'settings.channels.whatsapp.connect': 'Connect WhatsApp',
  'settings.channels.whatsapp.repair': 'Link again with a new QR code',
  'settings.extensions.title': 'Extensions',
  'settings.extensions.count': '{count} discovered',
  'settings.extensions.empty': 'No extensions discovered.',
  'settings.extensions.statusFailed': 'Failed',
  'settings.extensions.statusOverridden': 'Overridden',
  'settings.extensions.overriddenBy': 'Overridden by your copy at {path}',
  'settings.extensions.waiting': 'Needs setup',
  'settings.extensions.waitingFor': 'Waiting for: {fields}',
  'settings.extensions.enableAria': 'Enable extension {name}',
  'settings.extensions.enableSuccess': 'Extension enabled.',
  'settings.extensions.disableSuccess': 'Extension disabled.',
  'settings.extensions.error': 'Error',
  'settings.extensions.warning': 'Warning',
  'settings.extensions.hooks': 'Hooks',
  'settings.extensions.tools': 'Tools',
  'settings.extensions.commands': 'Commands',
  'settings.extensions.recallBackends': 'Recall backends',
  'settings.extensions.startup': 'startup',
  'settings.extensions.shutdown': 'shutdown',
  'settings.extensions.detailsAria': 'Details for extension {name}',
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
  'settings.extensions.reloadInfoAria': 'About reloading extensions',
  'settings.extensions.reloadSuccess': 'Extensions reloaded.',
  'settings.extensions.reloadHelp':
    'Rebuilds all extensions from disk — picks up code edits, new and removed extensions.',
  'settings.appearance.title': 'Appearance',
  'settings.appearance.language': 'Language',
  'settings.appearance.chatWidth.label': 'Chat width',
  'settings.appearance.chatWidth.comfortable': 'Comfortable',
  'settings.appearance.chatWidth.wide': 'Wide',
  'settings.appearance.chatWidth.full': 'Full width',
  'settings.appearance.chatWorkingMode.label': 'Work details',
  'settings.appearance.chatWorkingMode.description':
    'How Thinking and Tool activity appear in Chat.',
  'settings.appearance.chatWorkingMode.help':
    'Normal shows each Thinking block and Tool call in the Chat as it happens.\n\nCompact groups consecutive Thinking and Tool activity into one collapsible Working block, so the Agent’s replies stand out.',
  'settings.appearance.chatWorkingMode.normal': 'Normal',
  'settings.appearance.chatWorkingMode.compact': 'Compact',
  'settings.language.en': 'English',
  'settings.voice.title': 'Voice',
  'settings.voice.aboutAria': 'About {name}',
  'settings.voice.transcriptionProfile': 'Transcription audio profile',
  'settings.voice.transcriptionProfileLabel': 'Audio profile',
  'settings.voice.transcriptionProfileHelp':
    'Every recording for the Speech to text Model, from the Chat or Terminal microphone and from commands spoken after a wake phrase, is converted to this format first. Wakeword detection itself always listens at 16 kHz.\n\nMaximum compatibility sends 16 kHz WAV. High fidelity sends 48 kHz FLAC, which keeps more detail but makes larger uploads. Custom lets you choose the format and sample rate.',
  'settings.voice.transcriptionProfileCompatibility':
    'Maximum compatibility (recommended)',
  'settings.voice.transcriptionProfileHighQuality': 'High fidelity',
  'settings.voice.transcriptionProfileCustom': 'Custom',
  'settings.voice.transcriptionFormat': 'Format',
  'settings.voice.transcriptionFormatHelp':
    'Mono, signed 16-bit audio. WAV has the broadest Provider support; FLAC is lossless and smaller.',
  'settings.voice.transcriptionFormatWav': 'WAV (PCM16)',
  'settings.voice.transcriptionFormatFlac': 'FLAC (lossless PCM16)',
  'settings.voice.transcriptionSampleRate': 'Sample rate',
  'settings.voice.transcriptionSampleRateHelp':
    '16 kHz is the speech-focused default. Higher rates retain more source detail but create larger uploads.',
  'settings.voice.transcriptionSampleRate16': '16 kHz (recommended for speech)',
  'settings.voice.enabled': 'Wakeword listening',
  'settings.voice.enabledHelp':
    'Listens on this device for wake phrases that send a spoken command to an Agent or start Live voice.\n\nWhile listening is on, microphone audio is analyzed continuously on this device. Nothing is sent unless a wake phrase matches. After a match, the command recording, including up to 320 ms of audio from just before the phrase was detected, is sent to the Speech to text Model for transcription.\n\nTo discard a command, say “abbrechen” or “vergiss es” at the end of the same recording; no Run starts.',
  'settings.voice.desktopUpdateRequired':
    'Update the vBot Desktop app to use Voice with this server.',
  'settings.voice.phraseLimit': '{count} of {max} phrases active',
  'settings.voice.phraseNotReady': 'Not ready',
  'settings.voice.phraseUnavailable': 'Not installed',
  'settings.voice.deactivatePhrase': 'Stop listening',
  'settings.voice.deactivatePhraseAria': 'Stop listening for {name}',
  'settings.voice.overlapWarning':
    '“{name}” can also be heard as {others}, which does something else. Give them the same action or keep only one of them active.',
  'settings.voice.actionCommand': 'Send a command',
  'settings.voice.actionLiveToggle': 'Start or end Live voice',
  'settings.voice.actionLiveStart': 'Start Live voice',
  'settings.voice.phraseAgent': 'Agent',
  'settings.voice.phraseAgentAria': 'Agent for {name}',
  'settings.voice.agentDefault': 'Default Agent',
  'settings.voice.agentUnavailable': 'Not on this server',
  'settings.voice.phraseSession': 'Session',
  'settings.voice.phraseSessionAria': 'Session for {name}',
  'settings.voice.sessionDefault': 'Default Session behavior',
  'settings.voice.calibrate': 'Calibrate',
  'settings.voice.calibrateAria': 'Calibrate {name}',
  'settings.voice.defaultAgent': 'Default Agent',
  'settings.voice.defaultAgentDescription':
    'For phrases without an Agent of their own.',
  'settings.voice.defaultAgentHelp':
    'Receives the spoken commands of phrases that name no Agent of their own.\n\nThis choice applies to the server this Desktop app is connected to.',
  'settings.voice.noDefaultAgent': 'None',
  'settings.voice.defaultSession': 'Default Session behavior',
  'settings.voice.defaultSessionHelp':
    'Whether spoken commands continue the Agent’s active Session or start a new one each time, unless a phrase chooses otherwise.',
  'settings.voice.echoCancellation': 'Echo cancellation',
  'settings.voice.echoCancellationHelp':
    'Removes speaker output, such as Live voice or read-aloud replies, from the microphone signal before phrases are detected and commands are recorded.',
  'settings.voice.echoCancellationAria': 'Use echo cancellation',
  'settings.voice.echoOff': 'Off',
  'settings.voice.echoOffDetail':
    'Speaker output can trigger wake phrases and end up in command recordings.',
  'settings.voice.echoStarting': 'Starting',
  'settings.voice.echoStartingDetail':
    'Echo cancellation is still loading. Until it is ready, the microphone signal is used unprocessed.',
  'settings.voice.echoActive': 'Active',
  'settings.voice.echoNoReference': 'No speaker signal',
  'settings.voice.echoNoReferenceDetail':
    'The Desktop cannot capture the speaker output, so the microphone signal is used unprocessed.',
  'settings.voice.echoUnavailable': 'Unavailable',
  'settings.voice.echoUnavailableDetail':
    'Echo cancellation is not installed in this Desktop app, so the microphone signal is used unprocessed.',
  'settings.voice.calibrationHeading': 'Calibrating “{name}”',
  'settings.voice.error.missingTarget':
    'Choose an Agent for this phrase or a default Agent for this server.',
  'settings.voice.error.targetUnavailable':
    'The chosen Agent no longer exists on this server. Choose another Agent.',
  'settings.voice.error.session':
    'vBot could not open the target Agent Session. Check the server connection and retry.',
  'settings.voice.error.configInvalid':
    'The Desktop rejected this Voice setting. Reload Voice settings and try again.',
  'settings.voice.error.modelActive':
    'This wake phrase is active. Deactivate it before removing its model.',
  'settings.voice.error.modelDeleteFailed':
    'The Desktop could not remove this wakeword model. Check the Desktop log and try again.',
  'settings.voice.error.calibrationUnavailable':
    'Calibration needs Voice listening with this wake phrase active. Wait until Voice is listening, then try again.',
  'settings.voice.error.calibrationInactive':
    'No calibration is running anymore. Start the calibration again.',
  'settings.voice.models': 'Wake phrases',
  'settings.voice.modelsHelp':
    'Turn on the phrases to listen for. Each active phrase has its own sensitivity and action: send a spoken command to an Agent, or start or end Live voice.\n\nHigher sensitivity hears a phrase more easily but also reacts to similar sounds more often. Calibrate measures the room and your voice to suggest a value; it works while listening is on.\n\nTo add your own phrase, import a wakeword model in TFLite format.',
  'settings.voice.modelImported': 'Imported',
  'settings.voice.modelToggleAria': 'Listen for {name}',
  'settings.voice.importModel': 'Import TFLite model',
  'settings.voice.removeModel': 'Remove imported model',
  'settings.voice.importSuccessActive':
    'Wakeword model imported and activated.',
  'settings.voice.importSuccessInactive':
    'Wakeword model imported. Activate it to listen for it.',
  'settings.voice.importTooLargeTitle': 'Wakeword model is too large.',
  'settings.voice.importTooLargeMessage':
    'Choose a TFLite model no larger than 20 MiB.',
  'settings.voice.deleteConfirmTitle': 'Remove wakeword model',
  'settings.voice.deleteConfirm':
    'Remove “{name}” permanently from this Desktop? The TFLite file stored by vBot will be deleted.',
  'settings.voice.deleteSuccess': 'Wakeword model removed.',
  'settings.voice.microphone': 'Microphone',
  'settings.voice.sensitivity': 'Sensitivity',
  'settings.voice.modelAction': 'When heard',
  'settings.voice.modelActionAria': 'When {name} is heard',
  'settings.voice.sessionBehaviorActive': 'Use active Session',
  'settings.voice.sessionBehaviorNew': 'New Session each time',
  'settings.voice.systemAutomaticMic': 'Automatic selection',
  'settings.voice.compatibleMic': 'Compatible',
  'settings.voice.incompatibleMic': 'Unsupported format',
  'settings.voice.configuredMicUnavailable': 'Configured device unavailable',
  'settings.voice.calibrationNoiseInstruction':
    'Stay quiet for {seconds} seconds while vBot measures the room.',
  'settings.voice.calibrationPhraseInstruction':
    'Say “{name}” naturally — {count} of {required} repetitions captured. Pause briefly between repetitions.',
  'settings.voice.calibrationReviewInstruction':
    'Measurement complete. Review the calculated sensitivity, then apply it.',
  'settings.voice.calibrationListening': 'Commands paused',
  'settings.voice.calibrationReadyToApply': 'Ready to apply',
  'settings.voice.calibrationProgressAria': 'Calibration progress',
  'settings.voice.calibrationStepNoise': 'Room noise',
  'settings.voice.calibrationStepPhrases': 'Wakeword samples',
  'settings.voice.calibrationStepReview': 'Review',
  'settings.voice.calibrationScore': 'Score',
  'settings.voice.calibrationNoise': 'Noise',
  'settings.voice.calibrationPeak': 'Peak',
  'settings.voice.calibrationThreshold': 'Threshold',
  'settings.voice.calibrationSamples': '{count} / {required} samples',
  'settings.voice.calibrationRecommendation':
    'Recommended sensitivity {value}%',
  'settings.voice.calibrationMeterAria': '{name} detector score',
  'settings.voice.calibrationReset': 'Restart calibration',
  'settings.voice.calibrationDiscard': 'Discard and stop',
  'settings.voice.calibrationDiscardConfirmTitle': 'Discard calibration?',
  'settings.voice.calibrationDiscardConfirm':
    'All measurements will be discarded and the sensitivity stays unchanged.',
  'settings.voice.calibrationApply': 'Apply calibrated value',
  'settings.voice.calibrationStartFailed': 'Calibration could not start.',
  'settings.voice.calibrationResetFailed': 'Calibration could not restart.',
  'settings.voice.calibrationStopFailed': 'Calibration could not stop.',
  'settings.voice.calibrationApplied': 'Wakeword sensitivity applied.',
  'settings.voice.calibrationApplyFailed': 'Calibration could not be applied.',
  'settings.voice.calibrationNoiseHighWarning':
    'Room noise is high ({level}). Consider moving to a quieter environment or reducing background noise for better results.',
  'settings.voice.calibrationCurrentSensitivity': 'Current',
  'settings.voice.desktopOnly': 'Available in the vBot Desktop app.',
  'settings.voice.statusUnavailableTitle': 'Desktop Voice status unavailable',
  'settings.voice.statusUnavailableMessage':
    'The Desktop bridge did not return Voice settings. Retrying automatically…',
  'settings.voice.mockWarning':
    'Voice is running in demo mode. State changes are simulated; no microphone is heard and no command is sent. Restart Desktop without --mock-wakeword for real detection.',
  'settings.voice.error.serverUnreachable':
    'Voice could not reach the active server. Check the Desktop connection and try again.',
  'settings.voice.error.speechToTextUnconfigured':
    'Configure a Speech-to-text Model under Settings → Voice to send voice commands.',
  'settings.voice.error.speechToTextUnavailable':
    'The configured Speech-to-text Model is not currently usable. Check its Provider connection or choose another Model under Settings → Voice.',
  'settings.voice.error.speechToTextReadiness':
    'Voice could not verify the Speech-to-text configuration. Check the Desktop log and try again.',
  'settings.voice.error.pipeline':
    'The Voice pipeline stopped unexpectedly. Retry listening or restart the Desktop app.',
  'settings.voice.error.recordingInterrupted':
    'The recording was interrupted. Say the wake phrase again.',
  'settings.voice.error.unknown':
    'Voice stopped unexpectedly. Retry listening or restart the Desktop app.',
  'settings.voice.error.noServer':
    'Voice has no active server. Connect the Desktop app to a server and try again.',
  'settings.voice.error.engine':
    'The on-device wakeword model could not start. Restart the Desktop app and try again.',
  'settings.voice.error.modelUnavailable':
    'The selected wakeword model is no longer available. Choose another model or import it again.',
  'settings.voice.error.modelInvalid':
    'The wakeword model is not a compatible pyopen-wakeword TFLite model.',
  'settings.voice.error.microphone':
    'No compatible microphone is available. Connect a microphone or choose another input device, then retry.',
  'settings.voice.error.microphoneRead':
    'The microphone stopped responding. Check the device connection and retry.',
  'settings.voice.error.detection':
    'Wakeword detection stopped unexpectedly. Retry listening.',
  'settings.voice.error.send':
    'The spoken command could not be sent. Check the server connection and retry.',
  'settings.voice.error.stackUnavailable':
    'The Desktop Voice components are unavailable. Install the desktop Voice dependencies and restart vBot.',
  'settings.voice.microphoneDisconnectedTitle': 'Microphone disconnected',
  'settings.voice.errorTitle': 'Voice needs attention',
  'settings.voice.retryFailed': 'Voice could not restart.',
  'settings.voice.enabledAria': 'Enable wakeword listening',
  'settings.voice.retry': 'Retry listening',
  'settings.sessionTitles.title': 'Session titles',
  'settings.sessionTitles.enabled': 'Automatic Session titles',
  'settings.sessionTitles.enabledDescription':
    'Uses one extra Model request for each new Session.',
  'settings.sessionTitles.enabledHelp':
    'Every new Session first gets a local title from the start of its first message, up to 40 characters.\n\nWhen this is on, vBot also sends an excerpt of that first message to the Title model once and replaces the local title with a short generated one. If the request fails, the local title stays.',
  'settings.sessionTitles.model': 'Title model',
  'settings.sessionTitles.modelHelp':
    'Agent Model (default) uses the Model of the Agent the Session belongs to. A small, inexpensive Model keeps titles cheap.',
  'settings.sessionTitles.agentModel': 'Agent Model (default)',
  'settings.content': 'Settings content',
  'settings.models.loadError': 'Model catalog could not be loaded.',
});
