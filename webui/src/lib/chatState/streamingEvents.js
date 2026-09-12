import {
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
  RUN_EVENT_REASONING_DELTA,
  RUN_EVENT_TOOL_CALL_DELTA,
  RUN_EVENT_TOOL_CALL_STDERR,
  RUN_EVENT_TOOL_CALL_STDOUT,
  RUN_EVENT_STREAM_ATTEMPT_RESTARTED,
} from '../api.js';
import { createToolArgumentPreviewScanner } from '../toolArgumentPreview.js';

export function highestContiguousRunEventSequence(sessionState) {
  const runId = activeRunIdForReplay(sessionState);
  if (!runId) {
    return 0;
  }

  const sequences = new Set();
  for (const event of sessionState?.runEvents ?? []) {
    addSequenceForRun(sequences, event, runId);
  }
  for (const event of sessionState?.streamingRunEvents ?? []) {
    if (event?.run_id !== runId) {
      continue;
    }
    addCompressedStreamingEventSequences(sequences, event);
  }
  for (const eventKey of sessionState?.seenStreamingEventKeys ?? []) {
    addStreamingEventKeySequenceForRun(sequences, eventKey, runId);
  }

  return highestContiguousSequence(sequences);
}

function activeRunIdForReplay(sessionState) {
  if (sessionState?.currentRun?.runId) {
    return sessionState.currentRun.runId;
  }
  return latestRunIdFromEvents([
    ...(sessionState?.runEvents ?? []),
    ...(sessionState?.streamingRunEvents ?? []),
  ]);
}

function latestRunIdFromEvents(events) {
  for (let index = (events ?? []).length - 1; index >= 0; index -= 1) {
    const runId = events[index]?.run_id;
    if (typeof runId === 'string' && runId.length > 0) {
      return runId;
    }
  }
  return '';
}

function addSequenceForRun(sequences, event, runId) {
  if (event?.run_id !== runId) {
    return;
  }
  addSequence(sequences, event.sequence);
}

function addCompressedStreamingEventSequences(sequences, event) {
  const firstSequence = event?.sequence;
  const latestSequence = streamEventLatestSequence(event);
  const chunkCount = streamEventChunkCount(event);
  if (
    Number.isFinite(firstSequence) &&
    Number.isFinite(latestSequence) &&
    latestSequence >= firstSequence &&
    latestSequence - firstSequence + 1 === chunkCount
  ) {
    for (
      let sequence = firstSequence;
      sequence <= latestSequence;
      sequence += 1
    ) {
      addSequence(sequences, sequence);
    }
    return;
  }
  addSequence(sequences, firstSequence);
  addSequence(sequences, latestSequence);
}

function addStreamingEventKeySequenceForRun(sequences, eventKey, runId) {
  if (typeof eventKey !== 'string') {
    return;
  }
  const parts = eventKey.split(':');
  if (parts.length < 3) {
    return;
  }
  const sequence = Number(parts.at(-1));
  const eventRunId = parts.slice(0, -2).join(':');
  if (eventRunId !== runId) {
    return;
  }
  addSequence(sequences, sequence);
}

function addSequence(sequences, sequence) {
  if (!Number.isFinite(sequence) || sequence < 1) {
    return;
  }
  sequences.add(Math.trunc(sequence));
}

function highestContiguousSequence(sequences) {
  let sequence = 0;
  while (sequences.has(sequence + 1)) {
    sequence += 1;
  }
  return sequence;
}

function streamEventChunkCount(event) {
  if (
    Number.isFinite(event?._streamChunkCount) &&
    event._streamChunkCount > 0
  ) {
    return event._streamChunkCount;
  }
  return 1;
}

function streamEventLatestSequence(event) {
  if (Number.isFinite(event?._streamLatestSequence)) {
    return event._streamLatestSequence;
  }
  return event?.sequence;
}

export function isStreamingDeltaRunEvent(eventType) {
  return [
    RUN_EVENT_REASONING_DELTA,
    RUN_EVENT_ASSISTANT_OUTPUT_DELTA,
    RUN_EVENT_TOOL_CALL_DELTA,
    RUN_EVENT_TOOL_CALL_STDOUT,
    RUN_EVENT_TOOL_CALL_STDERR,
  ].includes(eventType);
}

