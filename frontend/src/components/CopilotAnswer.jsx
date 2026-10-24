import { memo, useEffect, useRef, useState } from "react";
import {
  AlertTriangle, Boxes, Calculator, CheckCircle2, ChevronDown, Cpu, FlaskConical, Layers, ListChecks, OctagonX,
  Pencil, ShieldAlert, Wrench,
} from "lucide-react";
import { useApp } from "../context/AppContext";
import { docKind } from "../lib/format";
import { FeedbackControls } from "./Answer";
import { ApprovalBadge, Badge, Markdown, Notice, Spinner } from "./ui";

export const DECISION_SUPPORT_NOTE = "Decision support only. Confirm safety-critical steps with your lab supervisor.";
const MATRIX_NOTE = "Limited demo matrix, not expert-reviewed.";

// Wording is deliberate: an answer is at most "Supported by uploaded SOP", never "verified" or "safe".
const STATUS = {
  supported: { tone: "success", label: "Supported by uploaded SOP", icon: CheckCircle2 },
  caution: { tone: "warning", label: "Caution", icon: AlertTriangle },
  refused: { tone: "critical", label: "Refused", icon: OctagonX },
};

export function CopilotStatusBadge({ status }) {
  const s = STATUS[status] ?? STATUS.refused;
  return <Badge tone={s.tone}><s.icon className="h-3 w-3" aria-hidden="true" />{s.label}</Badge>;
}

/* --------------------------------------------------------------------- trace */
/** "How I got this answer": one row per orchestrator step (tool, summary, duration). */
export function AgentTrace({ steps = [], live = false, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  useEffect(() => { if (live) setOpen(true); }, [live]);
  return (
    <section className="rounded-xl border border-line bg-surface" aria-label="How I got this answer">
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-semibold text-ink">
        <Cpu className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
        How I got this answer
        <span className="font-normal text-muted">{steps.length} step{steps.length === 1 ? "" : "s"}</span>
        {live && <Spinner className="h-3 w-3 text-muted" />}
        <ChevronDown className={`ml-auto h-4 w-4 text-faint transition-transform ${open ? "rotate-180" : ""}`} aria-hidden="true" />
      </button>
      {open && (
        <ol className="divide-y divide-line border-t border-line" aria-live={live ? "polite" : undefined}>
          {steps.map((t, i) => (
            <li key={`${t.step}-${i}`} className="grid grid-cols-[1.25rem_1fr_auto] gap-x-2 px-3 py-2 text-xs">
              <span className="pt-0.5 tabular-nums text-faint">{i + 1}</span>
              <div className="min-w-0">
                <p className="flex flex-wrap items-center gap-1.5">
                  <span className="font-medium text-ink">{t.step}</span>
                  <code className="rounded bg-subtle px-1 font-mono text-2xs text-muted">{t.tool}</code>
                  {t.ok === false && <Badge tone="warning">did not complete</Badge>}
                </p>
                {t.output_summary && <p className="mt-0.5 break-words text-muted">{t.output_summary}</p>}
              </div>
              <span className="tabular-nums text-muted">{t.duration_ms} ms</span>
            </li>
          ))}
          {live && <li className="flex items-center gap-2 px-3 py-2 text-xs text-muted"><Spinner className="h-3 w-3" /> Working...</li>}
        </ol>
      )}
    </section>
  );
}

/* ---------------------------------------------------------------- image code */
/** Ask the user to confirm (or correct) an error code read from an uploaded image before acting on it. */
export function ConfirmCodeCard({ msg, onConfirm, disabled }) {
  const code = msg.upload?.extracted_code ?? "";
  const [editing, setEditing] = useState(!code);
  const [value, setValue] = useState(code);
  const [error, setError] = useState(null);
  const valid = /^[A-Za-z]{1,4}[- ]?\d{2,4}$/.test(value.trim());
  const done = msg.confirmed != null;
  const submit = (c) => {
    if (!/^[A-Za-z]{1,4}[- ]?\d{2,4}$/.test(c.trim())) { setError("Enter a code like E-104 or IMB-01."); return; }
    setError(null);
    onConfirm(msg, c.trim().toUpperCase());
  };
  return (
    <article className="space-y-2 rounded-xl border border-line bg-surface p-3.5" aria-label="Confirm error code">
      <p className="flex items-center gap-2 text-sm font-semibold"><Wrench className="h-4 w-4 text-accent" aria-hidden="true" /> Error code from your image</p>
      {code ? (
        <p className="text-sm">I read <code className="rounded bg-subtle px-1.5 py-0.5 font-mono">{code}</code>
          {msg.upload?.equipment_type ? <> on what looks like a <span className="font-medium">{msg.upload.equipment_type}</span></> : null}.
          {" "}<span className="font-medium">Is this the code on your display?</span></p>
      ) : (
        <p className="text-sm text-muted">No error code could be read from the image. Type the code shown on the display to look up the documented fix.</p>
      )}
      {done ? (
        <p className="flex items-center gap-1.5 text-xs text-muted"><CheckCircle2 className="h-3.5 w-3.5 text-success" /> Confirmed: <code className="font-mono">{msg.confirmed}</code></p>
      ) : editing ? (
        <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); submit(value); }}>
          <label className="sr-only" htmlFor={`code-${msg.id}`}>Error code</label>
          <input id={`code-${msg.id}`} className="input h-9 w-36 font-mono" value={value} maxLength={12} autoFocus
            onChange={(e) => setValue(e.target.value)} placeholder="E-104" />
          <button className="btn-primary btn-sm" disabled={disabled || !valid}>Look up this code</button>
          {code && <button type="button" className="btn-ghost btn-sm" onClick={() => { setEditing(false); setValue(code); }}>Cancel</button>}
        </form>
      ) : (
        <div className="flex flex-wrap gap-2">
          <button className="btn-primary btn-sm" disabled={disabled} onClick={() => submit(code)}><CheckCircle2 className="h-3.5 w-3.5" /> Confirm</button>
          <button className="btn-secondary btn-sm" disabled={disabled} onClick={() => setEditing(true)}><Pencil className="h-3.5 w-3.5" /> Edit</button>
        </div>
      )}
      {error && <p role="alert" className="text-xs text-critical">{error}</p>}
    </article>
  );
}

