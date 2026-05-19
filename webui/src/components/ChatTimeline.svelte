<script>
  import { tick } from 'svelte';

  import { getAttachmentUrl } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { renderMarkdown } from '$lib/markdown.js';

  import { visibleTimelineItems } from '../lib/chatState.js';

  let {
    sessionState,
    agentName = '',
    onNavigateToSubAgent = () => {},
    onRetry = () => {},
  } = $props();

  function isNearBottom(container) {
    return (
      !container ||
      container.offsetHeight + container.scrollTop > container.scrollHeight - 56
    );
  }

  let timelineItems = $derived(visibleTimelineItems(sessionState));
  let scrollContainer = $state();
  let reasoningDisclosureState = $state({});
  let latestTerminalState = $derived.by(() => {
    for (let index = timelineItems.length - 1; index >= 0; index -= 1) {
      const item = timelineItems[index];
      if (item?.type === 'assistant_run') {
        const terminalType = item.terminalEvent?.type;
        if (
          typeof terminalType === 'string' &&
          terminalType.startsWith('run_')
        ) {
          return {
            itemId: item.id,
            failed: terminalType === 'run_failed',
          };
        }
        continue;
      }
      if (
        item?.type === 'event' &&
        typeof item.event?.type === 'string' &&
        item.event.type.startsWith('run_')
      ) {
        return {
          itemId: item.id,
          failed: item.event.type === 'run_failed',
        };
      }
    }
    return {
      itemId: '',
      failed: false,
    };
  });
  let timelineSignature = $derived(
    timelineItems.map((item) => timelineItemSignature(item)).join('|'),
  );

  $effect.pre(() => {
    timelineSignature;
    const shouldAutoscroll = isNearBottom(scrollContainer);
    if (shouldAutoscroll) {
      tick().then(() => {
        scrollContainer?.scrollTo?.(0, scrollContainer.scrollHeight);
      });
    }
  });

  function timelineItemSignature(item) {
    if (item.type === 'streaming') {
      return `${item.id}:${item.streamingItem.sequence}:${item.streamingItem.content ?? ''}:${item.streamingItem.name ?? ''}`;
    }
    if (item.type === 'assistant_run') {
      return `${item.id}:${item.status}:${(item.items ?? [])
        .map(
          (child) =>
            `${child.id}:${child.status ?? ''}:${child.content ?? ''}:${child.name ?? ''}:${formatJson(child.arguments ?? '')}:${formatJson(child.result ?? '')}`,
        )
        .join('~')}`;
    }
    return item.id;
  }

  function isReasoningOpen(id) {
    return Boolean(reasoningDisclosureState[id]);
  }

  function setReasoningOpen(id, isOpen) {
    reasoningDisclosureState[id] = isOpen;
  }

  const isUserItem = (item) =>
    item.type === 'streaming' || item.type === 'assistant_run'
      ? false
      : item.type === 'message'
        ? item.message.role === 'user'
        : item.event.type === 'user_message_persisted';

  const isAssistantItem = (item) =>
    item.type === 'assistant_run'
      ? true
      : item.type === 'streaming'
        ? ['assistant', 'reasoning', 'tool_call'].includes(
            item.streamingItem.type,
          )
        : item.type === 'message'
          ? item.message.role === 'assistant'
          : [
              'assistant_output',
              'reasoning',
              'tool_call_started',
              'tool_call_result',
            ].includes(item.event.type);

  const shouldRenderMessage = (message) =>
    hasUserContentBlocks(message) ||
    Boolean(textFromMessage(message)) ||
    hasReadableReasoning(message);

  const labelForMessage = (message) => {
    if (message.role === 'user') {
      return t('chat.role.user', 'You').toUpperCase();
    }
    if (message.role === 'assistant') {
      return t('chat.role.assistant', 'Assistant').toUpperCase();
    }
    if (message.role === 'system') {
      return t('chat.role.system', 'System').toUpperCase();
    }
    if (message.role === 'tool') {
      return t('chat.event.toolResult', 'Tool result').toUpperCase();
    }
    if (message.role === 'error') {
      return t('chat.role.error', 'Error').toUpperCase();
    }
    return t('common.unknown', 'Unknown').toUpperCase();
  };

  const labelForEvent = (event) => {
    if (event.type === 'reasoning') {
      return t('chat.event.thinking', 'Thinking').toUpperCase();
    }
    if (event.type === 'tool_call_started') {
      return t('chat.event.toolStarted', 'Tool started').toUpperCase();
    }
    if (event.type === 'tool_call_result') {
      return t('chat.event.toolResult', 'Tool result').toUpperCase();
    }
    if (event.type === 'assistant_output') {
      return t('chat.role.assistant', 'Assistant').toUpperCase();
    }
    if (event.type === 'run_completed') {
      return t('chat.event.completed', 'Run completed');
    }
    if (event.type === 'run_failed') {
      return t('chat.event.failed', 'Run failed');
    }
    if (event.type === 'run_cancelled') {
      return t('chat.event.cancelled', 'Run cancelled');
    }
    if (event.type === 'user_message_persisted') {
      return t('chat.role.user', 'You').toUpperCase();
    }
    return t('common.unknown', 'Unknown').toUpperCase();
  };

  const textFromMessage = (message) => {
    if (message.reasoning && !message.content) {
      return message.reasoning;
    }
    if (typeof message.content === 'string') {
      return message.content;
    }
    return '';
  };

  const userContentBlocks = (message) => {
    if (!Array.isArray(message?.content)) {
      return [];
    }
    return message.content.filter((block) =>
      isRenderableUserContentBlock(block),
    );
  };

  const hasUserContentBlocks = (message) =>
    message?.role === 'user' && userContentBlocks(message).length > 0;

  const isTextContentBlock = (block) =>
    isPlainObject(block) &&
    block.type === 'text' &&
    typeof block.text === 'string' &&
    block.text.trim() !== '';

  const isMediaContentBlock = (block) =>
    isPlainObject(block) &&
    block.type === 'media' &&
    trimmedString(block.attachment_id) !== '';

  const isImageMediaContentBlock = (block) =>
    isMediaContentBlock(block) &&
    trimmedString(block.media_type).startsWith('image/');

  const isFileContentBlock = (block) =>
    isPlainObject(block) &&
    block.type === 'file' &&
    trimmedString(block.attachment_id) !== '';

  const isRenderableUserContentBlock = (block) =>
    isTextContentBlock(block) ||
    isMediaContentBlock(block) ||
    isFileContentBlock(block);

  const attachmentUrlForId = (attachmentId) => {
    const normalizedId = trimmedString(attachmentId);
    if (!normalizedId) {
      return '';
    }

    try {
      return getAttachmentUrl(normalizedId);
    } catch {
      return '';
    }
  };

  const attachmentUrlForBlock = (block) =>
    attachmentUrlForId(block?.attachment_id);

  const attachmentFilename = (block) =>
    trimmedString(block?.filename) ||
    t('chat.attachment.fileLabel', 'Attached file');

  const attachmentPreviewLabel = (block) =>
    trimmedString(block?.filename) ||
    t('chat.attachment.preview', 'Preview attachment');

  const hasReadableReasoning = (message) =>
    message.role === 'assistant' && Boolean(message.reasoning);

  const hasAssistantContent = (message) =>
    message.role === 'assistant' && Boolean(message.content);

  const messageFromEvent = (event) => event.payload?.message ?? null;

  const toolCallFromEvent = (event) => event.payload?.tool_call ?? null;

  const textFromEvent = (event) => {
    const message = messageFromEvent(event);
    if (message) {
      return textFromMessage(message);
    }
    if (event.payload?.error) {
      return event.payload.error;
    }
    return event.payload?.status ?? '';
  };

  const toolNameForEvent = (event) => {
    const toolCall = toolCallFromEvent(event);
    const message = messageFromEvent(event);
    return toolCall?.name ?? message?.name ?? t('common.unknown', 'Unknown');
  };

  const toolArgumentForEvent = (event) => {
    const toolCall = toolCallFromEvent(event);
    if (!toolCall) {
      return '';
    }
    const label = humanReadableToolLabel(
      toolCall?.name ?? '',
      toolCall.arguments ?? {},
    );
    return label ? `(${label})` : '';
  };

  const visibleRunChildren = (assistantRun) =>
    (assistantRun.items ?? []).filter((child) => {
      if (child.type === 'tool_call') {
        return Boolean(child.name || child.toolCallId || child.startedEvent);
      }
      return Boolean(child.content);
    });

  const runIterationCount = (assistantRun) => {
    const outputCount = (assistantRun.outputs ?? []).length;
    const toolCount = (assistantRun.tools ?? []).length;
    return Math.max(1, outputCount + (toolCount > 0 ? 1 : 0));
  };

  const labelForRunIterations = (assistantRun) =>
    t('chat.runIterations', '{count} iter', {
      count: runIterationCount(assistantRun),
    });

  const runStatusLabel = (status) => {
    if (status === 'failed') {
      return t('chat.runStatus.failed', 'Failed');
    }
    if (status === 'cancelled') {
      return t('chat.runStatus.cancelled', 'Cancelled');
    }
    if (status === 'completed' || status === 'success') {
      return t('chat.runStatus.completed', 'Completed');
    }
    return t('chat.runStatus.running', 'Running');
  };

  const formatRunDuration = (assistantRun) => {
    const start = timestampToMs(
      assistantRun.startTimestamp ?? assistantRun.timestamp,
    );
    const end = timestampToMs(assistantRun.endTimestamp);
    if (start === null || end === null || end < start) {
      return '';
    }
    const elapsedSeconds = (end - start) / 1000;
    if (elapsedSeconds < 10) {
      return t('chat.runDurationSeconds', '{seconds}s', {
        seconds: elapsedSeconds.toFixed(1),
      });
    }
    return t('chat.runDurationSeconds', '{seconds}s', {
      seconds: Math.round(elapsedSeconds),
    });
  };

  const timestampToMs = (timestamp) => {
    if (!timestamp) {
      return null;
    }
    const value = new Date(timestamp).getTime();
    return Number.isNaN(value) ? null : value;
  };

  const runMetaParts = (assistantRun) =>
    [
      labelForRunIterations(assistantRun),
      formatRunDuration(assistantRun) || runStatusLabel(assistantRun.status),
    ].filter(Boolean);

  const toolStatus = (tool) => {
    if (tool.status === 'failed') {
      return 'failed';
    }
    if (tool.status === 'success' || tool.status === 'completed') {
      return 'success';
    }
    return 'running';
  };

  const toolArguments = (tool) => tool.arguments ?? tool.toolCall?.arguments;

  const TOOL_DETAIL_HIDDEN_KEYS = new Set(['artifacts', 'description']);
  const TOOL_DISPLAY_ARGS = {
    read: ['path'],
    write: ['path'],
    edit: ['path'],
    bash: ['command'],
    glob: ['pattern'],
    grep: ['pattern', 'path'],
    subagent: ['agent_id', 'content'],
    subagent_result: ['agent_id', 'session_id'],
  };
  const MAX_TOOL_LABEL_LENGTH = 80;
  const MAX_SUBAGENT_PREVIEW_LENGTH = 96;
  const SUBAGENT_TOOL_NAMES = new Set(['subagent', 'subagent_result']);
  const TOOL_ERROR_DETAIL_KEYS = [
    'error',
    'message',
    'code',
    'details',
    'status',
    'type',
  ];

  const humanReadableToolLabel = (toolName, argumentsValue) => {
    let args = argumentsValue;
    if (typeof args === 'string') {
      try {
        args = JSON.parse(args);
      } catch {
        return args;
      }
    }

    if (!args || typeof args !== 'object' || Array.isArray(args)) {
      return formatJson(argumentsValue);
    }

    if (toolName === 'glob') {
      return searchToolLabel(args, false) ?? formatJson(argumentsValue);
    }

    if (toolName === 'grep') {
      return searchToolLabel(args, true) ?? formatJson(argumentsValue);
    }

    if (SUBAGENT_TOOL_NAMES.has(toolName)) {
      return subAgentToolLabel(toolName, args) ?? formatJson(argumentsValue);
    }

    if (
      typeof args.description === 'string' &&
      args.description.trim() !== ''
    ) {
      return args.description;
    }

    const displayArgs = TOOL_DISPLAY_ARGS[toolName];
    if (displayArgs) {
      for (const key of displayArgs) {
        const value = args[key];
        if (typeof value === 'string' && value.trim() !== '') {
          return value;
        }
      }
    }

    const firstStringEntry = Object.values(args).find(
      (v) =>
        typeof v === 'string' &&
        v.length <= MAX_TOOL_LABEL_LENGTH &&
        v.trim() !== '',
    );
    if (firstStringEntry !== undefined) {
      return firstStringEntry;
    }

    return formatJson(argumentsValue);
  };

  const searchToolLabel = (args, includePath) => {
    const pattern = trimmedString(args.pattern);
    if (!pattern) {
      return null;
    }

    const path = includePath ? trimmedString(args.path) : '';
    return path ? `${pattern} · ${path}` : pattern;
  };

  const trimmedString = (value) => {
    if (typeof value !== 'string') {
      return '';
    }
    return value.trim();
  };

  const toolArgumentSummary = (tool) => {
    const argumentsValue = toolArguments(tool);
    if (argumentsValue === undefined || argumentsValue === null) {
      return '';
    }
    const label = humanReadableToolLabel(
      toolNameForRunTool(tool),
      argumentsValue,
    );
    return label ? `(${label})` : '';
  };

  const isSubAgentTool = (tool) =>
    SUBAGENT_TOOL_NAMES.has(toolNameForRunTool(tool));

  const subAgentArguments = (tool) => {
    const parsedArguments = parseJsonValue(toolArguments(tool));
    return isPlainObject(parsedArguments) ? parsedArguments : {};
  };

  const subAgentResultEnvelope = (tool) => {
    const parsedResult = parseJsonValue(tool.result);
    return isPlainObject(parsedResult) ? parsedResult : {};
  };

  const subAgentResultData = (tool) => {
    const resultEnvelope = subAgentResultEnvelope(tool);
    if (isPlainObject(resultEnvelope.data)) {
      return resultEnvelope.data;
    }
    return resultEnvelope;
  };

  const subAgentAgentId = (tool) => {
    const args = subAgentArguments(tool);
    const data = subAgentResultData(tool);
    return (
      trimmedString(args.agent_id) ||
      trimmedString(data.agent_id) ||
      t('common.unknown', 'Unknown')
    );
  };

  const subAgentPreview = (tool) => {
    const args = subAgentArguments(tool);
    const toolName = toolNameForRunTool(tool);
    if (toolName === 'subagent') {
      return truncateToolLabel(
        trimmedString(args.content),
        MAX_SUBAGENT_PREVIEW_LENGTH,
      );
    }
    return truncateToolLabel(
      trimmedString(args.session_id),
      MAX_SUBAGENT_PREVIEW_LENGTH,
    );
  };

  const subAgentToolLabel = (toolName, args) => {
    const agentId = trimmedString(args.agent_id);
    const preview =
      toolName === 'subagent'
        ? truncateToolLabel(
            trimmedString(args.content),
            MAX_SUBAGENT_PREVIEW_LENGTH,
          )
        : truncateToolLabel(
            trimmedString(args.session_id),
            MAX_SUBAGENT_PREVIEW_LENGTH,
          );
    return [agentId, preview].filter(Boolean).join(' · ');
  };

  const subAgentStatusLabel = (tool) => {
    if (toolStatus(tool) === 'running') {
      return t('chat.subagent.running', 'running');
    }

    const data = subAgentResultData(tool);
    const status = trimmedString(data.status) || toolStatus(tool);
    return t('chat.subagent.resultStatus', 'Status: {status}', { status });
  };

  const subAgentNavigationTarget = (tool) => {
    const data = subAgentResultData(tool);
    const agentId = trimmedString(data.agent_id);
    const sessionId = trimmedString(data.session_id);
    if (!agentId || !sessionId) {
      return null;
    }
    return { agentId, sessionId };
  };

  const handleSubAgentNavigate = (event, tool) => {
    event.preventDefault();
    event.stopPropagation();

    const target = subAgentNavigationTarget(tool);
    if (target) {
      onNavigateToSubAgent(target);
    }
  };

  const truncateToolLabel = (value, maxLength) => {
    if (!value || value.length <= maxLength) {
      return value;
    }
    return `${value.slice(0, maxLength - 1)}…`;
  };

  const isPlainObject = (value) =>
    Object.prototype.toString.call(value) === '[object Object]';

  const parseJsonValue = (value) => {
    if (typeof value !== 'string') {
      return value;
    }

    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  };

  const sanitizeToolDetailNode = (value) => {
    const parsedValue = parseJsonValue(value);

    if (Array.isArray(parsedValue)) {
      return parsedValue
        .map((entry) => sanitizeToolDetailNode(entry))
        .filter((entry) => entry !== undefined);
    }

    if (!isPlainObject(parsedValue)) {
      return parsedValue;
    }

    return Object.fromEntries(
      Object.entries(parsedValue).flatMap(([key, entryValue]) => {
        if (TOOL_DETAIL_HIDDEN_KEYS.has(key) || entryValue === undefined) {
          return [];
        }
        return [[key, sanitizeToolDetailNode(entryValue)]];
      }),
    );
  };

  const hasMeaningfulToolDetail = (value) => {
    if (value === undefined || value === null || value === '') {
      return false;
    }
    if (Array.isArray(value)) {
      return value.length > 0;
    }
    if (isPlainObject(value)) {
      return Object.keys(value).length > 0;
    }
    return true;
  };

  const preferredToolErrorValue = (value) => {
    if (!isPlainObject(value)) {
      return null;
    }

    if (hasMeaningfulToolDetail(value.error)) {
      const errorValue = sanitizeToolDetailNode(value.error);
      if (isPlainObject(errorValue)) {
        return errorValue;
      }

      const errorDetails = TOOL_ERROR_DETAIL_KEYS.reduce((details, key) => {
        const detailValue =
          key === 'error' ? errorValue : sanitizeToolDetailNode(value[key]);
        if (hasMeaningfulToolDetail(detailValue)) {
          details[key] = detailValue;
        }
        return details;
      }, {});

      return Object.keys(errorDetails).length > 1 ? errorDetails : errorValue;
    }

    if (
      value.ok === false ||
      value.success === false ||
      ['error', 'failed'].includes(value.status)
    ) {
      const errorDetails = TOOL_ERROR_DETAIL_KEYS.reduce((details, key) => {
        const detailValue = sanitizeToolDetailNode(value[key]);
        if (hasMeaningfulToolDetail(detailValue)) {
          details[key] = detailValue;
        }
        return details;
      }, {});

      return Object.keys(errorDetails).length > 0 ? errorDetails : value;
    }

    return null;
  };

  const isSuccessfulToolResult = (value) =>
    isPlainObject(value) &&
    !preferredToolErrorValue(value) &&
    (value.ok === true ||
      value.success === true ||
      ['success', 'completed'].includes(value.status) ||
      (value.ok !== false && value.success !== false && !value.status));

  const plainObjectKeys = (value) => Object.keys(value);

  const hasOnlyContentField = (value) =>
    isPlainObject(value) &&
    plainObjectKeys(value).length === 1 &&
    hasMeaningfulToolDetail(value.content);

  const preferredToolResultValue = (value, toolName = '') => {
    const sanitizedValue = sanitizeToolDetailNode(value);

    if (!isPlainObject(sanitizedValue)) {
      return sanitizedValue;
    }

    const errorValue = preferredToolErrorValue(sanitizedValue);
    if (errorValue !== null) {
      return errorValue;
    }

    if (
      isSuccessfulToolResult(sanitizedValue) &&
      isPlainObject(sanitizedValue.data)
    ) {
      if (
        ['read', 'glob', 'grep'].includes(toolName) &&
        hasMeaningfulToolDetail(sanitizedValue.data.content)
      ) {
        return sanitizeToolDetailNode(sanitizedValue.data.content);
      }

      if (hasOnlyContentField(sanitizedValue.data)) {
        return sanitizeToolDetailNode(sanitizedValue.data.content);
      }
    }

    if (hasMeaningfulToolDetail(sanitizedValue.data)) {
      return sanitizeToolDetailNode(sanitizedValue.data);
    }

    if (hasMeaningfulToolDetail(sanitizedValue.result)) {
      return sanitizeToolDetailNode(sanitizedValue.result);
    }

    return sanitizedValue;
  };

  const toolNameForRunTool = (tool) =>
    tool.name || tool.toolCall?.name || t('chat.toolPendingName', 'tool');

  const compactToolValue = (
    value,
    { preferPayload = false, toolName = '' } = {},
  ) => {
    const processed = preferPayload
      ? preferredToolResultValue(value, toolName)
      : sanitizeToolDetailNode(value);

    if (!hasMeaningfulToolDetail(processed)) {
      return t('chat.toolNoData', '—');
    }

    if (typeof processed === 'string') {
      return processed;
    }

    if (typeof processed === 'number' || typeof processed === 'boolean') {
      return String(processed);
    }

    if (isPlainObject(processed)) {
      return formatPlainObjectInner(processed);
    }

    try {
      return JSON.stringify(processed);
    } catch {
      return String(processed);
    }
  };

  const formatPlainObjectInner = (value) =>
    Object.entries(value)
      .filter(([, entryValue]) => hasMeaningfulToolDetail(entryValue))
      .map(([key, entryValue]) => `${key}: ${formatToolFieldValue(entryValue)}`)
      .join('\n');

  const formatToolFieldValue = (value) => {
    if (typeof value === 'string') {
      return value;
    }

    if (typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }

    if (value === null) {
      return 'null';
    }

    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  };

  const toolResultValueForEvent = (event) =>
    event.payload?.result ??
    event.payload?.error ??
    messageFromEvent(event)?.content;

  function formatJson(value) {
    if (typeof value === 'string') {
      return value;
    }
    try {
      return JSON.stringify(value ?? {});
    } catch {
      return String(value);
    }
  }

  const formatTime = (timestamp) => {
    if (!timestamp) {
      return '';
    }
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) {
      return '';
    }
    return new Intl.DateTimeFormat(undefined, {
      hour: 'numeric',
      minute: '2-digit',
    }).format(date);
  };

  const formatDate = (timestamp) => {
    if (!timestamp) {
      return t('chat.today', 'Today');
    }
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) {
      return t('chat.today', 'Today');
    }
    return new Intl.DateTimeFormat(undefined, {
      day: 'numeric',
      month: 'long',
      year: 'numeric',
    }).format(date);
  };

  const timestampForItem = (item) => {
    if (item.type === 'message') {
      return item.message.timestamp;
    }
    if (item.type === 'assistant_run') {
      return item.timestamp ?? item.startTimestamp ?? item.endTimestamp;
    }
    if (item.type === 'compaction_separator') {
      return item.timestamp;
    }
    return item.event.timestamp;
  };

  const avatarForItem = (item) => {
    if (isUserItem(item)) {
      return t('chat.role.userAvatar', 'Y');
    }
    if (isAssistantItem(item)) {
      return t('chat.role.assistantAvatar', 'A');
    }
    return t('chat.role.systemAvatar', 'S');
  };

  const metaForEvent = (event) => {
    if (event.type === 'run_failed') {
      return t('chat.runStatus.failed', 'Failed');
    }
    if (event.type === 'run_cancelled') {
      return t('chat.runStatus.cancelled', 'Cancelled');
    }
    if (event.type === 'run_completed') {
      return t('chat.runStatus.completed', 'Completed');
    }
    return '';
  };

  const isToolEvent = (event) =>
    event.type === 'tool_call_started' || event.type === 'tool_call_result';

  const isRunningToolEvent = (event) => event.type === 'tool_call_started';

  const hasErrorResult = (result) => {
    if (!result || typeof result !== 'object') {
      return false;
    }

    return Boolean(
      result.error ||
      result.ok === false ||
      result.success === false ||
      ['error', 'failed'].includes(result.status),
    );
  };

  const hasToolResultError = (event) => {
    if (event.payload?.error) {
      return true;
    }

    const content = messageFromEvent(event)?.content;
    if (!content) {
      return false;
    }

    try {
      return hasErrorResult(JSON.parse(content));
    } catch {
      return false;
    }
  };

  const isFailedToolEvent = (event) =>
    event.type === 'tool_call_result' && hasToolResultError(event);

  const isTerminalEvent = (event) => event.type.startsWith('run_');

  const shouldRenderRetryButton = (item) =>
    latestTerminalState.failed &&
    item?.id === latestTerminalState.itemId &&
    ((item.type === 'assistant_run' &&
      item.terminalEvent?.type === 'run_failed') ||
      (item.type === 'event' && item.event?.type === 'run_failed'));

  const labelForStreamingItem = (streamingItem) => {
    if (streamingItem.type === 'reasoning') {
      return t('chat.event.thinking', 'Thinking').toUpperCase();
    }
    if (streamingItem.type === 'tool_call') {
      return t('chat.event.toolPreparing', 'Preparing tool').toUpperCase();
    }
    return t('chat.role.assistant', 'Assistant').toUpperCase();
  };

  const streamingToolName = (streamingItem) =>
    streamingItem.name || t('chat.toolPendingName', 'tool');
