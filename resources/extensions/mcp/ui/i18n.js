export default Object.freeze({
  'mcp.catalogCounts':
    'Tools: {tools} · Resources: {resources} · Prompts: {prompts}',
  'mcp.capabilities': 'Capabilities & access',
  'mcp.inspectTitle': 'Capabilities & access: {name}',
  'mcp.searchTools': 'Search Tools',
  'mcp.showAll': 'Show all',
  'mcp.catalogMissing': 'No catalog discovered yet',
  'mcp.catalogMissingHint':
    'Close this view and test the connection to load its capabilities.',
  'mcp.catalogStale':
    'Showing the last discovered catalog. The connection is currently unavailable.',
  'mcp.availableTools': 'Tools ({count})',
  'mcp.noToolsFound': 'No Tools found',
  'mcp.noToolsFoundHint':
    'Search checks names and descriptions. Show all Tools to inspect general-purpose capabilities.',
  'mcp.serverGuidance': 'Server guidance',
  'mcp.serverPrompts': 'Server prompts',
  'mcp.fullDescription': 'Full description',
  'mcp.accessHelp':
    'Enable this connection in the Agent or Swarm Tool settings. It is off by default, including in All Tools mode.',
  'mcp.add': 'Add MCP connection',
  'mcp.addArgument': 'Add argument',
  'mcp.addEntry': 'Add {group}',
  'mcp.advanced': 'Advanced settings',
  'mcp.argument': 'Argument {number}',
  'mcp.arguments': 'Arguments',
  'mcp.argumentsHelp': 'Passed to the program in this order.',
  'mcp.cancelTest': 'Cancel test',
  'mcp.changed':
    'This connection changed elsewhere. Close the editor and reopen it before saving.',
  'mcp.clearCredential': 'Clear credential',
  'mcp.connected': 'Connected',
  'mcp.connecting': 'Connecting',
  'mcp.connectionSection': 'Connection',
  'mcp.connectionType': 'Connection type',
  'mcp.credentialCleared': 'Credential cleared.',
  'mcp.credentialEnvironment': 'Credentials for environment variables',
  'mcp.credentialHeaders': 'Credentials for HTTP headers',
  'mcp.credentialName': 'Credential name',
  'mcp.credentialSaved': 'Credential saved.',
  'mcp.credentials': 'Credentials',
  'mcp.credentialsFor': 'Credentials for {name}',
  'mcp.description': 'Description (optional)',
  'mcp.descriptionHelp':
    'One line that tells Agents what this connection is for. Without it, Agents see the name the server reports.',
  'mcp.directory': 'Working directory',
  'mcp.disconnected': 'Disconnected',
  'mcp.duplicate':
    'This connection already exists. Choose another name or edit the existing connection.',
  'mcp.edit': 'Edit MCP connection',
  'mcp.empty': 'No MCP connections yet',
  'mcp.emptyHint':
    'Connect a service from the catalog, or add a local program or a server URL from your MCP setup instructions.',
  'mcp.enabled': 'Enabled',
  'mcp.enabledFor': 'Enable {name}',
  'mcp.entryName': 'Name',
  'mcp.entryField': '{field} {number}',
  'mcp.environment': 'Environment variables',
  'mcp.failed': 'Connection failed',
  'mcp.help':
    'MCP connections give Agents Tools from other programs and services through the Model Context Protocol. Local programs run on the machine hosting vBot.\n\nAn Agent uses a connection only after you enable it in the Agent or Swarm Tool settings. It is off by default, including in All Tools mode.',
  'mcp.helpAria': 'About MCP connections',
  'mcp.detailsAria': 'Details for {name}',
  'mcp.http': 'Server URL (HTTP)',
  'mcp.local': 'Local program',
  'mcp.mappingInvalid': 'Each entry needs a unique, non-empty name.',
  'mcp.name': 'Connection name',
  'mcp.nameHelp':
    'Lowercase letters, numbers and underscores; start with a letter.',
  'mcp.oauth': 'Sign in with OAuth',
  'mcp.oauthClientId': 'OAuth client ID (optional)',
  'mcp.oauthClientIdHelpAria': 'About the OAuth client ID',
  'mcp.oauthClientIdHelp':
    "Only for a server that does not let vBot register itself. Register vBot with the server's sign-in service using the redirect URL shown in this connection's details, then enter the client ID it issues.",
  'mcp.oauthClientMetadataUrl': 'Client metadata URL (optional)',
  'mcp.oauthClientMetadataUrlHelp':
    "Public HTTPS address of your own client metadata document (CIMD), containing this connection's redirect URL. Leave empty for vBot's automatic client selection. An entered OAuth client ID takes precedence.",
  'mcp.oauthClientSecret': 'Credential name for the client secret (optional)',
  'mcp.oauthClientSecretHelp':
    'Use a credential name, not the secret. After saving, open Credentials to set its value.',
  'mcp.oauthScopes': 'OAuth scopes (optional)',
  'mcp.oauthScopesHelp':
    'Space-separated scopes vBot requests in addition to the ones the server asks for.',
  'mcp.program': 'Program',
  'mcp.programSection': 'Local program',
  'mcp.programHelp':
    'Executable on the vBot host, for example uvx, npx or an absolute path.',
  'mcp.reauthorize': 'Sign in again',
  'mcp.redirect': 'OAuth redirect URL (optional)',
  'mcp.redirectHelp':
    "Leave empty to use vBot's own address, where the browser completes the sign-in automatically.",
  'mcp.redirectUri': 'Sign-in redirect URL: {uri}',
  'mcp.removeArgument': 'Remove argument {number}',
  'mcp.removeBody':
    'Agents will lose access to this connection. The external application and installed software will remain.',
  'mcp.removeEntry': 'Remove {group} entry {number}',
  'mcp.removeTitle': 'Remove connection “{name}”?',
  'mcp.roots': 'Share the work directory',
  'mcp.rootsHelp':
    'Tells the server the work directory of the Agent it currently serves (MCP roots), for example so it can find project files.',
  'mcp.rootsHelpAria': 'About sharing the work directory',
  'mcp.rootsOff': 'Off',
  'mcp.rootsWorkspace': "The Agent's work directory",
  'mcp.sampling': 'Model requests from the server',
  'mcp.samplingAllow': 'Allow',
  'mcp.samplingAsk': 'Ask me each time',
  'mcp.samplingHelp':
    "Lets the server ask the Agent's Model for a reply while it serves that Agent (MCP sampling). Replies are limited in length and frequency and count toward the Agent's usage.",
  'mcp.samplingHelpAria': 'About Model requests from the server',
  'mcp.samplingOff': 'Off',
  'mcp.save': 'Save connection',
  'mcp.saveCredential': 'Save credential',
  'mcp.saveHelp':
    'Saving applies the connection on the vBot host. Editing its settings interrupts the current connection.',
  'mcp.saved': 'Connection saved. Test it to verify access to the server.',
  'mcp.secretHelp':
    'Saved values are never displayed. This named credential may also be used by other connections.',
  'mcp.secretValue': 'New secret value',
  'mcp.secretsHelp':
    'Use credential names here, not secret values. After saving, open Credentials to set their values.',
  'mcp.serverSection': 'Server',
  'mcp.signedIn': 'Signed in',
  'mcp.signedOut': 'Not signed in',
  'mcp.sse': 'Server URL (legacy SSE)',
  'mcp.test': 'Test connection',
  'mcp.testCancelled': 'Connection test cancelled.',
  'mcp.testFailed': 'Connection test failed.',
  'mcp.testPassed': 'Connection test passed: {checks}.',
  'mcp.testing':
    'Testing {name}. Complete any sign-in or question shown by vBot.',
  'mcp.timeout': 'Timeout (seconds)',
  'mcp.title': 'MCP connections',
  'mcp.url': 'Server URL',
  'mcp.value': 'Value (non-secret)',
  'mcp.import': 'Import',
  'mcp.importTitle': 'Import MCP servers',
  'mcp.importSource': 'Setup text',
  'mcp.importSourceHelp':
    'Paste the MCP setup a server gives for another app: an mcpServers or servers block (Claude Desktop, Cursor, VS Code, Windsurf and others), Codex TOML, a claude mcp add command, a command line or a server URL.',
  'mcp.importFile': 'Open file…',
  'mcp.importSafety':
    'Only import setups from sources you trust: a local server runs its program on the machine hosting vBot.',
  'mcp.importPreview': 'Preview',
  'mcp.importReview':
    'Check the program or URL of each server before importing it. Secrets in the setup are stored as vBot credentials, not in the connection.',
  'mcp.importApply': 'Import {count}',
  'mcp.importEdit': 'Edit setup text',
  'mcp.importInvalid': 'Cannot import',
  'mcp.importExists': 'Name in use',
  'mcp.importConflict':
    'A connection named {id} already exists. Enter another name to import this server.',
  'mcp.importDisabled': 'Saved turned off',
  'mcp.importCredentialProvided':
    'Its value from the setup is stored as credential {name}.',
  'mcp.importCredentialSet':
    'Uses credential {name}, which already has a value.',
  'mcp.importCredentialMissing':
    'Stored as credential {name}. Enter the value now, or set it later under Credentials.',
  'mcp.importCredentialValue': 'Value for {target} (optional)',
  'mcp.imported': 'Imported {count} connections.',
  'mcp.catalog': 'Catalog',
  'mcp.catalogBrowse': 'Browse the catalog',
  'mcp.catalogTitle': 'Connect a service',
  'mcp.catalogHelp':
    'Hosted services vBot connects in one step. A service that needs an account opens its sign-in page; then choose the Agents that may use it.',
  'mcp.catalogSearch': 'Search services',
  'mcp.catalogCategoryLabel': 'Category',
  'mcp.catalogAllCategories': 'All categories',
  'mcp.catalogCategory.knowledge': 'Knowledge & search',
  'mcp.catalogCategory.productivity': 'Productivity',
  'mcp.catalogCategory.design': 'Design',
  'mcp.catalogCategory.development': 'Development',
  'mcp.catalogCategory.analytics': 'Analytics & monitoring',
  'mcp.catalogCategory.business': 'Business',
  'mcp.catalogEmpty': 'No services found',
  'mcp.catalogEmptyHint': 'Try other words or another category.',
  'mcp.catalogAuthOauth': 'Sign-in required',
  'mcp.catalogAuthNone': 'No sign-in',
  'mcp.catalogConnected': 'Connected',
  'mcp.catalogConnectedAs':
    'Saved as connection {ids}. Connecting again adds another connection.',
  'mcp.catalogReadOnly': 'Read-only',
  'mcp.catalogDocs': 'Documentation',
  'mcp.catalogConnect': 'Connect',
  'mcp.catalogConnectAria': 'Connect {name}',
  'mcp.catalogServiceAs': '{name} ({id})',
  'mcp.catalogSignInHelp':
    'Open the sign-in page and sign in to {name} in your browser. This dialog continues by itself once the browser returns to vBot.',
  'mcp.catalogPreparing': 'Preparing the sign-in…',
  'mcp.catalogSignInWaiting': 'Waiting for the sign-in…',
  'mcp.catalogSignInExpires': 'Finish signing in by {time} ({distance}).',
  'mcp.catalogOtherDevice': 'Signing in with a browser on another device?',
  'mcp.catalogPasteHelp':
    'A browser that cannot reach vBot shows an error page after you sign in. Copy the complete address of that page and paste it here.',
  'mcp.catalogCompleteSignIn': 'Complete sign-in',
  'mcp.catalogSignInTimedOut': 'The sign-in was not completed in time.',
  'mcp.catalogSignInFailed': 'The sign-in failed.',
  'mcp.catalogRetrySignIn': 'Try again',
  'mcp.catalogSignedIn': 'Signed in to {name}.',
  'mcp.catalogAdded': 'Added {name}.',
  'mcp.catalogGrantTitle': 'Choose Agents for {name}',
  'mcp.catalogGrantHelp':
    "Choose the Agents that may use {name}. No Agent can use it until you allow it here or in the Agent's Tool settings.",
  'mcp.catalogNoAgents':
    'There are no Agents yet. Once you create one, enable this connection in its Tool settings.',
  'mcp.catalogHasAccess': 'Has access',
  'mcp.catalogUsesOther': 'Already uses {ids}',
  'mcp.catalogSkip': 'Skip',
  'mcp.catalogGrant': 'Allow access',
  'mcp.signIn': 'Sign in',
  'mcp.signInFor': 'Sign in to {name}',
  'mcp.quickFill': 'Command line or URL (optional)',
  'mcp.quickFillHelp':
    'Paste the command or server URL from the server setup instructions to fill in the form.',
  'mcp.quickFillPlaceholder': 'npx -y @scope/server-name or https://…',
  'mcp.quickFillApply': 'Fill in',
  'mcp.quickFillCredential':
    'After saving, set credential {name} for {target} under Credentials.',
  'mcp.reconnect': 'Reconnect',
  'mcp.reconnectHelp':
    'Close the connection and connect again. A local program is restarted.',
  'mcp.sseDeprecated': 'Deprecated in MCP',
  'mcp.sseHint':
    'Legacy SSE is deprecated in MCP. Choose Server URL (HTTP) unless the server only offers SSE.',
  'mcp.technicalDetails': 'Technical details',
  'mcp.serverOutput': 'Server output (last {count} lines)',
  'mcp.problemInstall':
    "Program {command} was not found on the machine hosting vBot. Install {requirement}, then restart vBot so it finds the program, or enter the program's full path.",
  'mcp.problem.command_not_found':
    "Program {command} was not found on the machine hosting vBot. Install it, then restart vBot so it finds the program, or enter the program's full path.",
  'mcp.problem.command_not_executable':
    'Program {command} could not be started: permission denied. Choose a program file vBot may run.',
  'mcp.problem.directory_not_found':
    'Working directory {directory} does not exist on the machine hosting vBot. Create it or choose another one.',
  'mcp.problem.credential_missing':
    'Credential {credential} has no value. Set it under Credentials.',
  'mcp.problem.process_exited':
    'The local program ended or closed its connection. Its last output usually names the cause.',
  'mcp.problem.server_unreachable':
    'Could not reach {host}. Check that the server is running and the URL is correct.',
  'mcp.problem.unauthorized':
    'The server refused access (HTTP {status}). Set the credential it expects, or turn on OAuth sign-in if it offers one.',
  'mcp.problem.endpoint_not_found':
    'The server has no MCP endpoint at this URL (HTTP {status}). Check the full URL, including a path such as /mcp.',
  'mcp.problem.method_not_allowed':
    'The server does not accept Server URL (HTTP) connections at this URL (HTTP {status}). Check the URL; an older server may need Server URL (legacy SSE).',
  'mcp.problem.server_error':
    'The server failed with HTTP {status}. Try again later.',
  'mcp.problem.timed_out':
    'The server did not answer within {seconds} seconds.',
});
