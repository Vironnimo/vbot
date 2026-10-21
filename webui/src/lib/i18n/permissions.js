export default Object.freeze({
  // Permission Rules editor (components/permissions/PermissionRulesEditor.svelte)
  'permissions.title': 'Permissions',
  'permissions.agentDescription':
    'Where this Agent may read and change files, which commands it may run and which sites it may open.',
  'permissions.defaultsTitle': 'Permissions',
  'permissions.defaultsDescription':
    'The Permission Rules that Identity Agents and Project Agents inherit for each part they do not set themselves. Without them, Agents have full access.',
  'permissions.overrideDescription':
    'Rules for this Agent in this Project. Each part it does not set comes from the inherited value.',
  'permissions.limits':
    'The rules limit vBot’s own Tools and check shell commands before they run; programs those commands start, MCP servers and Extensions are not confined.',
  'permissions.limitsHelp':
    'A script, interpreter or build tool started by a command acts with the server user’s full rights and is not inspected inside. In a terminal, only command lines typed at the shell’s own prompt are checked; input to programs running there is not. Pages the local browser opens can follow redirects and load content from sites the rules do not allow. MCP servers and Extensions run outside these checks; Tool access decides whether the Agent may use them. Hardlinks to files outside the area are not detected on read. Git can show the contents of files from its history. Git commands that replace the whole working tree, such as pull or switch, can change a repository’s permission settings files. If you go back to an older vBot version, it ignores these rules.',
  'permissions.createHelp':
    'Single bases and rules are set in the Agent editor after creating.',
  'permissions.preset': 'Preset',
  'permissions.presetHelp':
    'A preset sets where the Agent may read and change files. Full access lifts every limit, also inherited ones; the other presets keep your own rules.',
  'permissions.preset.full_access': 'Full access',
  'permissions.preset.work_dir': 'Work in the working directory',
  'permissions.preset.work_dir_read_only': 'Read-only in the working directory',
  'permissions.preset.custom': 'Custom',
  'permissions.preset.useDefault': 'Use the default ({preset})',
  'permissions.preset.useInherited': 'Use inherited ({preset})',
  'permissions.preset.customRules': 'custom rules',
  'permissions.preset.ignoresDefault':
    'This Agent ignores the default restrictions.',
  'permissions.preset.ignoresInherited':
    'This Agent ignores the inherited restrictions in this Project.',
  'permissions.part.read': 'Read files',
  'permissions.part.write': 'Change files',
  'permissions.part.commands': 'Shell commands',
  'permissions.part.web': 'Web access',
  'permissions.base.read.anywhere': 'Anywhere',
  'permissions.base.read.work_area': 'Only in the work area',
  'permissions.base.write.anywhere': 'Anywhere',
  'permissions.base.write.work_area': 'Only in the work area',
  'permissions.base.write.scratch': 'Only in the scratch area',
  'permissions.base.commands.any': 'Any command',
  'permissions.base.commands.listed': 'Only allowed commands',
  'permissions.base.web.any': 'Any site',
  'permissions.base.web.listed': 'Only allowed sites',
  'permissions.base.inherited': 'Inherited: {value}',
  'permissions.base.default': 'Default: {value}',
  'permissions.base.writeNarrowed':
    'Changing files now stays in the work area: an Agent can change only files it can read.',
  'permissions.base.writeWithinRead':
    'Not available while reading stays in the work area: an Agent can change only files it can read.',
  'permissions.base.workAreaHelp':
    'The work area is the folder a Session works in plus its scratch area.',
  'permissions.allowTitle': 'Allow rules',
  'permissions.allowDescription':
    'Extra folders, commands and sites the Agent may use beyond the settings above.',
  'permissions.denyTitle': 'Deny rules',
  'permissions.denyDescription':
    'Refused even where the settings above or an allow rule permit them.',
  'permissions.rulesHelp':
    'Write paths or globs: Read(docs/**), Edit({workdir}/src/**). Shell(git push *) matches a command, Web(*.github.com) a site. {workdir} is the folder a Session works in, {project} the Working Project’s repository, {scratch} the Session’s scratch area, {workspace} the Agent’s Workspace and ~ the home folder. A relative path starts in {workdir}.',
  'permissions.rulesInherited': 'Inherited rules',
  'permissions.rulesNone': 'No rules.',
  'permissions.rulesChange': 'Change',
  'permissions.rulesChangeHint':
    'Copies the inherited rules into this level’s own list so you can change them.',
  'permissions.rulesReset': 'Use inherited',
  'permissions.rulesResetHint':
    'Removes this level’s own list, so the inherited rules apply again.',
  'permissions.ruleKind': 'Kind of rule {index}',
  'permissions.rulePattern': 'Pattern of rule {index}',
  'permissions.rulePatternPlaceholder': 'Path, command or site',
  'permissions.ruleRemove': 'Remove rule {rule}',
  'permissions.ruleAdd': 'Add rule',
  'permissions.paste': 'Paste rules',
  'permissions.pasteHelp':
    'One rule per line, or Claude Code settings JSON with permissions.allow and permissions.deny. Bash(…) and Write(…) become Shell(…) and Edit(…).',
  'permissions.pasteLabel': 'Rules to add',
  'permissions.pasteAllow': 'Add as allow rules',
  'permissions.pasteDeny': 'Add as deny rules',
  'permissions.pasteCancel': 'Cancel',
  'permissions.checking': 'Checking rules…',
  'permissions.checkFailed': 'The rules could not be checked: {message}',
  'permissions.incomplete': 'Every rule needs a pattern before it is saved.',
  'permissions.preview': 'New Sessions can',
  'permissions.preview.read': 'Read',
  'permissions.preview.write': 'Change',
  'permissions.preview.commands': 'Run',
  'permissions.preview.web': 'Open',
  'permissions.preview.deny': 'Never',
  'permissions.preview.inactive': 'Skipped here',
  'permissions.preview.inactiveHint':
    'These rules use a placeholder without a value for this Agent, so its Runs skip them.',
  'permissions.preview.anywhere': 'Anywhere',
  'permissions.preview.anyCommand': 'Any command',
  'permissions.preview.anySite': 'Any site',
  'permissions.preview.nothing': 'Nothing',
  'permissions.preview.fullAccess': 'Full access: no Permission Rules apply.',
  'permissions.preview.scratch':
    '{scratch} is the conversation’s scratch area.',

  // Session badge (components/permissions/SessionPermissionsBadge.svelte)
  'permissions.session.badge': 'Restricted',
  'permissions.session.title': 'Permission Rules limit this Session',
  'permissions.session.text':
    'Its Tools refuse what these limits do not allow. When several sets of limits are listed, all of them apply.',
  'permissions.session.source.agent': 'This Agent’s rules',
  'permissions.session.source.parent':
    'Inherited from the Agent that started it',
  'permissions.session.source.extension': 'Set by an Extension',
  'permissions.session.source.cron':
    'Inherited from the Session that scheduled this Run',
  'permissions.session.source.other': 'Inherited limit',
  'permissions.session.workdir': 'Works in',
  'permissions.session.clear': 'Remove inherited limits',
  'permissions.session.clearTitle': 'Remove inherited limits?',
  'permissions.session.clearText':
    'Later Runs in this Session then follow only this Agent’s own rules. A scheduled job or the Session that started this one adds its limits again when it runs here.',
  'permissions.session.clearConfirm': 'Remove limits',
  'permissions.session.clearFailed': 'The limits could not be removed.',
});
