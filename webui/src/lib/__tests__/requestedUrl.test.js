import { describe, expect, it } from 'vitest';
import { loadDomainParser, requestedUrl } from '../requestedUrl.js';

const getDomain = await loadDomainParser();

describe('requested URLs', () => {
  it('spells the address with its subdomain and registrable domain apart', () => {
    expect(
      requestedUrl(' https://login.example.co.uk:8443/a?b#c ', getDomain),
    ).toEqual({
      href: 'https://login.example.co.uk:8443/a?b#c',
      host: 'login.example.co.uk',
      domain: 'example.co.uk',
      parts: [
        { kind: 'text', text: 'https://' },
        { kind: 'subdomain', text: 'login.' },
        { kind: 'domain', text: 'example.co.uk' },
        { kind: 'text', text: ':8443/a?b#c' },
      ],
      warnings: [],
    });
  });

  it.each([
    ['http://example.com/', 'example.com', ['insecure']],
    // A Cyrillic "а" that imitates "apple.com".
    ['https://аpple.com/', 'xn--pple-43d.com', ['international']],
    [
      'https://example.com@login.attacker.net/',
      'attacker.net',
      ['credentials'],
    ],
    ['https://192.168.0.1/', '192.168.0.1', ['ipAddress']],
    ['https://[::1]/', '[::1]', ['ipAddress']],
  ])('warns about %s, which opens on %s', (text, domain, warnings) => {
    const address = requestedUrl(text, getDomain);
    expect(address.domain).toBe(domain);
    expect(address.warnings).toEqual(warnings);
    expect(address.parts.map((part) => part.text).join('')).toBe(address.href);
  });

  it.each(['javascript:alert(1)', 'mailto:user@example.com', 'data:,x', ''])(
    'is no web address: %s',
    (text) => {
      expect(requestedUrl(text, getDomain)).toBeNull();
    },
  );

  it('marks the whole host until the domain parser has loaded', () => {
    expect(requestedUrl('https://login.example.com/').domain).toBe(
      'login.example.com',
    );
  });
});
