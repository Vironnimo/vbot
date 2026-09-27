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

function queuedSubAgentTool(overrides = {}) {
  return {
    name: 'subagent',
    status: 'success',
    arguments: {
      action: 'run',
      agent_id: 'worker',
      content: 'Inspect the project',
    },
    subAgentSession: {
      id: 'sub_queued',
      agent_id: 'worker',
      session_id: 'session-child',
      queue_item_id: 'queue-item-1',
      status: 'queued',
      delivery: 'automatic',
    },
    result: {
      ok: true,
      error: null,
      data: {
        id: 'sub_queued',
        agent_id: 'worker',
        session_id: 'session-child',
        status: 'queued',
        delivery: 'automatic',
      },
      artifacts: [],
    },
    ...overrides,
  };
}

function backgroundBashTool(overrides = {}) {
  return {
    type: 'tool_call',
    id: 'bash-background',
    name: 'bash',
    status: 'success',
    resultEvent: { type: 'tool_call_result' },
    arguments: { command: 'npm run dev', mode: 'background' },
    result: {
      ok: true,
      data: {
        process_id: 'process-one',
        status: 'running',
        delivery: 'automatic',
      },
      artifacts: [],
    },
    ...overrides,
  };
}

export { runningSubAgentTool, queuedSubAgentTool, backgroundBashTool };
