// Presentation conversion only; the Extension validates the complete response.
export function inputFields(request) {
  return Object.entries(
    request?.payload?.requestedSchema?.properties ?? {},
  ).map(([key, schema]) => ({ key, ...schema }));
}

export function inputResponse(request, drafts, action = 'accept') {
  if (request.kind === 'oauth')
    return { redirect_url: drafts.redirect_url ?? '', action };
  if (action !== 'accept') return { action };
  if (request.payload?.mode === 'url') return { action };
  const content = {};
  for (const field of inputFields(request)) {
    const value = drafts[field.key];
    if (value === undefined || value === '') continue;
    content[field.key] =
      field.type === 'string' ? value : JSON.parse(String(value));
  }
  return { action, content };
}

export function inputUrl(request) {
  try {
    const url = new URL(request?.payload?.url);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : '';
  } catch {
    return '';
  }
}
