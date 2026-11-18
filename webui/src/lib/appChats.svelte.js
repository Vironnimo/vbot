import { getContext, setContext } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { createAppChat } from './api.js';
import { qualifyAgentAddress } from './agentAddress.js';

const APP_CHATS = Symbol('vbot.appChats');

export function getAppChats() {
  return getContext(APP_CHATS);
}

export function provideAppChats(chats) {
  setContext(APP_CHATS, chats);
  return chats;
}

// Same 80 random bits and compact alphabet as a preallocated canonical Session id.
function newSessionId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const alphabet = '0123456789abcdefghjkmnpqrstvwxyz';
  return 'ses_' + Array.from(bytes, (byte) => alphabet[byte & 31]).join('');
}

export function createAppChats(currentView) {
  let popup = $state.raw(null);
  // Remember addresses and in-flight creations, never callbacks into retired views.
  const launches = new SvelteMap();

  function show(config) {
    const view = currentView();
    const instance = Symbol();
    popup = { ...config, view, instance };
    const close = () => {
      if (popup?.instance === instance) popup = null;
    };
    return { close };
  }

  function openSession({ address, title, readOnly = false, presentation }) {
    return show({ address, title, readOnly, presentation });
  }

  function openSpecialist({
    profile,
    title,
    key,
    getUiContext,
    createSession,
    presentation,
  }) {
    const reuse = key ? JSON.stringify([currentView(), profile, key]) : null;
    let launch = reuse && launches.get(reuse);
    if (!launch) {
      launch = { id: newSessionId(), address: null, pending: null };
      if (reuse) launches.set(reuse, launch);
    }
    const resolve = () => {
      if (launch.address) return Promise.resolve(launch.address);
      if (!launch.pending) {
        launch.pending = Promise.resolve()
          .then(() =>
            createSession
              ? createSession(launch.id)
              : createAppChat(profile, launch.id),
          )
          .then((address) => (launch.address = address))
          .finally(() => (launch.pending = null));
      }
      return launch.pending;
    };
    return show({ title, getUiContext, resolve, launch, presentation });
  }

  function close() {
    popup = null;
  }

  function forgetSession({ agentAddress, deletedSessionId }) {
    const matches = (address) =>
      address?.session_id === deletedSessionId &&
      qualifyAgentAddress(address.agent_id, address.project_id) ===
        agentAddress;
    for (const [key, launch] of launches) {
      if (matches(launch.address)) launches.delete(key);
    }
    if (matches(popup?.address ?? popup?.launch?.address)) close();
  }

  function renameAgent(oldId, newId) {
    const address = popup?.address;
    if (address?.agent_id === oldId && !address.project_id) {
      popup = { ...popup, address: { ...address, agent_id: newId } };
    }
  }

  return {
    get popup() {
      return popup;
    },
    openSession,
    openSpecialist,
    forgetSession,
    renameAgent,
    close,
  };
}
