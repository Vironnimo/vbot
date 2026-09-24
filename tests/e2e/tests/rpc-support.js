// Direct RPC access for spec setup and cleanup. A spec's journey still runs
// through the browser; RPC only prepares or removes the records it depends on.
export async function rpc(request, method, params = {}) {
  const response = await request.post("/api/rpc", {
    data: { method, params },
    // Browser interactions can outlive Uvicorn's idle keep-alive window between
    // direct RPC calls, so do not let Playwright race a stale pooled socket.
    headers: { Connection: "close" },
  });
  const payload = await response.json();
  if (!response.ok() || payload?.ok !== true) {
    throw new Error(`RPC ${method} failed: ${JSON.stringify(payload)}`);
  }
  return payload.result;
}

export async function createAgent(request, { id, name, ...fields }) {
  return rpc(request, "agent.create", { id, name, ...fields });
}

// Cleanup must not mask the spec's own failure when the Agent is already gone.
export async function deleteAgentIfPresent(request, agentId) {
  const { agents } = await rpc(request, "agent.list");
  if (agents.some((agent) => agent.id === agentId)) {
    await rpc(request, "agent.delete", { id: agentId });
  }
}
