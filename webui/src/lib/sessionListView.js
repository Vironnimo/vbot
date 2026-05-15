const SESSION_FALLBACK_NAME = 'Session';

export function createSessionListState() {
  return {
    sessions: [],
    loading: false,
    error: null,
    selectedSessionId: null,
  };
}

export function applySessionList(state, sessions) {
  const normalizedSessions = normalizeSessions(sessions);
  const currentSelectedSessionId = asOptionalText(state?.selectedSessionId);
  const selectedSessionId =
    currentSelectedSessionId !== null &&
    normalizedSessions.some(
      (session) => session.id === currentSelectedSessionId,
    )
      ? currentSelectedSessionId
      : null;

  return {
    ...(isPlainObject(state) ? state : {}),
    sessions: normalizedSessions,
    loading: false,
    error: null,
    selectedSessionId,
  };
}

export function selectSession(state, sessionId) {
  const normalizedSessionId = asOptionalText(sessionId);
  const sessions = Array.isArray(state?.sessions) ? state.sessions : [];
  const selectedSessionId =
    normalizedSessionId !== null &&
    sessions.some((session) => session.id === normalizedSessionId)
      ? normalizedSessionId
      : null;

  return {
    ...(isPlainObject(state) ? state : {}),
    selectedSessionId,
  };
}

export function sessionDisplayName(session) {
  const platform = asOptionalText(session?.platform);
  const platformConvId = asOptionalText(session?.platform_conv_id);

  if (platform !== null && platformConvId !== null) {
    return `${platform}/${platformConvId}`;
  }

  return asOptionalText(session?.id) ?? SESSION_FALLBACK_NAME;
}

function normalizeSessions(sessions) {
  const rawSessions = Array.isArray(sessions) ? sessions : [];
  const normalizedSessions = rawSessions
    .map((session) => normalizeSession(session))
    .filter((session) => session !== null);

  normalizedSessions.sort(compareSessions);

  return normalizedSessions;
}

function normalizeSession(session) {
  const id = asOptionalText(session?.id);
  if (id === null) {
    return null;
  }

  const platform = asOptionalText(session?.platform);
  const platformConvId = asOptionalText(session?.platform_conv_id);

  const normalizedSession = {
    id,
    created_at: asOptionalText(session?.created_at),
    last_active_at: asOptionalText(session?.last_active_at),
    source_channel_id: asOptionalText(session?.source_channel_id),
    platform,
    platform_conv_id: platformConvId,
    is_channel_session: platform !== null && platformConvId !== null,
  };

  normalizedSession.display_name = sessionDisplayName(normalizedSession);

  return normalizedSession;
}

function compareSessions(left, right) {
  const leftTimestamp = resolveTimestamp(left);
  const rightTimestamp = resolveTimestamp(right);

  if (leftTimestamp !== rightTimestamp) {
    return rightTimestamp - leftTimestamp;
  }

  return left.id.localeCompare(right.id);
}

function resolveTimestamp(session) {
  return (
    parseTimestamp(session?.last_active_at) ??
    parseTimestamp(session?.created_at) ??
    0
  );
}

function parseTimestamp(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return null;
  }

  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
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
