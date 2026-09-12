import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createTerminalsController,
  createTerminalsViewState,
} from '../terminalsView.js';
import { fakeApi, manual, terminal } from './terminalsView.support.js';

describe('terminal speech input', () => {
  let controller;
  afterEach(() => controller?.destroy());

  async function setup({ createRecorder, transcribe } = {}) {
    const streams = [];
    const state = createTerminalsViewState();
    const recorder = {
      start: vi.fn(),
      cancel: vi.fn(),
      stop: vi.fn().mockResolvedValue(new Blob(['audio'])),
      filename: () => 'recording.ogg',
    };
    const onTranscript = vi.fn();
    const onSpeechError = vi.fn();
    const api = fakeApi({
      streams,
      groups: [manual(), manual({ group_id: 'other' })],
    });
    const record = createRecorder ?? vi.fn().mockResolvedValue(recorder);
    const recognize =
      transcribe ?? vi.fn().mockResolvedValue({ text: 'dictation sentinel' });
    controller = createTerminalsController({
      state,
      api,
      createRecorder: record,
      transcribe: recognize,
      onTranscript,
      onSpeechError,
    });
    await controller.start();
    streams[0].emit({
      type: 'terminal_ready',
      sequence: 1,
      ansi: '',
      terminal: terminal('term-1'),
    });
    return {
      state,
      api,
      streams,
      recorder,
      onTranscript,
      onSpeechError,
      record,
      recognize,
    };
  }

  it('uploads the native recording once through the shared STT path', async () => {
    const h = await setup();
    await controller.toggleSpeech('term-1');
    await controller.toggleSpeech('term-2');
    expect(h.record).toHaveBeenCalledOnce();
    expect(h.state.speechState).toBe('recording');
    await controller.toggleSpeech('term-1');
    expect(h.recognize).toHaveBeenCalledWith(expect.any(Blob), {
      filename: 'recording.ogg',
      signal: expect.any(AbortSignal),
    });
    expect(h.onTranscript).toHaveBeenCalledWith('term-1', 'dictation sentinel');
    expect(h.state.speechState).toBe('idle');
  });

  it.each([
    'cancel',
    'destroy',
    'group',
    'close',
    'exit',
    'disconnect',
    'server',
  ])('releases recording and discards results on %s', async (action) => {
    let finish;
    const h = await setup({
      transcribe: vi.fn(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      ),
    });
    await controller.toggleSpeech('term-1');
    const pending = controller.toggleSpeech('term-1');
    await Promise.resolve();
    if (action === 'cancel') controller.cancelSpeech();
    if (action === 'destroy') controller.destroy();
    if (action === 'group') controller.selectGroup('other');
    if (action === 'close') await controller.closeTerminal('term-1');
    if (action === 'exit')
      h.streams[0].emit({
        type: 'terminal_state',
        sequence: 2,
        terminal: terminal('term-1', { state: 'exited' }),
      });
    if (action === 'disconnect') h.streams[0].close();
    if (action === 'server') controller.setServerUnavailable(true);
    expect(h.recognize.mock.calls[0][1].signal.aborted).toBe(true);
    finish({ text: 'late result sentinel' });
    await pending;
    expect(h.recorder.cancel).toHaveBeenCalled();
    expect(h.onTranscript).not.toHaveBeenCalled();
    expect(h.onSpeechError).not.toHaveBeenCalled();
    expect(h.state.speechState).toBe('idle');
  });

  it('releases late microphone permission without starting it or resetting a newer recording', async () => {
    let allow;
    const createRecorder = vi.fn(
      () =>
        new Promise((resolve) => {
          allow = resolve;
        }),
    );
    const h = await setup({ createRecorder });
    const pending = controller.toggleSpeech('term-1');
    controller.cancelSpeech();
    const newer = { ...h.recorder, start: vi.fn(), cancel: vi.fn() };
    createRecorder.mockResolvedValue(newer);
    await controller.toggleSpeech('term-1');
    allow(h.recorder);
    await pending;
    expect(h.recorder.start).not.toHaveBeenCalled();
    expect(h.recorder.cancel).toHaveBeenCalledOnce();
    expect(h.state.speechState).toBe('recording');
    expect(newer.cancel).not.toHaveBeenCalled();
  });

  it('does not upload when cancelled while the recorder is stopping', async () => {
    let stopped;
    const h = await setup();
    h.recorder.stop.mockImplementation(
      () =>
        new Promise((resolve) => {
          stopped = resolve;
        }),
    );
    await controller.toggleSpeech('term-1');
    const pending = controller.toggleSpeech('term-1');
    controller.cancelSpeech();
    stopped(new Blob(['audio']));
    await pending;
    expect(h.recognize).not.toHaveBeenCalled();
  });

  it.each(['permission', 'start', 'stop', 'transcribe'])(
    'reports %s failures and permits retry',
    async (stage) => {
      const failure = new Error('test failure sentinel');
      const h = await setup();
      if (stage === 'permission') h.record.mockRejectedValueOnce(failure);
      if (stage === 'start')
        h.recorder.start.mockImplementationOnce(() => {
          throw failure;
        });
      if (stage === 'stop') h.recorder.stop.mockRejectedValueOnce(failure);
      if (stage === 'transcribe') h.recognize.mockRejectedValueOnce(failure);
      await controller.toggleSpeech('term-1');
      if (stage === 'stop' || stage === 'transcribe')
        await controller.toggleSpeech('term-1');
      expect(h.onSpeechError).toHaveBeenCalledWith(
        failure,
        stage === 'permission' || stage === 'start'
          ? 'requesting'
          : 'transcribing',
      );
      expect(h.state.speechState).toBe('idle');
      expect(h.onTranscript).not.toHaveBeenCalled();
      await controller.toggleSpeech('term-1');
      expect(h.state.speechState).toBe('recording');
    },
  );

  it('ignores empty transcription and finished or disconnected targets', async () => {
    const h = await setup({
      transcribe: vi.fn().mockResolvedValue({ text: '\n\u001b\t' }),
    });
    await controller.toggleSpeech('term-1');
    await controller.toggleSpeech('term-1');
    expect(h.onTranscript).not.toHaveBeenCalled();
    h.streams[0].close();
    await controller.toggleSpeech('term-1');
    expect(h.record).toHaveBeenCalledOnce();
    h.streams[0].emit({
      type: 'terminal_ready',
      sequence: 2,
      ansi: '',
      terminal: terminal('term-1', { state: 'exited' }),
    });
    await controller.toggleSpeech('term-1');
    expect(h.record).toHaveBeenCalledOnce();
  });
});
