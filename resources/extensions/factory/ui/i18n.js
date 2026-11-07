export default {
  'factory.title': 'Software Factory',
  'factory.board': 'Board',
  'factory.setups': 'Project setups',
  'factory.newCard': 'New card',
  'factory.createCard': 'Create card',
  'factory.project': 'Project',
  'factory.allProjects': 'All Projects',
  'factory.selectProject': 'Select a Project',
  'factory.cardTitle': 'Title',
  'factory.description': 'Objective and acceptance criteria',
  'factory.createHelp':
    'New cards enter the backlog and start automatically when their Project is enabled and a WIP slot is free.',
  'factory.noEnabledProjects': 'Enable a Project setup before creating a card.',
  'factory.titleRequired': 'Enter a title for the card.',
  'factory.descriptionRequired':
    'Describe the objective and acceptance criteria.',
  'factory.projectRequired': 'Choose a Project.',
  'factory.running': '{running} of {limit} cards running',
  'factory.waitingCount': '{count} waiting for you',
  'factory.showDiscarded': 'Show discarded cards',
  'factory.column.proposals': 'Proposals',
  'factory.column.backlog': 'Backlog',
  'factory.column.progress': 'In progress',
  'factory.column.review': 'Review',
  'factory.column.waiting': 'Waiting for you',
  'factory.column.done': 'Done',
  'factory.emptyColumn': 'No cards',
  'factory.state.proposal': 'Proposal',
  'factory.state.queued': 'Queued',
  'factory.state.working': 'Working',
  'factory.state.reviewing': 'Reviewing',
  'factory.state.waiting': 'Waiting for you',
  'factory.state.integrating': 'Integrating',
  'factory.state.done': 'Integrated',
  'factory.state.discarded': 'Discarded',
  'factory.reason.approved': 'Reviewer approved — ready for your decision',
  'factory.reason.noAgreement': 'No agreement after the review rounds',
  'factory.reason.blocked': 'The Worker needs your answer',
  'factory.reason.budget': 'Worker turn budget reached',
  'factory.reason.runFailed': 'Worker Run failed',
  'factory.reason.reviewFailed': 'Reviewer could not finish',
  'factory.reason.integrationFailed': 'Integration needs attention',
  'factory.reason.restart': 'Interrupted again after a restart',
  'factory.origin.operator': 'Operator',
  'factory.origin.user': 'User request',
  'factory.origin.automation': 'Automation',
  'factory.origin.agent': 'Agent proposal',
  'factory.origin': 'Origin',
  'factory.provenance': 'Open creating Session',
  'factory.originRun': 'Creating Run',
  'factory.moveUp': 'Move {title} up',
  'factory.moveDown': 'Move {title} down',
  'factory.recommendation': 'Reviewer recommendation',
  'factory.noRecommendation': 'No recommendation has been submitted yet.',
  'factory.question': 'Question for you',
  'factory.waitingNote': 'Why this needs you',
  'factory.workerSummary': 'Worker summary',
  'factory.checks': 'Checks and results',
  'factory.submission': 'Reviewed change',
  'factory.branch': 'Branch',
  'factory.base': 'Base',
  'factory.commitRange': 'Reviewed commit range',
  'factory.integrationReference': 'Integration reference',
  'factory.landing.pending': 'Already integrated — review pending',
  'factory.landing.approved': 'Already integrated — reviewed after landing',
  'factory.landing.changesRequested':
    'Already integrated — review requested changes',
  'factory.landing.alreadyLanded':
    'An integration from this Card has already landed.',
  'factory.landing.noAutomaticRevert':
    'Further review, rework or discarding this Card does not automatically undo code on the base.',
  'factory.landing.historical':
    'This notice describes an earlier integration from this Card, not its current submission.',
  'factory.landing.commit': 'Landing commit',
  'factory.landing.copyCommit': 'Copy landing commit',
  'factory.landing.extraPaths': 'Additional changed paths',
  'factory.landing.copyExtraPaths': 'Copy additional changed paths',
  'factory.landing.mergePaths': 'Paths changed by merges',
  'factory.landing.mergeHelp':
    'Review these merge changes; they do not by themselves show an incorrect conflict resolution.',
  'factory.landing.copyMergePaths': 'Copy paths changed by merges',
  'factory.landing.discardWarning':
    'Discarding this Card does not revert code that has already been integrated into the base.',
  'factory.reviews': 'Review rounds',
  'factory.reviewRound': 'Round {round}',
  'factory.integrationReview': 'Integration review',
  'factory.reviewApproved': 'Approved',
  'factory.reviewChanges': 'Changes requested',
  'factory.findings': 'Findings',
  'factory.noFindings': 'No findings recorded.',
  'factory.timeline': 'Event timeline',
  'factory.noEvents': 'No events yet.',
  'factory.comments': 'Your comments',
  'factory.sessions': 'Worker and Reviewer Sessions',
  'factory.noSessions': 'Sessions appear when work starts.',
  'factory.worker': 'Worker',
  'factory.reviewer': 'Reviewer {round}',
  'factory.openSession': 'Read Session',
  'factory.live': 'Live',
  'factory.runStatus': 'Run status',
  'factory.noActiveRun': 'No active Run',
  'factory.permissionWaiting': 'Waiting for permission approval',
  'factory.reviewPermission': 'Review permission request',
  'factory.cost': 'Usage and cost',
  'factory.tokens': 'Tokens',
  'factory.requests': 'Model requests',
  'factory.toolCalls': 'Tool calls',
  'factory.costUnavailable': 'Cost unavailable',
  'factory.accept': 'Accept and integrate',
  'factory.approveProposal': 'Approve proposal',
  'factory.sendBack': 'Send back',
  'factory.answer': 'Answer Worker',
  'factory.retry': 'Retry failed phase',
  'factory.discard': 'Discard card',
  'factory.delete': 'Delete card',
  'factory.sendBackTitle': 'Send this card back to the Worker',
  'factory.comment': 'What should change?',
  'factory.answerLabel': 'Your answer',
  'factory.commentRequired': 'Enter a comment or answer.',
  'factory.discardTitle': 'Discard “{title}”?',
  'factory.discardHelp':
    'The Worker will be asked to clean up its isolated work. The card and its Sessions remain readable.',
  'factory.deleteTitle': 'Delete “{title}”?',
  'factory.deleteHelp':
    'The card and its history will be removed, and its Sessions archived.',
  'factory.editStopped':
    'This card is no longer in the editable backlog. Discard the remaining draft to use the saved objective.',
  'factory.discardDraft': 'Discard draft',
  'factory.saveConflict':
    'This record changed elsewhere. Conflicting fields now show the saved values; your other edits were kept.',
  'factory.setupDeleted':
    'This setup was deleted elsewhere. Your draft is kept, but creating the setup again requires an explicit Create setup action.',
  'factory.invalid.agent': 'Choose an Agent for the {role}.',
  'factory.invalid.reviewRounds':
    'Enter a whole number of review rounds from 1 to 20.',
  'factory.invalid.turnBudget':
    'Enter a whole number of Worker turns from 1 to 1,000.',
  'factory.setupHelp':
    'The Worker implements in isolation. A fresh Reviewer judges each submission before it reaches you.',
  'factory.noProjects': 'Register a Project to configure its Factory setup.',
  'factory.noSetups':
    'Choose a Project to configure the Worker, Reviewer and execution limits.',
  'factory.projects': 'Projects',
  'factory.chooseProject': 'Choose a Project',
  'factory.boardEmpty': 'No Project is set up yet',
  'factory.boardEmptyHelp':
    'Choose which Agents implement and review for a Project, then create cards for it.',
  'factory.setUpProject': 'Set up a Project',
  'factory.enabled': 'Enabled',
  'factory.disabled': 'Disabled',
  'factory.notConfigured': 'Not configured',
  'factory.enableSetup': 'Factory active for this Project',
  'factory.enableSetupHelp':
    'Cards can be created for this Project, and queued cards start automatically when a slot is free.',
  'factory.agent': 'Agent',
  'factory.selectAgent': 'Select an Agent',
  'factory.agentsGroup': 'Agents',
  'factory.teamGroup': '{project} Team',
  'factory.agentUnavailable': 'Unavailable Agent',
  'factory.modelUnresolved':
    'This Agent has no usable Model. Choose a Model here or give the Agent one.',
  'factory.model': 'Model',
  'factory.inheritModel': 'The Agent’s Model',
  'factory.effort': 'Thinking effort',
  'factory.defaultEffort': 'The Agent’s thinking effort',
  'factory.sameModel':
    'Worker and Reviewer use the same Model. A different Reviewer Model provides a more independent judgment.',
  'factory.reviewRounds': 'Total review rounds',
  'factory.reviewRoundsHelp':
    'From 1 to 20, including the first review. Each additional round allows one automatic rework; integration reviews do not count.',
  'factory.turnBudget': 'Worker turns per phase',
  'factory.turnBudgetHelp':
    'From 1 to 1,000. When the Worker uses them up, the card waits for you.',
  'factory.execution': 'Review and integration',
  'factory.baseBranch': 'Default base branch',
  'factory.integrationCheck': 'After integration, check',
  'factory.checkMerged': 'The reviewed commit is in the base branch',
  'factory.checkReference': 'Only that the Worker reports a reference',
  'factory.createSetup': 'Create setup',
  'factory.deleteSetup': 'Delete setup',
  'factory.deleteSetupTitle': 'Delete setup for “{project}”?',
  'factory.deleteSetupHelp':
    'New cards will no longer start for this Project. The server refuses deletion while cards are active.',
  'factory.history': 'Session history',
  'factory.noHistory': 'No Session activity yet.',
  'factory.olderHistory': 'Load older messages',
  'factory.olderSteps': 'Load earlier Run steps',
  'factory.hostUnavailable': 'The Factory page could not connect to vBot.',
  'factory.requestFailed': 'The request failed.',
  'factory.cardMissing': 'This card is no longer available.',
  'factory.queued.accept': 'Queued for integration',
  'factory.queued.sendBack': 'Queued for rework',
  'factory.queued.retry': 'Queued to retry',
  'factory.error.unavailable':
    'Factory is unavailable. Wait for the Extension to load, then retry.',
  'factory.error.projectDisabled':
    'Enable this Project’s Factory setup before creating cards.',
  'factory.error.projectMissing':
    'This Project is unavailable. Choose an existing Project.',
  'factory.error.profileMissing':
    'A selected Agent is unavailable. Choose an available Agent for each role.',
  'factory.error.modelMissing':
    'A selected Model is unavailable. Choose an available Model or use the Agent’s Model.',
  'factory.error.modelUnresolved':
    'An Agent has no usable Model. Choose a Model for that role or give the Agent one.',
  'factory.error.setupMissing':
    'This Project has no Factory setup. Create its setup first.',
  'factory.error.setupInUse':
    'This setup cannot be deleted while its Project has active cards.',
  'factory.error.cardNotFinal': 'Discard this card before deleting it.',
  'factory.error.conflict':
    'This record changed elsewhere. Refresh it before trying again.',
  'factory.error.phaseQueued':
    'This card is already waiting for an execution slot. Its reviewed objective cannot be changed.',
  'factory.error.stateChanged':
    'The card’s state changed. Refresh it and choose an available action.',
  'factory.error.notReviewed':
    'Only an approved or no-agreement result can be accepted.',
  'factory.error.notBlocked':
    'The Worker is no longer waiting for an answer. Refresh the card.',
  'factory.error.notRetryable':
    'This card cannot retry its current phase. Refresh it and choose an available action.',
  'factory.error.invalidBase':
    'Enter a non-empty base branch that does not begin with a hyphen.',
  'factory.error.invalidPosition':
    'The backlog order changed. Refresh the Board and try moving the card again.',
  'factory.error.required': 'Complete the required fields before continuing.',
  'factory.error.textTooLong':
    'Shorten the title to 200 characters and other text to 20,000 characters.',
  'factory.error.unknownAction':
    'This action is unavailable. Reload the Factory page.',
  'factory.error.gitUnavailable':
    'Git is unavailable on the server. Check the Project and Git installation before retrying.',
  'factory.error.gitTimeout':
    'Git did not finish in time. Check the Project repository before retrying.',
  'factory.error.gitFailed':
    'The repository could not be verified. Check the Project and the Worker’s Session.',
  'factory.error.submissionInvalid':
    'The submitted branch or commit range could not be verified. Send the card back to the Worker to correct its submission.',
};
