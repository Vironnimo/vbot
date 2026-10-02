// An address an Extension asks the user to open (an MCP server's URL-mode
// request or a sign-in page), taken apart so the user can judge where it
// leads before anything loads. Nothing here fetches the address.

// The registrable domain (`example.co.uk` of `login.example.co.uk`) needs
// the Public Suffix List, a large table loaded only when an address is shown.
let domainParser = null;

export function loadDomainParser() {
  domainParser ??= import('tldts').then(
    (module) => (hostname) => module.getDomain(hostname),
    () => {
      domainParser = null;
      return null;
    },
  );
  return domainParser;
}

// `text` as `{href, host, parts, domain, warnings}`, or `null` when it is no
// http(s) address. `parts` spell `href` in order, with the host split into
// its subdomain prefix and the registrable `domain`; `getDomain` names that
// domain, and without it the whole host stands for it.
export function requestedUrl(text, getDomain = null) {
  let url;
  try {
    url = new URL(String(text ?? '').trim());
  } catch {
    return null;
  }
  if (!['https:', 'http:'].includes(url.protocol)) return null;
  const host = url.hostname;
  const ip = isIpAddress(host);
  const domain = (!ip && getDomain?.(host)) || host;
  const href = url.href;
  const userInfo = url.username
    ? `${url.username}${url.password ? `:${url.password}` : ''}@`
    : '';
  const before = `${url.protocol}//${userInfo}`;
  let parts = [{ kind: 'text', text: href }];
  if (href.startsWith(before + host) && host.endsWith(domain)) {
    parts = [
      { kind: 'text', text: before },
      { kind: 'subdomain', text: host.slice(0, host.length - domain.length) },
      { kind: 'domain', text: domain },
      { kind: 'text', text: href.slice(before.length + host.length) },
    ].filter((part) => part.text);
  }
  const warnings = [];
  if (url.protocol !== 'https:') warnings.push('insecure');
  if (host.split('.').some((label) => label.startsWith('xn--')))
    warnings.push('international');
  if (userInfo) warnings.push('credentials');
  if (ip) warnings.push('ipAddress');
  return { href, host, parts, domain, warnings };
}

function isIpAddress(host) {
  return host.startsWith('[') || /^\d{1,3}(?:\.\d{1,3}){3}$/.test(host);
}
