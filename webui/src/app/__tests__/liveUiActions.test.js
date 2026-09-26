import { describe, expect, it, vi } from 'vitest';
import { createLiveUiActions } from '../liveUiActions.js';

function fixture({ view = 'chat', pendingTransition = false } = {}) {
  const state = {
    view,
    transitions: [],
    projectsView: null,
    terminalsView: null,
  };
  const selection = {
    selectedAgentId: 'main',
    selectedProjectId: 'vbot',
    selectedProjectAgentId: '',
    agents: [
      { id: 'main', name: 'Main' },
      { id: 'coder', name: 'Coder' },
    ],
    projects: [{ project_id: 'vbot', display_name: 'vBot', cwd: 'C:\\work' }],
    selectAgent: vi.fn(),
    selectManagedProject: vi.fn(),
  };
  const controller = {
    selectView: vi.fn((next) => {
      state.view = next;
      return true;
    }),
    navigateToSession: vi.fn(() => true),
  };
  const loadProject = vi.fn(async () => ({
    scan: { team: [{ agent_id: 'reviewer', display_name: 'Reviewer' }] },
  }));
  const actions = createLiveUiActions({
    selection,
    appController: () => controller,
    activeView: () => state.view,
    // A pending autosave defers the transition until the test runs it.
    requestTransition: (action) => {
      if (!pendingTransition) return action();
      state.transitions.push(action);
      return false;
    },
    chatSelection: () => ({ agent_id: 'main', session_id: 's1' }),
    loadProject,
    terminalsView: () => state.terminalsView,
    projectsView: () => state.projectsView,
    afterRender: async () => {},
  });
  const guard = { isCurrent: () => true };
  return { state, selection, controller, loadProject, actions, guard };
}

describe('Live voice UI actions', () => {
  it('reports the visible app with the selected Project team', async () => {
    const f = fixture();
    expect(await f.actions.context()).toEqual({
      view: 'chat',
      selected_agent_id: 'main',
      selected_project_id: 'vbot',
      selected_project_agent_id: '',
      chat_selection: { agent_id: 'main', session_id: 's1' },
      agents: [
        { agent_id: 'main', name: 'Main' },
        { agent_id: 'coder', name: 'Coder' },
      ],
      projects: [{ project_id: 'vbot', name: 'vBot', cwd: 'C:\\work' }],
      selected_project_team: [{ agent_id: 'reviewer@vbot', name: 'Reviewer' }],
    });
    expect(f.loadProject).toHaveBeenCalledWith('vbot');
  });

  it('opens a Chat Session or a view', () => {
    const f = fixture();
    expect(
      f.actions.open(
        { view: 'chat', agent_id: 'coder', session_id: 's2' },
        f.guard,
      ),
    ).toBe(true);
    expect(f.controller.navigateToSession).toHaveBeenCalledWith('coder', 's2');
    expect(f.actions.open({ view: 'terminals' }, f.guard)).toBe(true);
    expect(f.controller.selectView).toHaveBeenCalledWith('terminals');
  });

  it('opens an Agent page through the shared Agent selection', () => {
    const f = fixture();
    expect(f.actions.open({ view: 'agents', agent_id: 'coder' }, f.guard)).toBe(
      true,
    );
    expect(f.selection.selectAgent).toHaveBeenCalledWith('coder');
    expect(f.controller.selectView).toHaveBeenCalledWith('agents');
    expect(f.actions.open({ view: 'agents', agent_id: 'gone' }, f.guard)).toBe(
      false,
    );
    expect(f.selection.selectAgent).toHaveBeenCalledOnce();
  });

  it('opens a Project page in a new or an already open Projects view', () => {
    const f = fixture();
    expect(
      f.actions.open({ view: 'projects', project_id: 'vbot' }, f.guard),
    ).toBe(true);
    expect(f.selection.selectManagedProject).toHaveBeenCalledWith('vbot');
    expect(f.controller.selectView).toHaveBeenCalledWith('projects');

    f.state.projectsView = { selectVoiceProject: vi.fn(() => true) };
    expect(
      f.actions.open({ view: 'projects', project_id: 'vbot' }, f.guard),
    ).toBe(true);
    expect(f.state.projectsView.selectVoiceProject).toHaveBeenCalledWith(
      'vbot',
    );
    expect(f.controller.selectView).toHaveBeenCalledOnce();
    expect(
      f.actions.open({ view: 'projects', project_id: 'other' }, f.guard),
    ).toBe(false);
  });

  it('never navigates for a call that ended during a deferred transition', () => {
    const f = fixture({ pendingTransition: true });
    let current = true;
    f.actions.open(
      { view: 'agents', agent_id: 'coder' },
      { isCurrent: () => current },
    );
    current = false;
    expect(f.state.transitions[0]()).toBe(false);
    expect(f.selection.selectAgent).not.toHaveBeenCalled();
    expect(f.controller.selectView).not.toHaveBeenCalled();
  });

  it('applies Terminal layout requests in the Terminals view', async () => {
    const f = fixture();
    f.state.terminalsView = {
      applyVoiceAction: vi.fn(async () => ({ visible_order: ['t1'] })),
      getVoiceContext: () => ({ visible_order: ['t1'] }),
    };
    expect(
      await f.actions.terminalView({ op: 'show', terminal_id: 't1' }, f.guard),
    ).toEqual({ visible_order: ['t1'] });
    expect(f.controller.selectView).toHaveBeenCalledWith('terminals');
    expect(f.state.terminalsView.applyVoiceAction).toHaveBeenCalledWith(
      'show',
      { terminal_id: 't1' },
    );
  });
});
