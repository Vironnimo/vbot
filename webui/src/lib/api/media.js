import {
  rpc,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  RPC_ERROR_NETWORK,
  RPC_ERROR_HTTP,
  RPC_ERROR_RESPONSE,
  ApiClientError,
  buildHttpUrl,
  isNonEmptyString,
  requirePlainObject,
  requireNonEmptyString,
  readJsonHttpPayload,
} from './transport.js';
import { isPlainObject } from '../values.js';

export const openFilePreview = (source, options = {}) =>
  rpc('file.preview_open', { source }, options);

export const getFilePreviewRevision = (token, options = {}) =>
  rpc('file.preview_revision', { token }, options);

const ATTACHMENT_UPLOAD_ENDPOINT = '/api/upload';

const ATTACHMENT_BASE_ENDPOINT = '/api/attachments';

const SPEECH_TRANSCRIBE_ENDPOINT = '/api/speech/transcribe';

export async function uploadAttachment(file, options = {}) {
  if (!file || typeof file !== 'object') {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Attachment file must be provided',
      {
        method: 'upload_attachment',
      },
    );
  }

  const fetchFunction = options.fetch ?? globalThis.fetch;
  if (typeof fetchFunction !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'fetch is not available', {
      method: 'upload_attachment',
    });
  }

  const formData = new FormData();
  const filename = isNonEmptyString(file.name) ? file.name : 'upload.bin';
  formData.append('file', file, filename);

  let response;
  try {
    response = await fetchFunction(
      buildHttpUrl(
        options.uploadPath ?? ATTACHMENT_UPLOAD_ENDPOINT,
        options.baseUrl,
      ),
      {
        method: 'POST',
        body: formData,
        signal: options.signal,
      },
    );
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_NETWORK,
      'Attachment upload failed before a response arrived',
      {
        method: 'upload_attachment',
        cause: error,
      },
    );
  }

  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'Attachment upload response body must be valid JSON',
      {
        method: 'upload_attachment',
        status: response.status,
        cause: error,
      },
    );
  }

  if (!response.ok) {
    throw new ApiClientError(
      RPC_ERROR_HTTP,
      isNonEmptyString(payload?.detail)
        ? payload.detail
        : `Attachment upload failed with HTTP ${response.status}`,
      {
        method: 'upload_attachment',
        status: response.status,
        details: isPlainObject(payload) ? payload : null,
      },
    );
  }

  if (
    !isPlainObject(payload) ||
    !isNonEmptyString(payload.attachment_id) ||
    !isNonEmptyString(payload.filename) ||
    !isNonEmptyString(payload.media_type) ||
    typeof payload.size_bytes !== 'number'
  ) {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'Attachment upload response has an invalid shape',
      {
        method: 'upload_attachment',
        status: response.status,
        details: payload,
      },
    );
  }

  return {
    attachment_id: payload.attachment_id,
    filename: payload.filename,
    media_type: payload.media_type,
    size_bytes: payload.size_bytes,
  };
}

export function updateTaskModelSettings(modelTasks, options = {}) {
  requirePlainObject(
    modelTasks,
    'Task model settings must be an object',
    'task_model.update',
  );
  return rpc('task_model.update', { model_tasks: modelTasks }, options);
}

export function getLocalSpeechMemory(options = {}) {
  return rpc('speech.local_memory_status', {}, options);
}

export function unloadLocalSpeech(target, options = {}) {
  requireNonEmptyString(
    target,
    'Target must not be empty',
    'speech.local_unload',
  );
  return rpc('speech.local_unload', { target }, options);
}

export function getLocalSpeechSetup(options = {}) {
  return rpc(
    'speech.local_setup_status',
    options.target ? { target: options.target } : {},
    options,
  );
}

export function installLocalSpeechSupport(options = {}) {
  return rpc(
    'speech.local_setup_install',
    options.target ? { target: options.target } : {},
    options,
  );
}

export function restartAfterLocalSpeechSetup(options = {}) {
  return rpc(
    'speech.local_setup_restart',
    options.target ? { target: options.target } : {},
    options,
  );
}

export async function previewSpeech(text, options = {}) {
  requireNonEmptyString(text, 'Text must not be empty', 'speech.synthesize');
  const response = await (options.fetch ?? globalThis.fetch)(
    buildHttpUrl('/api/speech/synthesize', options.baseUrl),
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/x-ndjson',
      },
      body: JSON.stringify({ text }),
      signal: options.signal,
    },
  );
  if (!response.ok) {
    const payload = await response.json();
    throw new ApiClientError(RPC_ERROR_HTTP, payload.detail, {
      method: 'speech.synthesize',
      status: response.status,
    });
  }
  const result = await readSpeechProgress(response, options.onProgress);
  if (
    !isPlainObject(result) ||
    !/^\/api\/speech\/artifacts\/[^/?#]+$/.test(result.url ?? '')
  ) {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'Invalid speech preview response',
      {
        method: 'speech.synthesize',
      },
    );
  }
  return result;
}

