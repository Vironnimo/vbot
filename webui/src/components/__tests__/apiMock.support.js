export function rpcBackedApiMock(rpcMock, overrides = {}) {
  const call = (method, params) =>
    params === undefined ? rpcMock(method) : rpcMock(method, params);
  const providerParams = (providerId, connectionId, account) => ({
    provider_id: providerId,
    connection_id: connectionId,
    ...(account === undefined ? {} : { account }),
  });

  return {
    openFilePreview: (source) => call('file.preview_open', { source }),
    getFilePreviewRevision: (token) => call('file.preview_revision', { token }),
    rpc: (...args) => rpcMock(...args),
    extensionOperation: (name, operation, args = {}) =>
      call('extensions.operation', { name, operation, arguments: args }),
    getSettings: () => call('settings.get'),
    listExtensionPages: () => call('extensions.pages'),
    reportClientMetrics: (report) => call('performance.client_report', report),
    updateSettings: (params) => call('settings.update', params),
    getRecallIndexStatus: () => call('recall.status'),
    dismissBackgroundActivity: (id) => call('activity.dismiss', { id }),
    cancelLocalSetup: (target) =>
      call('task_model.local_setup_cancel', { target }),
    rebuildRecallIndex: () => call('recall.rebuild_index'),
    setServiceKey: (params) => call('settings.set_service_key', params),
    listAgents: () => call('agent.list'),
    addCalendarAction: (params) => call('calendar.add_action', params),
    updateCalendarAction: (params) => call('calendar.update_action', params),
    deleteCalendarAction: (id) => call('calendar.delete_action', { id }),
    reorderAgents: (agentIds, expectedRevision) =>
      call('agent.reorder', {
        agent_ids: agentIds,
        expected_revision: expectedRevision,
      }),
    getAgent: (id) => call('agent.get', { id }),
    createAgent: (params) => call('agent.create', params),
    updateAgent: (params) => call('agent.update', params),
    renameAgent: (id, newId) => call('agent.rename', { id, new_id: newId }),
    deleteAgent: (id, { permanent = false } = {}) =>
      call('agent.delete', { id, ...(permanent ? { permanent } : {}) }),
    listAgentMemories: (agentId) => call('memory.list', { agent_id: agentId }),
    addAgentMemory: (agentId, scope, content) =>
      call('memory.add', { agent_id: agentId, scope, content }),
    replaceAgentMemory: (agentId, scope, entryId, content) =>
      call('memory.replace', {
        agent_id: agentId,
        scope,
        entry_id: entryId,
        content,
      }),
    removeAgentMemory: (agentId, scope, entryId) =>
      call('memory.remove', {
        agent_id: agentId,
        scope,
        entry_id: entryId,
      }),
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
    createSkill: (params) => call('skill.create', params),
    installSkill: (params) => call('skill.install', params),
    updateSkill: (params) => call('skill.update', params),
    deleteSkill: (scope, name) => call('skill.delete', { scope, name }),
    skillInventory: () => call('skill.inventory'),
    inspectSkill: (id) => call('skill.inspect', { id }),
    setSkillDisabled: (id, disabled) =>
      call('skill.set_disabled', { id, disabled }),
    setSkillPinned: (scope, name, pinned) =>
      call('skill.set_pinned', { scope, name, pinned }),
    skillHistory: (scope, name, limit) =>
      call('skill.history', { scope, name, limit }),
    revertSkillRevisions: (scope, revisions) =>
      call('skill.revert', { scope, revisions }),
    librarianStatus: (agentId) =>
      call('librarian.status', { agent_id: agentId }),
    librarianOverview: () => call('librarian.overview', {}),
    runLibrarian: (agentId) => call('librarian.run', { agent_id: agentId }),
    restoreSkill: (scope, archiveId) =>
      call('skill.restore', { scope, archive_id: archiveId }),
    purgeSkill: (scope, archiveId) =>
      call('skill.purge', { scope, archive_id: archiveId }),
    shareSkill: (agentId, name, shared, receivers = []) =>
      call('skill.share', { agent_id: agentId, name, shared, receivers }),
    listChatCommands: (params = {}) => call('chat.commands', params),
    loadChatHistory: (params) => call('chat.history', params),
    loadReflectionRuns: (params) => call('chat.reflections', params),
    deleteSession: (agentId, sessionId, { permanent = false } = {}) =>
      call('session.delete', {
        agent_id: agentId,
        session_id: sessionId,
        ...(permanent ? { permanent } : {}),
      }),
    listSessionActivity: (agentIds) =>
      call('session.activity_list', { agent_ids: agentIds }),
    getSession: (agentId, sessionId) =>
      call('session.get', { agent_id: agentId, session_id: sessionId }),
    getSessionChangeStats: (agentId, sessionId) =>
      call('session.change_stats', {
        agent_id: agentId,
        session_id: sessionId,
      }),
    getCalendarWindow: (params) => call('calendar.window', params),
    createCalendarEvent: (params) => call('calendar.create', params),
    updateCalendarEvent: (params) => call('calendar.update', params),
    deleteCalendarEvent: (id) => call('calendar.delete', { id }),
    addCalendarExdate: (params) => call('calendar.add_exdate', params),
    startChatRun: (params) => call('chat.stream', params),
    controlRun: ({ agentId, sessionId, runId, action, toolCallId } = {}) =>
      call('chat.control_run', {
        agent_id: agentId,
        session_id: sessionId,
        run_id: runId,
        action,
        ...(toolCallId ? { tool_call_id: toolCallId } : {}),
      }),
    cancelToolCall: ({ agentId, runId, toolCallId } = {}) =>
      call('chat.cancel_tool_call', {
        agent_id: agentId,
        run_id: runId,
        tool_call_id: toolCallId,
      }),
    stopAll: (agentId, sessionId) =>
      call('chat.stop_all', { agent_id: agentId, session_id: sessionId }),
    listFiles: (agentId, { sessionId = '', workingProjectId } = {}) =>
      call('files.list', {
        agent_id: agentId,
        ...(sessionId
          ? { session_id: sessionId }
          : workingProjectId !== undefined
            ? { working_project_id: workingProjectId }
            : {}),
      }),
    listServerDirectory: ({ path = null, root, include_files } = {}) =>
      call('filesystem.list', {
        path,
        ...(root === undefined ? {} : { root }),
        ...(include_files ? { include_files: true } : {}),
      }),
    setSessionAgentOverrides: (agentId, sessionId, agentOverrides) =>
      call('session.set_agent_overrides', {
        agent_id: agentId,
        session_id: sessionId,
        agent_overrides: agentOverrides,
      }),
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
    listCustomProviders: () => call('provider.custom_list'),
    saveCustomProvider: (params) => call('provider.custom_save', params),
    deleteCustomProvider: (params) => call('provider.custom_delete', params),
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
    getProviderUsageHistory: (params = {}) =>
      call('provider.usage_history', params),
    clearProviderUsageHistory: () => call('provider.usage_history.clear', {}),
    listProviderRoutingOptions: (params) =>
      call('provider.routing_options', params),
    listChannels: () => call('channel.list'),
    getChannelStatus: (id) => call('channel.status', { id }),
    getChannelAccess: (id) => call('channel.access.get', { id }),
    setChannelIdentity: (id, userId) =>
      call('channel.identity.set', { id, user_id: userId }),
    grantChannelAdmin: (id, accessScopeId, userId) =>
      call('channel.admin.grant', {
        id,
        access_scope_id: accessScopeId,
        user_id: userId,
      }),
    revokeChannelAdmin: (id, accessScopeId, userId) =>
      call('channel.admin.revoke', {
        id,
        access_scope_id: accessScopeId,
        user_id: userId,
      }),
    createChannel: (params) => call('channel.create', params),
    updateChannel: (params) => call('channel.update', params),
    enableChannel: (id) => call('channel.enable', { id }),
    disableChannel: (id) => call('channel.disable', { id }),
    deleteChannel: (id) => call('channel.delete', { id }),
    listExtensions: () => call('extensions.list'),
    listExtensionRequests: () => call('extensions.requests'),
    reloadExtensions: () => call('extensions.reload'),
    setExtensionSecret: (params) => call('extensions.set_secret', params),
    getStatisticsReport: (params) => call('statistics.report', params),
    getStatisticsRunActivity: (params) =>
      call('statistics.run_activity', params),
    listProjects: () => call('project.list'),
    listCronJobs: () => call('cron.list'),
    showProject: (projectId) => call('project.show', { project_id: projectId }),
    detectProject: (cwd) => call('project.detect', { cwd }),
    addProject: (params) => call('project.add', params),
    setProject: (projectId, changes) =>
      call('project.set', { project_id: projectId, ...changes }),
    removeProject: (
      projectId,
      { copyRootedAgentIdentityFiles = false, permanent = false } = {},
    ) =>
      call('project.rm', {
        project_id: projectId,
        copy_rooted_agent_identity_files: copyRootedAgentIdentityFiles,
        ...(permanent ? { permanent } : {}),
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
    listArchiveEntries: (params = {}) => call('archive.list', params),
    showArchiveEntry: (entryId) => call('archive.show', { entry_id: entryId }),
    restoreArchiveEntry: (entryId, { targetId = '' } = {}) =>
      call('archive.restore', {
        entry_id: entryId,
        ...(targetId ? { target_id: targetId } : {}),
      }),
    purgeArchiveEntries: (params = {}) => call('archive.purge', params),
    ...overrides,
  };
}