export function appendCompressedStreamingRunEvent(sessionState, event) {
  if (event.type === RUN_EVENT_TOOL_CALL_DELTA) {
    appendCompressedToolCallDeltaEvent(sessionState, event);
    return;
  }
  if (
    event.type === RUN_EVENT_TOOL_CALL_STDOUT ||
    event.type === RUN_EVENT_TOOL_CALL_STDERR
  ) {
    appendCompressedToolOutputDeltaEvent(sessionState, event);
    return;
  }

  const payloadKey = streamingDeltaPayloadKey(event.type);
  if (!payloadKey) {
    return;
  }

  const deltaText = event.payload?.[payloadKey];
  if (!deltaText) {
    return;
  }

  const lastEvent = sessionState.streamingRunEvents.at(-1);
  if (
    canMergeCompressedStreamingEvent(
      lastEvent,
      event,
      payloadKey,
      sessionState.streamingPhase,
    )
  ) {
    lastEvent.payload[payloadKey] =
      `${lastEvent.payload?.[payloadKey] ?? ''}${deltaText}`;
    lastEvent.sequence = firstSeenSequence(lastEvent.sequence, event.sequence);
    lastEvent._streamChunkCount = streamEventChunkCount(lastEvent) + 1;
    lastEvent._streamLatestSequence = streamEventLatestSequence(event);
    lastEvent.timestamp ??= event.timestamp;
    return;
  }

  sessionState.streamingRunEvents = [
    ...sessionState.streamingRunEvents,
    {
      ...event,
      payload: {
        ...event.payload,
      },
      _streamingPhase: sessionState.streamingPhase,
      _streamChunkCount: 1,
      _streamLatestSequence: streamEventLatestSequence(event),
    },
  ];
}

// Tool-call deltas are compressed into one retained event per
// (run, tool call, streaming phase) so the live run projection can render a
// "preparing" tool row from the very first delta. Unlike text deltas, merging
// looks the event up by tool call id instead of only checking the trailing
// event, because sibling tool calls may interleave their argument fragments.
function appendCompressedToolCallDeltaEvent(sessionState, event) {
  const payload = event.payload ?? {};
  const toolCallId = payload.tool_call_id ?? payload.id;
  if (!toolCallId) {
    return;
  }
  const nameDelta = payload.name_delta ?? '';
  const argumentsDelta = payload.arguments_delta ?? '';
  if (!nameDelta && !argumentsDelta) {
    return;
  }

  const existingEvent = sessionState.streamingRunEvents.find(
    (candidate) =>
      candidate.type === event.type &&
      candidate.run_id === event.run_id &&
      candidate._streamingPhase === sessionState.streamingPhase &&
      (candidate.payload?.tool_call_id ?? candidate.payload?.id) === toolCallId,
  );
  if (existingEvent) {
    existingEvent.payload.name_delta = `${existingEvent.payload?.name_delta ?? ''}${nameDelta}`;
    existingEvent.payload.arguments_delta = `${existingEvent.payload?.arguments_delta ?? ''}${argumentsDelta}`;
    existingEvent.sequence = firstSeenSequence(
      existingEvent.sequence,
      event.sequence,
    );
    existingEvent._streamChunkCount = streamEventChunkCount(existingEvent) + 1;
    existingEvent._streamLatestSequence = streamEventLatestSequence(event);
    existingEvent.timestamp ??= event.timestamp;
    updateToolArgumentPreview(existingEvent, argumentsDelta);
    return;
  }

  const compressedEvent = {
    ...event,
    payload: {
      ...payload,
      tool_call_id: toolCallId,
      name_delta: nameDelta,
      arguments_delta: argumentsDelta,
    },
    _streamingPhase: sessionState.streamingPhase,
    _streamChunkCount: 1,
    _streamLatestSequence: streamEventLatestSequence(event),
  };
  updateToolArgumentPreview(compressedEvent, argumentsDelta);
  sessionState.streamingRunEvents = [
    ...sessionState.streamingRunEvents,
    compressedEvent,
  ];
}

