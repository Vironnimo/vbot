export const DEBUG_BODY_PLACEHOLDER = '—';
export const DEBUG_ID_TRUNCATE_LENGTH = 28;
export const DEBUG_TAB_RAW = 'raw';
export const DEBUG_TAB_FORMATTED = 'formatted';

export function createDebugViewState() {
  return {
    traces: [],
    selectedTrace: null,
    loading: false,
    error: '',
    modelProbeProviders: [],
    modelProbeProvider: '',
    modelProbeConnection: '',
    modelProbeResult: null,
    modelProbeLoading: false,
    modelProbeError: '',
  };
}

export function applyTraceList(state, result) {
  const rawTraces = Array.isArray(result?.traces) ? result.traces : [];
  state.traces = normalizeTraceEntries(rawTraces);
  state.selectedTrace = retainSelectedTrace(state);
  state.loading = false;
  state.error = '';
  return state.traces;
}

export function applyTraceDetail(state, result) {
  const trace = isPlainObject(result?.trace) ? result.trace : null;
  state.selectedTrace = trace;
  state.loading = false;
  state.error = '';
  return state.selectedTrace;
}

export function selectTrace(state, traceId) {
  const normalizedId = asOptionalText(traceId);
  const traces = Array.isArray(state?.traces) ? state.traces : [];
  const selectedTrace =
    normalizedId !== null &&
    traces.some((trace) => trace.trace_id === normalizedId)
      ? (traces.find((trace) => trace.trace_id === normalizedId) ?? null)
      : null;

  state.selectedTrace = selectedTrace;
  return state.selectedTrace;
}

export function clearTracesApplied(state) {
  state.traces = [];
  state.selectedTrace = null;
  return state.traces;
}

export function applyDebugStatus(state, result) {
  state.error = '';
  state.loading = false;
  return {
    enabled: resolveBoolean(result?.enabled, false),
    traceLimit: resolvePositiveInteger(result?.trace_limit, 50),
    traceCount: resolveNonNegativeInteger(result?.trace_count, 0),
    dataDirectory: asText(result?.data_directory),
  };
}

export function applyModelProbeProviders(state, result) {
  const providerItems = Array.isArray(result?.providers?.items)
    ? result.providers.items
    : [];
  state.modelProbeProviders = normalizeModelProbeProviders(providerItems);
  state.error = '';

  if (
    state.modelProbeProvider &&
    !state.modelProbeProviders.some((p) => p.id === state.modelProbeProvider)
  ) {
    state.modelProbeProvider = '';
    state.modelProbeConnection = '';
  }

  return state.modelProbeProviders;
}

export function selectModelProbeProvider(state, providerId) {
  const normalizedId = asOptionalText(providerId);
  const providers = Array.isArray(state?.modelProbeProviders)
    ? state.modelProbeProviders
    : [];
  const provider =
    normalizedId !== null
      ? (providers.find((p) => p.id === normalizedId) ?? null)
      : null;

  state.modelProbeProvider = provider ? provider.id : '';
  state.modelProbeConnection = '';
  state.modelProbeResult = null;
  state.modelProbeError = '';

  return state.modelProbeProvider;
}

export function selectModelProbeConnection(state, connectionId) {
  const normalizedId = asOptionalText(connectionId);
  const provider = resolveSelectedProbeProvider(state);

  if (!provider || normalizedId === null) {
    state.modelProbeConnection = '';
    return state.modelProbeConnection;
  }

  const connections = Array.isArray(provider.connections)
    ? provider.connections
    : [];
  const connection = connections.find((c) => c.id === normalizedId);

  state.modelProbeConnection = connection ? connection.id : '';
  state.modelProbeResult = null;
  state.modelProbeError = '';

  return state.modelProbeConnection;
}

export function applyModelProbeResult(state, result) {
  state.modelProbeLoading = false;
  state.modelProbeError = '';

  if (!isPlainObject(result)) {
    state.modelProbeResult = null;
    return state.modelProbeResult;
  }

  const normalizedResult = {
    raw: asText(result.raw_response),
    statusCode: resolveNonNegativeInteger(result.status_code, 0),
    durationMs: resolveNonNegativeInteger(result.duration_ms, 0),
    traceId: asText(result.trace_id),
    normalized: isPlainObject(result.model_preview)
      ? normalizeProbePreview(result.model_preview)
      : { modelCount: 0, preview: [] },
  };

  state.modelProbeResult = normalizedResult;
  return state.modelProbeResult;
}

export function modelProbeCanProbe(state) {
  const provider = resolveSelectedProbeProvider(state);
  if (!provider) {
    return false;
  }

  const connectionId = asOptionalText(state?.modelProbeConnection);
  if (connectionId === null) {
    return false;
  }

  const connections = Array.isArray(provider.connections)
    ? provider.connections
    : [];
  return connections.some((c) => c.id === connectionId);
}

export function modelProbeConnectionOptions(state) {
  const provider = resolveSelectedProbeProvider(state);
  if (!provider) {
    return [];
  }

  const connections = Array.isArray(provider.connections)
    ? provider.connections
    : [];

  return connections.map((connection) => ({
    value: connection.id,
    label: asText(connection.name) || connection.id,
  }));
}

export function normalizeTraceEntries(traces) {
  const rawTraces = Array.isArray(traces) ? traces : [];
  return rawTraces
    .map((trace) => normalizeTraceEntry(trace))
    .filter((trace) => trace !== null);
}

export function normalizeTraceEntry(trace) {
  const traceId = asOptionalText(trace?.trace_id);
  if (traceId === null) {
    return null;
  }

  return {
    trace_id: traceId,
    timestamp: asText(trace?.timestamp),
    provider_id: asText(trace?.provider_id),
    model_id: asText(trace?.model_id),
    method: asText(trace?.method),
    url: asText(trace?.url),
    status_code: resolveNullableInteger(trace?.status_code),
    duration_ms: resolveNullableInteger(trace?.duration_ms),
    type: asOptionalText(trace?.type),
  };
}

