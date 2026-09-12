const CHAT_STATUS_RUNNING = 'running';

const CHAT_STATUS_COMPLETED = 'completed';

function finishedRunEvents(runId, messageId) {
  return [
    {
      type: 'user_message_persisted',
      run_id: runId,
      sequence: 1,
      payload: {
        message: { id: `user-${runId}`, role: 'user', content: 'Hi' },
      },
    },
    {
      type: 'run_started',
      run_id: runId,
      sequence: 2,
      payload: { status: CHAT_STATUS_RUNNING },
    },
    {
      type: 'assistant_output',
      run_id: runId,
      sequence: 3,
      payload: {
        message: { id: messageId, role: 'assistant', content: 'Done.' },
      },
    },
    {
      type: 'run_completed',
      run_id: runId,
      sequence: 4,
      payload: { status: CHAT_STATUS_COMPLETED },
    },
  ];
}

export { CHAT_STATUS_RUNNING, CHAT_STATUS_COMPLETED, finishedRunEvents };
