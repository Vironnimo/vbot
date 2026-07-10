const DEFAULT_ACCOUNT_ID = 'default';

// Soft suitability filter threshold for agent-model pickers: models below this
// effective context window (or without tool calling) are hidden by default and
// revealed by the "show all models" toggle with an honest badge. A view
// constant — the backend never hard-filters.
export const SUITABLE_MIN_CONTEXT = 32768;

const SUITABILITY_REASON_NO_TOOLS = 'noTools';
const SUITABILITY_REASON_BELOW_MIN_CONTEXT = 'belowMinContext';
const SUITABILITY_REASON_CONTEXT_UNKNOWN = 'contextUnknown';

export function modelSuitability(model) {
  const reasons = [];

  if (model?.capabilities?.tools !== true) {
    reasons.push(SUITABILITY_REASON_NO_TOOLS);
  }

  const effectiveWindow =
    model?.effective_context_window ?? model?.context_window ?? null;

  if (effectiveWindow === null || effectiveWindow === undefined) {
    // Unknown context is hidden by default but badged honestly as unknown,
    // never as "below 32k".
    reasons.push(SUITABILITY_REASON_CONTEXT_UNKNOWN);
  } else if (effectiveWindow < SUITABLE_MIN_CONTEXT) {
    reasons.push(SUITABILITY_REASON_BELOW_MIN_CONTEXT);
  }

  return { suitable: reasons.length === 0, reasons };
}

export function filterModelSelectOptions(
  options,
  { showAll = false, selectedModelValue = '' } = {},
) {
  if (showAll) {
    return options;
  }

  const selection = parseModelSelectionValue(selectedModelValue);
  const exactValue = modelSelectionValue(
    selection.model,
    selection.connectionLocalId,
  );
  const canonicalValue = canonicalModelSelectionValue(exactValue);

  // The currently selected value stays visible even when unsuitable, so an
  // existing configuration is never silently masked by the filter.
  return options.filter(
    (option) =>
      option.suitable !== false ||
      option.value === exactValue ||
      option.value === canonicalValue,
  );
}

export function modelFilterFooterLabel({
  showAll = false,
  hiddenCount = 0,
  translate = defaultTranslate,
} = {}) {
  if (showAll) {
    return translate('models.filter.showSuitable', 'Show only suitable models');
  }

  if (hiddenCount <= 0) {
    return '';
  }

  return translate(
    'models.filter.showAll',
    'Show all models ({count} hidden)',
    {
      count: hiddenCount,
    },
  );
}

function suitabilityBadgeLabel(reasons, translate) {
  const labels = {
    [SUITABILITY_REASON_NO_TOOLS]: translate(
      'models.filter.noTools',
      'no tool calling',
    ),
    [SUITABILITY_REASON_BELOW_MIN_CONTEXT]: translate(
      'models.filter.belowMinContext',
      'below 32k context',
    ),
    [SUITABILITY_REASON_CONTEXT_UNKNOWN]: translate(
      'models.filter.contextUnknown',
      'context unknown',
    ),
  };

  return reasons
    .map((reason) => labels[reason])
    .filter(Boolean)
    .join(' · ');
}

function suitabilityFields(model, translate) {
  const { suitable, reasons } = modelSuitability(model);
  // A known-down local endpoint (model.list `reachable: false`, e.g. Ollama
  // not running) badges the option but never hides it — the model stays
  // selectable so agents can be configured ahead of starting the service.
  const unreachableLabel =
    model?.reachable === false
      ? translate('models.filter.unreachable', 'service not running')
      : '';

  if (suitable) {
    return unreachableLabel
      ? {
          suitable: true,
          suitabilityReasons: [],
          secondaryLabel: unreachableLabel,
        }
      : { suitable: true, suitabilityReasons: [] };
  }

  const badge = suitabilityBadgeLabel(reasons, translate);
  return {
    suitable: false,
    suitabilityReasons: reasons,
    secondaryLabel: unreachableLabel ? `${badge} · ${unreachableLabel}` : badge,
  };
}

