export default {
  'computers.server': 'Server',
  'computers.label': 'Computer',
  'computers.offline': 'Offline',
  'computers.offlineCapabilities': 'Offline · {capabilities}',
  'computers.availabilityUnknown': 'Availability unknown',
  'computers.terminalAndComputerUse': 'Terminal and Computer Use enabled',
  'computers.terminalOnly': 'Terminal enabled · Computer Use unavailable',
  'computers.computerUseOnly': 'Computer Use enabled · Terminal unavailable',
  'computers.noCapabilities': 'No capabilities shared',
  'computers.capabilitiesEnabled': '{capabilities} enabled',
  'computers.loadError':
    'Computers could not be loaded. Retry to check their availability.',
  'computers.sessionTitle': 'Computers for this conversation',
  'computers.sessionUnknown': 'Computer not loaded',
  'computers.sessionUnknownHelp':
    'The computer settings for this conversation must be loaded before they can be changed.',
  'computers.sessionHelp':
    'Terminal and Computer Use use the default computer. Other Tools, including file access and shell commands, run on the server.',
  'computers.default': 'Default computer',
  'computers.additional': 'Additional computers',
  'computers.additionalHelp':
    'Allow another computer when your task needs it. For Terminal and Computer Use, the Agent can use only the computers you select here.',
  'computers.running':
    'Computer access can be changed once the current Run ends.',
  'computers.selectedOffline': '{name} · Offline',
  'computers.selectedRevoked': '{name} · Not allowed',
  'computers.defaultRevoked':
    'This conversation may no longer use this computer. Choose another default.',
  'computers.terminalUnavailable': 'Terminal is unavailable on this computer.',
  'computers.remoteWorkdir': 'Working directory on the selected computer',
  'computers.settingsTitle': 'Computers',
  'computers.settingsEmpty':
    'No Desktop computers are registered. A computer appears here once its Desktop app shares a capability with this server.',
  'computers.online': 'Connected',
  'computers.remove.button': 'Remove',
  'computers.remove.aria': 'Remove {name}',
  'computers.remove.title': 'Remove {name}?',
  'computers.remove.body':
    'Its remaining terminals end, and conversations cannot reach it until its Desktop app shares with this server again.',
  'computers.remove.confirm': 'Remove computer',
  'computers.remove.done': '{name} was removed',
  'computers.remove.failed': 'The computer could not be removed.',
  'computers.sharingTitle': 'Share this computer',
  'computers.sharingHelp':
    'Choose the capabilities this computer shares with the connected server. They are available while this Desktop app is running, including when minimized.',
  'computers.extensionIssue':
    'Desktop Extension {name} could not provide all its capabilities: {reason}',
  'computers.name': 'Computer name',
  'computers.nameRequired': 'Enter a name for this computer.',
  'computers.shareTerminal': 'Share Terminal',
  'computers.shareTerminalHelp':
    'Allow the connected server to start and control terminal programs on this computer. Turning this off stops those programs.',
  'computers.shareComputerUse': 'Share Computer Use',
  'computers.shareComputerUseHelp':
    'Allow the connected server to see and operate this desktop with its mouse and keyboard.',
  'computers.capabilityUnavailable': 'Unavailable on this computer.',
  'computers.capabilityError': 'Unavailable: {reason}',
  'computers.reloadExtensions': 'Reload local Extensions',
  'computers.reloading': 'Reloading Extensions…',
  'computers.reloadHelp':
    'Apply changes to Extensions installed on this computer. Updated Extensions keep their sharing choices; new Extensions start unshared.',
  'computers.reloadError':
    'The local Extensions could not be reloaded. Try again.',
  'computers.extensionAdded':
    '{name} was added. Review its sharing choices above.',
  'computers.extensionUpdated': '{name} was updated.',
  'computers.extensionRemoved': '{name} was removed.',
  'computers.connectedTo': 'Connected to {server}',
  'computers.notConnected':
    'Not connected. Shared capabilities will become available when this app connects to its server.',
  'computers.activeTerminals':
    '{count} terminal programs are running in this app. Quitting the app ends them.',
  'computers.sharingLoadError':
    'Computer sharing settings could not be loaded.',
  'computers.sharingSaveError': 'Computer sharing settings could not be saved.',
  'computers.sharingError': 'Computer sharing is unavailable: {reason}',
};