export function normalizeModelProbeProviders(providers) {
  const rawProviders = Array.isArray(providers) ? providers : [];
  return rawProviders
    .map((provider) => {
      const id = asOptionalText(provider?.id ?? provider?.provider_id);
      if (id === null) {
        return null;
      }

      const name = asOptionalText(provider?.name);
      const rawConnections = Array.isArray(provider?.connections)
        ? provider.connections
        : [];

      const connections = rawConnections
        .map((connection) => {
          const connectionId = asOptionalText(
            connection?.id ?? connection?.connection_id,
          );
          if (connectionId === null) {
            return null;
          }

          return {
            id: connectionId,
            name: asOptionalText(connection?.name) ?? connectionId,
          };
        })
        .filter((connection) => connection !== null);

      return {
        id,
        name: name ?? id,
        connections,
      };
    })
    .filter((provider) => provider !== null);
}

export function normalizeProbePreview(normalized) {
  if (!isPlainObject(normalized)) {
    return { modelCount: 0, preview: [] };
  }

  const modelCount = resolveNonNegativeInteger(normalized.model_count, 0);
  const rawPreview = Array.isArray(normalized.models) ? normalized.models : [];

  const preview = rawPreview
    .map((model) => {
      if (!isPlainObject(model)) {
        return null;
      }

      const modelId = asOptionalText(model.id);
      if (modelId === null) {
        return null;
      }

      return {
        id: modelId,
        name: asOptionalText(model.name) ?? modelId,
      };
    })
    .filter((model) => model !== null);

  return {
    modelCount,
    preview,
  };
}

function resolveSelectedProbeProvider(state) {
  const providerId = asOptionalText(state?.modelProbeProvider);
  if (providerId === null) {
    return null;
  }

  const providers = Array.isArray(state?.modelProbeProviders)
    ? state.modelProbeProviders
    : [];

  return providers.find((p) => p.id === providerId) ?? null;
}

export function retainSelectedTrace(state) {
  const traces = Array.isArray(state?.traces) ? state.traces : [];
  const currentSelection = state?.selectedTrace;
  const selectedId = asOptionalText(currentSelection?.trace_id);
  if (selectedId === null) {
    return null;
  }
  return traces.find((trace) => trace.trace_id === selectedId) ?? null;
}

export function isJsonParseableText(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return false;
  }
  try {
    JSON.parse(value);
    return true;
  } catch {
    return false;
  }
}

export function rawBodyText(body) {
  if (body === null || body === undefined) {
    return '';
  }
  if (typeof body === 'string') {
    return body;
  }
  if (typeof body === 'object') {
    return safeStringify(body);
  }
  return String(body);
}

export function formattedBodyText(body) {
  if (body === null || body === undefined || body === '') {
    return '';
  }
  if (typeof body === 'string') {
    if (!isJsonParseableText(body)) {
      return body;
    }
    try {
      return JSON.stringify(JSON.parse(body), null, 2);
    } catch {
      return body;
    }
  }
  if (typeof body === 'object') {
    return safeStringify(body);
  }
  return String(body);
}

export function hasParseableBody(body) {
  if (typeof body !== 'string' || body.length === 0) {
    return false;
  }
  return isJsonParseableText(body);
}

export function formatHeadersForDisplay(headers) {
  if (!isPlainObject(headers)) {
    return '';
  }
  const entries = Object.entries(headers);
  if (entries.length === 0) {
    return '';
  }
  return entries
    .map(([name, value]) => `${name}: ${formatHeaderValue(value)}`)
    .join('\n');
}

export function streamEventText(event) {
  if (event === null || event === undefined) {
    return '';
  }
  if (typeof event === 'string') {
    return event;
  }
  return safeStringify(event);
}

export function truncatedId(value, maxLength = DEBUG_ID_TRUNCATE_LENGTH) {
  const text = asText(value);
  if (text.length === 0) {
    return { text: '', full: '', truncated: false };
  }
  if (text.length <= maxLength) {
    return { text, full: text, truncated: false };
  }
  const safeMax = Math.max(1, Math.floor(maxLength));
  const truncated = `${text.slice(0, safeMax - 1)}…`;
  return { text: truncated, full: text, truncated: true };
}

export function isRawBodyTab(tab) {
  return tab === DEBUG_TAB_RAW || tab == null;
}

export function isFormattedBodyTab(tab) {
  return tab === DEBUG_TAB_FORMATTED;
}

function formatHeaderValue(value) {
  if (value === null || value === undefined) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => formatHeaderValue(item)).join(', ');
  }
  if (typeof value === 'object') {
    return safeStringify(value);
  }
  return String(value);
}

function safeStringify(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return '';
  }
}

function asText(value) {
  return value === null || value === undefined ? '' : String(value);
}

function asOptionalText(value) {
  if (value === null || value === undefined) {
    return null;
  }

  const normalized = String(value).trim();
  return normalized.length > 0 ? normalized : null;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function resolveBoolean(value, fallback) {
  return typeof value === 'boolean' ? value : fallback;
}

function resolvePositiveInteger(value, fallback) {
  const numberValue = Number(value);
  return Number.isInteger(numberValue) && numberValue > 0
    ? numberValue
    : fallback;
}

function resolveNonNegativeInteger(value, fallback) {
  const numberValue = Number(value);
  return Number.isInteger(numberValue) && numberValue >= 0
    ? numberValue
    : fallback;
}

function resolveNullableInteger(value) {
  const numberValue = Number(value);
  return Number.isInteger(numberValue) ? numberValue : null;
}
