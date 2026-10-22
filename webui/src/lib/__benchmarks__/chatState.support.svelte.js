import { createChatState } from '../chatState.js';

// The Chat state as ChatView holds it: one deep `$state` proxy. The
// `.svelte.js` suffix compiles this module with runes.
export function reactiveChatState() {
  const chatState = $state(createChatState());
  return chatState;
}
