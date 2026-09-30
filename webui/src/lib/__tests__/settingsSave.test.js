import { beforeEach, describe, expect, it, vi } from 'vitest';

const updateSettingsMock = vi.fn();
const getSettingsMock = vi.fn();

vi.mock('../api.js', () => ({
  getSettings: getSettingsMock,
  updateSettings: updateSettingsMock,
}));

const { createSettingsDraft, SETTINGS_CONFLICT_CODE } =
  await import('../settingsSave.js');

const CONFLICT_NOTICE =
  'Some of these settings were changed elsewhere while you edited. The editor now shows the saved values.';

function deferred() {
  let resolve;
  const promise = new Promise((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function conflict() {
  return Object.assign(new Error('Settings changed since they were read'), {
    code: SETTINGS_CONFLICT_CODE,
  });
}

function debug(enabled, traceLimit) {
  return { debug: { enabled, trace_limit: traceLimit } };
}

// A debug-panel-style editor: the draft holds form values (a typed number is
// a string until the payload normalizes it).
function editor(settings) {
  const state = { draft: { ...settings.debug } };
  const draft = createSettingsDraft({
    settings,
    fromSettings: (next) => ({ ...next.debug }),
    read: () => state.draft,
    write: (next) => {
      state.draft = next;
    },
    toPayload: (values) =>
      debug(values.enabled, Number.parseInt(values.trace_limit, 10)),
  });
  const callbacks = {
    onCommit: vi.fn(),
    onError: vi.fn(),
    setSaving: vi.fn(),
  };
  return { state, save: () => draft.save(callbacks), callbacks };
}

describe('createSettingsDraft', () => {
  beforeEach(() => {
    updateSettingsMock.mockReset();
    getSettingsMock.mockReset();
  });

  it('sends its origin as base and keeps only edits made during the request', async () => {
    const request = deferred();
    const subject = editor(debug(false, 50));
    updateSettingsMock.mockReturnValue(request.promise);
    subject.state.draft = { ...subject.state.draft, trace_limit: '100' };

    const saving = subject.save();
    subject.state.draft = { ...subject.state.draft, enabled: true };
    const response = debug(false, 100);
    request.resolve(response);

    await expect(saving).resolves.toBe(true);
    expect(updateSettingsMock).toHaveBeenCalledWith({
      ...debug(false, 100),
      base: debug(false, 50),
    });
    expect(subject.callbacks.onCommit).toHaveBeenCalledWith(response);
    // The saved (normalized) value replaces the unchanged field only.
    expect(subject.state.draft).toEqual({ enabled: true, trace_limit: 100 });

    updateSettingsMock.mockResolvedValue(debug(true, 100));
    await subject.save();
    expect(updateSettingsMock).toHaveBeenLastCalledWith({
      ...debug(true, 100),
      base: debug(false, 100),
    });
  });

  it('rebases a refused draft onto re-read Settings and retries its edits', async () => {
    const subject = editor(debug(false, 50));
    subject.state.draft = { ...subject.state.draft, enabled: true };
    const current = debug(false, 80);
    updateSettingsMock
      .mockRejectedValueOnce(conflict())
      .mockResolvedValueOnce(debug(true, 80));
    getSettingsMock.mockResolvedValue(current);

    await expect(subject.save()).resolves.toBe(true);

    expect(updateSettingsMock).toHaveBeenLastCalledWith({
      ...debug(true, 80),
      base: current,
    });
    expect(subject.callbacks.onCommit.mock.calls).toEqual([
      [current],
      [debug(true, 80)],
    ]);
    expect(subject.callbacks.onError).not.toHaveBeenCalledWith(CONFLICT_NOTICE);
  });

  it('lets a value saved elsewhere win over the same field in the draft and reports it', async () => {
    const subject = editor(debug(false, 50));
    subject.state.draft = { ...subject.state.draft, trace_limit: 100 };
    const current = debug(false, 80);
    updateSettingsMock.mockRejectedValueOnce(conflict());
    getSettingsMock.mockResolvedValue(current);

    await expect(subject.save()).resolves.toBe(true);

    // Nothing is left to write once the draft shows the saved values.
    expect(updateSettingsMock).toHaveBeenCalledTimes(1);
    expect(subject.state.draft).toEqual(current.debug);
    expect(subject.callbacks.onCommit).toHaveBeenCalledWith(current);
    expect(subject.callbacks.onError).toHaveBeenLastCalledWith(CONFLICT_NOTICE);
  });

  it('fails after a bounded number of refused writes', async () => {
    const subject = editor(debug(false, 50));
    subject.state.draft = { ...subject.state.draft, enabled: true };
    updateSettingsMock.mockRejectedValue(conflict());
    getSettingsMock.mockResolvedValue(debug(false, 80));

    await expect(subject.save()).resolves.toBe(false);

    expect(updateSettingsMock).toHaveBeenCalledTimes(3);
    expect(subject.callbacks.onError).toHaveBeenLastCalledWith(
      'Settings could not be saved. Settings changed since they were read',
    );
    expect(subject.callbacks.setSaving).toHaveBeenLastCalledWith(false);
  });
});
