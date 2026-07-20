// First-run onboarding logic. Pure, unit-testable helpers that decide whether
// the guided setup shows, resolve the three step-1 entry points to the exact
// props ProviderConnectModal expects, and carry the per-provider model-step
// recommendation. No Svelte, no rpc — the wizard component orchestrates; the
// decisions live here.

import {
  getAddProviderCandidates,
  getAddableConnections,
  getUsableProviderItems,
  isOAuthDeviceFlowConnection,
} from './settingsView.js';

// The provider highlighted as the recommended starting point (the hero card):
// one account unlocks many models including free ones, so a newcomer reaches a
// first success at zero cost without a pre-existing subscription.
export const ONBOARDING_HERO_PROVIDER_ID = 'openrouter';

// The bootstrap agent the wizard assigns the chosen model to. A fresh install
// always seeds this single identity agent.
export const ONBOARDING_TARGET_AGENT_ID = 'main';

// Per-provider model-search prefill: a language-neutral technical token that
// matches text inside model ids (NOT user copy, so deliberately not routed
// through i18n). Typing `free` in OpenRouter's model search narrows to its
// no-cost models (ids containing `:free`).
export const PROVIDER_MODEL_SEARCH_PREFILL = Object.freeze({
  [ONBOARDING_HERO_PROVIDER_ID]: 'free',
});

// Operational ⇔ at least one Connection is server-confirmed usable. This is the
// live trigger the wizard hangs off: a configured but disabled keyless local
// Connection must not skip setup; connecting or enabling one flips it true.
export function isOperational(settings) {
  return getUsableProviderItems(settings).length > 0;
}

// Whether an agent has no model assigned yet (empty/whitespace). A model-less
// agent cannot run, so Chat surfaces an actionable notice instead of failing a
// send at the provider.
export function agentNeedsModel(agent) {
  const model = typeof agent?.model === 'string' ? agent.model.trim() : '';
  return model.length === 0;
}

// The connected provider that drives the model step's tip/prefill: the first
// provider with a usable Connection. On a fresh onboarding there is exactly one.
export function connectedProviderId(settings) {
  const first = getUsableProviderItems(settings)[0];
  return typeof first?.id === 'string' ? first.id : '';
}

// The i18n key for a provider's model-step recommendation tip. The component
// renders it only when the catalog actually carries the key (most providers
// have none), so an unknown provider simply gets no tip.
export function providerTipKey(providerId) {
  const id = typeof providerId === 'string' ? providerId.trim() : '';
  return id ? `onboarding.provider.tip.${id}` : '';
}

// The prefill token for a provider's model search, or '' when it has none.
export function providerModelSearchPrefill(providerId) {
  const id = typeof providerId === 'string' ? providerId.trim() : '';
  return PROVIDER_MODEL_SEARCH_PREFILL[id] ?? '';
}

function apiKeyConnection(provider) {
  return (
    getAddableConnections(provider).find(
      (connection) => connection?.type === 'api_key',
    ) ?? null
  );
}

function deviceFlowConnection(provider) {
  return (
    getAddableConnections(provider).find(isOAuthDeviceFlowConnection) ?? null
  );
}

// Resolve the ProviderConnectModal scoping props for one provider, preferring a
// connection of the given kind ('api_key' | 'oauth'). A single addable
// connection is always scoped; with several, the preferred kind is scoped so
// the modal opens straight on that method (else it opens on the method chooser
// with the connection left null).
export function providerModalScope(provider, preferredType = 'api_key') {
  if (!provider) {
    return null;
  }
  const addable = getAddableConnections(provider);
  if (addable.length === 0) {
    return null;
  }
  const connection =
    addable.length === 1
      ? addable[0]
      : preferredType === 'oauth'
        ? deviceFlowConnection(provider)
        : apiKeyConnection(provider);
  return {
    scopedProvider: provider,
    scopedConnection: connection ?? null,
    providers: [],
  };
}

// Step-1 hero entry: OpenRouter scoped to its API-key connection, or null when
// OpenRouter is already connected (not an addable candidate) so the hero hides.
export function onboardingHeroScope(settings) {
  const hero = getAddProviderCandidates(settings).find(
    (provider) => provider.id === ONBOARDING_HERO_PROVIDER_ID,
  );
  return hero ? providerModalScope(hero, 'api_key') : null;
}

// Step-1 subscription entries: addable candidates offering an OAuth device flow
// — sign in with an existing subscription, no API key (ChatGPT, GitHub Copilot).
export function onboardingSubscriptionProviders(settings) {
  return getAddProviderCandidates(settings).filter(
    (provider) => deviceFlowConnection(provider) !== null,
  );
}

// Step-1 "more services": addable candidates with an API-key connection, minus
// the hero (surfaced above). OpenAI stays here for its key even though it also
// appears under subscription; a device-flow-only provider (Copilot) does not.
export function onboardingMoreProviders(settings) {
  return getAddProviderCandidates(settings).filter(
    (provider) =>
      provider.id !== ONBOARDING_HERO_PROVIDER_ID &&
      apiKeyConnection(provider) !== null,
  );
}
