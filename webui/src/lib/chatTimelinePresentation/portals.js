// Portals in the timeline: the card a `portal` call shows after its row, and
// the pure decisions behind it (its address, its state, whether this browser
// reaches it). The card reads the Portal's live state by id through
// `lib/portals.svelte.js`; the Tool display carries no credential.
import { isPlainObject } from '$lib/values.js';
import { toolDisplay, toolNameForRunTool } from './toolFacts.js';
import { trimmedString } from './values.js';

export const PORTAL_TOOL_NAME = 'portal';
export const PORTAL_TOKEN_QUERY = 'vbot_portal';

export const PORTAL_STATUS_LOADING = 'loading';
export const PORTAL_STATUS_UNKNOWN = 'unknown';
export const PORTAL_STATUS_RUNNING = 'running';
export const PORTAL_STATUS_WAITING = 'waiting';
export const PORTAL_STATUS_CLOSED = 'closed';

// The Portal a Tool row opened, `{ id, title, port }`, or null for a row
// without a Portal card (other Tools, `portal` list or close, a refused open).
export function portalFromTool(tool) {
  const portal = toolDisplay(tool)?.portal;
  const id = trimmedString(portal?.id);
  if (!isPlainObject(portal) || !id) return null;
  return {
    id,
    title: trimmedString(portal.title),
    port: Number.isInteger(portal.port) ? portal.port : null,
  };
}

// A `portal` row may show a card for the user, so compact mode keeps it out
// of Working groups. Decided by name, which a row has from its start, so a
// group does not split once the call finishes.
export const isPortalTool = (tool) =>
  toolNameForRunTool(tool) === PORTAL_TOOL_NAME;

function bareHostname(hostname) {
  const host = String(hostname ?? '').toLowerCase();
  return host.startsWith('[') && host.endsWith(']') ? host.slice(1, -1) : host;
}

// Whether the page is served from this computer's loopback interface.
export function isLoopbackHostname(hostname) {
  const host = bareHostname(hostname);
  return (
    host === 'localhost' || host === '::1' || /^127(?:\.\d{1,3}){3}$/.test(host)
  );
}

// Portals listen on ports of their own, which a reverse proxy in front of
// vBot does not forward: behind one, only a page on this computer reaches
// them.
export const portalsReachable = (reverseProxy, hostname) =>
  !reverseProxy || isLoopbackHostname(hostname);

// The address that opens a listed Portal in this browser: the page's host on
// the Portal's own port, its path, and its credential as a query parameter
// (the Portal's first answer moves it into a cookie and drops it from the
// address). '' when the Portal cannot be addressed.
export function portalUrl(portal, location) {
  const port = portal?.listen_port;
  const token = typeof portal?.token === 'string' ? portal.token : '';
  const protocol = location?.protocol;
  const host = String(location?.hostname ?? '');
  if (
    !Number.isInteger(port) ||
    port <= 0 ||
    port > 65535 ||
    !token ||
    (protocol !== 'http:' && protocol !== 'https:') ||
    !host
  ) {
    return '';
  }
  const hostPart =
    host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
  let path = typeof portal.path === 'string' ? portal.path : '';
  if (!path.startsWith('/')) path = `/${path}`;
  const hashAt = path.indexOf('#');
  const fragment = hashAt >= 0 ? path.slice(hashAt) : '';
  const base = hashAt >= 0 ? path.slice(0, hashAt) : path;
  const separator = !base.includes('?') ? '?' : /[?&]$/.test(base) ? '' : '&';
  return `${protocol}//${hostPart}:${port}${base}${separator}${PORTAL_TOKEN_QUERY}=${encodeURIComponent(token)}${fragment}`;
}

// The state a card shows for Portal `id` from its Session's `portal.list`
// projection (`lib/portals.svelte.js`): loading before the first answer,
// unknown when no answer could be read, closed when the Portal is no longer
// listed, else running while its app answers and waiting until it does.
export function portalStatus(sessionPortals, id) {
  if (!sessionPortals?.loaded) {
    return sessionPortals?.error
      ? PORTAL_STATUS_UNKNOWN
      : PORTAL_STATUS_LOADING;
  }
  const portal = sessionPortals.portals?.[id];
  if (!portal) return PORTAL_STATUS_CLOSED;
  return portal.listening ? PORTAL_STATUS_RUNNING : PORTAL_STATUS_WAITING;
}
