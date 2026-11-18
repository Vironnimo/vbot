import { getContext, setContext } from 'svelte';

const PROFILE = Symbol('agent profile binding');

export const provideAgentProfileBinding = (read) => setContext(PROFILE, read);
export const useAgentProfileBinding = () => getContext(PROFILE);

export function agentProfileParams(read, agentId) {
  const profile = read?.();
  return profile?.agentId === agentId && profile?.expectedIncarnation
    ? { expected_incarnation: profile.expectedIncarnation }
    : {};
}