// Tool stdout/stderr chunks are compressed into ONE retained event per
// (run, tool call, stream). The live run projection replays every retained
// event on each render, so per-chunk retention made the rebuild cost quadratic
// in the streamed output size — a multi-MiB bash output froze the whole UI.
// A tool call id is unique per run, so no phase scoping is needed: all chunks
// of one call belong to the same stream regardless of phase boundaries.
function appendCompressedToolOutputDeltaEvent(sessionState, event) {
  const payload = event.payload ?? {};
  const toolCallId = payload.tool_call_id ?? payload.id;
  const data = typeof payload.data === 'string' ? payload.data : '';
  if (!toolCallId || !data) {
    return;
  }

  const existingEvent = sessionState.streamingRunEvents.find(
    (candidate) =>
      candidate.type === event.type &&
      candidate.run_id === event.run_id &&
      (candidate.payload?.tool_call_id ?? candidate.payload?.id) === toolCallId,
  );
  if (existingEvent) {
    existingEvent.payload.data = `${existingEvent.payload?.data ?? ''}${data}`;
    existingEvent.sequence = firstSeenSequence(
      existingEvent.sequence,
      event.sequence,
    );
    existingEvent._streamChunkCount = streamEventChunkCount(existingEvent) + 1;
    existingEvent._streamLatestSequence = streamEventLatestSequence(event);
    existingEvent.timestamp ??= event.timestamp;
    return;
  }

  sessionState.streamingRunEvents = [
    ...sessionState.streamingRunEvents,
    {
      ...event,
      payload: {
        ...payload,
        tool_call_id: toolCallId,
        data,
      },
      _streamingPhase: sessionState.streamingPhase,
      _streamChunkCount: 1,
      _streamLatestSequence: streamEventLatestSequence(event),
    },
  ];
}

// Argument fragments are scanned incrementally so a display field (e.g. a
// write's path) can label the preparing tool row long before the arguments
// finish streaming. Scanner state lives outside the event so the compressed
// event stays plain JSON-shaped data.
const toolArgumentPreviewScanners = new WeakMap();

function updateToolArgumentPreview(compressedEvent, argumentsDelta) {
  if (!argumentsDelta) {
    return;
  }
  let scanner = toolArgumentPreviewScanners.get(compressedEvent);
  if (!scanner) {
    scanner = createToolArgumentPreviewScanner();
    toolArgumentPreviewScanners.set(compressedEvent, scanner);
  }
  if (scanner.push(argumentsDelta)) {
    compressedEvent.payload.preview_arguments = scanner.fields();
  }
}

function canMergeCompressedStreamingEvent(
  existingEvent,
  incomingEvent,
  payloadKey,
  streamingPhase,
) {
  return (
    existingEvent?.type === incomingEvent.type &&
    existingEvent?.run_id === incomingEvent.run_id &&
    existingEvent?._streamingPhase === streamingPhase &&
    typeof existingEvent.payload?.[payloadKey] === 'string'
  );
}

function streamingDeltaPayloadKey(eventType) {
  if (eventType === RUN_EVENT_REASONING_DELTA) {
    return 'reasoning_delta';
  }
  if (eventType === RUN_EVENT_ASSISTANT_OUTPUT_DELTA) {
    return 'content_delta';
  }
  return null;
}

export function streamingDeltaEventKey(event) {
  if (!Number.isFinite(event?.sequence)) {
    return null;
  }
  return `${event.run_id ?? 'run'}:${event.type}:${event.sequence}`;
}

function firstSeenSequence(existingSequence, candidateSequence) {
  if (!Number.isFinite(existingSequence)) {
    return candidateSequence;
  }
  if (!Number.isFinite(candidateSequence)) {
    return existingSequence;
  }
  return Math.min(existingSequence, candidateSequence);
}

// Streaming deltas are grouped into phases so text that streams after a tool
// call does not merge with text from before it. `tool_call_started` and
// `tool_call_result` mark phase boundaries; the compressed `streamingRunEvents`
// tag each retained delta with the current phase.
export function advanceStreamingPhase(sessionState, event) {
  if (
    event.type === 'tool_call_started' ||
    event.type === 'tool_call_result' ||
    event.type === RUN_EVENT_STREAM_ATTEMPT_RESTARTED
  ) {
    sessionState.streamingPhase += 1;
  }
}

export function discardStreamingAttempt(sessionState, runId) {
  sessionState.streamingRunEvents = sessionState.streamingRunEvents.filter(
    (event) =>
      event.run_id !== runId ||
      event._streamingPhase !== sessionState.streamingPhase,
  );
}
