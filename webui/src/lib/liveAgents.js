// The built-in Agents of a Live voice call as the WebUI shows them: the voice
// model's Agent and the vBot backend's Agent. They are not in the Agent list,
// so a view that shows one of their Sessions names it itself. Settings ->
// Live voice edits both; their Sessions are read in Chat but not written to.
import { t } from './i18n.js';

export const LIVE_VOICE_AGENT_ID = 'live-voice';
export const LIVE_BACKEND_AGENT_ID = 'live-backend';

export function isLiveAgentId(agentId) {
  return agentId === LIVE_VOICE_AGENT_ID || agentId === LIVE_BACKEND_AGENT_ID;
}

// The display name of a Live Agent, or null for any other id.
export function liveAgentName(agentId) {
  if (agentId === LIVE_VOICE_AGENT_ID) return t('live.agent.voice');
  if (agentId === LIVE_BACKEND_AGENT_ID) return t('live.agent.backend');
  return null;
}
