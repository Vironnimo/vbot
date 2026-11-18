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
  let popups = $state.raw([]);
  const popup = $derived(popups.at(-1) ?? null);
  // Remember addresses and in-flight creations, never callbacks into retired views.
  const launches = new SvelteMap();

  function dismiss(instance) {
    const index = popups.findIndex((entry) => entry.instance === instance);
    if (index >= 0) popups = popups.slice(0, index);
  }

  function show(config, fromPopup) {
    const view = currentView();
    const instance = Symbol();
    const entry = { ...config, view, instance, opener: document.activeElement };
    if (!fromPopup) popups = [entry];
    else if (popup?.instance === fromPopup) popups = [...popups, entry];
    return { close: () => dismiss(instance) };
  }

  function openSession(
    {
      address,
      title,
      notice,
      readOnly = false,
      presentation,
      includeInherited = true,
    },
    fromPopup,
  ) {
    return show(
      { address, title, notice, readOnly, presentation, includeInherited },
      fromPopup,
    );
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
    popups = [];
  }

  function forgetSession({ agentAddress, deletedSessionId }) {
    const matches = (address) =>
      address?.session_id === deletedSessionId &&
      qualifyAgentAddress(address.agent_id, address.project_id) ===
        agentAddress;
    for (const [key, launch] of launches) {
      if (matches(launch.address)) launches.delete(key);
    }
    const removed = popups.find((entry) =>
      matches(entry.address ?? entry.launch?.address),
    );
    if (removed) dismiss(removed.instance);
  }

  function renameAgent(oldId, newId) {
    popups = popups.map((entry) => {
      const address = entry.address;
      return address?.agent_id === oldId && !address.project_id
        ? { ...entry, address: { ...address, agent_id: newId } }
        : entry;
    });
  }

  return {
    get popups() {
      return popups;
    },
    get popup() {
      return popup;
    },
    openSession,
    openSpecialist,
    forgetSession,
    renameAgent,
    dismiss,
    close,
  };
}
