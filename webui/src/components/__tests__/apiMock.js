export function rpcBackedApiMock(rpcMock, overrides = {}) {
  const call = (method, params) =>
    params === undefined ? rpcMock(method) : rpcMock(method, params);
  const providerParams = (providerId, connectionId, account) => ({
    provider_id: providerId,
    connection_id: connectionId,
    ...(account === undefined ? {} : { account }),
  });

  return {
    rpc: (...args) => rpcMock(...args),
    getSettings: () => call('settings.get'),
    updateSettings: (params) => call('settings.update', params),
    listAgents: () => call('agent.list'),
    getAgent: (id) => call('agent.get', { id }),
    createAgent: (params) => call('agent.create', params),
    updateAgent: (params) => call('agent.update', params),
    deleteAgent: (id) => call('agent.delete', { id }),
    listModels: (params = {}) =>
      Object.keys(params).length === 0
        ? call('model.list')
        : call('model.list', params),
    refreshModelDatabase: (params = {}) =>
      Object.keys(params).length === 0
        ? call('model.refresh_db')
        : call('model.refresh_db', params),
    listConnections: () => call('connection.list'),
    setConnectionEnabled: (params) => call('connection.set_enabled', params),
    listTools: () => call('tool.list'),
    listSkills: (params = {}) =>
      Object.keys(params).length === 0
        ? call('skill.list')
        : call('skill.list', params),
    readSkills: (scope) => call('skill.read', { scope }),
    createSkill: (params) => call('skill.create', params),
    updateSkill: (params) => call('skill.update', params),
    deleteSkill: (scope, name) => call('skill.delete', { scope, name }),
    listChatCommands: (params = {}) => call('chat.commands', params),
    loadChatHistory: (params) => call('chat.history', params),
    createSession: (params) => call('session.create', params),
    startChatRun: (params) => call('chat.stream', params),
    listFiles: (agentId) => call('files.list', { agent_id: agentId }),
    listPrompts: (params = {}) => call('prompt.list', params),
    updatePromptBlock: (params) => call('prompt.update', params),
    resetPromptBlock: (params) => call('prompt.reset', params),
    createPromptBlock: (params) => call('prompt.create_block', params),
    removePromptBlock: (params) => call('prompt.remove_block', params),
    resetPromptLayout: (params = {}) => call('prompt.reset_layout', params),
    setPromptLayout: (params) => call('prompt.set_layout', params),
    previewPrompt: (params) => call('prompt.preview', params),
    setProviderKey: (params) => call('provider.set_key', params),
    unsetProviderKey: (params) => call('provider.unset_key', params),
    connectProvider: (providerId, connectionId, account) =>
      call(
        'provider.connect',
        providerParams(providerId, connectionId, account),
      ),
    disconnectProvider: (providerId, connectionId, account) =>
      call(
        'provider.disconnect',
        providerParams(providerId, connectionId, account),
      ),
    getProviderUsage: () => call('provider.usage'),
    listProviderRoutingOptions: (params) =>
      call('provider.routing_options', params),
    listChannels: () => call('channel.list'),
    getChannelStatus: (id) => call('channel.status', { id }),
    createChannel: (params) => call('channel.create', params),
    updateChannel: (params) => call('channel.update', params),
    enableChannel: (id) => call('channel.enable', { id }),
    disableChannel: (id) => call('channel.disable', { id }),
    deleteChannel: (id) => call('channel.delete', { id }),
    listExtensions: () => call('extensions.list'),
    reloadExtensions: () => call('extensions.reload'),
    setExtensionSecret: (params) => call('extensions.set_secret', params),
    getStatisticsReport: () => call('statistics.report'),
    listProjects: () => call('project.list'),
    showProject: (projectId) => call('project.show', { project_id: projectId }),
    detectProject: (cwd) => call('project.detect', { cwd }),
    addProject: (params) => call('project.add', params),
    setProject: (projectId, changes) =>
      call('project.set', { project_id: projectId, ...changes }),
    removeProject: (projectId, copyIdentityFiles = false) =>
      call('project.remove', {
        project_id: projectId,
        copy_identity_files: copyIdentityFiles,
      }),
    setOverride: (projectId, agentId, field, value) =>
      call('project.set_override', {
        project_id: projectId,
        agent_id: agentId,
        field,
        value,
      }),
    clearOverride: (projectId, agentId, field) =>
      call('project.clear_override', {
        project_id: projectId,
        agent_id: agentId,
        field,
      }),
    debugStatus: () => call('debug.status'),
    debugTraceList: () => call('debug.trace_list'),
    debugTraceGet: (traceId) => call('debug.trace_get', { trace_id: traceId }),
    debugTraceClear: () => call('debug.trace_clear'),
    ...overrides,
  };
}
