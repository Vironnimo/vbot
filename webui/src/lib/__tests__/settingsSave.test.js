import { beforeEach, describe, expect, it, vi } from 'vitest';

const updateSettingsMock = vi.fn();

vi.mock('../api.js', () => ({
  updateSettings: updateSettingsMock,
}));

const { runSettingsSave } = await import('../settingsSave.js');

function deferred() {
  let resolve;
  const promise = new Promise((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function saveOptions(draft, applyResult) {
  return {
    buildPayload: () => ({ defaults: { agent: { model: draft.model } } }),
    getDraftSnapshot: () => draft,
    onCommit: vi.fn(),
    onToast: vi.fn(),
    onError: vi.fn(),
    setSaving: vi.fn(),
    successKey: 'settings.defaults.saveSuccess',
    successFallback: 'Agent defaults updated.',
    applyResult,
  };
}

describe('runSettingsSave', () => {
  beforeEach(() => {
    updateSettingsMock.mockReset();
  });

  it('does not apply a stale response over a draft edited during the request', async () => {
    const request = deferred();
    const draft = { model: 'provider/first' };
    const applyResult = vi.fn();
    const options = saveOptions(draft, applyResult);
    updateSettingsMock.mockReturnValue(request.promise);

    const saving = runSettingsSave(options);
    draft.model = 'provider/latest';
    const response = {
      defaults: { agent: { model: 'provider/first' } },
    };
    request.resolve(response);

    await expect(saving).resolves.toBe(true);
    expect(updateSettingsMock).toHaveBeenCalledWith({
      defaults: { agent: { model: 'provider/first' } },
    });
    expect(options.onCommit).toHaveBeenCalledWith(response);
    expect(applyResult).not.toHaveBeenCalled();
    expect(draft.model).toBe('provider/latest');
  });

  it('applies the server response when the submitted draft is still current', async () => {
    const draft = { model: 'provider/first' };
    const applyResult = vi.fn();
    const options = saveOptions(draft, applyResult);
    const response = {
      defaults: { agent: { model: 'provider/canonical' } },
    };
    updateSettingsMock.mockResolvedValue(response);

    await expect(runSettingsSave(options)).resolves.toBe(true);

    expect(options.onCommit).toHaveBeenCalledWith(response);
    expect(applyResult).toHaveBeenCalledWith(response);
  });
});
