import {
  ApiClientError,
  RPC_ERROR_NETWORK,
  RPC_ERROR_RESPONSE,
} from '../apiErrors.js';
import { reportRefusedResponse } from './accessLoss.js';
import { buildHttpUrl, readJsonHttpPayload } from './transport.js';

export function interruptedTransfer(error) {
  if (error?.code === 'media_transfer_settled') return false;
  if (
    ['auth_required', 'access_denied', 'rate_limited'].includes(
      error?.details?.error?.code,
    )
  )
    return false;
  return (
    error?.code === RPC_ERROR_NETWORK ||
    error?.code === RPC_ERROR_RESPONSE ||
    [408, 502, 503, 504].includes(error?.status)
  );
}

// The browser reserves one server-owned ticket per draft file or recording.
// Only a still-empty ticket may accept a body. A lost response is read back;
// an expired ticket can never silently become a second paid transcription.
export function createMediaTransfer(kind) {
  let ticket = '';
  let closed = false;
  let active = null;
  let completed = null;
  let options = {};
  const url = () =>
    buildHttpUrl(`/api/media/transfers/${kind}`, options.baseUrl);
  const send = (...args) => (options.fetch ?? globalThis.fetch)(...args);
  function release() {
    if (ticket) {
      void send(url(), {
        method: 'DELETE',
        headers: { 'X-Vbot-Media-Transfer': ticket },
      }).catch(() => {});
      ticket = '';
    }
  }
  async function request(method) {
    let response;
    try {
      response = await send(url(), {
        method,
        cache: 'no-store',
        signal: options.signal,
        headers: ticket ? { 'X-Vbot-Media-Transfer': ticket } : {},
      });
    } catch (cause) {
      throw new ApiClientError(
        RPC_ERROR_NETWORK,
        'Media transfer connection lost',
        { cause },
      );
    }
    if (response.status === 204) return null;
    const payload = await readJsonHttpPayload(response, kind);
    if (!response.ok) {
      reportRefusedResponse(
        response.status,
        payload,
        response.headers.get('retry-after'),
      );
      const code = response.headers.get('x-vbot-media-settled')
        ? 'media_transfer_settled'
        : 'media_transfer_failed';
      throw new ApiClientError(
        code,
        typeof payload.detail === 'string'
          ? payload.detail
          : 'Media transfer failed',
        { status: response.status, details: payload },
      );
    }
    return payload;
  }
  return {
    cancel() {
      closed = true;
      completed = null;
      release();
    },
    async run(submit, nextOptions) {
      if (closed) throw new DOMException('Cancelled', 'AbortError');
      if (completed) return completed;
      if (active) {
        try {
          return await active;
        } catch {
          return this.run(submit, nextOptions);
        }
      }
      active = attempt(submit, nextOptions);
      try {
        return await active;
      } finally {
        active = null;
      }
    },
  };
  async function attempt(submit, nextOptions) {
    options = nextOptions;
    if (closed) throw new DOMException('Cancelled', 'AbortError');
    options.signal?.throwIfAborted();
    let result;
    if (ticket) result = await request('GET');
    else {
      const reservation = await request('POST');
      if (typeof reservation?.ticket !== 'string' || !reservation.ticket)
        throw new ApiClientError(
          RPC_ERROR_RESPONSE,
          'Invalid media transfer reservation',
        );
      ticket = reservation.ticket;
    }
    if (closed || options.signal?.aborted) {
      if (closed) release();
      throw new DOMException('Cancelled', 'AbortError');
    }
    if (result == null) {
      try {
        result = await submit(ticket);
      } catch (error) {
        if (error?.details?.detail?.code !== 'media_already_submitted')
          throw error;
        result = await request('GET');
      }
    }
    if (
      kind === 'transcription'
        ? typeof result?.text !== 'string'
        : !result?.attachment_id ||
          typeof result.filename !== 'string' ||
          typeof result.media_type !== 'string' ||
          !Number.isFinite(result.size_bytes)
    ) {
      throw new ApiClientError(
        RPC_ERROR_RESPONSE,
        'Invalid media transfer result',
      );
    }
    if (closed) throw new DOMException('Cancelled', 'AbortError');
    completed = result;
    release();
    return result;
  }
}
