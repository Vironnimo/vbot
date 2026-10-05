function runningSubAgentTool(overrides = {}) {
  return {
    name: 'subagent',
    status: 'success',
    arguments: {
      action: 'run',
      agent_id: 'worker',
      content: 'Inspect the project',
    },
    subAgentSession: {
      id: 'sub_child',
      agent_id: 'worker',
      session_id: 'session-child',
      run_id: 'run-child',
      status: 'running',
      delivery: 'automatic',
    },
    result: {
      ok: true,
      error: null,
      data: {
        id: 'sub_child',
        agent_id: 'worker',
        session_id: 'session-child',
        status: 'running',
        delivery: 'automatic',
      },
      artifacts: [],
    },
    ...overrides,
  };
}

// A `run` row restored from History: the Tool result names the Sub-Agent and
// its Session, but not the Run the live start event announced.
function reloadedSubAgentTool(overrides = {}) {
  return {
    name: 'subagent',
    status: 'success',
    arguments: {
      action: 'run',
      agent_id: 'worker',
      content: 'Inspect the project',
    },
    result: {
      ok: true,
      error: null,
      data: {
        id: 'sub_reloaded',
        agent_id: 'worker',
        session_id: 'session-child',
        status: 'running',
      },
      artifacts: [],
    },
    ...overrides,
  };
}

// A `send` row: another message to the existing Sub-Agent `sub_child`.
function sendSubAgentTool(overrides = {}) {
  return {
    name: 'subagent',
    status: 'success',
    arguments: {
      action: 'send',
      id: 'sub_child',
      content: 'Also check the tests',
    },
    result: {
      ok: true,
      error: null,
      data: {
        id: 'sub_child',
        agent_id: 'worker',
        session_id: 'session-child',
        status: 'steered',
      },
      artifacts: [],
    },
    ...overrides,
  };
}

// A `bash` call whose command went on running in terminal `term_one`.
function backgroundCommandTool(overrides = {}) {
  return {
    type: 'tool_call',
    id: 'bash-background',
    name: 'bash',
    status: 'success',
    resultEvent: { type: 'tool_call_result' },
    arguments: { command: 'npm run dev' },
    result: {
      ok: true,
      data: {
        status: 'running',
        terminal_id: 'term_one',
        output: 'listening',
        next: 'The result arrives as a new message.',
      },
      artifacts: [],
    },
    ...overrides,
  };
}

export {
  backgroundCommandTool,
  reloadedSubAgentTool,
  runningSubAgentTool,
  sendSubAgentTool,
};
