import { useEffect, useRef, useState } from "react";
import { BookOpen, Boxes, Camera, FileSearch, FileText, Plus, SendHorizontal, UploadCloud, WifiOff, X } from "lucide-react";
import { useApp } from "../context/AppContext";
import { formatBytes } from "../lib/format";
import { appendDictation } from "../lib/speech";
import { MenuButton } from "./Popover";
import { ProgressBar, Spinner } from "./ui";
import { MicButton, VoiceHint, useDictationTarget, useVoice } from "./VoiceCoPilot";

export const ATTACH_ACCEPT = ".pdf,.png,.jpg,.jpeg,.webp";
const IMAGE_RE = /\.(png|jpe?g|webp)$/i;

/** Classify and validate a file before it is attached. Returns {kind} or {error}. */
export function checkAttachment(file, maxMB = 25) {
  const name = file.name.toLowerCase();
  const kind = name.endsWith(".pdf") ? "pdf" : IMAGE_RE.test(name) ? "image" : null;
  if (!kind) return { error: `"${file.name}" is not supported. Attach a PDF or a PNG, JPEG or WebP image.` };
  if (file.size > maxMB * 1024 * 1024) return { error: `"${file.name}" is larger than the ${maxMB} MB upload limit.` };
  if (file.size === 0) return { error: `"${file.name}" is empty.` };
  return { kind };
}

export function makeAttachment(file) {
  const { kind } = checkAttachment(file);
  return { file, kind, name: file.name, size: file.size, previewUrl: kind === "image" ? URL.createObjectURL(file) : null };
}

function AttachmentChip({ attachment, status, onRemove }) {
  return (
    <div className="flex items-center gap-2.5 rounded-xl border border-line bg-canvas p-2 pr-2.5">
      {attachment.previewUrl
        ? <img src={attachment.previewUrl} alt="" className="h-10 w-10 rounded object-cover" />
        : <div className="flex h-10 w-10 items-center justify-center rounded bg-subtle"><FileText className="h-5 w-5 text-muted" aria-hidden="true" /></div>}
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">{attachment.name}</p>
        {status?.phase === "uploading" ? (
          <div className="mt-1 flex items-center gap-2"><ProgressBar value={status.progress} label="Upload progress" /><span className="meta tabular-nums">{Math.round(status.progress * 100)}%</span></div>
        ) : status?.phase === "processing" ? (
          <p className="meta flex items-center gap-1.5"><Spinner className="h-3 w-3" /> {attachment.kind === "pdf" ? "Extracting pages and indexing..." : "Analysing image..."}</p>
        ) : (
          <p className="meta">{attachment.kind === "pdf" ? "PDF · will be added to Documents as Unreviewed" : "Image"} · {formatBytes(attachment.size)}</p>
        )}
      </div>
      {onRemove && !status && (
        <button type="button" className="btn-ghost btn-icon h-8 w-8" onClick={onRemove} aria-label={`Remove ${attachment.name}`}><X className="h-4 w-4" /></button>
      )}
    </div>
  );
}

export const PLUS_ACTIONS = [
  { id: "search", label: "Search SOPs", description: "Find passages in your documents", icon: FileSearch },
  { id: "image", label: "Analyze image", description: "Ask about an equipment diagram", icon: Camera },
  { id: "reagent", label: "Find reagent", description: "Stock, location, expiry, storage", icon: Boxes },
  { id: "literature", label: "Explore literature", description: "Search published research", icon: BookOpen },
  { id: "div", divider: true },
  { id: "upload", label: "Upload a PDF", description: "Add SOPs, manuals or safety documents", icon: UploadCloud },
];

