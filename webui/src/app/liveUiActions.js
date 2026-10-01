// Live voice UI: what the app shows, navigation, and Terminal layout.
//
// The server resolves every target against vBot's catalogs before it asks; this
// owner applies the request to the visible app and reports whether it applied.
// Navigation goes through the autosave transition like a click would.

/**
 * @param {object} deps
 * @param {object} deps.selection App selection state (Agents, Projects, selections).
 * @param {object} deps.navigator The App navigator (`lib/navigation.svelte.js`).
 * @param {(agentId: string, sessionId: string) => any} deps.navigateToSession Opens a Chat Session.
 * @param {() => string} deps.activeView The current view id.
 * @param {(action: () => any) => any} deps.requestTransition Autosave-guarded transition.
 * @param {() => object | null} deps.chatSelection The Session Chat shows.
 * @param {() => object | undefined} deps.terminalsView The mounted Terminals view.
 * @param {() => Promise<void>} deps.afterRender Resolves once pending view updates rendered.
 */
export function createLiveUiActions({
  selection,
  navigator,
  navigateToSession,
  activeView,
  requestTransition,
  chatSelection,
  terminalsView,
  afterRender,
}) {
  // What the app shows; the Live call learns it without asking.
  function context() {
    const shown = chatSelection();
    return {
      view: activeView(),
      selected_agent_id: selection.selectedAgentId || null,
      selected_project_id: selection.selectedProjectId || null,
      chat_session:
        shown?.agentId && shown?.sessionId
          ? { agent_id: shown.agentId, session_id: shown.sessionId }
          : null,
    };
  }

  // `isCurrent` turns false once the requesting call stops, so a deferred
  // autosave transition never navigates for an old call. `false` means the
  // app did not switch.
  function navigate(view, target = {}, isCurrent = () => true) {
    // A transition deferred behind a running save answers `false` at once;
    // the voice model then hears it did not switch, so it never runs later.
    let abandoned = false;
    const outcome = requestTransition(() => {
      if (abandoned || !isCurrent()) return false;
      if (view === 'chat' && target.session_id) {
        return navigateToSession(target.agent_id, target.session_id);
      }
      if (view === 'agents' && target.agent_id) {
        if (!selection.agents.some((agent) => agent.id === target.agent_id))
          return false;
        // The Agents view follows the shared Agent selection.
        selection.selectAgent(target.agent_id);
        navigator.navigate('agents', [target.agent_id]);
        return true;
      }
      if (view === 'projects' && target.project_id) {
        const { project_id: projectId } = target;
        if (
          !selection.projects.some(
            (project) => project.project_id === projectId,
          )
        )
          return false;
        navigator.navigate('projects', [projectId]);
        return true;
      }
      if (activeView() !== view) navigator.open(view);
      return true;
    });
    if (outcome === false) abandoned = true;
    return outcome;
  }

  async function terminalView(action, args, isCurrent) {
    if ((await navigate('terminals', {}, isCurrent)) === false)
      throw new Error('navigation_not_applied');
    await afterRender();
    const view = terminalsView();
    if (!view || !isCurrent()) throw new Error('terminal_view_unavailable');
    return view.applyVoiceAction(action, args);
  }

  return {
    context,
    open: ({ view, agent_id, session_id, project_id }, { isCurrent }) =>
      navigate(view, { agent_id, session_id, project_id }, isCurrent),
    terminalView: ({ op, ...args }, { isCurrent }) =>
      terminalView(op, args, isCurrent),
  };
}
