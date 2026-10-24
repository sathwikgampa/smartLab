import { memo, useState } from "react";
import {
  AlignLeft, Check, CheckCircle2, Copy, Eye, FileText, Flag, ImageIcon, Layers, RefreshCw, Search, ThumbsUp, UploadCloud, Wrench,
} from "lucide-react";
import { useApp } from "../context/AppContext";
import { api } from "../lib/api";
import { answerBody, copyText, docKind } from "../lib/format";
import { ApprovalBadge, Badge, Markdown, Notice, Spinner } from "./ui";
import { ReadAloudButton, markdownReadable, useReadable } from "./VoiceCoPilot";

/* ------------------------------------------------------------ evidence state */
const EVIDENCE = {
  grounded: { tone: "success", label: "Supported by documents" },
  partial: { tone: "warning", label: "Partially supported" },
  insufficient: { tone: "neutral", label: "No supporting evidence found" },
};

export function EvidenceBadge({ status }) {
  const e = EVIDENCE[status] ?? EVIDENCE.insufficient;
  return <Badge tone={e.tone}>{e.label}</Badge>;
}

/** Plain-language notices derived from the structured answer fields. */
function EvidenceNotices({ msg, question }) {
  const notices = [];
  const flags = msg.guardrails?.flags ?? [];
  if (flags.length) {
    notices.push(
      <Notice key="guard" tone="critical" title="Safety limit">
        {msg.guardrails.warnings.join(" ")}
      </Notice>,
    );
  }
  if (msg.evidence_status === "insufficient") {
    notices.push(
      <Notice key="none" tone="info" title="No supporting document found"
        action={
          <div className="flex flex-wrap gap-2">
            <a className="btn-secondary btn-sm" href="#/documents?upload=1"><UploadCloud className="h-3.5 w-3.5" /> Upload a document</a>
            <a className="btn-secondary btn-sm" href={`#/search?q=${encodeURIComponent(question ?? "")}`}><Search className="h-3.5 w-3.5" /> Search all records</a>
          </div>
        }>
        SmartLab found no passage relevant enough to answer this, so it has not guessed. Try terms used in the
        procedure itself, or add the relevant SOP or manual.
      </Notice>,
    );
  }
  for (const issue of msg.source_issues ?? []) {
    const superseded = issue.type === "superseded_version_excluded";
    notices.push(
      <Notice key={`${issue.type}-${issue.document_id}`} tone={superseded ? "info" : "warning"}
        title={superseded ? "Conflicting versions found" : "Cited source is not approved"}>
        {issue.message}
      </Notice>,
    );
  }
  if (msg.evidence_status === "partial" && msg.missing_terms?.length > 0 && !(msg.source_issues ?? []).some((i) => i.type !== "superseded_version_excluded")) {
    notices.push(
      <Notice key="partial" tone="warning" title="Only partly covered">
        The cited documents do not mention: {msg.missing_terms.join(", ")}. Treat anything beyond the cited passages as unverified.
      </Notice>,
    );
  }
  if (msg.requires_verification && msg.evidence_status !== "insufficient") {
    notices.push(
      <Notice key="verify" tone="warning" title="Verify before acting">
        This involves safety guidance or a source that is not approved. Confirm the steps against the cited procedure and
        with a qualified person before acting. In an emergency, follow your institution's emergency procedures.
      </Notice>,
    );
  }
  if (msg.service_notice) notices.push(<Notice key="svc" tone="info" title="AI service unavailable">{msg.service_notice}</Notice>);
  return notices.length ? <div className="space-y-2">{notices}</div> : null;
}

