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
  const modelExistsInCatalog = models.some(
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
        }
      : null;
  const emptyOption = {
    value: '',
    label: emptyLabel,
    isUnavailable: false,
  };
  const catalogOptions = models.flatMap((model) => {
    const providerConnections = connectionsByProvider[model.provider_id] ?? [];

    return providerConnections.map((connection) => ({
      value: modelSelectionValue(
        model.id,
        connectionLocalIdFromConnectionId(connection.id),
      ),
      label: modelOptionLabel(model, connection, providerConnections.length),
      isUnavailable: false,
    }));
  });

  if (
    !selectedValue ||
    catalogOptions.some((option) => option.value === selectedValue) ||
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

function modelOptionLabel(model, connection, providerConnectionCount) {
  if (providerConnectionCount <= 1) {
    return model.id;
  }

  return `${model.id} (${connection.label})`;
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
      connection: connectionDisplayLabel(connectionId, connections),
      model,
    },
  );
}

function connectionDisplayLabel(connectionId, connections) {
  const connection = connections.find((item) => item.id === connectionId);

  return connection?.label || connectionId;
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
