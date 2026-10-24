import { lazy, Suspense, useCallback, useEffect, useId, useRef, useState } from "react";
import {
  AlertOctagon, AlertTriangle, CheckCircle2, FileText, Info, Loader2, RefreshCw, ShieldAlert, UploadCloud, X,
} from "lucide-react";

/* ------------------------------------------------------------------ feedback */
export function Spinner({ className = "h-4 w-4" }) {
  return <Loader2 className={`${className} animate-spin`} aria-hidden="true" />;
}

export function Skeleton({ className = "" }) {
  return <div className={`animate-pulse rounded-md bg-subtle ${className}`} aria-hidden="true" />;
}

export function SkeletonRows({ rows = 4, className = "h-10" }) {
  return (
    <div className="space-y-2" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => <Skeleton key={i} className={`${className} w-full`} />)}
    </div>
  );
}

export function EmptyState({ icon: Icon = FileText, title, children, action }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center">
      <Icon className="h-6 w-6 text-muted" aria-hidden="true" />
      <p className="font-medium text-ink">{title}</p>
      {children && <div className="max-w-sm text-sm text-muted">{children}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry, title = "Something went wrong" }) {
  return (
    <div role="alert" className="flex flex-col items-center gap-2 px-4 py-10 text-center">
      <AlertTriangle className="h-6 w-6 text-critical" aria-hidden="true" />
      <p className="font-medium">{title}</p>
      <p className="max-w-md text-sm text-muted">{error?.message ?? String(error)}</p>
      {onRetry && <button className="btn-secondary btn-sm mt-1" onClick={onRetry}><RefreshCw className="h-3.5 w-3.5" /> Try again</button>}
    </div>
  );
}

const NOTICE = {
  info: { icon: Info, box: "border-info/30 bg-info-soft", title: "text-info", label: "Note" },
  success: { icon: CheckCircle2, box: "border-success/30 bg-success-soft", title: "text-success", label: "Supported" },
  warning: { icon: AlertTriangle, box: "border-warning/40 bg-warning-soft", title: "text-warning-ink", label: "Review" },
  critical: { icon: ShieldAlert, box: "border-critical/30 bg-critical-soft", title: "text-critical", label: "Safety" },
};

