import MarkdownIt from 'markdown-it';
import { describe, expect, it, vi } from 'vitest';

import { renderMarkdown, renderMarkdownStreaming } from '../markdown.js';

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

  it('renders streaming content with an unclosed fence as a code block', () => {
    const html = renderMarkdownStreaming('## Title\n\n```js\nconst value = 1;');

    expect(html).toContain('<h2>Title</h2>');
    expect(html).toContain('<pre><code>');
    expect(html).toContain('const value = 1;');
  });

  it('falls back to normal rendering for closed fences while streaming', () => {
    const html = renderMarkdownStreaming('```\nconst value = 1;\n```');

    expect(html).toContain('<pre><code>');
    expect(html).toContain('const value = 1;');
  });

  it('does not treat triple backticks inside code content as a closing fence', () => {
    const html = renderMarkdownStreaming(
      '```js\nconsole.log("``` not a fence");',
    );

    expect(html).toContain('<pre><code>');
    expect(html).toContain('console.log(&quot;``` not a fence&quot;);');
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

  it('memoizes rendering so identical source is parsed only once', () => {
    const renderSpy = vi.spyOn(MarkdownIt.prototype, 'render');
    const source = `cache-hit-${Math.random()}\n\n**bold**`;

    const first = renderMarkdown(source);
    const second = renderMarkdown(source);

    expect(second).toBe(first);
    expect(second).toContain('<strong>bold</strong>');
    expect(renderSpy).toHaveBeenCalledTimes(1);

    renderSpy.mockRestore();
  });

  it('keeps returning correct output after the cache limit is exceeded', () => {
    for (let index = 0; index < 350; index += 1) {
      renderMarkdown(`cache-filler-${index}\n\ncontent ${index}`);
    }

    const html = renderMarkdown('# After eviction');

    expect(html).toContain('<h1>After eviction</h1>');
  });
});
