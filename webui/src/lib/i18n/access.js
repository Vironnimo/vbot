export default Object.freeze({
  // Access gate (components/access/AccessGate.svelte)
  'access.gate.checking': 'Connecting to vBot…',
  'access.denied.title': 'The server refuses this connection',
  'access.denied.body':
    'This vBot server does not accept connections from this address or over this kind of connection.',
  'access.denied.hint':
    'Its access mode decides who may connect. This computer only accepts just the server’s own computer, Home network without login also the home network, and With login (internet) needs HTTPS for every other computer. On the server’s computer, vbot application access show prints the current setting.',
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
    'On a device that is already signed in, open Settings → System → Access & devices, choose Add device and enter this code.',
  'access.pairing.stepServer': 'Or run this on the server’s computer:',
  'access.pairing.copyCommand': 'Copy approval command',
  'access.pairing.deviceName': 'This browser will be listed as “{name}”.',
  'access.pairing.deviceNameBrowserOs': '{browser} on {os}',
  'access.pairing.deviceNameOs': 'Browser on {os}',
  'access.pairing.deviceNameFallback': 'Web browser',
  'access.pairing.newCode': 'Get a new code',
  'access.pairing.tryAgain': 'Try again',
  'access.pairing.startAgain': 'Start again',
});
