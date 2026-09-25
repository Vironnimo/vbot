// Live voice UI requests: what the app shows, navigation, and Terminal layout.
//
// The server resolves every target against vBot's catalogs before it asks; this
// owner applies the request to the visible app and reports whether it applied.
// Navigation goes through the autosave transition like a click would.

/**
 * @param {object} deps
 * @param {object} deps.selection App selection state (Agents, Projects, selections).
 * @param {() => object} deps.appController App navigation (views, Chat Sessions).
 * @param {() => string} deps.activeView The current view id.
 * @param {(action: () => any) => any} deps.requestTransition Autosave-guarded transition.
 * @param {() => object | null} deps.chatSelection The Chat view's voice-relevant selection.
 * @param {(projectId: string) => Promise<object>} deps.loadProject The project.show RPC.
 * @param {() => object | undefined} deps.terminalsView The mounted Terminals view.
 * @param {() => object | undefined} deps.projectsView The mounted Projects view.
 * @param {() => Promise<void>} deps.afterRender Resolves once pending view updates rendered.
 */
export function createLiveUiActions({
  selection,
  appController,
  activeView,
  requestTransition,
  chatSelection,
  loadProject,
  terminalsView,
  projectsView,
  afterRender,
}) {
  async function context() {
    const projectId = selection.selectedProjectId;
    const shown = {
      view: activeView(),
      selected_agent_id: selection.selectedAgentId,
      selected_project_id: projectId,
      selected_project_agent_id: selection.selectedProjectAgentId,
      chat_selection: chatSelection(),
      agents: selection.agents.map((agent) => ({
        agent_id: agent.id,
        name: agent.name,
      })),
      projects: selection.projects.map((project) => ({
        project_id: project.project_id,
        name: project.display_name,
        cwd: project.cwd,
      })),
    };
    const team = projectId
      ? ((await loadProject(projectId)).scan?.team || []).map((agent) => ({
          agent_id: `${agent.agent_id}@${projectId}`,
          name: agent.display_name,
        }))
      : [];
    return { ...shown, selected_project_team: team };
  }

  // `isCurrent` turns false once the requesting call stops, so a deferred
  // autosave transition never navigates for an old call. `false` means the
  // app did not switch.
  function navigate(view, target = {}, isCurrent = () => true) {
    return requestTransition(() => {
      if (!isCurrent()) return false;
      if (view === 'chat' && target.session_id) {
        return appController().navigateToSession(
          target.agent_id,
          target.session_id,
        );
      }
      if (view === 'agents' && target.agent_id) {
        if (!selection.agents.some((agent) => agent.id === target.agent_id))
          return false;
        // The Agents view follows the shared Agent selection.
        selection.selectAgent(target.agent_id);
      }
      if (view === 'projects' && target.project_id) {
        const { project_id: projectId } = target;
        if (
          !selection.projects.some(
            (project) => project.project_id === projectId,
          )
        )
          return false;
        // A Projects view opening next starts on this Project; an open one
        // switches to it itself.
        selection.selectManagedProject(projectId);
        const mounted = activeView() === 'projects' ? projectsView() : null;
        if (mounted) return mounted.selectVoiceProject(projectId);
      }
      if (activeView() !== view) return appController().selectView(view);
      return true;
    });
  }

  async function terminalView(action, args, isCurrent) {
    if (action === 'context')
      return terminalsView()?.getVoiceContext() ?? { visible_order: [] };
    if ((await navigate('terminals', {}, isCurrent)) === false)
      throw new Error('navigation_not_applied');
    await afterRender();
    const view = terminalsView();
    if (!view || !isCurrent()) throw new Error('terminal_view_unavailable');
    return view.applyVoiceAction(action, args);
  }

  return {
    context: () => context(),
    open: ({ view, agent_id, session_id, project_id }, { isCurrent }) =>
      navigate(view, { agent_id, session_id, project_id }, isCurrent),
    terminalView: ({ op, ...args }, { isCurrent }) =>
      terminalView(op, args, isCurrent),
  };
}