/* ------------------------------------------------------------------ sources */
/** Compact numbered source cards. Clicking one opens the page with the excerpt highlighted. */
function SourceCards({ citations, onOpen }) {
  const { citationsOn } = useApp();
  const [show, setShow] = useState(false);
  const visible = citationsOn || show;
  return (
    <section aria-label="Sources">
      <div className="mb-2 flex items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-xs font-semibold text-ink"><Layers className="h-3.5 w-3.5 text-accent" aria-hidden="true" /> Sources</h3>
        <span className="text-xs text-muted">{citations.length}</span>
        {!citationsOn && (
          <button type="button" className="link text-xs" aria-expanded={show} onClick={() => setShow((v) => !v)}>{show ? "Hide" : "Show"}</button>
        )}
      </div>
      {visible && (
        <ol className="flex snap-x gap-2 overflow-x-auto pb-1 [scrollbar-width:thin]">
          {citations.map((c) => (
            <li key={c.n} className="w-52 shrink-0 snap-start">
              <button type="button" onClick={() => onOpen(c)} title={c.snippet}
                aria-label={`Source ${c.n}: ${c.document_title}, page ${c.page}`}
                className="flex h-full w-full flex-col gap-1.5 rounded-xl border border-line bg-surface p-2.5 text-left shadow-panel transition-colors hover:border-accent/40 hover:bg-accent-soft/30">
                <span className="flex items-center gap-1.5">
                  <span className="flex h-5 min-w-5 items-center justify-center rounded-md bg-accent-soft px-1 font-mono text-2xs font-semibold text-accent-strong">{c.n}</span>
                  <span className="type-chip h-5 px-1.5 text-2xs">{docKind(c)}</span>
                  {c.approval_status && c.approval_status !== "approved" && <ApprovalBadge status={c.approval_status} />}
                </span>
                <span className="line-clamp-2 text-[0.8125rem] font-medium leading-snug text-ink">{c.document_title}</span>
                <span className="text-xs text-muted">Page {c.page}{c.version ? ` · v${c.version}` : ""}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/* ----------------------------------------------------------------- feedback */
export function FeedbackControls({ messageId, text, initial, readableId }) {
  const { toast } = useApp();
  const [state, setState] = useState(initial?.rating ?? null);
  const [reporting, setReporting] = useState(false);
  const [rating, setRating] = useState("incorrect");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const send = async (r, c) => {
    setBusy(true);
    try {
      await api.feedback(messageId, r, c);
      setState(r);
      setReporting(false);
      toast(r === "helpful" ? "Thanks for the feedback." : "Reported. The answer is now in the expert review queue.", "success");
    } catch (err) { toast(err.message, "error"); } finally { setBusy(false); }
  };

  return (
    <div>
      <div className="flex flex-wrap items-center gap-1">
        {readableId && <ReadAloudButton id={readableId} />}
        <button className="btn-ghost btn-sm" onClick={async () => { setCopied(await copyText(text)); setTimeout(() => setCopied(false), 1500); }}>
          {copied ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />} {copied ? "Copied" : "Copy"}
        </button>
        {messageId && (state && state !== "helpful" ? (
          <span className="flex items-center gap-1 px-2 text-xs text-warning-ink"><Flag className="h-3.5 w-3.5" /> Reported for review</span>
        ) : (
          <>
            <button className="btn-ghost btn-sm" disabled={busy || state === "helpful"} onClick={() => send("helpful")} aria-pressed={state === "helpful"}>
              {state === "helpful" ? <CheckCircle2 className="h-3.5 w-3.5 text-success" /> : <ThumbsUp className="h-3.5 w-3.5" />} Helpful
            </button>
            <button className="btn-ghost btn-sm" onClick={() => setReporting((r) => !r)} aria-expanded={reporting}>
              <Flag className="h-3.5 w-3.5" /> Report a problem
            </button>
          </>
        ))}
      </div>
      {reporting && (
        <form className="mt-2 space-y-2 rounded-lg border border-line bg-canvas p-3" onSubmit={(e) => { e.preventDefault(); send(rating, comment); }}>
          <fieldset className="flex flex-wrap gap-3 text-sm">
            <legend className="label">What is wrong?</legend>
            {[["incorrect", "Incorrect"], ["unsafe", "Potentially unsafe"], ["outdated", "Outdated"]].map(([v, l]) => (
              <label key={v} className="flex items-center gap-1.5"><input type="radio" name={`r-${messageId}`} value={v} checked={rating === v}
                onChange={() => setRating(v)} className="accent-accent-strong" />{l}</label>
            ))}
          </fieldset>
          <label className="label" htmlFor={`c-${messageId}`}>Details for the reviewer</label>
          <textarea id={`c-${messageId}`} className="input" rows={2} required value={comment} onChange={(e) => setComment(e.target.value)}
            placeholder="e.g. Our system's pressure limit is 600 bar, see SOP rev. 4" />
          <div className="flex justify-end gap-2">
            <button type="button" className="btn-ghost btn-sm" onClick={() => setReporting(false)}>Cancel</button>
            <button className="btn-primary btn-sm" disabled={busy || !comment.trim()}>{busy && <Spinner className="h-3.5 w-3.5" />} Send to review</button>
          </div>
        </form>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- messages */
export function UserMessage({ text, attachment }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] space-y-2">
        {attachment && (
          <div className="ml-auto flex w-fit items-center gap-2 rounded-lg border border-line bg-surface p-1.5 pr-3 text-xs">
            {attachment.previewUrl ? <img src={attachment.previewUrl} alt={`Attached image ${attachment.name}`} className="h-14 w-14 rounded object-cover" />
              : attachment.kind === "image" ? <ImageIcon className="h-4 w-4 text-muted" /> : <FileText className="h-4 w-4 text-muted" />}
            <span className="max-w-[12rem] truncate">{attachment.name}</span>
          </div>
        )}
        {text && <p className="whitespace-pre-wrap rounded-2xl rounded-br-md bg-bubble px-4 py-2.5 text-[0.9375rem] leading-6 text-ink">{text}</p>}
      </div>
    </div>
  );
}

export const AnswerMessage = memo(function AnswerMessage({ msg, question }) {
  const { openEvidence } = useApp();
  const citations = msg.citations ?? [];
  const onCite = (c) => openEvidence(c, citations);
  const readableId = `answer-${msg.id}`;
  useReadable(msg.evidence_status !== "insufficient" ? markdownReadable(readableId, "Answer", answerBody(msg.answer)) : null);
  return (
    <article className="space-y-3" aria-label="SmartLab answer">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-ink">SmartLab</span>
        <EvidenceBadge status={msg.evidence_status} />
        {msg.mode === "extractive" && <span className="meta">Quoted from documents</span>}
      </div>
      <EvidenceNotices msg={msg} question={question} />
      {citations.length > 0 && <SourceCards citations={citations} onOpen={onCite} />}
      {msg.evidence_status !== "insufficient" && (
        <section aria-label="Answer">
          <h3 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-ink"><AlignLeft className="h-3.5 w-3.5 text-accent" aria-hidden="true" /> Answer</h3>
          <Markdown citations={citations} onCite={onCite}>{answerBody(msg.answer)}</Markdown>
        </section>
      )}
      <FeedbackControls messageId={msg.message_id} text={msg.answer} initial={msg.feedback}
        readableId={msg.evidence_status !== "insufficient" ? readableId : null} />
    </article>
  );
});

export function ImageAnswer({ msg }) {
  const { openEvidence } = useApp();
  const r = msg.vision;
  const a = r.analysis;
  const matches = r.sop_matches ?? [];
  const readableId = `image-${msg.id}`;
  useReadable({
    id: readableId, title: "Image analysis",
    parts: [
      { key: "visible", label: "What is visible", text: a.visual_description },
      ...(r.troubleshooting ?? []).map((t) => ({ key: `fix-${t.id}`, label: `Documented fix${t.error_code ? ` for ${t.error_code}` : ""}`, text: t.fix })),
    ].filter((p) => p.text),
  });
  return (
    <article className="space-y-4" aria-label="Image analysis">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-ink">SmartLab</span>
        <Badge tone={r.mode === "vision-llm" ? "info" : "neutral"}>{r.mode === "vision-llm" ? "Vision model" : "Basic image analysis"}</Badge>
        <EvidenceBadge status={matches.length ? "partial" : "insufficient"} />
      </div>
      {r.guardrails.flags.includes("substance_identification") && (
        <Notice tone="critical" title="Identification not possible from an image">
          SmartLab does not identify substances, organisms or contaminants from photos. Treat unknown materials as hazardous
          and use a validated analytical method.
        </Notice>
      )}
      {r.guardrails.redactions > 0 && (
        <Notice tone="warning" title="Part of the description was withheld">
          {r.guardrails.redactions} statement(s) attempted to identify a chemical, organism or medical condition and were removed.
        </Notice>
      )}
      {r.mode !== "vision-llm" && (
        <Notice tone="info" title="No vision model configured">
          Only colours, brightness and indicator-like regions can be measured. Text on the display was not read, so include any
          error code in your question.
        </Notice>
      )}

      <section>
        <h3 className="mb-1.5 flex items-center gap-1.5 text-sm font-semibold"><Eye className="h-4 w-4 text-muted" /> What is visible</h3>
        <p className="max-w-prose text-[0.9375rem] leading-7">{a.visual_description}</p>
        {a.observed_text?.length > 0 && (
          <p className="mt-2 text-sm"><span className="text-muted">Text read from the image: </span>
            {a.observed_text.map((t) => <code key={t} className="mr-1 rounded bg-subtle px-1.5 py-0.5 font-mono text-xs">{t}</code>)}</p>
        )}
        {a.sample_observations?.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">{a.sample_observations.map((o) => <li key={o}>{o}</li>)}</ul>}
      </section>

      <section>
        <h3 className="mb-1.5 text-sm font-semibold">Related documented guidance</h3>
        <p className="mb-2 text-xs text-muted">Matched from your documents using the observations and your question. These are leads to check, not a diagnosis of the cause.</p>
        {matches.length === 0 && (r.troubleshooting ?? []).length === 0 && (
          <p className="text-sm text-muted">No SOP passage or verified fix matched. Add the error code or symptom to your question, or upload the instrument manual.</p>
        )}
        {matches.length > 0 && (
          <ol className="divide-y divide-line rounded-lg border border-line">
            {matches.map((m, i) => (
              <li key={m.chunk_id} className="px-3 py-2 text-sm">
                <button className="text-left font-medium hover:text-accent-strong hover:underline"
                  onClick={() => openEvidence({ ...m, n: i + 1 }, matches.map((x, j) => ({ ...x, n: j + 1 })))}>
                  {m.document_title} <span className="font-normal text-muted">· page {m.page}</span>
                </button>
                {m.approval_status && m.approval_status !== "approved" && <span className="ml-2"><ApprovalBadge status={m.approval_status} /></span>}
                <p className="mt-0.5 line-clamp-2 text-xs text-muted">{m.snippet}</p>
              </li>
            ))}
          </ol>
        )}
        {(r.troubleshooting ?? []).length > 0 && (
          <div className="mt-3 space-y-2">
            {r.troubleshooting.map((t) => (
              <div key={t.id} className="rounded-lg border border-line p-3 text-sm">
                <p className="flex flex-wrap items-center gap-1.5 font-medium">
                  <Wrench className="h-3.5 w-3.5 text-muted" />
                  {t.error_code && <code className="font-mono text-xs">{t.error_code}</code>} {t.equipment}
                  {t.verified && <Badge tone="success">Verified fix</Badge>}
                </p>
                <p className="mt-1 text-xs text-muted">Possible cause (from records): {t.probable_cause}</p>
                <p className="mt-1">{t.fix}</p>
                {t.source_document && <p className="mt-1 text-xs text-muted">Source: {t.source_document}, p. {t.source_page}</p>}
              </div>
            ))}
          </div>
        )}
      </section>
      <p className="text-xs text-muted">{r.disclaimer}</p>
      <FeedbackControls messageId={msg.message_id} text={a.visual_description} readableId={readableId} />
    </article>
  );
}

export function ErrorMessage({ msg, onRetry }) {
  return (
    <Notice tone="critical" title="The request did not complete"
      action={onRetry && <button className="btn-secondary btn-sm" onClick={onRetry}><RefreshCw className="h-3.5 w-3.5" /> Retry</button>}>
      {msg.error}
    </Notice>
  );
}

export function SystemNote({ children }) {
  return <p className="flex items-center gap-2 text-xs text-muted"><CheckCircle2 className="h-3.5 w-3.5 text-success" />{children}</p>;
}
