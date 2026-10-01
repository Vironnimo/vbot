// Recordings in this page announce that they use the microphone, so a running
// Live call can pause meanwhile instead of hearing a dictation as speech.

const listeners = new Set();
let claims = 0;

function changed() {
  const inUse = claims > 0;
  for (const listener of listeners) {
    try {
      listener(inUse);
    } catch {
      // One failing listener does not keep the others from hearing it.
    }
  }
}

// Claim the microphone for one recording; the returned release is idempotent.
export function claimMicrophone() {
  claims += 1;
  if (claims === 1) changed();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    claims -= 1;
    if (claims === 0) changed();
  };
}

export function microphoneInUse() {
  return claims > 0;
}

// Calls `listener(inUse)` whenever recordings start or all of them end;
// returns the unsubscribe function.
export function onMicrophoneUse(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
