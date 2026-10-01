import { describe, expect, it } from 'vitest';

import {
  describeLocalModelDownload,
  describeLocalModelSetup,
  formatDownloadSize,
} from '../settingsView.js';

describe('local Model download facts', () => {
  it.each([
    [346_806_730, '347 MB'],
    [8_400_000, '8.4 MB'],
    [1_234_000_000, '1.2 GB'],
    [null, ''],
    [-1, ''],
  ])('formats %s bytes as "%s"', (bytes, expected) => {
    expect(formatDownloadSize(bytes)).toBe(expected);
  });

  it('names the download size and license it knows', () => {
    expect(
      describeLocalModelDownload({
        license: 'Apache-2.0',
        download_bytes: 346_806_730,
      }),
    ).toBe('347 MB download · Apache-2.0 license');
    expect(describeLocalModelDownload({ license: 'MIT' })).toBe('MIT license');
    expect(describeLocalModelDownload(undefined)).toBe('');
  });
});

describe('local Model installation', () => {
  it.each([
    [
      'the first status check',
      { state: 'checking', status: null, error: '' },
      {
        tone: 'neutral',
        message: 'Checking whether it is installed…',
        progress: null,
        action: null,
      },
    ],
    [
      'a Model not set up yet',
      {
        state: 'missing',
        status: { state: 'missing', error: 'model_missing' },
        error: '',
      },
      {
        tone: 'neutral',
        message: 'Not set up on this computer yet.',
        progress: null,
        action: 'install',
      },
    ],
    [
      'the runtime installation',
      {
        state: 'installing',
        status: { state: 'installing', phase: 'python', error: '' },
        error: '',
      },
      {
        tone: 'neutral',
        message: 'Preparing the local model runtime…',
        progress: null,
        action: 'installing',
      },
    ],
    [
      'the model download with its progress',
      {
        state: 'installing',
        status: {
          state: 'installing',
          phase: 'downloading',
          error: '',
          progress: { completed: 120_000_000, total: 346_806_730 },
        },
        error: '',
      },
      {
        tone: 'neutral',
        message: 'Downloading the model. You can leave this page.',
        progress: { percent: 34, text: '120 MB of 347 MB' },
        action: 'installing',
      },
    ],
    [
      'an unknown phase',
      {
        state: 'installing',
        status: { state: 'installing', phase: 'something_new', error: '' },
        error: '',
      },
      expect.objectContaining({
        message: 'Installing the local model runtime…',
      }),
    ],
    [
      'a failed download',
      {
        state: 'failed',
        status: {
          state: 'failed',
          phase: 'downloading',
          error: 'checksum_mismatch',
        },
        error: '',
      },
      {
        tone: 'warn',
        message: 'The downloaded model files were damaged. Try again.',
        progress: null,
        action: 'retry',
      },
    ],
    [
      'an unknown failure code',
      {
        state: 'failed',
        status: { state: 'failed', error: 'something_new' },
        error: '',
      },
      expect.objectContaining({
        message: expect.stringMatching(/^Installation failed\./),
        action: 'retry',
      }),
    ],
    [
      'an unreachable server',
      {
        state: 'installing',
        status: { state: 'installing' },
        error: 'connection',
      },
      {
        tone: 'warn',
        message:
          'The server could not be reached. Check again to see the installation status.',
        progress: null,
        action: 'check',
      },
    ],
    [
      'an installed Model',
      { state: 'ready', status: { state: 'ready' }, error: '' },
      expect.objectContaining({ message: 'Installed.', action: null }),
    ],
    [
      'an installation that needs a server restart',
      {
        state: 'restart_required',
        status: { state: 'restart_required' },
        error: '',
      },
      expect.objectContaining({
        message: 'Installed. Restart the vBot server to use it.',
        action: null,
      }),
    ],
  ])('describes %s', (_name, job, expected) => {
    expect(describeLocalModelSetup(job)).toEqual(expected);
  });
});