/** Severity-labelled notice. Meaning is carried by icon + title text, never colour alone. */
export function Notice({ tone = "info", title, children, action, className = "" }) {
  const n = NOTICE[tone];
  const Icon = tone === "critical" && !title?.toLowerCase().includes("safety") ? AlertOctagon : n.icon;
  return (
    <div className={`flex gap-2.5 rounded-lg border px-3 py-2.5 text-sm ${n.box} ${className}`} role={tone === "critical" ? "alert" : undefined}>
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${n.title}`} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        {title && <p className={`font-semibold ${n.title}`}>{title}</p>}
        {children && <div className="text-ink">{children}</div>}
        {action && <div className="mt-2">{action}</div>}
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------- badges */
const TONES = {
  neutral: "bg-subtle text-ink ring-line",
  accent: "bg-accent-soft text-accent-strong ring-accent/30",
  success: "bg-success-soft text-success ring-success/30",
  warning: "bg-warning-soft text-warning-ink ring-warning/40",
  critical: "bg-critical-soft text-critical ring-critical/30",
  info: "bg-info-soft text-info ring-info/30",
};

// Legacy tone names used by early-feature pages map onto the semantic palette.
const TONE_ALIAS = { emerald: "success", blue: "info", amber: "warning", rose: "critical", slate: "neutral", violet: "info" };

export function Badge({ tone = "neutral", children, className = "", title }) {
  tone = TONE_ALIAS[tone] ?? tone;
  return (
    <span title={title} className={`inline-flex items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-2xs font-semibold ring-1 ring-inset ${TONES[tone]} ${className}`}>
      {children}
    </span>
  );
}

const APPROVAL = {
  approved: ["success", "Approved", CheckCircle2],
  pending: ["warning", "Pending review", AlertTriangle],
  outdated: ["critical", "Outdated", AlertOctagon],
  unreviewed: ["neutral", "Unreviewed", Info],
};

export function ApprovalBadge({ status }) {
  const [tone, label, Icon] = APPROVAL[status] ?? APPROVAL.unreviewed;
  return <Badge tone={tone}><Icon className="h-3 w-3" aria-hidden="true" />{label}</Badge>;
}

const STATUS = {
  ok: ["success", "In stock"], low_stock: ["warning", "Low stock"], out_of_stock: ["critical", "Out of stock"],
  expired: ["critical", "Expired"], expiring_soon: ["warning", "Expires soon"],
  draft: ["neutral", "Draft"], in_progress: ["info", "In progress"], in_review: ["info", "In review"],
  approved: ["success", "Approved"], changes_requested: ["warning", "Changes requested"], rejected: ["critical", "Rejected"],
  pending: ["neutral", "Pending"], completed: ["success", "Completed"], archived: ["neutral", "Archived"],
  open: ["warning", "Open"], resolved: ["success", "Resolved"], closed: ["neutral", "Closed"],
};

export function StatusBadge({ status }) {
  const [tone, label] = STATUS[status] ?? ["neutral", String(status ?? "").replace(/_/g, " ")];
  return <Badge tone={tone}>{label}</Badge>;
}

/* --------------------------------------------------------------------- layout */
export function PageHeader({ title, description, subtitle, actions, children }) {
  description = description ?? subtitle;
  return (
    <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 max-w-2xl text-sm text-muted">{description}</p>}
        {children}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Page({ children, width = "max-w-6xl" }) {
  return <div className={`mx-auto w-full ${width} px-4 py-6 sm:px-6 lg:px-8`}>{children}</div>;
}

/* --------------------------------------------------------- dialogs & drawers */
function useDialogBehaviour(open, onClose, ref, focusFirstField = false) {
  // Keep the latest onClose in a ref so re-renders (inline callbacks) don't re-run the effect and steal focus.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return undefined;
    const previous = document.activeElement;
    const onKey = (e) => {
      if (e.key === "Escape") closeRef.current();
      if (e.key === "Tab" && ref.current) {
        const items = ref.current.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
        const focusable = [...items].filter((el) => !el.disabled && el.offsetParent !== null);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    const t = setTimeout(() => {
      // Dialogs focus their first field; drawers focus the panel itself so no control shows a stray focus ring.
      const target = ref.current?.querySelector("[data-autofocus]")
        ?? (focusFirstField ? ref.current?.querySelector("input, select, textarea") : null) ?? ref.current;
      target?.focus();
    }, 0);
    return () => { document.removeEventListener("keydown", onKey); clearTimeout(t); previous?.focus?.(); };
  }, [open, ref, focusFirstField]);
}

export function Dialog({ open, onClose, title, description, children, footer, size = "max-w-lg" }) {
  const ref = useRef(null);
  const id = useId();
  useDialogBehaviour(open, onClose, ref, true);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/25 p-0 sm:items-center sm:p-4" onMouseDown={onClose}>
      <div ref={ref} role="dialog" aria-modal="true" tabIndex={-1} aria-labelledby={`${id}-t`} onMouseDown={(e) => e.stopPropagation()}
        className={`flex max-h-[92vh] w-full ${size} flex-col rounded-t-2xl bg-surface shadow-pop outline-none sm:rounded-2xl`}>
        <div className="flex items-start gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0 flex-1">
            <h2 id={`${id}-t`} className="text-base font-semibold">{title}</h2>
            {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
          </div>
          <button className="btn-ghost btn-icon -mr-2 -mt-1" onClick={onClose} aria-label="Close"><X className="h-4 w-4" /></button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex flex-wrap justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>
  );
}

export function Drawer({ open, onClose, title, subtitle, children, footer, width = "sm:w-[30rem]", bare = false }) {
  const ref = useRef(null);
  const id = useId();
  useDialogBehaviour(open, onClose, ref);
  if (!open) return null;
  if (bare) {
    // Content supplies its own header and close button.
    return (
      <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onMouseDown={onClose}>
        <aside ref={ref} role="dialog" aria-modal="true" tabIndex={-1} aria-label={title} onMouseDown={(e) => e.stopPropagation()}
          className={`h-full w-full ${width} bg-surface shadow-pop outline-none`}>{children}</aside>
      </div>
    );
  }
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onMouseDown={onClose}>
      <aside ref={ref} role="dialog" aria-modal="true" tabIndex={-1} aria-labelledby={`${id}-t`} onMouseDown={(e) => e.stopPropagation()}
        className={`flex h-full w-full ${width} flex-col bg-surface shadow-pop outline-none`}>
        <div className="flex items-start gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0 flex-1">
            <h2 id={`${id}-t`} className="text-base font-semibold leading-snug">{title}</h2>
            {subtitle && <div className="mt-1">{subtitle}</div>}
          </div>
          <button className="btn-ghost btn-icon -mr-2 -mt-1" onClick={onClose} aria-label="Close"><X className="h-4 w-4" /></button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex flex-wrap justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </aside>
    </div>
  );
}

/* -------------------------------------------------------------- form helpers */
/** Label + control + hint. `children` is a render function receiving the control id: {(id) => <input id={id} />}. */
export function Field({ label, hint, children, className = "" }) {
  const id = useId();
  return (
    <div className={className}>
      <label htmlFor={id} className="label">{label}</label>
      {children(id)}
      {hint && <p className="field-hint">{hint}</p>}
    </div>
  );
}

export function SegmentedControl({ options, value, onChange, label }) {
  return (
    <div role="tablist" aria-label={label} className="inline-flex rounded-lg border border-line bg-surface p-0.5">
      {options.map((o) => (
        <button key={o.id} role="tab" aria-selected={value === o.id} onClick={() => onChange(o.id)}
          className={`h-8 rounded-md px-3 text-xs font-medium transition-colors ${value === o.id ? "bg-accent-soft text-accent-strong" : "text-muted hover:text-ink"}`}>
          {o.label}{o.count != null && <span className="ml-1.5 tabular-nums text-muted">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

/**
 * Drag-and-drop zone. Performs client-side type/size checks for fast feedback;
 * the server re-validates every file.
 */
export function DropZone({ accept, maxMB = 25, multiple = false, onFiles, label, hint, disabled, compact = false }) {
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);

  const handle = useCallback((list) => {
    const files = Array.from(list ?? []);
    if (!files.length) return;
    const exts = accept.split(",").map((s) => s.trim().toLowerCase());
    const bad = files.find((f) => !exts.some((e) => f.name.toLowerCase().endsWith(e)));
    if (bad) return setError(`"${bad.name}" is not a supported file type (${accept.replaceAll(",", ", ")}).`);
    const big = files.find((f) => f.size > maxMB * 1024 * 1024);
    if (big) return setError(`"${big.name}" is larger than ${maxMB} MB.`);
    setError(null);
    onFiles(multiple ? files : [files[0]]);
  }, [accept, maxMB, multiple, onFiles]);

  return (
    <div>
      <button type="button" disabled={disabled} onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); if (!disabled) handle(e.dataTransfer.files); }}
        className={`flex w-full flex-col items-center justify-center gap-1 rounded-xl border border-dashed text-center transition-colors disabled:opacity-50
          ${compact ? "px-3 py-4" : "px-4 py-8"} ${dragging ? "border-accent bg-accent-soft" : "border-faint/50 bg-canvas hover:border-accent"}`}>
        <UploadCloud className="h-5 w-5 text-accent-strong" aria-hidden="true" />
        <span className="text-sm font-medium">{label}</span>
        {hint && <span className="text-xs text-muted">{hint}</span>}
      </button>
      <input ref={inputRef} type="file" hidden accept={accept} multiple={multiple}
        onChange={(e) => { handle(e.target.files); e.target.value = ""; }} />
      {error && <p role="alert" className="mt-2 text-xs text-critical">{error}</p>}
    </div>
  );
}

export function ProgressBar({ value, label }) {
  return (
    <div role="progressbar" aria-label={label} aria-valuenow={Math.round(value * 100)} aria-valuemin={0} aria-valuemax={100}
      className="h-1.5 w-full overflow-hidden rounded-full bg-subtle">
      <div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${Math.round(value * 100)}%` }} />
    </div>
  );
}

