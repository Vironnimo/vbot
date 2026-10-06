import {
  setToolAccessPreference,
  toolAccessIncludes,
  TOOL_ACTIVATION_CONFIGURABLE,
} from './toolAccess.js';

// The MCP connector catalog (`catalog` and `add_from_catalog` of the MCP
// Extension): hosted services a user connects in one step, then signs in to
// and grants to Agents. Presentation and state only; the dialog owns the
// calls.

// The catalog's categories in display order (`CATEGORIES` in
// `resources/extensions/mcp/_connectors.py`).
export const MCP_CATALOG_CATEGORIES = [
  'knowledge',
  'productivity',
  'design',
  'development',
  'analytics',
  'business',
];

// The entries whose name, description or id contain every word of `query`,
// in `category` unless it is empty.
export function mcpCatalogMatches(entries, { query = '', category = '' }) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  return entries.filter((entry) => {
    if (category && entry.category !== category) return false;
    const text = `${entry.name} ${entry.description} ${entry.id}`.toLowerCase();
    return words.every((word) => text.includes(word));
  });
}

// A tile's mark without a vendor logo: the name's initials and a hue that
// the entry id fixes, so a service keeps its color across catalog changes.
export function mcpCatalogMark(entry) {
  const words = entry.name.split(/\s+/).filter(Boolean);
  const initials =
    words.length > 1
      ? `${words[0][0]}${words[1][0]}`.toUpperCase()
      : `${entry.name.charAt(0).toUpperCase()}${entry.name.charAt(1)}`;
  let hash = 0;
  for (const character of entry.id)
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return { initials, hue: hash % 360 };
}

// Where the sign-in of a connection stands, from its `status`:
// - `connected`: the connection works; nothing to sign in to any more.
// - `waiting`: the sign-in `request` waits for the user's browser.
// - `failed`: the connection failed for another reason than the time limit.
// - `timeout`: the sign-in (`request`, or the last one seen in
//   `lastRequest`) ran past its `expires_at`.
// - `preparing`: the connection is still starting its sign-in.
export function mcpSignInPhase(status, lastRequest = null, now = Date.now()) {
  if (status?.state === 'connected') return { phase: 'connected' };
  const expired = (request) =>
    Boolean(request?.expires_at) && Date.parse(request.expires_at) <= now;
  const request = (status?.pending_requests ?? []).find(
    (item) => item.kind === 'oauth',
  );
  if (request)
    return expired(request)
      ? { phase: 'timeout', request }
      : { phase: 'waiting', request };
  if (status?.state === 'failed')
    return { phase: expired(lastRequest) ? 'timeout' : 'failed' };
  return { phase: 'preparing' };
}

// The Identity Agents of an `agent.list` result that can be granted the
// connection Tool `toolName`, with whether each one already has it; vBot's
// built-in Agents are left out.
export function mcpAgentAccess(agents, tools, toolName) {
  const catalog = withConnectionTool(tools, toolName);
  return agents
    .filter((agent) => !agent.builtin)
    .map((agent) => ({
      agent,
      granted: toolAccessIncludes(agent.tool_access, toolName, catalog),
    }));
}

// The Tool access of `agent` with the connection Tool `toolName` enabled.
export function mcpGrantedToolAccess(agent, tools, toolName) {
  const catalog = withConnectionTool(tools, toolName);
  const tool = catalog.find((item) => item.name === toolName);
  return setToolAccessPreference(agent.tool_access, tool, true, catalog);
}

// The Tool list with the connection Tool `toolName`: one the list does not
// show yet still requires opt-in, like every connection Tool.
function withConnectionTool(tools, toolName) {
  if (tools.some((tool) => tool.name === toolName)) return tools;
  return [
    ...tools,
    {
      name: toolName,
      activation: TOOL_ACTIVATION_CONFIGURABLE,
      requires_opt_in: true,
    },
  ];
}