export function buildModelSelectOptions({
  models = [],
  connections = [],
  selectedModelValue = '',
  emptyLabel = '',
  translate = defaultTranslate,
} = {}) {
  const connectionsByProvider = usableConnectionsByProvider(connections);
  const selectedModel = parseModelSelectionValue(selectedModelValue);
  const selectedConnectionId = connectionIdFromModel(
    selectedModel.model,
    selectedModel.connectionLocalId,
  );
  const selectedValue = modelSelectionValue(
    selectedModel.model,
    selectedModel.connectionLocalId,
  );
  const canonicalSelectedValue = canonicalModelSelectionValue(selectedValue);
  const modelExistsInCatalog = models.some(
    (model) => model.id === selectedModel.model,
  );
  const selectedCatalogModel = models.find(
    (model) => model.id === selectedModel.model,
  );
  const selectedModelOption =
    selectedModel.model &&
    !selectedModel.connectionLocalId &&
    modelExistsInCatalog
      ? {
          value: selectedModel.model,
          label: selectedModel.model,
          isUnavailable: false,
          ...suitabilityFields(selectedCatalogModel, translate),
        }
      : null;
  const emptyOption = {
    value: '',
    label: emptyLabel,
    isUnavailable: false,
  };
  const catalogOptions = models.flatMap((model) => {
    const providerConnections = connectionsAllowedForModel(
      model,
      connectionsByProvider[model.provider_id] ?? [],
    );

    return providerConnections.flatMap((connection) =>
      connectionModelOptions(
        model,
        connection,
        providerConnections.length,
        translate,
      ),
    );
  });

  if (
    !selectedValue ||
    catalogOptions.some((option) => option.value === canonicalSelectedValue) ||
    selectedModelOption
  ) {
    return selectedModelOption
      ? [emptyOption, selectedModelOption, ...catalogOptions]
      : [emptyOption, ...catalogOptions];
  }

  return [
    emptyOption,
    {
      value: selectedValue,
      label: unavailableModelOptionLabel(
        selectedModel.model,
        selectedConnectionId,
        connections,
        translate,
      ),
      isUnavailable: true,
    },
    ...catalogOptions,
  ];
}

export function selectModelValue(modelValue, options) {
  const selection = parseModelSelectionValue(modelValue);

  if (!selection.model) {
    return '';
  }

  const exactValue = modelSelectionValue(
    selection.model,
    selection.connectionLocalId,
  );

  if (options.some((option) => option.value === exactValue)) {
    return exactValue;
  }

  const canonicalValue = canonicalModelSelectionValue(exactValue);

  if (
    canonicalValue !== exactValue &&
    options.some((option) => option.value === canonicalValue)
  ) {
    return canonicalValue;
  }

  if (selection.connectionLocalId) {
    return exactValue;
  }

  return selection.model;
}

export function modelSelectionValue(model, connectionLocalId) {
  if (!model) {
    return '';
  }

  if (!connectionLocalId) {
    return model;
  }

  return `${model}::${connectionLocalId}`;
}

export function parseModelSelectionValue(selectedValue) {
  if (!selectedValue) {
    return { model: '', connectionLocalId: '' };
  }

  const separatorIndex = selectedValue.lastIndexOf('::');

  if (separatorIndex === -1) {
    return { model: selectedValue, connectionLocalId: '' };
  }

  return {
    model: selectedValue.slice(0, separatorIndex),
    connectionLocalId: selectedValue.slice(separatorIndex + 2),
  };
}

function connectionModelOptions(
  model,
  connection,
  providerConnectionCount,
  translate,
) {
  const localId = connectionLocalIdFromConnectionId(connection.id);
  const usableAccounts = usableConnectionAccounts(connection);
  const suitability = suitabilityFields(model, translate);

  if (usableAccounts.length <= 1) {
    return [
      {
        value: modelSelectionValue(model.id, localId),
        label: modelOptionLabel(model, connection, providerConnectionCount),
        isUnavailable: false,
        ...suitability,
      },
    ];
  }

  return usableAccounts.map((account) => ({
    value: modelSelectionValue(
      model.id,
      accountConnectionSuffix(localId, account.id),
    ),
    label: accountModelOptionLabel(
      model,
      connection,
      account.id,
      providerConnectionCount,
      translate,
    ),
    isUnavailable: false,
    ...suitability,
  }));
}

function accountConnectionSuffix(localId, accountId) {
  if (accountId === DEFAULT_ACCOUNT_ID) {
    return localId;
  }

  return `${localId}:${accountId}`;
}

function usableConnectionAccounts(connection) {
  if (!Array.isArray(connection?.accounts)) {
    return [];
  }

  return connection.accounts.filter(
    (account) =>
      account?.usable === true &&
      typeof account.id === 'string' &&
      account.id.length > 0,
  );
}

