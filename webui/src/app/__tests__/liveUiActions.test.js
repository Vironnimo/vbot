import { describe, expect, it, vi } from 'vitest';
import { createLiveUiActions } from '../liveUiActions.js';

function fixture({ view = 'chat', pendingTransition = false } = {}) {
  const state = {
    view,
    transitions: [],
    terminalsView: null,
  };
  const selection = {
    selectedAgentId: 'main',
    selectedProjectId: 'vbot',
    agents: [
      { id: 'main', name: 'Main' },
      { id: 'coder', name: 'Coder' },
    ],
    projects: [{ project_id: 'vbot', display_name: 'vBot', cwd: 'C:\\work' }],
    selectAgent: vi.fn(),
    selectManagedProject: vi.fn(),
  };
  const navigator = {
    open: vi.fn((next) => {
      state.view = next;
    }),
    navigate: vi.fn((next) => {
      state.view = next;
    }),
  };
  const navigateToSession = vi.fn(() => true);
  let shown = { agentId: 'main', sessionId: 's1', subAgent: false };
  const actions = createLiveUiActions({
    selection,
    navigator,
    navigateToSession,
    activeView: () => state.view,
    // A pending autosave defers the transition until the test runs it.
    requestTransition: (action) => {
      if (!pendingTransition) return action();
      state.transitions.push(action);
      return false;
    },
    chatSelection: () => shown,
    terminalsView: () => state.terminalsView,
    afterRender: async () => {},
  });
  const guard = { isCurrent: () => true };
  return {
    state,
    selection,
    navigator,
    navigateToSession,
    actions,
    guard,
    showSession: (session) => {
      shown = session;
    },
  };
}

describe('Live voice UI actions', () => {
  it('reports what the app shows', () => {
    const f = fixture();
    expect(f.actions.context()).toEqual({
      view: 'chat',
      selected_agent_id: 'main',
      selected_project_id: 'vbot',
      chat_session: { agent_id: 'main', session_id: 's1' },
    });
    f.selection.selectedProjectId = '';
    f.showSession(null);
    f.state.view = 'terminals';
    expect(f.actions.context()).toEqual({
      view: 'terminals',
      selected_agent_id: 'main',
      selected_project_id: null,
      chat_session: null,
    });
  });

  it('opens a Chat Session or a view', () => {
    const f = fixture();
    expect(
      f.actions.open(
        { view: 'chat', agent_id: 'coder', session_id: 's2' },
        f.guard,
      ),
    ).toBe(true);
    expect(f.navigateToSession).toHaveBeenCalledWith('coder', 's2');
    expect(f.actions.open({ view: 'terminals' }, f.guard)).toBe(true);
    expect(f.navigator.open).toHaveBeenCalledWith('terminals');
  });

  it('opens an Agent page through the shared Agent selection', () => {
    const f = fixture();
    expect(f.actions.open({ view: 'agents', agent_id: 'coder' }, f.guard)).toBe(
      true,
    );
    expect(f.selection.selectAgent).toHaveBeenCalledWith('coder');
    expect(f.navigator.navigate).toHaveBeenCalledWith('agents', ['coder']);
    expect(f.actions.open({ view: 'agents', agent_id: 'gone' }, f.guard)).toBe(
      false,
    );
    expect(f.selection.selectAgent).toHaveBeenCalledOnce();
  });

  it('opens a known Project page', () => {
    const f = fixture();
    expect(
      f.actions.open({ view: 'projects', project_id: 'vbot' }, f.guard),
    ).toBe(true);
    expect(f.navigator.navigate).toHaveBeenCalledWith('projects', ['vbot']);
    expect(
      f.actions.open({ view: 'projects', project_id: 'other' }, f.guard),
    ).toBe(false);
  });

  it.each([
    ['the call ended meanwhile', false],
    ['the call still runs', true],
  ])('never runs a deferred transition later when %s', (_label, still) => {
    // The deferred request already answered that the app did not switch.
    const f = fixture({ pendingTransition: true });
    let current = true;
    expect(
      f.actions.open(
        { view: 'agents', agent_id: 'coder' },
        { isCurrent: () => current },
      ),
    ).toBe(false);
    current = still;
    expect(f.state.transitions[0]()).toBe(false);
    expect(f.selection.selectAgent).not.toHaveBeenCalled();
    expect(f.navigator.navigate).not.toHaveBeenCalled();
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
    expect(f.navigator.open).toHaveBeenCalledWith('terminals');
    expect(f.state.terminalsView.applyVoiceAction).toHaveBeenCalledWith(
      'show',
      { terminal_id: 't1' },
    );
  });
});