/* ------------------------------------------------------------------- pieces */
function Section({ icon: Icon, title, aside, children }) {
  return (
    <section>
      <h3 className="mb-1.5 flex flex-wrap items-center gap-1.5 text-xs font-semibold text-ink">
        <Icon className="h-3.5 w-3.5 text-accent" aria-hidden="true" />{title}{aside}
      </h3>
      {children}
    </section>
  );
}

function CitationMarker({ n, citation, onClick }) {
  return (
    <button type="button" onClick={onClick}
      aria-label={citation ? `Source ${n}: ${citation.document}, page ${citation.page}` : `Source ${n}`}
      title={citation ? `${citation.document}, p. ${citation.page}` : undefined}
      className="mx-0.5 inline-flex h-[1.15rem] min-w-[1.15rem] items-center justify-center rounded bg-accent-soft px-1 align-[0.1em] font-mono text-2xs font-semibold text-accent-strong ring-1 ring-inset ring-accent/30 hover:bg-accent/20">
      {n}
    </button>
  );
}

function Checklist({ msg, citations, onMarker }) {
  const byN = Object.fromEntries(citations.map((c) => [c.n, c]));
  const refused = msg.status === "refused";
  return (
    <Section icon={ListChecks} title="Pre-Flight Checklist">
      {msg.checklist_label && (
        <p className="mb-2 flex items-center gap-2 rounded-lg border border-critical/30 bg-critical-soft px-3 py-2 text-sm font-semibold text-critical" role="alert">
          <OctagonX className="h-4 w-4 shrink-0" aria-hidden="true" />{msg.checklist_label}
        </p>
      )}
      <ol className={`space-y-2 ${refused ? "opacity-80" : ""}`}>
        {msg.checklist.map((s) => (
          <li key={s.step} className={`rounded-lg border px-3 py-2.5 ${s.unsupported ? "border-critical/40 bg-critical-soft/40" : "border-line bg-surface"}`}>
            <div className="flex gap-2.5">
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-subtle text-xs font-semibold tabular-nums">{s.step}</span>
              <div className="min-w-0 flex-1 space-y-1.5">
                <p className="text-[0.9375rem] leading-6">
                  {s.action}
                  {(s.citation_numbers ?? []).map((n) => <CitationMarker key={n} n={n} citation={byN[n]} onClick={() => onMarker(n)} />)}
                </p>
                {s.unsupported && (
                  <div className="text-xs text-critical">
                    <Badge tone="critical">Unsupported</Badge>
                    <ul className="mt-1 list-disc space-y-0.5 pl-4">{(s.unsupported_reasons ?? []).map((r) => <li key={r}>{r}</li>)}</ul>
                  </div>
                )}
                {s.safety_note && (
                  <p className="flex gap-1.5 text-xs text-muted">
                    <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning-ink" aria-hidden="true" />
                    <span><span className="font-medium text-ink">Safety:</span> {s.safety_note}</span>
                  </p>
                )}
              </div>
            </div>
          </li>
        ))}
      </ol>
    </Section>
  );
}

