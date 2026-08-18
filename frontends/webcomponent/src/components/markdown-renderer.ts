/**
 * Small, dependency-free Markdown renderer for trusted assistant text.
 *
 * The backend may return Markdown in both streamed components and persisted
 * conversation history.  We escape the complete source before applying the
 * limited formatting rules so a model response cannot inject HTML into the
 * host page.  This intentionally covers the report syntax we expose (titles,
 * paragraphs, emphasis, lists, blockquotes, separators, code and tables)
 * instead of pretending to be a complete CommonMark implementation.
 */

export function escapeHtml(value: unknown): string {
  const div = document.createElement('div');
  div.textContent = String(value ?? '');
  return div.innerHTML;
}

export function renderMarkdown(source: string): string {
  const lines = escapeHtml(source).split('\n');
  const blocks: string[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index].trim();
    if (!line) {
      index += 1;
      continue;
    }

    // LLM responses often put a blank line between the header and separator
    // (and between every data row). Markdown tables remain unambiguous when
    // the next non-empty line is the separator, so tolerate that formatting
    // without falling back to displaying the pipe syntax literally.
    let separatorIndex = index + 1;
    while (separatorIndex < lines.length && !lines[separatorIndex].trim()) {
      separatorIndex += 1;
    }
    if (
      separatorIndex < lines.length &&
      isTableRow(line) &&
      isTableSeparator(lines[separatorIndex])
    ) {
      const headers = splitTableRow(line);
      const rows: string[][] = [];
      index = separatorIndex + 1;
      // Blank lines between generated table rows are common in LLM output;
      // ignore them while the following non-empty lines remain table rows.
      while (index < lines.length) {
        const candidate = lines[index].trim();
        if (!candidate) {
          index += 1;
          continue;
        }
        if (!isTableRow(candidate)) break;
        rows.push(splitTableRow(candidate));
        index += 1;
      }
      blocks.push(renderTable(headers, rows));
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      blocks.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^(?:---+|\*\s*\*\s*\*|___+)$/.test(line)) {
      blocks.push('<hr>');
      index += 1;
      continue;
    }

    if (line.startsWith('&gt; ')) {
      const quote: string[] = [];
      while (index < lines.length && lines[index].trim().startsWith('&gt; ')) {
        quote.push(renderInlineMarkdown(lines[index].trim().slice(5)));
        index += 1;
      }
      blocks.push(`<blockquote>${quote.join('<br>')}</blockquote>`);
      continue;
    }

    if (line.startsWith('- ') || line.startsWith('* ')) {
      const items: string[] = [];
      while (index < lines.length) {
        const candidate = lines[index].trim();
        if (!candidate) {
          index += 1;
          continue;
        }
        if (!candidate.startsWith('- ') && !candidate.startsWith('* ')) break;
        items.push(`<li>${renderInlineMarkdown(candidate.slice(2))}</li>`);
        index += 1;
      }
      blocks.push(`<ul>${items.join('')}</ul>`);
      continue;
    }

    if (/^\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length) {
        const candidate = lines[index].trim();
        if (!candidate) {
          index += 1;
          continue;
        }
        const item = candidate.match(/^\d+\.\s+(.+)$/);
        if (!item) break;
        items.push(`<li>${renderInlineMarkdown(item[1])}</li>`);
        index += 1;
      }
      blocks.push(`<ol>${items.join('')}</ol>`);
      continue;
    }

    if (line.startsWith('```')) {
      const language = line.slice(3).trim();
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith('```')) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const className = language ? ` class="language-${escapeHtml(language)}"` : '';
      blocks.push(`<pre class="text-markdown-code"><code${className}>${code.join('\n')}</code></pre>`);
      continue;
    }

    const paragraph: string[] = [];
    while (index < lines.length) {
      const candidate = lines[index].trim();
      if (
        !candidate ||
        isTableRow(candidate) ||
        /^(#{1,6})\s+/.test(candidate) ||
        /^(?:---+|\*\s*\*\s*\*|___+)$/.test(candidate) ||
        candidate.startsWith('&gt; ') ||
        candidate.startsWith('- ') ||
        candidate.startsWith('* ') ||
        /^\d+\.\s+/.test(candidate) ||
        candidate.startsWith('```')
      ) {
        break;
      }
      paragraph.push(renderInlineMarkdown(candidate));
      index += 1;
    }
    if (paragraph.length) blocks.push(`<p>${paragraph.join('<br>')}</p>`);
    else index += 1;
  }

  return blocks.join('');
}

function isTableRow(line: string): boolean {
  return line.trim().startsWith('|') && line.trim().endsWith('|');
}

function isTableSeparator(line: string): boolean {
  return isTableRow(line) &&
    splitTableRow(line).every((cell) => /^:?-{3,}:?$/.test(cell));
}

function splitTableRow(line: string): string[] {
  return line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
}

function renderTable(headers: string[], rows: string[][]): string {
  const headerHtml = headers
    .map((header) => `<th>${renderInlineMarkdown(header)}</th>`)
    .join('');
  const bodyHtml = rows.map((row) => {
    const cells = headers
      .map((_, column) => `<td>${renderInlineMarkdown(row[column] || '')}</td>`)
      .join('');
    return `<tr>${cells}</tr>`;
  }).join('');
  return `<div class="text-markdown-table-wrap"><table class="text-markdown-table"><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`;
}

function renderInlineMarkdown(text: string): string {
  return text
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/__(.*?)__/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g, '<em>$1</em>')
    .replace(/_(.*?)_/g, '<em>$1</em>');
}
