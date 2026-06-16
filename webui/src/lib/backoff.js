// Shared exponential-backoff delay for reconnect loops (WebSocket and SSE).
//
// Only the delay math lives here. Each reconnect call site keeps its own timer,
// attempt counter, attempt cap, and connection lifecycle — those differ per
// transport and are not worth unifying. What every site must share is the
// jittered curve: without jitter, multiple clients that drop on the same server
// blip reconnect in lockstep and hammer the server in synchronized waves.

export const DEFAULT_RECONNECT_JITTER_FACTOR = 0.25;

/**
 * Exponential backoff delay with symmetric jitter.
 *
 * The base delay doubles each attempt (`initialDelayMs * 2 ** attempt`) and is
 * clamped to `maxDelayMs`. Jitter then spreads the result uniformly across
 * `±jitterFactor` of the base, so concurrent clients desynchronize.
 *
 * @param {number} attempt - Zero-based retry attempt.
 * @param {object} options
 * @param {number} options.initialDelayMs - Delay for attempt 0, before jitter.
 * @param {number} [options.maxDelayMs] - Upper bound on the base delay. Defaults
 *   to no clamp (call sites that cap the attempt count don't need one).
 * @param {number} [options.jitterFactor] - Fraction of the base delay to jitter
 *   by, in each direction. Defaults to {@link DEFAULT_RECONNECT_JITTER_FACTOR}.
 * @returns {number} Delay in milliseconds.
 */
export function reconnectBackoffDelay(
  attempt,
  {
    initialDelayMs,
    maxDelayMs = Number.POSITIVE_INFINITY,
    jitterFactor = DEFAULT_RECONNECT_JITTER_FACTOR,
  } = {},
) {
  const baseDelay = Math.min(initialDelayMs * 2 ** attempt, maxDelayMs);
  const jitter = baseDelay * jitterFactor;
  return baseDelay - jitter + Math.random() * jitter * 2;
}
