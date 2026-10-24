import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-react";
import { api, UNREACHABLE } from "../lib/api";
import { Dialog } from "../components/ui";

const AppContext = createContext(null);

const TOAST_ICON = {
  success: <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-hidden="true" />,
  error: <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-critical/80" aria-hidden="true" />,
  warning: <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning-ink" aria-hidden="true" />,
  info: <Info className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden="true" />,
};

/** Persisted per-browser preference; storage may be unavailable (private mode). */
export function usePref(key, initial) {
  const [value, setValue] = useState(() => {
    try { const raw = localStorage.getItem(key); return raw == null ? initial : JSON.parse(raw); } catch { return initial; }
  });
  useEffect(() => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ } }, [key, value]);
  return [value, setValue];
}

/**
 * App-wide state: toasts, a promise-based confirm dialog, server capabilities (health),
 * the evidence panel, and the (demo) current user. There is no authentication yet.
 */
export function AppProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(null);
  const [evidence, setEvidence] = useState(null); // { citations: [...], active: n }
  const [confirmState, setConfirmState] = useState(null);
  // Signed-in account: undefined while checking, null when signed out.
  const [account, setAccount] = useState(undefined);
  // The person using the app. `id` is their researcher profile, used to attribute comments, files and experiments.
  const currentUser = useMemo(() => (account ? {
    id: account.profile_id, accountId: account.id, name: account.name, email: account.email,
    organization: account.organization, role: account.role === "admin" ? "Admin" : "Member",
  } : { id: null, name: "", role: "" }), [account]);
  // Shared between the top bar and the Ask view.
  const [askScope, setAskScope] = useState(null); // {id, title}: answer from one document only
  const [threadTitle, setThreadTitle] = useState(null);
  const [answerMode, setAnswerMode] = usePref("smartlab-answer-mode", "concise"); // UI only: no API parameter yet
  const [citationsOn, setCitationsOn] = usePref("smartlab-citations", true);
  const toastId = useRef(0);

  const toast = useCallback((message, type = "info") => {
    const id = ++toastId.current;
    setToasts((t) => [...t.slice(-3), { id, message, type }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);

  useEffect(() => {
    api.me().then((r) => setAccount(r.user ?? null)).catch(() => setAccount(null));
    const onUnauthorized = () => setAccount(null);
    window.addEventListener("smartlab:unauthorized", onUnauthorized);
    return () => window.removeEventListener("smartlab:unauthorized", onUnauthorized);
  }, []);
  const logout = useCallback(async () => {
    try { await api.logout(); } catch { /* the session is cleared below either way */ }
    // Full reload so no data from this account stays in memory.
    window.location.replace(`${window.location.pathname}#/`);
    window.location.reload();
  }, []);

  const refreshHealth = useCallback(() => {
    api.health().then((h) => { setHealth(h); setHealthError(null); }).catch(setHealthError);
  }, []);
  useEffect(() => { refreshHealth(); }, [refreshHealth]);

  // Keep the connection indicator current: any failed request marks the server unreachable,
  // and the health check is repeated (faster while down, paused while the tab is hidden).
  useEffect(() => {
    let lastToast = 0;
    const onConn = (e) => {
      if (e.detail.reachable) return;
      setHealthError((h) => h ?? new Error("unreachable"));
      // One toast per burst of failed requests (send, upload, search...), never stacked.
      if (Date.now() - lastToast > 6000) { lastToast = Date.now(); toast(UNREACHABLE, "error"); }
    };
    window.addEventListener("smartlab:connection", onConn);
    return () => window.removeEventListener("smartlab:connection", onConn);
  }, [toast]);
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) refreshHealth(); }, healthError ? 10000 : 60000);
    const onVisible = () => { if (!document.hidden) refreshHealth(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", onVisible); };
  }, [healthError, refreshHealth]);

  /** Open the evidence panel on one citation, optionally with its sibling citations. */
  const openEvidence = useCallback((citation, siblings) => {
    const citations = siblings?.length ? siblings : [citation];
    const idx = citations.findIndex((c) => c === citation || (c.n != null && c.n === citation.n));
    setEvidence({ citations, active: Math.max(0, idx) });
  }, []);
  const closeEvidence = useCallback(() => setEvidence(null), []);

  /** confirm({title, message, confirmLabel, danger}) -> Promise<boolean> */
  const confirm = useCallback((opts) => new Promise((resolve) => setConfirmState({ ...opts, resolve })), []);
  const settle = (value) => { confirmState?.resolve(value); setConfirmState(null); };

  const connected = health != null && !healthError;
  const value = useMemo(() => ({
    toast, confirm, health, healthError, connected, refreshHealth, evidence, openEvidence, closeEvidence, currentUser,
    askScope, setAskScope, threadTitle, setThreadTitle, answerMode, setAnswerMode, citationsOn, setCitationsOn,
    account, setAccount, logout,
  }), [account, logout, toast, confirm, health, healthError, connected, refreshHealth, evidence, openEvidence, closeEvidence, currentUser,
    askScope, threadTitle, answerMode, setAnswerMode, citationsOn, setCitationsOn]);

  return (
    <AppContext.Provider value={value}>
      {children}
      <Dialog open={!!confirmState} onClose={() => settle(false)} title={confirmState?.title ?? "Are you sure?"}
        footer={<>
          <button className="btn-secondary" onClick={() => settle(false)}>Cancel</button>
          <button data-autofocus className={confirmState?.danger ? "btn-danger" : "btn-primary"} onClick={() => settle(true)}>
            {confirmState?.confirmLabel ?? "Confirm"}
          </button>
        </>}>
        <p className="text-sm text-ink">{confirmState?.message}</p>
      </Dialog>
      <div className="pointer-events-none fixed bottom-5 right-5 z-[60] flex w-[min(22rem,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} role={t.type === "error" ? "alert" : "status"}
            className="toast-in pointer-events-auto flex items-start gap-2.5 rounded-xl border border-line bg-surface px-3.5 py-3 text-sm shadow-pop">
            {TOAST_ICON[t.type]}
            <p className="flex-1 text-ink">{t.message}</p>
            <button className="-m-1 rounded-md p-1 text-faint hover:bg-subtle hover:text-ink" onClick={() => setToasts((all) => all.filter((x) => x.id !== t.id))} aria-label="Dismiss notification">
              <X className="h-4 w-4" />
            </button>
          </div>
        ))}
      </div>
    </AppContext.Provider>
  );
}

export const useApp = () => useContext(AppContext);