</script>

{#snippet toolDetailSection(
  label,
  value,
  isError = false,
  preferPayload = false,
  toolName = '',
)}
  <div class="teb-row">
    <span class="teb-label">{label}</span>
    <span class:error={isError} class="teb-code"
      >{compactToolValue(value, { preferPayload, toolName })}</span
    >
  </div>
{/snippet}

{#snippet reasoningSummary(isStreaming = false, isOpen = false)}
  <summary class="reasoning-header">
    <svg class="reasoning-icon" viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M8 2a4 4 0 0 0-4 4c0 1.5.8 2.8 2 3.5V11h4V9.5A4 4 0 0 0 12 6a4 4 0 0 0-4-4z"
      />
      <path d="M6 13h4" />
    </svg>
    <span>{t('chat.event.thinking', 'Thinking').toUpperCase()}</span>
    {#if isStreaming}
      <span class="streaming-caret" aria-hidden="true"></span>
    {/if}
    <svg
      class="r-chevron"
      viewBox="0 0 16 16"
      width="10"
      height="10"
      style:transform={isOpen ? 'rotate(180deg)' : 'none'}
      aria-hidden="true"
    >
      <path d="M4 6l4 4 4-4" />
    </svg>
  </summary>
{/snippet}

{#snippet userContentBlock(block)}
  {#if isTextContentBlock(block)}
    <p class="msg-body-text">{block.text}</p>
  {:else if isImageMediaContentBlock(block)}
    {@const mediaUrl = attachmentUrlForBlock(block)}
    {#if mediaUrl}
      <a
        class="inline-attachment"
        href={mediaUrl}
        target="_blank"
        rel="noopener noreferrer"
        title={attachmentFilename(block)}
        aria-label={attachmentPreviewLabel(block)}
      >
        <img
          class="inline-attachment-image"
          src={mediaUrl}
          alt={attachmentPreviewLabel(block)}
          loading="lazy"
        />
        <span class="inline-attachment-name">{attachmentFilename(block)}</span>
      </a>
    {/if}
  {:else if isFileContentBlock(block) || isMediaContentBlock(block)}
    {@const fileUrl = attachmentUrlForBlock(block)}
    <div class="inline-file">
      <svg
        class="inline-file-icon"
        viewBox="0 0 16 16"
        width="14"
        height="14"
        aria-hidden="true"
      >
        <path
          d="M3.5 1.5h6.5l2.5 2.5v10.5H3.5z"
          fill="none"
          stroke="currentColor"
          stroke-width="1.2"
        />
        <path
          d="M10 1.5V4h2.5"
          fill="none"
          stroke="currentColor"
          stroke-width="1.2"
        />
      </svg>
      {#if fileUrl}
        <a
          class="inline-file-link"
          href={fileUrl}
          download={attachmentFilename(block)}
          title={attachmentFilename(block)}
        >
          {attachmentFilename(block)}
        </a>
      {:else}
        <span class="inline-file-name">{attachmentFilename(block)}</span>
      {/if}
    </div>
  {/if}
{/snippet}

<section class="messages" bind:this={scrollContainer} aria-live="polite">
  <div class="messages__content">
    {#if timelineItems.length === 0}
      <div class="empty-state chat-empty-state">
        <svg class="empty-state-icon" viewBox="0 0 32 32" aria-hidden="true">
          <path d="M5 7h22v14H16l-6 5v-5H5z" />
        </svg>
        <p class="empty-state-title">
          {t('chat.historyEmptyTitle', 'No messages yet')}
        </p>
        <p class="empty-state-sub">
          {t(
            'chat.historyEmpty',
            'No messages yet. Send the first message to this agent.',
          )}
        </p>
      </div>
    {:else}
      <div class="date-sep">
        {formatDate(timestampForItem(timelineItems[0]))}
      </div>
      {#each timelineItems as item (item.id)}
        {#if item.type === 'streaming'}
          <article class="msg assistant streaming-message">
            <div class="msg-header">
              <div class="msg-avatar">{avatarForItem(item)}</div>
              <span class="msg-author"
                >{item.streamingItem.type === 'assistant'
                  ? agentName ||
                    t('chat.role.assistant', 'Assistant').toUpperCase()
                  : labelForStreamingItem(item.streamingItem)}</span
              >
            </div>
            <div class="msg-content">
              {#if item.streamingItem.type === 'reasoning'}
                <details
                  class="reasoning-block streaming-reasoning"
                  open={isReasoningOpen(item.id)}
                  ontoggle={(event) =>
                    setReasoningOpen(item.id, event.currentTarget.open)}
                >
                  {@render reasoningSummary(true, isReasoningOpen(item.id))}
                  <div class="reasoning-body">{item.streamingItem.content}</div>
                </details>
              {:else if item.streamingItem.type === 'tool_call'}
                <details class="tool-event streaming-tool-event" open>
                  <summary class="tool-event-line">
                    <span class="te-dot running">●</span>
                    <span class="te-fn"
                      >{streamingToolName(item.streamingItem)}</span
                    >
                    <span class="te-time">
                      {t('chat.toolPreparingArguments', 'preparing arguments')}
                    </span>
                  </summary>
                  <div class="tool-event-body">
                    <div class="teb-row">
                      <span class="teb-label"
                        >{t('chat.toolStatus', 'Status')}</span
                      >
                      <span class="teb-code">
                        {t(
                          'chat.toolArgumentsHidden',
                          'Arguments are streaming and will appear when ready.',
                        )}
                      </span>
                    </div>
                  </div>
                </details>
              {:else}
                <div class="msg-markdown streaming-text">
                  <!-- eslint-disable-next-line svelte/no-at-html-tags -->
                  {@html renderMarkdown(item.streamingItem.content ?? '')}
                  <span class="streaming-caret" aria-hidden="true"></span>
                </div>
              {/if}
            </div>
          </article>
        {:else if item.type === 'assistant_run'}
          <article class="msg assistant assistant-run">
            <div class="msg-header">
              <div class="msg-avatar">{avatarForItem(item)}</div>
              <span class="msg-author"
                >{agentName ||
                  t('chat.role.assistant', 'Assistant').toUpperCase()}</span
              >
              {#if formatTime(timestampForItem(item))}
                <span class="msg-timestamp"
                  >{formatTime(timestampForItem(item))}</span
                >
              {/if}
              {#each runMetaParts(item) as metaPart (metaPart)}
                <span class="msg-meta-extra">· {metaPart}</span>
              {/each}
              {#if shouldRenderRetryButton(item)}
                <button type="button" class="retry-btn" onclick={onRetry}
                  >{t('chat.retryRun', 'Retry last turn')}</button
                >
              {/if}
            </div>
            <div class="msg-content assistant-run-content">
              {#each visibleRunChildren(item) as child (child.id)}
                {#if child.type === 'reasoning'}
                  <details
                    class="reasoning-block"
                    open={isReasoningOpen(child.id)}
                    ontoggle={(event) =>
                      setReasoningOpen(child.id, event.currentTarget.open)}
                  >
                    {@render reasoningSummary(
                      Boolean(child.streaming),
                      isReasoningOpen(child.id),
                    )}
                    <div class="reasoning-body">{child.content}</div>
                  </details>
                {:else if child.type === 'tool_call'}
                  {#if isSubAgentTool(child)}
                    <details
                      class="tool-event run-tool-event subagent-tool-event"
                      open={toolStatus(child) === 'running'}
                    >
                      <summary class="tool-event-line subagent-line">
                        <span
                          class:done={toolStatus(child) === 'success'}
                          class:error={toolStatus(child) === 'failed'}
                          class:running={toolStatus(child) === 'running'}
                          class="te-dot">●</span
                        >
                        <span class="te-fn">
                          {t('chat.subagent.label', 'Sub-agent')}
                        </span>
                        <span class="subagent-agent">
                          {t('agents.form.id', 'Agent ID')}: {subAgentAgentId(
                            child,
                          )}
                        </span>
                        {#if subAgentPreview(child)}
                          <span class="te-arg subagent-preview">
                            {subAgentPreview(child)}
                          </span>
                        {/if}
                        <span class="te-time subagent-status">
                          {subAgentStatusLabel(child)}
                        </span>
                        {#if subAgentNavigationTarget(child)}
                          <button
                            type="button"
                            class="subagent-link"
                            onclick={(event) =>
                              handleSubAgentNavigate(event, child)}
                          >
                            {t('chat.subagent.viewSession', 'view session')}
                          </button>
                        {/if}
                      </summary>
                      <div class="tool-event-body">
                        {@render toolDetailSection(
                          t('chat.toolArgs', 'Args'),
                          toolArguments(child),
                        )}
                        {@render toolDetailSection(
                          t('chat.toolResultLabel', 'Result'),
                          child.result,
                          toolStatus(child) === 'failed',
                          true,
                          toolNameForRunTool(child),
                        )}
                      </div>
                    </details>
                  {:else}
                    <details class="tool-event run-tool-event">
                      <summary class="tool-event-line">
                        <span
                          class:done={toolStatus(child) === 'success'}
                          class:error={toolStatus(child) === 'failed'}
                          class:running={toolStatus(child) === 'running'}
                          class="te-dot">●</span
                        >
                        <span class="te-fn">{toolNameForRunTool(child)}</span>
                        {#if toolArgumentSummary(child)}
                          <span class="te-arg"
                            >{toolArgumentSummary(child)}</span
                          >
                        {/if}
                      </summary>
                      <div class="tool-event-body">
                        {@render toolDetailSection(
                          t('chat.toolArgs', 'Args'),
                          toolArguments(child),
                        )}
                        {@render toolDetailSection(
                          t('chat.toolResultLabel', 'Result'),
                          child.result,
                          toolStatus(child) === 'failed',
                          true,
                          toolNameForRunTool(child),
                        )}
                      </div>
                    </details>
                  {/if}
                {:else if child.type === 'assistant_output'}
                  <div
                    class="msg-markdown"
                    class:streaming-text={child.streaming}
                  >
                    <!-- eslint-disable-next-line svelte/no-at-html-tags -->
                    {@html renderMarkdown(child.content ?? '')}
                    {#if child.streaming}<span
                        class="streaming-caret"
                        aria-hidden="true"
                      ></span>{/if}
                  </div>
                {:else if child.type === 'model_fallback'}
                  <div class="model-fallback-notice">
                    {t('chat.modelFallbackActivated', 'Switched to {model}', {
                      model: child.to_model,
                    })}
                  </div>
                {/if}
              {/each}
            </div>
          </article>
        {:else if item.type === 'message' && shouldRenderMessage(item.message)}
          <article
            class:assistant={item.message.role === 'assistant'}
            class:user={item.message.role === 'user'}
            class:error={item.message.role === 'error'}
            class="msg"
          >
            <div class="msg-header">
              <div class="msg-avatar">{avatarForItem(item)}</div>
              <span class="msg-author"
                >{item.message.role === 'assistant'
                  ? agentName || labelForMessage(item.message)
                  : labelForMessage(item.message)}</span
              >
              {#if formatTime(item.message.timestamp)}
                <span class="msg-timestamp"
                  >{formatTime(item.message.timestamp)}</span
                >
              {/if}
            </div>
            <div class="msg-content">
              {#if hasReadableReasoning(item.message) && hasAssistantContent(item.message)}
                <details
                  class="reasoning-block"
                  open={isReasoningOpen(item.id)}
                  ontoggle={(event) =>
                    setReasoningOpen(item.id, event.currentTarget.open)}
                >
                  {@render reasoningSummary(false, isReasoningOpen(item.id))}
                  <div class="reasoning-body">{item.message.reasoning}</div>
                </details>
              {/if}
              {#if hasUserContentBlocks(item.message)}
                <div class="msg-body-blocks">
                  {#each userContentBlocks(item.message) as block, blockIndex (`${item.id}-block-${blockIndex}`)}
                    {@render userContentBlock(block)}
                  {/each}
                </div>
              {:else if textFromMessage(item.message)}
                {#if item.message.role === 'assistant'}
                  <div class="msg-markdown">
                    <!-- eslint-disable-next-line svelte/no-at-html-tags -->
                    {@html renderMarkdown(textFromMessage(item.message))}
                  </div>
                {:else}
                  <p class="msg-body-text">{textFromMessage(item.message)}</p>
                {/if}
              {/if}
            </div>
          </article>
        {:else if item.type === 'compaction_separator'}
          <div class="date-sep compaction-sep">
            {t('chat.compacted', 'Context compacted')}
          </div>
        {:else if item.type === 'event'}
          {#if isToolEvent(item.event)}
            <article class="msg assistant">
              <div class="msg-header">
                <div class="msg-avatar">{avatarForItem(item)}</div>
                <span class="msg-author">{labelForEvent(item.event)}</span>
                {#if formatTime(item.event.timestamp)}
                  <span class="msg-timestamp"
                    >{formatTime(item.event.timestamp)}</span
                  >
                {/if}
              </div>
              <div class="msg-content">
                <details class="tool-event">
                  <summary class="tool-event-line">
                    <span
                      class:error={isFailedToolEvent(item.event)}
                      class:running={isRunningToolEvent(item.event)}
                      class:done={!isRunningToolEvent(item.event) &&
                        !isFailedToolEvent(item.event)}
                      class="te-dot">●</span
                    >
                    <span class="te-fn">{toolNameForEvent(item.event)}</span>
                    {#if toolArgumentForEvent(item.event)}
                      <span class="te-arg"
                        >{toolArgumentForEvent(item.event)}</span
                      >
                    {/if}
                  </summary>
                  <div class="tool-event-body">
                    {@render toolDetailSection(
                      t('chat.toolArgs', 'Args'),
                      toolCallFromEvent(item.event)?.arguments,
                    )}
                    {#if toolResultValueForEvent(item.event)}
                      {@render toolDetailSection(
                        t('chat.toolResultLabel', 'Result'),
                        toolResultValueForEvent(item.event),
                        isFailedToolEvent(item.event),
                        true,
                        toolNameForEvent(item.event),
                      )}
                    {/if}
                  </div>
                </details>
              </div>
            </article>
          {:else if isTerminalEvent(item.event)}
            <p class="chat-terminal-event">
              <span>{labelForEvent(item.event)}</span>
              {#if metaForEvent(item.event)}
                <span>· {metaForEvent(item.event)}</span>
              {/if}
              {#if shouldRenderRetryButton(item)}
                <button type="button" class="retry-btn" onclick={onRetry}
                  >{t('chat.retryRun', 'Retry last turn')}</button
                >
              {/if}
            </p>
          {:else if textFromEvent(item.event) || hasUserContentBlocks(messageFromEvent(item.event))}
            <article
              class:assistant={isAssistantItem(item)}
              class:user={isUserItem(item)}
              class="msg"
            >
              <div class="msg-header">
                <div class="msg-avatar">{avatarForItem(item)}</div>
                <span class="msg-author">{labelForEvent(item.event)}</span>
                {#if formatTime(item.event.timestamp)}
                  <span class="msg-timestamp"
                    >{formatTime(item.event.timestamp)}</span
                  >
                {/if}
              </div>
              <div class="msg-content">
                {#if item.event.type === 'reasoning'}
                  <details
                    class="reasoning-block"
                    open={isReasoningOpen(item.id)}
                    ontoggle={(event) =>
                      setReasoningOpen(item.id, event.currentTarget.open)}
                  >
                    {@render reasoningSummary(false, isReasoningOpen(item.id))}
                    <div class="reasoning-body">
                      {textFromEvent(item.event)}
                    </div>
                  </details>
                {:else if hasUserContentBlocks(messageFromEvent(item.event))}
                  <div class="msg-body-blocks">
                    {#each userContentBlocks(messageFromEvent(item.event)) as block, blockIndex (`${item.id}-block-${blockIndex}`)}
                      {@render userContentBlock(block)}
                    {/each}
                  </div>
                {:else}
                  <p class="msg-body-text">{textFromEvent(item.event)}</p>
                {/if}
              </div>
            </article>
          {/if}
        {/if}
      {/each}
    {/if}
  </div>
</section>

<style>
  .messages {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    overflow-y: auto;
    background: var(--bg);
  }

  .messages__content {
    display: flex;
    min-width: 0;
    min-height: 100%;
    flex: 1;
    flex-direction: column;
  }

  .chat-empty-state {
    min-height: 100%;
    flex: 1;
  }

  .empty-state-icon {
    width: 38px;
    height: 38px;
  }

  .msg-body-text,
  .reasoning-body,
  .teb-code,
  .chat-terminal-event {
    white-space: pre-wrap;
  }

  .reasoning-block,
  .tool-event {
    max-width: 100%;
  }

  .reasoning-block {
    align-self: flex-start;
  }

  .reasoning-block summary,
  .tool-event summary {
    cursor: pointer;
    list-style: none;
  }

  .reasoning-block summary::-webkit-details-marker,
  .tool-event summary::-webkit-details-marker {
    display: none;
  }

  .reasoning-header {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 2px 0;
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: 12px;
    user-select: none;
  }

  .reasoning-header:hover {
    color: var(--text-hi);
  }

  .reasoning-icon {
    width: 10px;
    height: 10px;
    flex-shrink: 0;
    opacity: 0.4;
  }

  .r-chevron {
    width: 10px;
    height: 10px;
    flex-shrink: 0;
    margin-left: 4px;
    opacity: 0.4;
    transform-origin: center;
    transition: transform 0.2s;
  }

  .reasoning-body {
    display: none;
    margin-top: 4px;
    border-left: 2px solid var(--border-2);
    padding: 6px 0 2px 16px;
    color: var(--text-med);
    font-size: 13px;
    font-style: italic;
    line-height: 1.6;
  }

  .reasoning-block[open] .reasoning-body,
  .tool-event[open] .tool-event-body {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .reasoning-block[open] .reasoning-body {
    display: block;
  }

  .reasoning-block[open] .r-chevron {
    transform: rotate(180deg);
  }

  .tool-event-body {
    max-width: min(64rem, calc(100vw - 340px));
  }

  .teb-row {
    display: flex;
    flex-direction: row;
    align-items: baseline;
    gap: 8px;
  }

  .teb-label {
    flex-shrink: 0;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10px;
    text-transform: uppercase;
  }

  .teb-code {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  .chat-terminal-event {
    align-self: center;
    margin: 8px 28px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    text-align: center;
  }

  .retry-btn {
    margin-left: 7px;
    border: 0;
    border-bottom: 1px solid rgba(232, 135, 10, 0.28);
    padding: 0;
    background: transparent;
    color: var(--text-med);
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 10.5px;
    line-height: 1.2;
  }

  .retry-btn:hover {
    border-bottom-color: rgba(232, 135, 10, 0.48);
    color: var(--accent);
  }

  .retry-btn:focus-visible {
    border-radius: 3px;
    outline: 1px solid rgba(232, 135, 10, 0.35);
    outline-offset: 3px;
  }

  .streaming-message .msg-author {
    color: var(--text-med);
  }

  .streaming-text {
    color: var(--text-hi);
  }

  .streaming-reasoning .reasoning-body {
    color: var(--text-med);
    font-style: italic;
  }

  .streaming-tool-event .te-time {
    color: var(--amber);
  }

  .model-fallback-notice {
    border-left: 2px solid rgba(232, 135, 10, 0.25);
    margin: 2px 0;
    padding: 4px 0 4px 12px;
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: 12px;
    font-style: italic;
  }

  .subagent-tool-event .tool-event-line {
    align-items: center;
  }

  .subagent-agent {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11.5px;
  }

  .subagent-preview {
    max-width: min(34rem, 42vw);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .subagent-status {
    color: var(--text-lo);
  }

  .subagent-tool-event .te-dot.running + .te-fn ~ .subagent-status {
    color: var(--amber);
  }

  .subagent-link {
    border: 0;
    border-bottom: 1px solid rgba(232, 135, 10, 0.32);
    padding: 0;
    background: transparent;
    color: var(--accent);
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 11.5px;
  }

  .subagent-link:hover {
    border-bottom-color: var(--accent);
    color: var(--text-hi);
  }

  .subagent-link:focus-visible {
    border-radius: 3px;
    outline: 1px solid rgba(232, 135, 10, 0.4);
    outline-offset: 3px;
  }

  .streaming-caret {
    display: inline-block;
    width: 5px;
    height: 1em;
    margin-left: 3px;
    transform: translateY(2px);
    background: var(--accent);
    animation: stream-pulse 900ms steps(2, start) infinite;
  }

  .reasoning-header .streaming-caret {
    width: 4px;
    height: 10px;
    margin-left: 2px;
  }

  @keyframes stream-pulse {
    0%,
    45% {
      opacity: 1;
    }

    46%,
    100% {
      opacity: 0.2;
    }
  }

  .msg.error {
    border-left: 3px solid var(--red);
    padding-left: 12px;
  }

  .msg.error .msg-author {
    color: var(--red);
  }

  .msg-body-blocks {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .msg-body-blocks .msg-body-text {
    margin: 0;
  }

  .inline-attachment {
    display: flex;
    width: fit-content;
    max-width: min(30rem, 100%);
    flex-direction: column;
    gap: 8px;
    border: 1px solid var(--border-2);
    border-radius: 6px;
    padding: 8px;
    background: var(--surface-2);
    text-decoration: none;
  }

  .inline-attachment:hover {
    border-color: rgba(232, 135, 10, 0.38);
    background: rgba(232, 135, 10, 0.06);
  }

  .inline-attachment:focus-visible {
    border-radius: 6px;
    outline: 1px solid rgba(232, 135, 10, 0.4);
    outline-offset: 2px;
  }

  .inline-attachment-image {
    width: 100%;
    max-height: 320px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg);
    object-fit: contain;
  }

  .inline-attachment-name {
    overflow: hidden;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .inline-file {
    display: flex;
    width: fit-content;
    max-width: min(30rem, 100%);
    align-items: center;
    gap: 8px;
    border: 1px solid var(--border-2);
    border-radius: 6px;
    padding: 8px 10px;
    background: var(--surface-2);
  }

  .inline-file-icon {
    flex-shrink: 0;
    color: var(--text-lo);
  }

  .inline-file-link,
  .inline-file-name {
    min-width: 0;
    overflow: hidden;
    font-family: var(--font-mono);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .inline-file-link {
    border-bottom: 1px solid rgba(232, 135, 10, 0.3);
    color: var(--accent);
    text-decoration: none;
  }

  .inline-file-link:hover {
    border-bottom-color: var(--accent);
    color: var(--text-hi);
  }

  .inline-file-link:focus-visible {
    border-radius: 3px;
    outline: 1px solid rgba(232, 135, 10, 0.4);
    outline-offset: 2px;
  }

  .inline-file-name {
    color: var(--text-med);
  }

  @media (max-width: 760px) {
    .tool-event-body {
      max-width: 100%;
    }
  }
</style>
