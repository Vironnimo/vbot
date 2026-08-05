import MarkdownIt from 'markdown-it';
import { describe, expect, it, vi } from 'vitest';

import {
  linkifiedTextSegments,
  reasoningMarkdownSource,
  renderMarkdown,
  renderMarkdownDocument,
  renderMarkdownStreaming,
  renderReasoningMarkdown,
  renderReasoningMarkdownStreaming,
} from '../markdown.js';

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

  it('renders fenced code blocks with the shared header contract', () => {
    const html = renderMarkdown('```\nconst x = 1;\n```');

    expect(html).toContain('<div class="msg-code">');
    expect(html).toContain('<span class="msg-code__language">text</span>');
    expect(html).toContain('data-markdown-code-index="0"');
    expect(html).toContain('<pre><code>');
    expect(html).toContain('const x = 1;');
    expect(html).not.toContain('class="language-');
  });

  it('projects fenced code text separately from safe HTML', () => {
    const document = renderMarkdownDocument('```python\nprint("& safe")\n```');

    expect(document.html).toContain(
      '<span class="msg-code__language">python</span>',
    );
    expect(document.html).toContain('print(&quot;&amp; safe&quot;)');
    expect(document.codeBlocks).toEqual([
      { text: 'print("& safe")\n', copyable: true },
    ]);
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

  it('autolinks literal http and https URLs without trailing punctuation', () => {
    const html = renderMarkdown(
      'Open http://localhost:8421/test or https://example.com/docs.',
    );

    expect(html).toContain('href="http://localhost:8421/test"');
    expect(html).toContain('href="https://example.com/docs"');
    expect(html).not.toContain('href="https://example.com/docs."');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('does not autolink unsupported schemes, bare domains, or code', () => {
    const html = renderMarkdown(
      'example.com ftp://example.com file:///tmp/x `https://example.com/code`',
    );

    expect(html).not.toContain('<a');
    expect(html).toContain('<code>https://example.com/code</code>');
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
    expect(html).toContain('<span class="msg-code__language">js</span>');
    expect(html).toContain('<pre><code>');
    expect(html).toContain('const value = 1;');
    expect(html).not.toContain('data-markdown-code-index');
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

  it('keeps an open tilde fence non-copyable while streaming', () => {
    const html = renderMarkdownStreaming('~~~json\n{"partial": true}');

    expect(html).toContain('<span class="msg-code__language">json</span>');
    expect(html).toContain('{&quot;partial&quot;: true}');
    expect(html).not.toContain('data-markdown-code-index');
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

describe('linkifiedTextSegments()', () => {
  it('splits plain text around safe HTTP(S) URLs', () => {
    expect(
      linkifiedTextSegments(
        '**literal** https://example.com/docs, then http://localhost:8421.',
      ),
    ).toEqual([
      { text: '**literal** ', href: null },
      {
        text: 'https://example.com/docs',
        href: 'https://example.com/docs',
      },
      { text: ', then ', href: null },
      {
        text: 'http://localhost:8421',
        href: 'http://localhost:8421',
      },
      { text: '.', href: null },
    ]);
  });

  it('leaves unsupported schemes and path-like text inert', () => {
    const source =
      'ftp://example.com mailto:a@example.com file:///tmp/x C:/tmp/x';

    expect(linkifiedTextSegments(source)).toEqual([
      { text: source, href: null },
    ]);
  });

  it('leaves URLs inside plain-text code markers inert', () => {
    expect(
      linkifiedTextSegments(
        '`https://example.com/inline`\n```\nhttps://example.com/fenced\n```\nhttps://example.com/live',
      ),
    ).toEqual([
      {
        text: '`https://example.com/inline`\n```\nhttps://example.com/fenced\n```\n',
        href: null,
      },
      {
        text: 'https://example.com/live',
        href: 'https://example.com/live',
      },
    ]);
  });
});

describe('reasoning Markdown', () => {
  it('separates adjacent bold reasoning blocks while preserving their emphasis', () => {
    const source = '**Designing the component****Planning the tests**';

    expect(reasoningMarkdownSource(source)).toBe(
      '**Designing the component**\n**Planning the tests**',
    );

    for (const html of [
      renderReasoningMarkdown(source),
      renderReasoningMarkdownStreaming(source),
    ]) {
      expect(html).toContain('<strong>Designing the component</strong>');
      expect(html).toContain('<br>');
      expect(html).toContain('<strong>Planning the tests</strong>');
      expect(html).not.toContain('componentPlanning');
    }
  });

  it('leaves adjacent bold markers untouched outside reasoning', () => {
    const html = renderMarkdown('**First****Second**');

    expect(html).not.toContain('<br>');
  });

  it('does not reinterpret longer asterisk runs as reasoning boundaries', () => {
    expect(reasoningMarkdownSource('Before ****** after')).toBe(
      'Before ****** after',
    );
  });
});