export function listTaskModelTargets(taskType, options = {}) {
  requireNonEmptyString(
    taskType,
    'Task type must be a non-empty string',
    'task_model.list_targets',
  );
  return rpc('task_model.list_targets', { task_type: taskType }, options);
}

export function getTaskModelOptions(taskType, target, options = {}) {
  if (!isNonEmptyString(taskType) || !isNonEmptyString(target)) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Task type and target must be non-empty strings',
      {
        method: 'task_model.options',
      },
    );
  }
  return rpc('task_model.options', { task_type: taskType, target }, options);
}

export async function transcribeSpeech(audioBlob, options = {}) {
  if (!audioBlob || typeof audioBlob !== 'object') {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Audio blob must be provided',
      {
        method: 'speech.transcribe',
      },
    );
  }

  const fetchFunction = options.fetch ?? globalThis.fetch;
  if (typeof fetchFunction !== 'function') {
    throw new ApiClientError(RPC_ERROR_NETWORK, 'fetch is not available', {
      method: 'speech.transcribe',
    });
  }

  const formData = new FormData();
  const filename = isNonEmptyString(options.filename)
    ? options.filename
    : filenameForAudioBlob(audioBlob);
  formData.append('file', audioBlob, filename);

  let response;
  try {
    response = await fetchFunction(
      buildHttpUrl(
        options.transcribePath ?? SPEECH_TRANSCRIBE_ENDPOINT,
        options.baseUrl,
      ),
      {
        method: 'POST',
        body: formData,
        signal: options.signal,
        headers: options.onProgress
          ? { Accept: 'application/x-ndjson' }
          : undefined,
      },
    );
  } catch (error) {
    throw new ApiClientError(
      RPC_ERROR_NETWORK,
      'Speech transcription failed before a response arrived',
      {
        method: 'speech.transcribe',
        cause: error,
      },
    );
  }

  const payload =
    response.ok &&
    response.headers.get('content-type')?.includes('application/x-ndjson')
      ? await readSpeechProgress(response, options.onProgress)
      : await readJsonHttpPayload(response, 'speech.transcribe');
  if (!response.ok) {
    throw new ApiClientError(
      RPC_ERROR_HTTP,
      isNonEmptyString(payload?.detail)
        ? payload.detail
        : `Speech transcription failed with HTTP ${response.status}`,
      {
        method: 'speech.transcribe',
        status: response.status,
        details: isPlainObject(payload) ? payload : null,
      },
    );
  }
  if (!isPlainObject(payload) || typeof payload.text !== 'string') {
    throw new ApiClientError(
      RPC_ERROR_RESPONSE,
      'Speech transcription response has an invalid shape',
      {
        method: 'speech.transcribe',
        status: response.status,
        details: payload,
      },
    );
  }
  return payload;
}

async function readSpeechProgress(
  response,
  onProgress,
  method = 'speech.transcribe',
) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = '';
  try {
    while (true) {
      let timer;
      const chunk = await Promise.race([
        reader.read(),
        new Promise((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new ApiClientError(
                  RPC_ERROR_NETWORK,
                  'The speech server stopped responding. Please try again.',
                  { method },
                ),
              ),
            30_000,
          );
        }),
      ]).finally(() => clearTimeout(timer));
      pending += decoder.decode(chunk.value, { stream: !chunk.done });
      let boundary;
      while ((boundary = pending.indexOf('\n')) !== -1) {
        const line = pending.slice(0, boundary);
        pending = pending.slice(boundary + 1);
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === 'progress') onProgress?.(event);
        else if (event.type === 'result') return event.result;
        else if (event.type === 'error') {
          throw new ApiClientError(RPC_ERROR_HTTP, event.detail, {
            method,
            status: event.status,
          });
        }
      }
      if (chunk.done)
        throw new ApiClientError(
          RPC_ERROR_RESPONSE,
          'Speech ended before a result arrived. Please try again.',
          { method },
        );
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export function getAttachmentUrl(attachmentId) {
  requireNonEmptyString(
    attachmentId,
    'Attachment id must be a non-empty string',
  );
  return `${ATTACHMENT_BASE_ENDPOINT}/${attachmentId}`;
}

function filenameForAudioBlob(audioBlob) {
  if (isNonEmptyString(audioBlob.name)) {
    return audioBlob.name;
  }
  const type = isNonEmptyString(audioBlob.type) ? audioBlob.type : '';
  if (type.includes('webm')) {
    return 'recording.webm';
  }
  if (type.includes('ogg')) {
    return 'recording.ogg';
  }
  if (type.includes('mpeg') || type.includes('mp3')) {
    return 'recording.mp3';
  }
  if (type.includes('wav')) {
    return 'recording.wav';
  }
  return 'recording.webm';
}
