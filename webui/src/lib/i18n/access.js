export default Object.freeze({
  // Access gate (components/access/AccessGate.svelte)
  'access.gate.checking': 'Connecting to vBot…',
  'access.denied.title': 'The server refuses this connection',
  'access.denied.body':
    'This vBot server does not accept connections from this address or over this kind of connection.',
  'access.denied.hint':
    'Its access mode decides who may connect. This computer only accepts just the server’s own computer, Home network without login also the home network, and With login (internet) needs HTTPS for every other computer. On the server’s computer, vbot application access show prints the current setting.',
  'access.unavailable.title': 'The server cannot check this sign-in right now',
  'access.unavailable.body':
    'The server cannot read its list of signed-in devices, so it lets no device in for now. This browser stays signed in, and vBot tries again on its own.',
  'access.unavailable.hint':
    'The cause is in the server’s log on the server’s computer. Once the server can read its device list again, vBot opens by itself.',
  'access.unreachable.title': 'vBot cannot reach the server',
  'access.unreachable.body':
    'vBot has to ask the server whether this browser may still use it, but the server does not answer. It keeps trying on its own.',
  'access.looping.title': 'vBot keeps losing its access',
  'access.looping.body':
    'The server let this browser in, then refused its requests several times in a row. Try again; if it keeps happening, check the server’s access settings and its log.',

  // Pairing screen (components/access/PairingScreen.svelte)
  'access.pairing.title': 'Sign in to vBot',
  'access.pairing.intro':
    'This vBot server asks every device to sign in once. Approve this browser from a device that is already signed in, or on the server itself.',
  'access.pairing.notKept':
    'This browser was approved but did not keep its sign-in. Browsers keep it only on a secure connection: open vBot through its https:// address, or through localhost on the server’s own computer, and pair again.',
  'access.pairing.starting': 'Requesting a pairing code…',
  'access.pairing.codeLabel': 'Pairing code',
  'access.pairing.waiting': 'Waiting for approval. The code expires at {time}.',
  'access.pairing.unstable':
    'The server does not answer right now. vBot keeps asking until the code expires.',
  'access.pairing.approved': 'Approved. Opening vBot…',
  'access.pairing.denied': 'This sign-in request was denied.',
  'access.pairing.expired': 'The pairing code expired before it was approved.',
  'access.pairing.rateLimited':
    'Too many sign-in requests came from this network. Wait a moment, then try again.',
  'access.pairing.error.devicesUnavailable':
    'The server cannot store devices right now; its log says why.',
  'access.pairing.error.network':
    'vBot cannot reach the server. Check the connection and try again.',
  'access.pairing.error.generic': 'The sign-in request failed. Try again.',
  'access.pairing.stepDevice':
    'On a device that is already signed in to vBot at {origin}, open Settings → System → Access & devices, choose Add device and enter this code. Enter it only there: whoever approves it lets this browser in.',
  'access.pairing.stepServer': 'Or run this on the server’s computer:',
  'access.pairing.copyCommand': 'Copy approval command',
  'access.pairing.deviceName': 'This browser will be listed as “{name}”.',
  'access.pairing.deviceNameBrowserOs': '{browser} on {os}',
  'access.pairing.deviceNameOs': 'Browser on {os}',
  'access.pairing.deviceNameFallback': 'Web browser',
  'access.pairing.newCode': 'Get a new code',
  'access.pairing.tryAgain': 'Try again',
  'access.pairing.startAgain': 'Start again',

  // Settings -> System -> Access & devices
  'settings.sections.access': 'Access & devices',
  'settings.access.statusError': 'The access mode could not be loaded.',
  'settings.access.mode': 'Access mode',
  'settings.access.modeHelp':
    'Who may connect to this vBot server.\n\nThis computer only: just the server’s own computer; nobody signs in.\n\nHome network without login: also computers on the home network, without signing in.\n\nWith login (internet): any device that can reach the server, and each one signs in once by pairing. Every other computer connects over HTTPS.',
  'settings.access.modeLocal':
    'Only the server’s own computer can connect; nobody signs in.',
  'settings.access.modeLan':
    'Computers on the home network can connect without signing in.',
  'settings.access.modeAuthenticated':
    'Every device signs in once by pairing before it can use vBot.',
  'settings.access.change': 'Change the access mode',
  'settings.access.changeDescription':
    'Only on the server’s computer: vbot application access set with the mode and its options records a new mode, and vbot server restart applies it. This command shows the current setting:',
  'settings.access.copyCommand': 'Copy command',
  'settings.access.devices.title': 'Devices',
  'settings.access.devices.description':
    'Devices that signed in by pairing or were opened from a signed-in device. Removing a device also signs out the browsers opened from it.',
  'settings.access.devices.descriptionOpen':
    'No device needs to sign in in this access mode. Devices paired while the server asked for sign-in stay listed until you remove them.',
  'settings.access.devices.empty': 'No device is paired yet.',
  'settings.access.devices.emptyOpen': 'No device is paired.',
  'settings.access.devices.thisDevice': 'This device',
  'settings.access.devices.added': 'added {time}',
  'settings.access.devices.approvedByHost': 'approved on the server’s computer',
  'settings.access.devices.approvedBy': 'approved by {name}',
  'settings.access.devices.approvedByRemoved': 'approved by a removed device',
  'settings.access.devices.lastUsed': 'last used {time}',
  'settings.access.devices.lastClient': 'from {address}',
  'settings.access.kind.browser': 'Browser',
  'settings.access.kind.desktop': 'Desktop app',
  'settings.access.kind.cli': 'Command line',
  'settings.access.kind.unknown': 'Device',
  'settings.access.error.devicesUnavailable':
    'The server cannot read or change its device list right now; its log says why.',
  'settings.access.error.deviceNotFound': 'This device no longer exists.',
  'settings.access.error.invalid': 'The server refused this change.',
  'settings.access.error.generic': 'The request failed.',
  'settings.access.rename.button': 'Rename',
  'settings.access.rename.aria': 'Rename {name}',
  'settings.access.rename.title': 'Rename device',
  'settings.access.rename.label': 'Name',
  'settings.access.rename.done': 'Device renamed.',
  'settings.access.revoke.button': 'Remove',
  'settings.access.revoke.aria': 'Remove {name}',
  'settings.access.revoke.title': 'Remove device “{name}”?',
  'settings.access.revoke.body':
    '{name} is signed out at once and has to sign in again to use vBot.',
  'settings.access.revoke.cascade':
    'Browsers opened from this device, and browsers opened from them, also lose access. Devices approved separately with a pairing code stay signed in.',
  'settings.access.revoke.browsers': 'Affected browsers: {names}.',
  'settings.access.revoke.currentBrowser':
    'This browser is among them: this window will be signed out too.',
  'settings.access.revoke.confirm': 'Remove device',
  'settings.access.revoke.done': '{name} was removed.',
  'settings.access.revokeOthers.button': 'Sign out other devices',
  'settings.access.revokeOthers.title': 'Sign out all other devices?',
  'settings.access.revokeOthers.body':
    'Every other device is signed out at once and has to sign in again. This device stays signed in.',
  'settings.access.revokeOthers.bodyAll':
    'Every paired device is signed out at once and has to sign in again.',
  'settings.access.revokeOthers.confirm': 'Sign out devices',
  'settings.access.revokeOthers.done': 'Devices signed out: {count}.',
  'settings.access.signOut.button': 'Sign out',
  'settings.access.signOut.title': 'Sign out this device?',
  'settings.access.signOut.body':
    'This browser loses its access to vBot right away. To use vBot here again, sign in again.',
  'settings.access.signOut.confirm': 'Sign out',
  'settings.access.add.button': 'Add device…',
  'settings.access.add.title': 'Add device',
  'settings.access.add.intro':
    'Open vBot on the new device. It shows a pairing code; enter that code here.',
  'settings.access.add.codeLabel': 'Pairing code',
  'settings.access.add.codeHelp':
    'Eight letters and digits, for example K7QM-4XPA.',
  'settings.access.add.codePlaceholder': 'XXXX-XXXX',
  'settings.access.add.continue': 'Continue',
  'settings.access.add.confirmIntro':
    'A device waits with this code. Check that it is the device in front of you.',
  'settings.access.add.name': 'Name',
  'settings.access.add.kind': 'Kind',
  'settings.access.add.address': 'Address',
  'settings.access.add.requested': 'Requested',
  'settings.access.add.code': 'Code',
  'settings.access.add.warning':
    'Approve only a device you have in front of you, never a code someone sent you or another website showed you. An approved device can use everything in vBot and act with the server’s rights on its computer.',
  'settings.access.add.deny': 'Deny',
  'settings.access.add.approve': 'Approve',
  'settings.access.add.approved': '{name} is approved and signs in now.',
  'settings.access.add.denied': 'The request from {name} was denied.',
  'settings.access.add.notFound':
    'No device is waiting with this code. Check the code, or start pairing again on the new device.',
  'settings.access.add.rateLimited':
    'Too many wrong codes. Try again in {minutes} min.',
  'settings.access.add.authDisabled':
    'Devices can be added only while the server asks for sign-in.',
});