function yesNo(v) {
  if (v === true) return <Badge tone="success">Yes</Badge>;
  if (v === false) return <Badge tone="critical">No</Badge>;
  return <Badge>Not checked</Badge>;
}

function InventoryTable({ rows, demo }) {
  return (
    <Section icon={Boxes} title="Inventory" aside={demo && <Badge>Demo data</Badge>}>
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full min-w-[34rem] text-left text-xs">
          <thead className="bg-subtle text-muted">
            <tr>{["Reagent", "Required", "Available", "Sufficient", "Location", "Expiry"].map((h) => <th key={h} scope="col" className="px-2.5 py-1.5 font-medium">{h}</th>)}</tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map((r, i) => (
              <tr key={`${r.reagent}-${i}`}>
                <td className="px-2.5 py-1.5 font-medium text-ink">
                  {r.reagent}
                  {r.candidates?.length > 0 && <p className="font-normal text-muted">Did you mean: {r.candidates.join(", ")}?</p>}
                </td>
                <td className="px-2.5 py-1.5 tabular-nums">{r.required ?? "-"}</td>
                <td className="px-2.5 py-1.5">{r.found ? yesNo(r.available) : <Badge tone="critical">Not in stock</Badge>}</td>
                <td className="px-2.5 py-1.5">{r.required ? yesNo(r.sufficient) : "-"}</td>
                <td className="px-2.5 py-1.5">{r.location ?? "-"}</td>
                <td className="px-2.5 py-1.5">
                  {r.expires ?? "-"}
                  {r.expired && <Badge tone="critical" className="ml-1">Expired</Badge>}
                  {!r.expired && r.expiring_soon && <Badge tone="warning" className="ml-1">Expires soon</Badge>}
                  {r.low_stock && <Badge tone="warning" className="ml-1">Low stock</Badge>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.some((r) => r.note) && <p className="mt-1 text-2xs text-muted">{rows.filter((r) => r.note).map((r) => r.note).join(" ")}</p>}
    </Section>
  );
}

const PAIR = {
  incompatible: ["critical", "Incompatible"],
  unknown: ["warning", "Unknown (not safe to assume)"],
  no_known_issue: ["neutral", "No known issue in matrix"],
};

function Compatibility({ pairs }) {
  return (
    <Section icon={FlaskConical} title="Compatibility check">
      <p className="mb-1.5 text-xs font-medium text-warning-ink">{MATRIX_NOTE}</p>
      <ul className="divide-y divide-line rounded-lg border border-line text-sm">
        {pairs.map((p) => {
          const [tone, label] = PAIR[p.result] ?? PAIR.unknown;
          return (
            <li key={p.pair.join("+")} className="flex flex-wrap items-start gap-2 px-3 py-2">
              <span className="font-medium">{p.pair.join(" + ")}</span>
              <Badge tone={tone}>{label}</Badge>
              {p.note && <span className="w-full text-xs text-muted">{p.note}</span>}
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

function Calculations({ items }) {
  return (
    <Section icon={Calculator} title="Calculation (computed in code)">
      <div className="space-y-2">
        {items.map((c) => (
          <div key={c.compound} className="rounded-lg border border-line px-3 py-2 text-sm">
            <p className="font-medium">{c.compound}: <span className="tabular-nums">{c.mass}</span>
              {c.molecular_weight && <span className="font-normal text-muted"> (MW {c.molecular_weight} g/mol)</span>}</p>
            <p className="text-xs text-muted">{c.formula}</p>
            <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-xs">{c.steps.map((s) => <li key={s}>{s}</li>)}</ol>
          </div>
        ))}
      </div>
    </Section>
  );
}

function Citations({ citations, highlighted, onOpen }) {
  const refs = useRef({});
  useEffect(() => {
    if (highlighted != null) refs.current[highlighted]?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [highlighted]);
  return (
    <Section icon={Layers} title="Citations">
      <ol className="space-y-1.5">
        {citations.map((c) => (
          <li key={c.chunk_id} ref={(el) => { refs.current[c.n] = el; }}>
            <button type="button" onClick={() => onOpen(c)}
              className={`flex w-full gap-2.5 rounded-lg border px-3 py-2 text-left transition-colors hover:bg-subtle ${
                highlighted === c.n ? "border-accent bg-accent-soft/60 ring-2 ring-accent/30" : "border-line bg-surface"}`}>
              <span className="flex h-5 min-w-5 items-center justify-center rounded-md bg-accent-soft px-1 font-mono text-2xs font-semibold text-accent-strong">{c.n}</span>
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-1.5 text-[0.8125rem] font-medium text-ink">
                  {c.document} <span className="font-normal text-muted">· page {c.page}</span>
                  <span className="type-chip h-5 px-1.5 text-2xs">{docKind(c)}</span>
                  {c.approval_status && c.approval_status !== "approved" && <ApprovalBadge status={c.approval_status} />}
                </span>
                <span className="mt-0.5 line-clamp-2 block text-xs text-muted">{c.snippet}</span>
              </span>
            </button>
          </li>
        ))}
      </ol>
    </Section>
  );
}

/* --------------------------------------------------------------------- answer */
function CopilotAnswer({ msg, demo = false }) {
  const { openEvidence } = useApp();
  const [highlighted, setHighlighted] = useState(null);
  const citations = msg.citations ?? [];
  const open = (c) => { setHighlighted(c.n); openEvidence(c, citations); };
  const onMarker = (n) => { const c = citations.find((x) => x.n === n); if (c) open(c); };
  const refused = msg.status === "refused";
  const cautions = msg.cautions ?? [];

  return (
    <article className="space-y-4" aria-label="SmartLab co-pilot answer">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-ink">SmartLab co-pilot</span>
        <CopilotStatusBadge status={msg.status} />
        {typeof msg.confidence === "number" && (
          <span className="meta" title="Computed from retrieval similarity, supported steps and tool outcomes; not a model self-rating">
            Confidence {msg.confidence.toFixed(2)}
          </span>
        )}
      </div>

      {msg.uncertainty_flag && (
        <Notice tone="critical" title="Uncertain">{msg.uncertainty_message}</Notice>
      )}
      {refused && msg.blockers?.length > 0 && (
        <Notice tone="critical" title="Refused: blocked by">
          <ul className="list-disc space-y-0.5 pl-4">{msg.blockers.map((b) => <li key={b}>{b}</li>)}</ul>
        </Notice>
      )}

      {msg.summary && <Markdown>{msg.summary}</Markdown>}

      {msg.calculations?.length > 0 && <Calculations items={msg.calculations} />}
      {msg.checklist?.length > 0 && <Checklist msg={msg} citations={citations} onMarker={onMarker} />}

      {msg.diagnosis && (
        <Section icon={Wrench} title={`Error ${msg.diagnosis.error_code}`}>
          <p className="text-sm">{msg.diagnosis.machine_model}. Probable cause (from troubleshooting records): {msg.diagnosis.probable_cause}</p>
          {msg.diagnosis.ppe_warnings?.length > 0 && (
            <Notice tone="warning" title="PPE" className="mt-2">{msg.diagnosis.ppe_warnings.join(" ")}</Notice>
          )}
        </Section>
      )}

      {msg.inventory_status?.length > 0 && <InventoryTable rows={msg.inventory_status} demo={demo} />}
      {msg.compatibility?.length > 0 && <Compatibility pairs={msg.compatibility} />}

      {cautions.length > 0 && (
        <Notice tone="warning" title={refused ? "Also noted" : "Caution"}>
          <ul className="list-disc space-y-0.5 pl-4">{cautions.map((c) => <li key={c}>{c}</li>)}</ul>
        </Notice>
      )}

      {citations.length > 0 && <Citations citations={citations} highlighted={highlighted} onOpen={open} />}
      {msg.agent_trace?.length > 0 && <AgentTrace steps={msg.agent_trace} />}
      <p className="text-2xs text-muted">{DECISION_SUPPORT_NOTE}</p>
      <FeedbackControls messageId={msg.message_id} text={msg.response ?? msg.answer} initial={msg.feedback} />
    </article>
  );
}

export default memo(CopilotAnswer);
