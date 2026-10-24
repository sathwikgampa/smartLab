import { useCallback, useEffect, useRef, useState } from "react";
import "../components/Markdown";
import {
  AlertTriangle, BookOpen, Boxes, ChevronRight, CornerDownRight, FileText, FileUp, History, ImagePlus, MessageSquareText,
  ShieldAlert, Trash2, Wrench, X,
} from "lucide-react";
import { useApp } from "../context/AppContext";
import { api } from "../lib/api";
import { docKind, formatDateTime, timeAgo } from "../lib/format";
import { takePendingQuestion } from "../lib/pending";
import { navigate } from "../lib/router";
import Composer, { ATTACH_ACCEPT, checkAttachment, makeAttachment } from "../components/Composer";
import SmartLabOrb from "../components/SmartLabOrb";
import { AnswerMessage, ErrorMessage, ImageAnswer, SystemNote, UserMessage } from "../components/Answer";
import CopilotAnswer, { AgentTrace, ConfirmCodeCard, DECISION_SUPPORT_NOTE } from "../components/CopilotAnswer";
import { ApprovalBadge, Drawer, EmptyState, Skeleton, SkeletonRows, useAsync } from "../components/ui";

const STARTERS = [
  "What does HPLC error E-104 mean and how do I fix it?",
  "How long should trypsin-EDTA incubate when passaging HEK293 cells?",
  "What should I do if a culture flask looks contaminated?",
  "How should reversed-phase columns be stored overnight?",
];

const newSessionId = () => (crypto.randomUUID?.() ?? `${Date.now()}${Math.random()}`).replace(/[^a-z0-9]/gi, "").slice(0, 32);

/** Convert stored chat rows into UI messages. */
function fromHistory(rows) {
  return rows.map((r) => {
    if (r.role === "user") return { id: r.id, role: "user", text: r.content, attachment: r.meta?.attachment ?? null };
    if (r.meta?.kind === "image") return { id: r.id, role: "image", vision: r.meta.vision, message_id: r.id, feedback: r.feedback };
    // Lab co-pilot runs store their structured result in meta (request_id marks them).
    if (r.meta?.request_id) {
      return { id: r.id, role: "copilot", message_id: r.id, response: r.content, citations: r.citations, feedback: r.feedback, ...r.meta };
    }
    return { id: r.id, role: "answer", message_id: r.id, answer: r.content, citations: r.citations, feedback: r.feedback, ...r.meta };
  });
}