/* ------------------------------------------------------------------ markdown */
// The renderer (react-markdown, remark/rehype, KaTeX + CSS) is loaded on demand so it is not in the entry chunk.
const MarkdownRenderer = lazy(() => import("./Markdown"));

/** Markdown + LaTeX. Inline markers like [2] become citation buttons when `citations` are supplied. */
export function Markdown(props) {
  return (
    <Suspense fallback={<div className="prose-lab whitespace-pre-wrap">{String(props.children)}</div>}>
      <MarkdownRenderer {...props} />
    </Suspense>
  );
}

/* --------------------------------------------------------------------- hooks */
export function useAsync(fn, deps = []) {
  const [state, setState] = useState({ data: null, loading: true, error: null });
  const reload = useCallback(async () => {
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      setState({ data: await fn(), loading: false, error: null });
    } catch (error) {
      setState((s) => ({ data: s.data, loading: false, error }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => { reload(); }, [reload]);
  return { ...state, reload, setData: (data) => setState((s) => ({ ...s, data })) };
}

export function useDebounced(value, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

/* ------------------------------------------- compatibility for early-feature pages */
export function Tabs({ tabs, value, onChange, label = "Sections" }) {
  return (
    <div role="tablist" aria-label={label} className="mb-4 flex gap-5 overflow-x-auto border-b border-line">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} className="tab-u shrink-0" onClick={() => onChange(t.id)}>{t.label}</button>
      ))}
    </div>
  );
}
export const Dropzone = DropZone;
