// Pure media-address vocabulary shared by presentation and the API adapter.
export const attachmentUrl = (id) => `/api/attachments/${id}`;

export function isSpeechPlaybackUrl(url) {
  return (
    typeof url === 'string' &&
    /^\/api\/speech\/playback\/[a-zA-Z0-9_-]+$/.test(url)
  );
}