function canonicalModelSelectionValue(value) {
  const selection = parseModelSelectionValue(value);

  if (!selection.connectionLocalId) {
    return value;
  }

  const { localId, accountId } = splitConnectionSuffix(
    selection.connectionLocalId,
  );

  if (accountId !== DEFAULT_ACCOUNT_ID) {
    return value;
  }

  return modelSelectionValue(selection.model, localId);
}

function splitConnectionSuffix(suffix) {
  const separatorIndex = suffix.indexOf(':');

  if (separatorIndex === -1) {
    return { localId: suffix, accountId: '' };
  }

  return {
    localId: suffix.slice(0, separatorIndex),
    accountId: suffix.slice(separatorIndex + 1),
  };
}

function usableConnectionsByProvider(connections) {
  const connectionsByProvider = {};

  for (const connection of connections) {
    if (!connection?.usable || !connection.provider_id) {
      continue;
    }

    if (!connectionsByProvider[connection.provider_id]) {
      connectionsByProvider[connection.provider_id] = [];
    }

    connectionsByProvider[connection.provider_id].push(connection);
  }

  return connectionsByProvider;
}

function connectionsAllowedForModel(model, providerConnections) {
  const allowlist = Array.isArray(model?.connections) ? model.connections : [];
  if (allowlist.length === 0) {
    return providerConnections;
  }

  return providerConnections.filter((connection) =>
    allowlist.includes(connectionLocalIdFromConnectionId(connection.id)),
  );
}

function modelOptionLabel(model, connection, providerConnectionCount) {
  if (providerConnectionCount <= 1) {
    return model.id;
  }

  return `${model.id} (${connection.label})`;
}

function accountModelOptionLabel(
  model,
  connection,
  accountId,
  providerConnectionCount,
  translate,
) {
  const accountName = accountDisplayName(accountId, translate);

  if (providerConnectionCount <= 1) {
    return `${model.id} (${accountName})`;
  }

  return `${model.id} (${connection.label} – ${accountName})`;
}

function accountDisplayName(accountId, translate) {
  if (accountId === DEFAULT_ACCOUNT_ID) {
    return translate('settings.providers.accounts.defaultLabel', 'Default');
  }

  return accountId;
}

function unavailableModelOptionLabel(
  model,
  connectionId,
  connections,
  translate,
) {
  if (!connectionId) {
    return translate(
      'agents.form.modelUnavailableOption',
      'Unavailable / custom: {model}',
      {
        model,
      },
    );
  }

  return translate(
    'agents.form.modelUnavailableConnectionOption',
    'Unavailable / custom: {model} ({connection})',
    {
      connection: connectionDisplayLabel(connectionId, connections, translate),
      model,
    },
  );
}

function connectionDisplayLabel(connectionId, connections, translate) {
  const { baseConnectionId, accountId } =
    splitAccountFromConnectionId(connectionId);
  const connection = connections.find((item) => item.id === baseConnectionId);
  const baseLabel = connection?.label || baseConnectionId;

  if (!accountId) {
    return baseLabel;
  }

  return `${baseLabel} – ${accountDisplayName(accountId, translate)}`;
}

function splitAccountFromConnectionId(connectionId) {
  const providerSeparatorIndex = connectionId.indexOf(':');

  if (providerSeparatorIndex === -1) {
    return { baseConnectionId: connectionId, accountId: '' };
  }

  const accountSeparatorIndex = connectionId.indexOf(
    ':',
    providerSeparatorIndex + 1,
  );

  if (accountSeparatorIndex === -1) {
    return { baseConnectionId: connectionId, accountId: '' };
  }

  return {
    baseConnectionId: connectionId.slice(0, accountSeparatorIndex),
    accountId: connectionId.slice(accountSeparatorIndex + 1),
  };
}

function connectionLocalIdFromConnectionId(connectionId) {
  if (!connectionId) {
    return '';
  }

  const separatorIndex = connectionId.indexOf(':');
  if (separatorIndex === -1) {
    return connectionId;
  }

  return connectionId.slice(separatorIndex + 1);
}

function connectionIdFromModel(model, connectionLocalId) {
  if (!model || !connectionLocalId) {
    return '';
  }

  const providerSeparatorIndex = model.indexOf('/');
  if (providerSeparatorIndex === -1) {
    return '';
  }

  const providerId = model.slice(0, providerSeparatorIndex);
  if (!providerId) {
    return '';
  }

  return `${providerId}:${connectionLocalId}`;
}

function defaultTranslate(_key, fallback) {
  return fallback;
}
