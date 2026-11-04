// The server resolves ownership; the browser never reconstructs its proof.
export function agentPlacement(agent) {
  return agent?.placement ?? { kind: 'chat' };
}
export function placementKind(agent) {
  return agentPlacement(agent).kind;
}
export function isChatAgent(agent) {
  return placementKind(agent) === 'chat';
}
export function firstChatAgent(agents = []) {
  return agents.find(isChatAgent);
}
export function privateAgents(agents = [], ownerId) {
  return agents.filter((agent) => {
    const placement = agentPlacement(agent);
    return (
      placement.kind === 'private' &&
      !placement.orphaned &&
      placement.owner_id === ownerId
    );
  });
}
export function automaticAgentTargets(agents = [], caller) {
  const placement = agentPlacement(caller);
  if (isChatAgent(caller)) return privateAgents(agents, caller?.id);
  if (placement.kind === 'private' && !placement.orphaned)
    return privateAgents(agents, placement.owner_id).filter(
      (agent) => agent.id !== caller.id,
    );
  return [];
}
export function helperAgents(agents = []) {
  return agents.filter(
    (agent) =>
      placementKind(agent) === 'helper' || agentPlacement(agent).orphaned,
  );
}
export function agentDisplayOrder(agents = []) {
  return [
    ...agents
      .filter(isChatAgent)
      .flatMap((agent) => [agent, ...privateAgents(agents, agent.id)]),
    ...helperAgents(agents),
  ];
}
export function agentDisplayName(agent, agents = []) {
  const placement = agentPlacement(agent);
  const name = agent?.name || agent?.id || '';
  if (placement.kind !== 'private' || placement.orphaned) return name;
  const owner = agents.find((candidate) => candidate.id === placement.owner_id);
  return owner ? `${owner.name || owner.id} / ${name}` : name;
}
// Keep the flat roster's interleaving stable while reordering one group.
export function reorderAgentGroup(agents, reorderedIds) {
  const members = new Set(reorderedIds);
  let index = 0;
  return agents.map((agent) =>
    members.has(agent.id) ? reorderedIds[index++] : agent.id,
  );
}
export function canDeleteAgent(agent, agents = []) {
  return (
    Boolean(agent) &&
    (!isChatAgent(agent) || agents.filter(isChatAgent).length > 1)
  );
}
