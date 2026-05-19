import { describe, expect, it } from 'vitest';

import { renderMarkdown } from '../markdown.js';

describe('renderMarkdown()', () => {
  it('renders headings', () => {
    const html = renderMarkdown('# Title\n\n## Subtitle');

    expect(html).toContain('<h1>Title</h1>');
    expect(html).toContain('<h2>Subtitle</h2>');
  });

  it('renders bold and italic text', () => {
    const html = renderMarkdown('**bold** and _italic_');

    expect(html).toContain('<strong>bold</strong>');
    expect(html).toContain('<em>italic</em>');
  });

  it('renders fenced code blocks without syntax highlighting classes', () => {
    const html = renderMarkdown('```\nconst x = 1;\n```');

    expect(html).toContain('<pre><code>');
    expect(html).toContain('const x = 1;');
    expect(html).not.toContain('class="language-');
  });

  it('renders inline code', () => {
    const html = renderMarkdown('Use `code` here.');

    expect(html).toContain('<code>code</code>');
  });

  it('renders https links with target and rel attributes', () => {
    const html = renderMarkdown('[text](https://example.com)');

    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('does not create a live javascript link', () => {
    const html = renderMarkdown('[x](javascript:alert(1))');

    expect(html).not.toContain('href="javascript:');
    expect(html).toContain('[x](javascript:alert(1))');
  });

  it('escapes raw html tags', () => {
    const html = renderMarkdown('<script>alert(1)</script>');

    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).not.toContain('<script>alert(1)</script>');
  });

  it('renders unclosed code fences without throwing', () => {
    expect(() => renderMarkdown('```\nunterminated')).not.toThrow();

    const html = renderMarkdown('```\nunterminated');
    expect(html).toContain('<pre><code>');
  });

  it('returns an empty string for empty input', () => {
    expect(renderMarkdown('')).toBe('');
  });

  it('renders unordered and ordered lists', () => {
    const html = renderMarkdown('- a\n- b\n\n1. c\n2. d');

    expect(html).toContain('<ul>');
    expect(html).toContain('<li>a</li>');
    expect(html).toContain('<li>b</li>');
    expect(html).toContain('<ol>');
    expect(html).toContain('<li>c</li>');
    expect(html).toContain('<li>d</li>');
  });

  it('renders gfm tables', () => {
    const html = renderMarkdown('|A|B|\n|-|-|\n|1|2|');

    expect(html).toContain('<table>');
    expect(html).toContain('<thead>');
    expect(html).toContain('<tbody>');
    expect(html).toContain('<th>A</th>');
    expect(html).toContain('<td>1</td>');
  });
});
