const conversationRowsByRun = new WeakMap();

function isConversationInput(message) {
  return (
    message?.role === 'user' &&
    message.input_origin !== 'extension' &&
    message.sender?.kind !== 'session' &&
    !message.automation
  );
}

// A compact conversation shows real messages, including Steer delivered in
// the middle of a Run. The reducer knows when an answer is final: inferring
// that from the last piece of text can briefly show Tool commentary or leave
// it on screen after a silent, interrupted or failed turn.
export function conversationTimelineRows(items) {
  return items.flatMap((item) => {
    if (item.type === 'external_history')
      return item.message.external_history.kind === 'message' &&
        ['user', 'assistant'].includes(item.message.external_history.role)
        ? [item]
        : [];
    if (item.type === 'message') {
      const message = item.message;
      if (isConversationInput(message) || message.role === 'error')
        return [item];
      if (
        message.role === 'assistant' &&
        message.content &&
        !message.tool_calls?.length &&
        !message.interrupted &&
        message.phase !== 'commentary'
      )
        return [
          {
            ...item,
            message: { ...message, reasoning: null, reasoning_summary: null },
          },
        ];
      return [];
    }
    if (item.type !== 'assistant_run') return [];
    const cached = conversationRowsByRun.get(item);
    if (cached) return cached;
    const rows = [];
    // A recovery continues an interrupted text at its exact last character.
    // Show that prefix only once its continuation proves to be an answer.
    let answerPrefix = '';
    for (const child of item.items) {
      if (child.type === 'user_message' || child.type === 'tool_call')
        answerPrefix = '';
      if (child.type === 'user_message' && isConversationInput(child.message)) {
        rows.push({
          id: child.id,
          type: 'message',
          runId: item.runId,
          message: child.message,
        });
      } else if (child.type === 'assistant_output') {
        if (child.interrupted && child.messages?.[0]?.phase !== 'commentary') {
          answerPrefix += child.content;
          continue;
        }
        if (!child.finalAnswer) {
          if (!child.streaming) answerPrefix = '';
          continue;
        }
        rows.push({
          id: child.id,
          type: 'message',
          runId: item.runId,
          message: {
            id: child.messageId ?? child.id,
            timestamp: child.timestamp,
            role: 'assistant',
            content: answerPrefix + child.content,
            run_id: item.runId,
          },
        });
        answerPrefix = '';
      } else if (child.type === 'history_gap') {
        // Earlier pages can still hold user messages or answers.
        rows.push(child);
      }
    }
    if (['running', 'cancelled', 'interrupted', 'failed'].includes(item.status))
      rows.push({
        id: item.id,
        type: 'run_status',
        status: item.status,
        timestamp: item.endTimestamp ?? item.timestamp,
      });
    conversationRowsByRun.set(item, rows);
    return rows;
  });
}
