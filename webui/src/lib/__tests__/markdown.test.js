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
  it.each([
    [
      'headings',
      '# Title\n\n## Subtitle',
      ['<h1>Title</h1>', '<h2>Subtitle</h2>'],
      [],
    ],
    [
      'bold and italic text',
      '**bold** and _italic_',
      ['<strong>bold</strong>', '<em>italic</em>'],
      [],
    ],
    ['inline code', 'Use `code` here.', ['<code>code</code>'], []],
    [
      'unordered and ordered lists',
      '- a\n- b\n\n1. c\n2. d',
      ['<ul>', '<li>a</li>', '<li>b</li>', '<ol>', '<li>c</li>', '<li>d</li>'],
      [],
    ],
    [
      'GFM tables',
      '|A|B|\n|-|-|\n|1|2|',
      ['<table>', '<thead>', '<tbody>', '<th>A</th>', '<td>1</td>'],
      [],
    ],
    [
      'https links with target and rel attributes',
      '[text](https://example.com)',
      [
        'href="https://example.com"',
        'target="_blank"',
        'rel="noopener noreferrer"',
      ],
      [],
    ],
    [
      'literal http and https URLs as links without trailing punctuation',
      'Open http://localhost:8421/test or https://example.com/docs.',
      [
        'href="http://localhost:8421/test"',
        'href="https://example.com/docs"',
        'target="_blank"',
        'rel="noopener noreferrer"',
      ],
      ['href="https://example.com/docs."'],
    ],
    [
      'unsupported schemes, bare domains, and code without links',
      'example.com ftp://example.com file:///tmp/x `https://example.com/code`',
      ['<code>https://example.com/code</code>'],
      ['<a'],
    ],
    [
      'a javascript link as inert text',
      '[x](javascript:alert(1))',
      ['[x](javascript:alert(1))'],
      ['href="javascript:'],
    ],
    [
      'raw HTML tags escaped',
      '<script>alert(1)</script>',
      ['&lt;script&gt;alert(1)&lt;/script&gt;'],
      ['<script>alert(1)</script>'],
    ],
    [
      'an unclosed code fence as a code block',
      '```\nunterminated',
      ['<pre><code>'],
      [],
    ],
  ])('renders %s', (_label, source, contained, absent) => {
    const html = renderMarkdown(source);

    for (const fragment of contained) {
      expect(html).toContain(fragment);
    }
    for (const fragment of absent) {
      expect(html).not.toContain(fragment);
    }
  });

  it('returns an empty string for empty input', () => {
    expect(renderMarkdown('')).toBe('');
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

  it.each([
    [
      'an unclosed fence as a non-copyable code block',
      '## Title\n\n```js\nconst value = 1;',
      [
        '<h2>Title</h2>',
        '<span class="msg-code__language">js</span>',
        '<pre><code>',
        'const value = 1;',
      ],
      ['data-markdown-code-index'],
    ],
    [
      'a closed fence like normal rendering',
      '```\nconst value = 1;\n```',
      ['<pre><code>', 'const value = 1;', 'data-markdown-code-index="0"'],
      [],
    ],
    [
      'triple backticks inside code content without closing the fence',
      '```js\nconsole.log("``` not a fence");',
      ['<pre><code>', 'console.log(&quot;``` not a fence&quot;);'],
      [],
    ],
    [
      'an open tilde fence as a non-copyable code block',
      '~~~json\n{"partial": true}',
      [
        '<span class="msg-code__language">json</span>',
        '{&quot;partial&quot;: true}',
      ],
      ['data-markdown-code-index'],
    ],
  ])('streams %s', (_label, source, contained, absent) => {
    const html = renderMarkdownStreaming(source);

    for (const fragment of contained) {
      expect(html).toContain(fragment);
    }
    for (const fragment of absent) {
      expect(html).not.toContain(fragment);
    }
  });

  it('parses identical source once and stays correct past the cache limit', () => {
    const renderSpy = vi.spyOn(MarkdownIt.prototype, 'render');
    const source = `cache-hit-${Math.random()}\n\n**bold**`;

    const first = renderMarkdown(source);
    const second = renderMarkdown(source);

    expect(second).toBe(first);
    expect(second).toContain('<strong>bold</strong>');
    expect(renderSpy).toHaveBeenCalledTimes(1);

    // Streaming drafts never enter or evict the finished-document cache,
    // including the rendered prefix preceding an unfinished code fence.
    const saved = renderMarkdownDocument(source);
    for (let index = 0; index < 350; index += 1) {
      renderMarkdownStreaming(`Draft ${index}\n\n${'text '.repeat(index % 5)}`);
      renderMarkdownStreaming(`Prefix ${index}\n\n\`\`\`js\ncode`);
    }
    expect(renderMarkdownDocument(source)).toBe(saved);

    for (let index = 0; index < 350; index += 1) {
      renderMarkdown(`cache-filler-${index}\n\ncontent ${index}`);
    }
    expect(renderMarkdown('# After eviction')).toContain(
      '<h1>After eviction</h1>',
    );

    // A small number of large finished documents also evicts old text. An
    // oversized document remains correct without displacing useful entries.
    renderSpy.mockImplementation((text) => `<p>${text}</p>`);
    const large = 'x'.repeat(600_000);
    const firstLarge = renderMarkdownDocument(`first ${large}`);
    renderMarkdownDocument(`second ${large}`);
    renderMarkdownDocument(`third ${large}`);
    expect(renderMarkdownDocument(`first ${large}`)).not.toBe(firstLarge);
    const small = renderMarkdownDocument('keep this finished text');
    expect(renderMarkdown('y'.repeat(2_000_000))).toHaveLength(2_000_007);
    expect(renderMarkdownDocument('keep this finished text')).toBe(small);
    renderSpy.mockRestore();
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
