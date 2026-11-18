import { getContext, setContext } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import {
  createAppChat,
  createSideChat,
  listFiles,
  startChatRun,
} from './api.js';
import { qualifyAgentAddress } from './agentAddress.js';
import { createChatSubmissions } from './chatState/submissions.js';
import { sessionInputParams } from './chatState/controller.js';
import { sessionKey } from './chatState/sessionState.js';
import { t } from './i18n.js';

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

function newLaunch(source) {
  const launch = $state({
    id: newSessionId(),
    address: null,
    admitted: null,
    firstCommand: null,
    pending: null,
    source,
    invalidated: false,
    error: '',
  });
  return launch;
}

export function createAppChats(currentView) {
  let popups = $state.raw([]);
  const popup = $derived(popups.at(-1) ?? null);
  // Remember addresses and in-flight creations, never callbacks into retired views.
  const launches = new SvelteMap();
  const submit = createChatSubmissions(startChatRun);

  function dismiss(instance) {
    const index = popups.findIndex((entry) => entry.instance === instance);
    if (index >= 0) popups = popups.slice(0, index);
  }

  function show(config, fromPopup) {
    const view = currentView();
    const instance = Symbol();
    const entry = {
      ...config,
      view,
      instance,
      submit,
      opener: document.activeElement,
    };
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

  function openDraft(config, createSession, { reuse, fromPopup, source } = {}) {
    let launch = reuse && launches.get(reuse);
    if (!launch) {
      launch = newLaunch(source);
      if (reuse) launches.set(reuse, launch);
    }
    const resolve = () => {
      if (launch.invalidated) throw new Error(t('common.viewLoadError'));
      if (launch.address) return Promise.resolve(launch.address);
      if (!launch.pending) {
        launch.pending = Promise.resolve()
          .then(() => createSession(launch.id))
          .then((address) => {
            if (launch.invalidated) throw new Error(t('common.viewLoadError'));
            return (launch.address = address);
          })
          .finally(() => (launch.pending = null));
      }
      return launch.pending;
    };
    async function sendFirst(content, options) {
      const address = await resolve();
      const agent = qualifyAgentAddress(address.agent_id, address.project_id);
      const draftKey = sessionKey(agent, address.session_id);
      const response = await submit(
        `embed:${draftKey}`,
        sessionInputParams(agent, address.session_id, content, {
          ...options,
          expectedGenerationId: address.generation_id,
        }),
      );
      if (source && launches.get(reuse) === launch) launches.delete(reuse);
      launch.source = null;
      launch.firstCommand = response?.command_handled ? response : null;
      launch.admitted = address;
      return { address, response, draftKey };
    }
    // A specialist has no trustworthy file scope before the user sends. At
    // submission its actual Session can resolve typed @mentions normally.
    async function listFilesOnSend(request = {}) {
      const address = await resolve();
      return listFiles(
        qualifyAgentAddress(address.agent_id, address.project_id),
        {
          ...request,
          sessionId: address.session_id,
        },
      );
    }
    return show({ ...config, sendFirst, launch, listFilesOnSend }, fromPopup);
  }

  function openSpecialist({ profile, key, createSession, ...config }) {
    const reuse = key ? JSON.stringify([currentView(), profile, key]) : null;
    return openDraft(
      config,
      createSession ?? ((id) => createAppChat(profile, id)),
      { reuse },
    );
  }

  function openSideDraft(
    { address, sourceGenerationId, ...config },
    fromPopup,
  ) {
    const agent = qualifyAgentAddress(address.agent_id, address.project_id);
    const reuse = JSON.stringify([
      'side',
      agent,
      address.session_id,
      sourceGenerationId,
    ]);
    // A fresh draft outcome is the server's decision that no live child exists.
    // Keep an unresolved launch id (including a lost create acknowledgement),
    // but never resurrect a previously resolved destination from this cache.
    if (launches.get(reuse)?.address) launches.delete(reuse);
    return openDraft(
      config,
      (id) => createSideChat(agent, address.session_id, sourceGenerationId, id),
      {
        reuse,
        fromPopup,
        source: address,
      },
    );
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
      if (matches(launch.address) || matches(launch.source)) {
        launch.invalidated = true;
        launches.delete(key);
      }
    }
    const removed = popups.find(
      (entry) =>
        matches(entry.address ?? entry.launch?.address) ||
        matches(entry.launch?.source),
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
    openSideDraft,
    openSpecialist,
    forgetSession,
    renameAgent,
    dismiss,
    close,
  };
}