/** The "+" button and its action menu (same actions as the former quick-action cards). */
export function PlusMenu({ onAction, showUploadDot, disabled }) {
  return (
    <MenuButton label="Add or choose an action" menuLabel="Actions" placement="top-start" caret width="w-72"
      items={PLUS_ACTIONS.map((a) => (a.id === "upload" ? { ...a, dot: showUploadDot } : a))}
      onSelect={onAction}
      renderButton={({ ref, props, open }) => (
        <button ref={ref} type="button" disabled={disabled} {...props}
          className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-ink transition-colors hover:bg-subtle disabled:opacity-50 ${open ? "bg-subtle" : ""}`}>
          <Plus className="h-[18px] w-[18px]" strokeWidth={1.6} aria-hidden="true" />
        </button>
      )} />
  );
}

export function CitationsToggle() {
  const { citationsOn, setCitationsOn } = useApp();
  return (
    <button type="button" role="switch" aria-checked={citationsOn} onClick={() => setCitationsOn(!citationsOn)}
      title={citationsOn ? "Sources are shown with answers" : "Sources are collapsed under answers"}
      className="flex h-8 shrink-0 items-center gap-2 rounded-full px-2 text-[0.8125rem] font-medium text-ink hover:bg-subtle">
      <span className={`sr-only sm:not-sr-only ${citationsOn ? "text-accent-strong" : "text-muted"}`}>Citations</span>
      <span className="switch" aria-hidden="true"><span className="switch-knob" /></span>
    </button>
  );
}

/**
 * Prompt bar: "+" action menu, auto-growing input (Enter sends, Shift+Enter adds a line),
 * Citations toggle and send button. One optional attachment (image or PDF) can be dropped in
 * or added from the menu; it shows above the input.
 */
export default function Composer({
  onSubmit, busy, attachment, onAttach, onRemoveAttachment, uploadStatus, placeholder, autoFocus, footer, maxMB = 25,
  initialText = "", onAction, showUploadDot = false, offline = false, onFocusChange, inputId = "composer-input",
}) {
  const [text, setText] = useState(initialText);
  const [error, setError] = useState(null);
  const ref = useRef(null);
  // Mirrors `text` synchronously so a phrase dictated and sent in one breath is not lost to batching.
  const textRef = useRef(text);
  textRef.current = text;
  const voice = useVoice();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);
  useEffect(() => { if (autoFocus) ref.current?.focus(); }, [autoFocus]);

  const pick = (file) => {
    if (!file) return;
    const check = checkAttachment(file, maxMB);
    if (check.error) return setError(check.error);
    setError(null);
    onAttach(file);
  };

  const submit = (value = text) => {
    const q = value.trim();
    if (busy || offline || (!q && !attachment)) return;
    if (attachment?.kind === "image" && !q) {
      setError("Add a question about the image, for example \"What does this error display mean?\"");
      return;
    }
    setError(null);
    voice?.noteQuestion();
    onSubmit(q);
    setText("");
    textRef.current = "";
  };

  // Spoken words land in this input; "send" and "clear" act on it.
  useDictationTarget({
    append: (phrase) => { textRef.current = appendDictation(textRef.current, phrase).slice(0, 2000); setText(textRef.current); },
    submit: () => submit(textRef.current),
    clear: () => { textRef.current = ""; setText(""); },
  });

  const canSend = !busy && !offline && (text.trim() || attachment);
  return (
    <div>
      <form className="rounded-2xl border border-line bg-surface shadow-prompt transition-[border-color,box-shadow] focus-within:border-accent/50 focus-within:ring-4 focus-within:ring-accent/10"
        onSubmit={(e) => { e.preventDefault(); submit(); }}
        onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); if (onAttach) pick(e.dataTransfer.files?.[0]); }}>
        {attachment && (
          <div className="px-3 pt-3">
            <AttachmentChip attachment={attachment} status={uploadStatus} onRemove={onRemoveAttachment} />
          </div>
        )}
        <div className="flex items-end gap-1.5 p-2">
          {onAction && <PlusMenu onAction={onAction} showUploadDot={showUploadDot} disabled={busy} />}
          <label htmlFor={inputId} className="sr-only">Your question</label>
          <textarea id={inputId} ref={ref} rows={1} value={text} maxLength={2000} disabled={busy}
            onChange={(e) => setText(e.target.value)}
            onFocus={() => onFocusChange?.(true)} onBlur={() => onFocusChange?.(false)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }}
            placeholder={placeholder ?? "Ask about a procedure, document, experiment, or reagent..."}
            className="block max-h-[200px] min-h-[36px] flex-1 resize-none bg-transparent px-1.5 py-[7px] text-[0.9375rem] leading-[22px] text-ink placeholder:text-faint focus:outline-none disabled:opacity-60" />
          <CitationsToggle />
          <MicButton />
          <button type="submit" disabled={!canSend}
            aria-label={offline ? "Send question (unavailable: server not reachable)" : "Send question"}
            title={offline ? "Server not reachable" : undefined}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary text-white transition-colors hover:bg-primary-hover disabled:cursor-not-allowed disabled:bg-line disabled:text-faint">
            {busy ? <Spinner className="h-4 w-4" /> : <SendHorizontal className="h-4 w-4" strokeWidth={1.8} />}
          </button>
        </div>
      </form>
      <VoiceHint />
      {offline && <p className="mt-2 flex items-center gap-1.5 text-xs text-warning-ink" role="status"><WifiOff className="h-3.5 w-3.5" aria-hidden="true" /> Server not reachable. Sending is paused until the connection is back.</p>}
      {error && <p role="alert" className="mt-2 text-xs text-critical">{error}</p>}
      {footer}
    </div>
  );
}
