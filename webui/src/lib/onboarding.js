// First-run projections use the server's Provider catalog and readiness flags.
import {
  getAddableConnections,
  getProviderItems,
  getUsableProviderItems,
  isOAuthDeviceFlowConnection,
} from './settingsView.js';

export const ONBOARDING_TARGET_AGENT_ID = 'main';

// Credentials alone do not make a disabled Connection operational.
export function isOperational(settings) {
  return getUsableProviderItems(settings).length > 0;
}

export function agentNeedsModel(agent) {
  const model = typeof agent?.model === 'string' ? agent.model.trim() : '';
  return model.length === 0;
}

export function connectedProviderId(settings, preferredId = '') {
  const usable = getUsableProviderItems(settings);
  return (
    usable.find((provider) => provider.id === preferredId)?.id ??
    usable[0]?.id ??
    ''
  );
}

// Choose the Provider first. Skip the method chooser only if there is no choice.
export function providerModalScope(provider) {
  const addable = getAddableConnections(provider);
  if (!provider || addable.length === 0) return null;
  return {
    scopedProvider: provider,
    scopedConnection: addable.length === 1 ? addable[0] : null,
    providers: [],
  };
}

export function onboardingProviders(settings, search = '') {
  const query = search.trim().toLocaleLowerCase();
  return getProviderItems(settings)
    .map((provider) => {
      const connections = (provider.connections ?? []).filter(
        (connection) =>
          connection.type === 'api_key' ||
          connection.type === 'none' ||
          isOAuthDeviceFlowConnection(connection) ||
          connection.usable === true,
      );
      return {
        provider,
        connected: connections.some((connection) => connection.usable === true),
        scope: provider.custom ? null : providerModalScope(provider),
        methodTypes: [
          ...new Set(connections.map((connection) => connection.type)),
        ],
        searchText: [
          provider.id,
          provider.name,
          ...connections.map((connection) => connection.label),
        ]
          .join(' ')
          .toLocaleLowerCase(),
      };
    })
    .filter(
      (item) =>
        (item.connected ||
          (!item.provider.custom && item.methodTypes.length > 0)) &&
        item.searchText.includes(query),
    )
    .sort((a, b) =>
      (a.provider.name ?? a.provider.id).localeCompare(
        b.provider.name ?? b.provider.id,
      ),
    );
}
