/**
 * 极简 Markdown → HTML（报告初稿用）。
 *
 * 不引第三方库的理由：报告正文只需要标题 / 段落 / 列表 / 引用 / 粗体 / 行内代码，
 * 而第三方渲染器要么带 XSS 风险（需再配一套 sanitizer），要么把包体翻倍。
 * 这里先全量转义再套标签，天然免疫标签注入。
 */

const ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

function escapeHtml(input: string): string {
  return input.replace(/[&<>"']/g, (char) => ESCAPE_MAP[char] ?? char);
}

function inline(text: string): string {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code class="rounded bg-slate-100 px-1 py-0.5 font-mono text-[0.85em]">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong class="font-semibold text-slate-900">$1</strong>');
}

export function renderMarkdown(markdown: string): string {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const html: string[] = [];
  let listType: 'ul' | 'ol' | null = null;
  let paragraph: string[] = [];

  const closeList = (): void => {
    if (listType) {
      html.push(`</${listType}>`);
      listType = null;
    }
  };

  const flushParagraph = (): void => {
    if (paragraph.length > 0) {
      html.push(`<p class="my-2 leading-7 text-slate-600">${inline(paragraph.join(' '))}</p>`);
      paragraph = [];
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();

    if (line.trim() === '') {
      flushParagraph();
      closeList();
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      closeList();
      const level = heading[1].length;
      const size = level <= 2 ? 'text-lg' : 'text-base';
      html.push(
        `<h${level} class="mt-6 mb-2 ${size} font-semibold tracking-tight text-slate-900">${inline(heading[2])}</h${level}>`,
      );
      continue;
    }

    const bullet = /^[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushParagraph();
      if (listType !== 'ul') {
        closeList();
        html.push('<ul class="my-2 list-disc space-y-1 pl-5 text-slate-600">');
        listType = 'ul';
      }
      html.push(`<li class="leading-7">${inline(bullet[1])}</li>`);
      continue;
    }

    const ordered = /^\d+[.)]\s+(.*)$/.exec(line);
    if (ordered) {
      flushParagraph();
      if (listType !== 'ol') {
        closeList();
        html.push('<ol class="my-2 list-decimal space-y-1 pl-5 text-slate-600">');
        listType = 'ol';
      }
      html.push(`<li class="leading-7">${inline(ordered[1])}</li>`);
      continue;
    }

    const quote = /^>\s?(.*)$/.exec(line);
    if (quote) {
      flushParagraph();
      closeList();
      html.push(
        `<blockquote class="my-3 border-l-2 border-primary/40 bg-primary-soft/40 px-3 py-2 text-sm leading-6 text-slate-600">${inline(quote[1])}</blockquote>`,
      );
      continue;
    }

    closeList();
    paragraph.push(line.trim());
  }

  flushParagraph();
  closeList();
  return html.join('');
}
