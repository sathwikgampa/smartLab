// Markdown + LaTeX renderer. This is the only module that imports react-markdown, remark/rehype and
// KaTeX (with its CSS), so that stack stays out of the entry chunk; ui.jsx loads it on demand.
import { memo, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

const CITE_RE = /\[(\d+)\](?!\()/g;
// Module-level so ReactMarkdown sees the same plugin arrays on every render.
const REMARK_PLUGINS = [remarkGfm, remarkMath];
const REHYPE_PLUGINS = [rehypeKatex];
const NO_CITATIONS = [];

function Markdown({ children, citations = NO_CITATIONS, onCite }) {
  const text = useMemo(
    () => (citations.length ? String(children).replace(CITE_RE, (m, n) => `[${n}](#cite-${n})`) : String(children)),
    [children, citations],
  );
  const components = useMemo(() => ({
    a({ href, children: label }) {
      if (href?.startsWith("#cite-")) {
        const c = citations.find((x) => x.n === Number(href.slice(6)));
        return (
          <button type="button" onClick={() => c && onCite?.(c)}
            aria-label={c ? `Source ${c.n}: ${c.document_title}, page ${c.page}` : "Citation"}
            title={c ? `${c.document_title}, p. ${c.page}` : undefined}
            className="mx-0.5 inline-flex h-[1.15rem] min-w-[1.15rem] items-center justify-center rounded bg-accent-soft px-1 align-[0.1em] font-mono text-2xs font-semibold text-accent-strong ring-1 ring-inset ring-accent/30 hover:bg-accent/20">
            {label}
          </button>
        );
      }
      return <a href={href} target="_blank" rel="noreferrer noopener">{label}</a>;
    },
  }), [citations, onCite]);

  return (
    <div className="prose-lab">
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

export default memo(Markdown);
