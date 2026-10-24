import { untrack } from 'svelte';
import { parseAgentAddress } from '$lib/agentAddress.js';
import { sameLocation } from '$lib/navigation.svelte.js';

// Synchronizes the first retained Chat area with shell locations and external
// Session links, including drafts, hidden reports, split requests and renames.
export function createAppChatNavigation({ navigator, selection, openChat }) {
  // Chat's first area reports every place it moves to; App turns places it
  // did not choose itself (Back/Forward, deep links, the main navigation)
  // into a restore request for that area.
  const chatNavigation = navigator.view('chat');
  const activeViewId = $derived(navigator.location.view);
  let chatShownSession = $state(null);
  let pendingSessionNavigation = $state(null);
  let chatRestoreRequestId = 0;
  let lastChatLocation = null;
  let initialChatProjectCatalogSettled = $state(false);

  // A copied Project link has no selection metadata. Wait until its Team is
  // known before deciding whether it selects a member or views a hidden owner.
  const chatLinkWaitingForProjects = (location) =>
    !selection.projectsLoaded &&
    !initialChatProjectCatalogSettled &&
    !location.extra?.selection &&
    Boolean(parseAgentAddress(location.place[0]).projectId);

  // Restores wait for the Agent roster, so an entry's Agent is known when
  // Chat applies it.
  $effect.pre(() => {
    const location = navigator.location;
    if (location.view !== 'chat') {
      pendingSessionNavigation = null;
      lastChatLocation = null;
      return;
    }
    if (selection.agents.length === 0 || chatLinkWaitingForProjects(location))
      return;
    untrack(() => {
      const previous = lastChatLocation;
      lastChatLocation = location;
      if (location.origin === 'view' || sameLocation(previous, location)) {
        return;
      }
      // Chat names its first shown Session itself once it resolves; when it
      // already resolved while hidden, the start place is corrected below.
      if (
        !previous &&
        !chatShownSession &&
        location.place.length === 0 &&
        !location.extra
      ) {
        return;
      }
      const [agentId = '', sessionId = ''] = location.place;
      const subAgent = location.extra?.subAgent === true;
      const shown = chatShownSession;
      if (
        shown?.agentId === agentId &&
        shown.sessionId === sessionId &&
        shown.subAgent === subAgent &&
        (shown.runId ?? null) === (location.extra?.runId ?? null)
      ) {
        return;
      }
      chatRestoreRequestId += 1;
      // A place naming only an Agent is that Agent's unsaved draft.
      pendingSessionNavigation = {
        ...(agentId && sessionId
          ? {
              agentId,
              sessionId,
              subAgent,
              runId: location.extra?.runId ?? null,
              followSession: subAgent && location.origin === 'app',
            }
          : agentId
            ? { agentId, draft: true }
            : { returnToCurrent: true }),
        selection: location.extra?.selection ?? null,
        requestId: chatRestoreRequestId,
      };
    });
  });

  const chatExtra = (subAgent) => ({
    selection: selection.currentNavigationSelection(),
    subAgent: subAgent === true,
  });

  // `session` is the Session Chat's first area shows now (`{agentId,
  // sessionId, subAgent}`; no `sessionId` for a draft, whose place names only
  // the Agent). A report while Chat is hidden corrects the place Chat returns
  // to and never pulls the app back to Chat.
  const handleChatSessionNavigation = (session, { replace = false } = {}) => {
    chatShownSession = session ?? null;
    if (!session) return false;
    if (
      replace &&
      activeViewId === 'chat' &&
      chatLinkWaitingForProjects(navigator.location)
    )
      return false;
    const place = [session.agentId, session.sessionId];
    const extra = {
      ...chatExtra(session.subAgent),
      ...(session.runId ? { runId: session.runId } : {}),
    };
    return replace || activeViewId !== 'chat'
      ? chatNavigation.replace(place, { extra })
      : chatNavigation.navigate(place, { extra });
  };

  const navigateToSession = (
    agentId,
    sessionId,
    subAgent = false,
    runId = null,
  ) => {
    if (!agentId || !sessionId) return false;
    return navigator.navigate('chat', [agentId, sessionId], {
      extra: { ...chatExtra(subAgent), ...(runId ? { runId } : {}) },
    });
  };

  // Open in split view on an Agent row: Chat shows that Agent beside the area
  // it shows now. A request whose switch to Chat did not happen (an autosave
  // prompt, then another view) is dropped instead of applying later.
  let chatSplitRequest = $state(null);
  const openAgentInSplit = (agentId) => {
    if (!agentId) return false;
    chatSplitRequest = {
      target: { agentAddress: agentId },
      requestId: (chatSplitRequest?.requestId ?? 0) + 1,
      revision: navigator.location.revision,
    };
    return openChat();
  };
  $effect(() => {
    const location = navigator.location;
    if (
      chatSplitRequest &&
      location.view !== 'chat' &&
      location.revision !== untrack(() => chatSplitRequest.revision)
    ) {
      chatSplitRequest = null;
    }
  });

  // A remembered Location with an Identity Agent's old id replaced by its
  // new one.
  function renameAgentInLocation(location, oldAgentId, newAgentId) {
    const resolve = (agentId) =>
      agentId === oldAgentId ? newAgentId : agentId;
    const remapScope = (segment) =>
      segment.startsWith('agent:')
        ? `agent:${resolve(segment.slice('agent:'.length))}`
        : segment;
    if (location.view === 'chat') {
      const [agentId, ...rest] = location.place;
      const chatSelection = location.extra?.selection;
      return {
        ...location,
        place:
          agentId && !agentId.includes('@')
            ? [resolve(agentId), ...rest]
            : location.place,
        extra: chatSelection
          ? {
              ...location.extra,
              selection: {
                ...chatSelection,
                agentId: resolve(chatSelection.agentId),
              },
            }
          : location.extra,
      };
    }
    if (location.view === 'agents' && location.place[0]) {
      return {
        ...location,
        place: [resolve(location.place[0]), ...location.place.slice(1)],
      };
    }
    if (location.view === 'skills' || location.view === 'system-prompt') {
      return { ...location, place: location.place.map(remapScope) };
    }
    return location;
  }

  const navigateToSubAgent = (target) =>
    navigateToSession(target?.agentId, target?.sessionId, true);

  // Opens one Session from outside the page, the way Live UI's `open` action
  // does: a startup link or a request of the Desktop app.
  const openSessionLink = ({ agentId, sessionId }) =>
    navigateToSession(agentId, sessionId);

  function remapIdentityAgentId(oldAgentId, newAgentId) {
    if (chatShownSession?.agentId === oldAgentId) {
      chatShownSession = { ...chatShownSession, agentId: newAgentId };
    }
    navigator.remapAll((location) =>
      renameAgentInLocation(location, oldAgentId, newAgentId),
    );
  }

  function settleInitialProjects() {
    initialChatProjectCatalogSettled = true;
  }

  return {
    get shownSession() {
      return chatShownSession;
    },
    get pendingSessionNavigation() {
      return pendingSessionNavigation;
    },
    get splitRequest() {
      return chatSplitRequest;
    },
    handleSessionNavigation: handleChatSessionNavigation,
    navigateToSession,
    navigateToSubAgent,
    openSessionLink,
    openAgentInSplit,
    remapIdentityAgentId,
    settleInitialProjects,
  };
}