function greeting(date = new Date()) {
  const h = date.getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function OverviewSection({ title, href, onLink, linkLabel, children }) {
  const link = <>{linkLabel}<ChevronRight className="h-3.5 w-3.5" aria-hidden="true" /></>;
  return (
    <section className="card overflow-hidden">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <h2 className="text-sm font-semibold">{title}</h2>
        {href && <a href={href} className="link flex items-center gap-0.5 text-xs">{link}</a>}
        {onLink && <button type="button" onClick={onLink} className="link flex items-center gap-0.5 text-xs">{link}</button>}
      </div>
      {children}
    </section>
  );
}

function HistoryDrawer({ open, onClose, current }) {
  const { toast, confirm } = useApp();
  const sessions = useAsync(() => (open ? api.sessions(50) : Promise.resolve(null)), [open]);
  const remove = async (s) => {
    if (!(await confirm({ title: "Delete conversation?", message: `"${s.title}" and its answers will be permanently deleted. Feedback already sent for review is also removed.`, confirmLabel: "Delete", danger: true }))) return;
    try {
      await api.deleteSession(s.session_id);
      sessions.reload();
      if (s.session_id === current) navigate("ask");
    } catch (err) { toast(err.message, "error"); }
  };
  return (
    <Drawer open={open} onClose={onClose} title="Conversations" width="sm:w-[24rem]">
      {sessions.loading ? <SkeletonRows rows={5} /> : !sessions.data?.length ? (
        <EmptyState icon={History} title="No conversations yet">Questions you ask are saved here.</EmptyState>
      ) : (
        <ul className="-mx-2 space-y-0.5">
          {sessions.data.map((s) => (
            <li key={s.session_id} className={`group flex items-start gap-2 rounded-lg px-2 py-2 ${s.session_id === current ? "bg-accent-soft" : "hover:bg-subtle"}`}>
              <a href={`#/ask/${s.session_id}`} onClick={onClose} className="min-w-0 flex-1 rounded-md">
                <p className="truncate text-sm font-medium">{s.title || "Untitled"}</p>
                <p className="meta" title={formatDateTime(s.updated_at)}>{timeAgo(s.updated_at)} · {Math.ceil(s.message_count / 2)} question(s)</p>
              </a>
              <button className="btn-ghost btn-icon h-8 w-8 opacity-0 focus:opacity-100 group-hover:opacity-100" onClick={() => remove(s)} aria-label={`Delete conversation ${s.title}`}>
                <Trash2 className="h-4 w-4" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </Drawer>
  );
}

const TASKS = [
  { label: "Check reagent stock", href: "#/reagents", icon: Boxes },
  { label: "Find a verified fix", href: "#/troubleshooting", icon: Wrench },
  { label: "Search literature", href: "#/literature", icon: BookOpen },
  { label: "Safety resources", href: "#/safety", icon: ShieldAlert },
];

/** Thread side panel: add sources, jump to related tasks, ask a suggested question. */
function ThreadPanel({ messages, onUploadFile, onUploadMedia, onAsk, disabled }) {
  const { openEvidence } = useApp();
  const asked = new Set(messages.filter((m) => m.role === "user").map((m) => m.text));
  const cited = [];
  const seen = new Set();
  for (const m of messages) {
    for (const c of m.citations ?? []) {
      if (!seen.has(c.document_id)) { seen.add(c.document_id); cited.push({ c, all: m.citations }); }
    }
  }
  return (
    <aside aria-label="Thread tools" className="hidden w-[280px] shrink-0 flex-col gap-6 overflow-y-auto border-l border-line bg-surface px-4 py-5 xl:flex">
      <section>
        <h2 className="mb-2 text-xs font-semibold text-ink">Sources</h2>
        <div className="grid grid-cols-2 gap-2">
          <button type="button" className="pill h-9 justify-center" onClick={onUploadFile} disabled={disabled}><FileUp aria-hidden="true" /> Upload file</button>
          <button type="button" className="pill h-9 justify-center" onClick={onUploadMedia} disabled={disabled}><ImagePlus aria-hidden="true" /> Upload media</button>
        </div>
        {cited.length > 0 ? (
          <ul className="mt-3 space-y-1">
            {cited.map(({ c, all }) => (
              <li key={c.document_id}>
                <button type="button" onClick={() => openEvidence(c, all)} className="flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-subtle">
                  <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-faint" aria-hidden="true" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[0.8125rem] text-ink">{c.document_title}</span>
                    <span className="block text-2xs text-muted">{docKind(c)}{c.version ? ` · v${c.version}` : ""}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : <p className="mt-3 text-xs text-muted">Documents cited in this thread will be listed here.</p>}
      </section>
      <section>
        <h2 className="mb-2 text-xs font-semibold text-ink">Suggested tasks</h2>
        <div className="flex flex-wrap gap-1.5">
          {TASKS.map((t) => (
            <a key={t.label} href={t.href} className="chip"><t.icon className="h-3.5 w-3.5 text-muted" aria-hidden="true" />{t.label}</a>
          ))}
        </div>
      </section>
      <section>
        <h2 className="mb-1 text-xs font-semibold text-ink">Suggested questions</h2>
        <ul className="-mx-2">
          {STARTERS.filter((q) => !asked.has(q)).map((q) => (
            <li key={q}>
              <button type="button" disabled={disabled} onClick={() => onAsk(q)}
                className="flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left text-[0.8125rem] leading-snug text-ink hover:bg-subtle disabled:opacity-50">
                <CornerDownRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-faint" aria-hidden="true" />{q}
              </button>
            </li>
          ))}
        </ul>
      </section>
    </aside>
  );
}

export default function AskView({ route }) {
  const {
    toast, health, healthError, currentUser, askScope: scope, setAskScope: setScope, setThreadTitle, evidence,
  } = useApp();
  const isOverview = route.path[0] === "overview";
  const routeSession = route.path[1] ?? null;
  const [sessionId, setSessionId] = useState(routeSession);
  const [messages, setMessages] = useState([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [busy, setBusy] = useState(false);
  const [attachment, setAttachment] = useState(null);
  const [uploadStatus, setUploadStatus] = useState(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [promptFocused, setPromptFocused] = useState(false);
  const [liveTrace, setLiveTrace] = useState(null); // trace steps streamed while the co-pilot works
  const agentInfo = useAsync(() => api.agentInfo().catch(() => null), []);
  const bottomRef = useRef(null);
  const ownSession = useRef(null); // session created in this view; don't reload it from the server
  const imageInputRef = useRef(null);
  const pdfInputRef = useRef(null);

  const empty = !loadingHistory && messages.length === 0;
  const home = isOverview && empty;
  const offline = healthError != null;

  // Library state drives the "Upload a PDF" hint dot; the Ask landing also lists recent items.
  const docs = useAsync(() => (empty ? api.documents({ sort: "updated_at", order: "desc" }) : Promise.resolve(null)), [empty]);
  const recentSessions = useAsync(() => (empty && !isOverview ? api.sessions(5) : Promise.resolve(null)), [empty, isOverview]);
  const summary = useAsync(() => (empty && !isOverview ? api.reagentSummary() : Promise.resolve(null)), [empty, isOverview]);

  // Load a conversation when navigating to #/ask/<id>; reset for #/ask and #/overview.
  const prevRoute = useRef(routeSession);
  useEffect(() => {
    const changed = prevRoute.current !== routeSession;
    prevRoute.current = routeSession;
    setSessionId(routeSession);
    if (!routeSession) {
      ownSession.current = null;
      if (changed) setMessages([]);
      return undefined;
    }
    if (routeSession === ownSession.current) return undefined;
    let alive = true;
    setLoadingHistory(true);
    api.session(routeSession)
      .then((rows) => alive && setMessages(fromHistory(rows)))
      .catch((err) => { if (alive) { toast(err.message, "error"); setMessages([]); } })
      .finally(() => alive && setLoadingHistory(false));
    return () => { alive = false; };
  }, [routeSession, toast]);

  // The top-bar breadcrumb shows the thread's first question.
  const firstQuestion = messages.find((m) => m.role === "user")?.text ?? null;
  useEffect(() => { setThreadTitle(firstQuestion ? firstQuestion.slice(0, 80) : null); }, [firstQuestion, setThreadTitle]);
  useEffect(() => () => setThreadTitle(null), [setThreadTitle]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [messages, busy]);

  const ensureSession = () => {
    if (sessionId) return sessionId;
    const id = newSessionId();
    ownSession.current = id;
    setSessionId(id);
    navigate(`ask/${id}`, { replace: true });
    return id;
  };

  const push = (m) => setMessages((all) => [...all, { id: `${Date.now()}-${Math.random()}`, ...m }]);

  const run = useCallback(async ({ question, att, docScope, uploadId, errorCode }) => {
    setBusy(true);
    const sid = ensureSession();
    let targetScope = docScope;
    try {
      if (att?.kind === "pdf") {
        setUploadStatus({ phase: "uploading", progress: 0 });
        let doc;
        try {
          doc = await api.uploadDocument(att.file, {}, {
            onProgress: (p) => setUploadStatus(p >= 1 ? { phase: "processing" } : { phase: "uploading", progress: p }),
          });
          push({ role: "system", text: `Added "${doc.title}" to Documents (${doc.page_count} pages indexed, status: Unreviewed).` });
        } catch (err) {
          if (err.status !== 409 || !err.data?.document) throw err;
          doc = err.data.document;
          push({ role: "system", text: `"${doc.title}" is already in Documents; using the existing copy.` });
        }
        targetScope = { id: doc.id, title: doc.title };
        setScope(targetScope);
        if (!question) return;
      }
      if (att?.kind === "image") {
        // The image is stored under a server-generated name and the display is read once; the co-pilot only
        // ever sees the returned upload_id. A code read from the image is never acted on until confirmed.
        const progress = (p) => setUploadStatus(p >= 1 ? { phase: "processing" } : { phase: "uploading", progress: p });
        setUploadStatus({ phase: "uploading", progress: 0 });
        const up = await api.agentUpload(att.file, { onProgress: progress });
        if (!up.extracted_code) {
          // No code on a display: keep the observational image analysis, then offer manual code entry.
          const vision = await api.analyzeImage(att.file, question, { onProgress: progress });
          const saved = await api.recordExchange(sid, {
            question, answer: vision.analysis.visual_description, citations: vision.sop_matches,
            attachment: { name: att.name, kind: "image" }, meta: { kind: "image", vision },
          }).catch(() => null);
          push({ role: "image", vision, message_id: saved?.message_id });
        }
        push({ role: "confirm", upload: up, question });
        return;
      }
      setLiveTrace([]);
      const res = await api.chatStream(question, sid, targetScope ? [targetScope.id] : undefined,
        { upload_id: uploadId, error_code: errorCode },
        { onEvent: (kind, step) => kind === "trace" && setLiveTrace((t) => [...(t ?? []), step]) });
      push({ role: "copilot", ...res });
    } catch (err) {
      push({ role: "error", error: err.message, retry: { question, att, docScope: targetScope, uploadId, errorCode } });
    } finally {
      setBusy(false);
      setUploadStatus(null);
      setLiveTrace(null);
    }
  }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = (question) => {
    const att = attachment;
    push({ role: "user", text: question, attachment: att ? { name: att.name, kind: att.kind, previewUrl: att.previewUrl } : null });
    setAttachment(null);
    run({ question, att, docScope: scope });
  };

  const confirmCode = (msg, code) => {
    setMessages((all) => all.map((m) => (m === msg ? { ...m, confirmed: code } : m)));
    push({ role: "user", text: `Confirmed code ${code}` });
    const base = (msg.question || "What does this error mean and how do I fix it?").slice(0, 1900);
    run({ question: `${base} (confirmed display code ${code})`, docScope: scope, uploadId: msg.upload.upload_id, errorCode: code });
  };

  const retry = (msg) => {
    setMessages((all) => all.filter((m) => m !== msg));
    run(msg.retry);
  };

  const pickImage = (file) => {
    if (!file) return;
    const check = checkAttachment(file, health?.max_upload_mb ?? 25);
    if (check.error || check.kind !== "image") {
      toast(check.error ?? "Choose a PNG, JPEG or WebP image.", "error");
      return;
    }
    setAttachment(makeAttachment(file));
  };

  // "+" menu: the same destinations as the former quick-action cards.
  const onAction = (id) => {
    if (id === "search") navigate("search");
    else if (id === "image") imageInputRef.current?.click();
    else if (id === "reagent") navigate("reagents");
    else if (id === "literature") navigate("literature");
    else if (id === "upload") navigate("documents?upload=1");
  };

  // Question handed over from any view or external trigger
  useEffect(() => {
    const pending = takePendingQuestion();
    if (!pending) return;
    if (pending.scope) { ownSession.current = null; setScope(pending.scope); }
    if (pending.attachOnly && pending.file) { setAttachment(makeAttachment(pending.file)); return; }
    if (pending.question || pending.file) {
      const att = pending.file ? makeAttachment(pending.file) : null;
      push({ role: "user", text: pending.question, attachment: att ? { name: att.name, kind: att.kind, previewUrl: att.previewUrl } : null });
      run({ question: pending.question, att, docScope: pending.scope ?? null });
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const lastQuestion = (idx) => [...messages.slice(0, idx)].reverse().find((m) => m.role === "user")?.text;

  const allDocs = docs?.data ?? [];
  const noDocuments = !docs?.loading && !docs?.error && allDocs.length === 0;
  const needsReview = allDocs.filter((d) => d.approval_status === "pending" || d.approval_status === "unreviewed");
  const reagentAlerts = summary?.data ? [...summary.data.expired, ...summary.data.expiring_soon] : [];
  const firstName = currentUser.name.split(/\s+/)[0];

  const composer = (
    <Composer busy={busy} autoFocus attachment={attachment} uploadStatus={uploadStatus} offline={offline}
      onAttach={(f) => setAttachment(makeAttachment(f))} onRemoveAttachment={() => setAttachment(null)}
      maxMB={health?.max_upload_mb ?? 25} onAction={onAction} showUploadDot={noDocuments}
      onFocusChange={setPromptFocused}
      placeholder={attachment?.kind === "image" ? "What does this instrument display mean?" : "Ask about a procedure, a buffer to prepare, an error code, or a reagent..."}
      onSubmit={submit} />
  );
  const scopeNote = scope && (
    <div className="mb-2 flex items-center gap-2 text-xs">
      <FileText className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
      <span className="text-muted">Answering from</span>
      <span className="truncate font-medium">{scope.title}</span>
      <button className="btn-ghost h-6 px-1.5 text-xs" onClick={() => setScope(null)} aria-label="Search all documents again"><X className="h-3.5 w-3.5" /> All documents</button>
    </div>
  );
  const pickPdf = (file) => {
    if (!file) return;
    const check = checkAttachment(file, health?.max_upload_mb ?? 25);
    if (check.error || check.kind !== "pdf") { toast(check.error ?? "Choose a PDF document.", "error"); return; }
    setAttachment(makeAttachment(file));
  };
  const imageInput = (
    <>
      <input ref={imageInputRef} type="file" hidden accept={ATTACH_ACCEPT.replace(".pdf,", "")} onChange={(e) => { pickImage(e.target.files?.[0]); e.target.value = ""; }} />
      <input ref={pdfInputRef} type="file" hidden accept=".pdf" onChange={(e) => { pickPdf(e.target.files?.[0]); e.target.value = ""; }} />
    </>
  );

  /* ------------------------------------------------------------- home (Overview) */
  if (home) {
    return (
      <div className="flex min-h-full flex-col items-center justify-center px-4 pb-[10vh] pt-10">
        <SmartLabOrb state={busy ? "thinking" : promptFocused ? "focused" : "idle"} />
        <h1 className="mt-7 text-center font-serif text-[clamp(2.25rem,4vw,3.25rem)] font-medium leading-[1.12] tracking-[-0.01em] text-ink">
          {greeting()}, {firstName}
        </h1>
        <p className="mt-2 text-center font-serif text-[clamp(1.5rem,2.6vw,2.1rem)] font-normal leading-tight text-muted">
          What are you cooking <span className="gradient-text">today</span>?
        </p>
        <div className="mt-9 w-full max-w-[44rem]">
          {scopeNote}
          {composer}
        </div>
        {imageInput}
      </div>
    );
  }

  /* -------------------------------------------- Ask landing and conversation thread */
  return (
    <div className="flex h-full">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className={`mx-auto w-full px-4 py-6 sm:px-6 ${empty ? "max-w-4xl" : "max-w-3xl space-y-6"}`}>
            {loadingHistory && <div className="space-y-4"><Skeleton className="ml-auto h-10 w-2/3" /><Skeleton className="h-32 w-full" /></div>}
  
            {empty && (
              <div className="space-y-6 pt-1">
                <div className="flex flex-wrap items-end justify-between gap-3">
                  <div>
                    <h1 className="text-xl font-semibold tracking-tight">Ask SmartLab</h1>
                    <p className="mt-1 text-sm text-muted">Answers come from your documents, with the page behind every claim.</p>
                  </div>
                  <button className="pill" onClick={() => setHistoryOpen(true)}><History aria-hidden="true" /> History</button>
                </div>
  
                <div>
                  <p className="eyebrow mb-2">Suggested questions</p>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    {STARTERS.map((q) => (
                      <button key={q} onClick={() => submit(q)} disabled={busy || offline}
                        className="rounded-xl border border-line bg-surface px-3.5 py-3 text-left text-sm shadow-panel transition-colors hover:bg-subtle disabled:opacity-50">
                        {q}
                      </button>
                    ))}
                  </div>
                </div>
  
                <div className="grid grid-cols-1 items-start gap-5 lg:grid-cols-2">
                  {(needsReview.length > 0 || reagentAlerts.length > 0) && (
                    <div className="lg:col-span-2">
                      <OverviewSection title="Needs attention">
                        <ul className="divide-y divide-line text-sm">
                          {needsReview.length > 0 && (
                            <li>
                              <a href="#/documents?status=review" className="flex items-center gap-3 px-4 py-2.5 hover:bg-subtle">
                                <AlertTriangle className="h-4 w-4 shrink-0 text-warning-ink" aria-hidden="true" />
                                <span className="flex-1">{needsReview.length} document(s) awaiting review or approval</span>
                                <ChevronRight className="h-4 w-4 text-faint" aria-hidden="true" />
                              </a>
                            </li>
                          )}
                          {reagentAlerts.length > 0 && (
                            <li>
                              <a href="#/reagents?status=expired" className="flex items-center gap-3 px-4 py-2.5 hover:bg-subtle">
                                <AlertTriangle className="h-4 w-4 shrink-0 text-warning-ink" aria-hidden="true" />
                                <span className="flex-1">{summary.data.expired.length} reagent(s) expired, {summary.data.expiring_soon.length} expiring within 30 days</span>
                                <ChevronRight className="h-4 w-4 text-faint" aria-hidden="true" />
                              </a>
                            </li>
                          )}
                        </ul>
                      </OverviewSection>
                    </div>
                  )}
  
                  <OverviewSection title="Recent conversations" onLink={() => setHistoryOpen(true)} linkLabel="All history">
                    {recentSessions.loading ? <div className="p-4"><Skeleton className="h-24" /></div> : !recentSessions.data?.length ? (
                      <p className="px-4 py-6 text-sm text-muted">No conversations yet. Questions you ask will appear here.</p>
                    ) : (
                      <ul className="divide-y divide-line">
                        {recentSessions.data.slice(0, 5).map((s) => (
                          <li key={s.session_id}>
                            <a href={`#/ask/${s.session_id}`} className="flex items-center gap-3 px-4 py-2.5 hover:bg-subtle">
                              <MessageSquareText className="h-4 w-4 shrink-0 text-faint" aria-hidden="true" />
                              <span className="min-w-0 flex-1 truncate text-sm">{s.title || "Untitled"}</span>
                              <span className="shrink-0 text-xs text-muted">{timeAgo(s.updated_at)}</span>
                            </a>
                          </li>
                        ))}
                      </ul>
                    )}
                  </OverviewSection>
  
                  <OverviewSection title="Recently updated documents" href="#/documents" linkLabel="All documents">
                    {docs.loading ? <div className="p-4"><Skeleton className="h-24" /></div> : !allDocs.length ? (
                      <p className="px-4 py-6 text-sm text-muted">No documents yet. <a className="link" href="#/documents?upload=1">Upload a PDF</a> to start.</p>
                    ) : (
                      <ul className="divide-y divide-line">
                        {allDocs.slice(0, 5).map((d) => (
                          <li key={d.id}>
                            <a href={`#/documents/${d.id}`} className="flex items-center gap-3 px-4 py-2.5 hover:bg-subtle">
                              <FileText className="h-4 w-4 shrink-0 text-faint" aria-hidden="true" />
                              <span className="min-w-0 flex-1">
                                <span className="block truncate text-sm">{d.title}</span>
                                <span className="block text-xs text-muted">{d.version ? `v${d.version} · ` : ""}{d.category}</span>
                              </span>
                              <ApprovalBadge status={d.approval_status} />
                            </a>
                          </li>
                        ))}
                      </ul>
                    )}
                  </OverviewSection>
                </div>
              </div>
            )}
  
            {messages.map((m, i) => {
              if (m.role === "user") return <UserMessage key={m.id} text={m.text} attachment={m.attachment} />;
              if (m.role === "copilot") return <CopilotAnswer key={m.id} msg={m} demo={!!agentInfo.data?.demo_data} />;
              if (m.role === "confirm") return <ConfirmCodeCard key={m.id} msg={m} onConfirm={confirmCode} disabled={busy || offline} />;
              if (m.role === "answer") return <AnswerMessage key={m.id} msg={m} question={lastQuestion(i)} />;
              if (m.role === "image") return <ImageAnswer key={m.id} msg={m} />;
              if (m.role === "system") return <SystemNote key={m.id}>{m.text}</SystemNote>;
              if (m.role === "error") return <ErrorMessage key={m.id} msg={m} onRetry={() => retry(m)} />;
              return null;
            })}
  
            {busy && !uploadStatus && (liveTrace ? (
              <div className="space-y-2" role="status" aria-label="Working on your question">
                <p className="text-xs text-muted">Checking your SOPs, inventory and calculations...</p>
                <AgentTrace steps={liveTrace} live />
              </div>
            ) : (
              <div className="space-y-2" role="status" aria-label="Searching documents">
                <p className="text-xs text-muted">Searching your documents...</p>
                <Skeleton className="h-4 w-11/12" /><Skeleton className="h-4 w-10/12" /><Skeleton className="h-4 w-7/12" />
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        </div>
  
        <div className="px-4 pb-4 pt-2 sm:px-6">
          <div className="mx-auto w-full max-w-3xl">
            {scopeNote}
            {composer}
            <p className="mt-2 text-center text-2xs text-muted">
              {DECISION_SUPPORT_NOTE}
              {health && !health.llm.enabled && (
                <span title="Set ANTHROPIC_API_KEY on the server to enable generated answers and image reading"> Offline mode: answers quote your documents directly.</span>
              )}
            </p>
          </div>
        </div>
      </div>
      {!empty && !evidence && (
        <ThreadPanel messages={messages} disabled={busy || offline} onAsk={submit}
          onUploadFile={() => pdfInputRef.current?.click()} onUploadMedia={() => imageInputRef.current?.click()} />
      )}
      {imageInput}
      <HistoryDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} current={sessionId} />
    </div>
  );
}
