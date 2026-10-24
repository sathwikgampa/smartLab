import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity, AlignLeft, BookOpen, Boxes, Check, ChevronDown, Copy, FileText, FlaskConical, FolderPlus, House, Layers, Link2,
  MessageSquareText, LogOut, Plus, Search, Settings, ShieldAlert, UserPlus, Wrench,
} from "lucide-react";
import { useApp } from "../context/AppContext";
import { api } from "../lib/api";
import { copyText } from "../lib/format";
import { navigate } from "../lib/router";
import { Floating, MenuButton, useDismiss, useMenuKeys } from "./Popover";
import { Dialog, Field } from "./ui";
import { HandsFreeToggle, VoiceDock, VoiceProvider } from "./VoiceCoPilot";
import { SkipLink } from "../site/SiteChrome";

/* ----------------------------------------------------------------- navigation model */
export const MAIN_NAV = [
  { id: "overview", label: "Overview", icon: House },
  { id: "ask", label: "Ask SmartLab", icon: MessageSquareText },
  { id: "documents", label: "Documents", icon: FileText },
  { id: "reagents", label: "Reagents", icon: Boxes },
  { id: "literature", label: "Scientific literature", icon: BookOpen },
  { id: "safety", label: "Safety resources", icon: ShieldAlert },
];
export const NAV_GROUPS = [
  {
    id: "lab-ops", label: "Lab operations", icon: Wrench,
    items: [{ id: "troubleshooting", label: "Troubleshooting" }, { id: "experiments", label: "Experiments" }, { id: "data", label: "Data analysis" }, { id: "visualizer", label: "Visualizer" }],
  },
  {
    id: "early", label: "Early features", hint: "Working prototypes with limited scope", icon: Layers,
    items: [{ id: "collaboration", label: "Collaboration" }, { id: "simulation", label: "Simulation" }, { id: "training", label: "Equipment training" }],
  },
];
export const FOOTER_NAV = [
  { id: "activity", label: "Recent activity", icon: Activity },
  { id: "settings", label: "Settings", icon: Settings },
];
export const ALL_NAV = [
  ...MAIN_NAV,
  ...NAV_GROUPS.flatMap((g) => g.items),
  ...FOOTER_NAV,
  { id: "search", label: "Search" },
];

const initials = (name = "") => name.split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join("");

/* ------------------------------------------------------------------------ icon rail */
function RailTooltip({ tip }) {
  if (!tip) return null;
  return (
    <span role="tooltip" id="rail-tooltip" style={{ top: tip.top, left: tip.left }}
      className="pointer-events-none fixed z-[56] -translate-y-1/2 whitespace-nowrap rounded-md bg-primary px-2 py-1 text-xs font-medium text-white shadow-pop">
      {tip.label}
    </span>
  );
}

function GroupFlyout({ group, current, open, onOpenChange, showTip, hideTip }) {
  const btnRef = useRef(null);
  const listRef = useRef(null);
  const active = group.items.some((i) => i.id === current);
  const close = useCallback(({ restoreFocus } = {}) => { onOpenChange(false); if (restoreFocus) btnRef.current?.focus(); }, [onOpenChange]);
  useDismiss(open, close, [btnRef, listRef]);
  const onKeyDown = useMenuKeys(open, listRef);

  return (
    <>
      <button ref={btnRef} type="button" data-rail-item className="rail-btn" data-active={active}
        aria-label={group.label} aria-haspopup="menu" aria-expanded={open}
        onClick={() => { hideTip(); onOpenChange(!open); }}
        onMouseEnter={(e) => !open && showTip(e.currentTarget, group.label)} onMouseLeave={hideTip}
        onFocus={(e) => !open && e.currentTarget.matches(":focus-visible") && showTip(e.currentTarget, group.label)} onBlur={hideTip}
        onKeyDown={(e) => { if (e.key === "ArrowRight") { e.preventDefault(); onOpenChange(true); } }}>
        {active && <span className="rail-bar" aria-hidden="true" />}
        <group.icon className="h-[18px] w-[18px]" strokeWidth={1.6} aria-hidden="true" />
      </button>
      <Floating anchorRef={btnRef} panelRef={listRef} open={open} placement="right-start" offset={10} className="w-52">
        <p className="px-3.5 pb-1 pt-3 text-xs font-semibold text-ink">{group.label}</p>
        {group.hint && <p className="px-3.5 text-2xs text-muted">{group.hint}</p>}
        <div role="menu" aria-label={group.label} className="p-1.5" onKeyDown={(e) => { if (e.key === "ArrowLeft") close({ restoreFocus: true }); else onKeyDown(e); }}>
          {group.items.map((item) => (
            <a key={item.id} href={`#/${item.id}`} role="menuitem" tabIndex={-1} aria-current={current === item.id ? "page" : undefined}
              onClick={() => close({ restoreFocus: false })}
              className={`menu-item items-center py-1.5 text-sm ${current === item.id ? "font-medium text-accent-strong" : "text-ink"}`}>
              {item.label}
            </a>
          ))}
        </div>
      </Floating>
    </>
  );
}

/** Avatar button with the signed-in account's details, Settings and Log out. */
function AccountMenu({ user, onLogout, hideTip, tipProps }) {
  const header = (
    <div className="border-b border-line px-3.5 pb-2.5 pt-3">
      <p className="truncate text-sm font-medium text-ink">{user.name}</p>
      <p className="truncate text-xs text-muted">{user.email}</p>
      <p className="mt-1 text-2xs text-muted">{user.role}{user.organization ? ` · ${user.organization}` : ""}</p>
    </div>
  );
  return (
    <MenuButton label={`Account: ${user.name}`} placement="right-start" width="w-60"
      items={[
        { id: "settings", label: "Settings", icon: Settings },
        { id: "logout", label: "Log out", icon: LogOut },
      ]}
      onSelect={(id) => (id === "logout" ? onLogout() : navigate("settings"))}
      footer={null}
      renderButton={({ ref, props, open }) => (
        <button ref={ref} type="button" {...props} {...(open ? {} : tipProps(user.name))}
          onClick={() => { hideTip(); props.onClick(); }}
          className="mt-2 flex h-8 w-8 items-center justify-center rounded-full bg-accent-soft text-[0.6875rem] font-semibold text-accent-strong hover:ring-2 hover:ring-accent/30">
          {initials(user.name)}
        </button>
      )}
      header={header} />
  );
}

function Rail({ current }) {
  const { connected, health, currentUser, logout } = useApp();
  const [tip, setTip] = useState(null);
  const [openGroup, setOpenGroup] = useState(null);
  const navRef = useRef(null);

  const showTip = useCallback((el, label) => {
    const r = el.getBoundingClientRect();
    setTip({ label, top: r.top + r.height / 2, left: r.right + 10 });
  }, []);
  const hideTip = useCallback(() => setTip(null), []);
  useEffect(() => { setOpenGroup(null); }, [current]);

  // Up/Down arrows move between rail items.
  const onKeyDown = (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    if (!e.target.matches("[data-rail-item]")) return;
    const items = [...navRef.current.querySelectorAll("[data-rail-item]")];
    const i = items.indexOf(e.target);
    if (i < 0) return;
    e.preventDefault();
    items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  };

  const statusLabel = health == null && !connected ? "Checking connection" : connected ? "Connected" : "Server not reachable";
  const tipProps = (label) => ({
    onMouseEnter: (e) => showTip(e.currentTarget, label), onMouseLeave: hideTip,
    onFocus: (e) => e.currentTarget.matches(":focus-visible") && showTip(e.currentTarget, label), onBlur: hideTip,
  });
  const railLink = (item) => (
    <a key={item.id} href={`#/${item.id}`} data-rail-item className="rail-btn" aria-label={item.label}
      aria-current={current === item.id ? "page" : undefined} {...tipProps(item.label)}>
      {current === item.id && <span className="rail-bar" aria-hidden="true" />}
      <item.icon className="h-[18px] w-[18px]" strokeWidth={1.6} aria-hidden="true" />
    </a>
  );

  return (
    <nav ref={navRef} aria-label="Main" onKeyDown={onKeyDown}
      className="flex w-14 shrink-0 flex-col items-center border-r border-line bg-surface py-3">
      <div className="relative mb-3 flex h-10 w-10 items-center justify-center">
        <a href="#/overview" className="flex h-9 w-9 items-center justify-center rounded-[10px] text-accent hover:bg-subtle" aria-label="SmartLab AI home" {...tipProps("SmartLab AI")}>
          <FlaskConical className="h-[22px] w-[22px]" strokeWidth={1.8} aria-hidden="true" />
        </a>
        <span role="img" aria-label={statusLabel} {...tipProps(statusLabel)} tabIndex={0}
          className={`absolute bottom-0.5 right-0.5 flex h-2.5 w-2.5 items-center justify-center rounded-full ring-2 ring-surface ${
            connected ? "bg-success" : health == null ? "bg-faint" : "bg-warning"}`}>
          {/* Shape differs as well as colour: a hollow centre when not connected. */}
          {!connected && <span className="h-1 w-1 rounded-full bg-surface" aria-hidden="true" />}
        </span>
      </div>

      <div className="flex min-h-0 w-full flex-1 flex-col items-center gap-1 overflow-y-auto overflow-x-hidden px-2 [scrollbar-width:none]">
        {MAIN_NAV.map(railLink)}
        {NAV_GROUPS.map((g) => (
          <GroupFlyout key={g.id} group={g} current={current} open={openGroup === g.id}
            onOpenChange={(o) => setOpenGroup(o ? g.id : null)} showTip={showTip} hideTip={hideTip} />
        ))}
        <div className="my-2 w-6 border-t border-line" role="separator" />
        {FOOTER_NAV.map(railLink)}
      </div>

      <AccountMenu user={currentUser} onLogout={logout} hideTip={hideTip} tipProps={tipProps} />
      <RailTooltip tip={tip} />
    </nav>
  );
}

/* --------------------------------------------------------------------------- top bar */
function ScopePill() {
  const { askScope, setAskScope } = useApp();
  const [docs, setDocs] = useState([]);
  // Reload on every open so documents uploaded since the last look are listed.
  const load = useCallback(() => { api.documents({ sort: "title", order: "asc" }).then(setDocs).catch(() => {}); }, []);
  useEffect(() => { load(); }, [load]);
  const items = [
    { id: "all", label: "All documents", description: "Search the whole library" },
    ...(docs.length ? [{ id: "div", divider: true }] : []),
    ...docs.slice(0, 30).map((d) => ({ id: String(d.id), label: d.title, description: [d.doc_type, d.version && `v${d.version}`].filter(Boolean).join(" · ") })),
  ];
  return (
    <MenuButton label="Answer scope" width="w-72" className="max-h-[60vh] overflow-y-auto" onOpen={load}
      items={items} value={askScope ? String(askScope.id) : "all"}
      onSelect={(id, item) => setAskScope(id === "all" ? null : { id: Number(id), title: item.label })}
      renderButton={({ ref, props }) => (
        <button ref={ref} type="button" className="pill max-w-[13rem]" {...props} title="Which documents answers come from">
          <FileText aria-hidden="true" /><span className="hidden truncate sm:inline">{askScope ? askScope.title : "All documents"}</span>
          <ChevronDown aria-hidden="true" />
        </button>
      )} />
  );
}

function ModePill() {
  const { answerMode, setAnswerMode } = useApp();
  return (
    <MenuButton label="Answer style" value={answerMode} onSelect={setAnswerMode}
      items={[
        { id: "concise", label: "Concise", description: "Short answers with key steps" },
        { id: "detailed", label: "Detailed", description: "Fuller explanations" },
      ]}
      renderButton={({ ref, props }) => (
        <button ref={ref} type="button" className="pill" {...props}>
          <AlignLeft aria-hidden="true" /><span className="hidden sm:inline">{answerMode === "detailed" ? "Detailed" : "Concise"}</span>
          <ChevronDown aria-hidden="true" />
        </button>
      )} />
  );
}

function CollectionButton() {
  const { toast } = useApp();
  return (
    <MenuButton label="Collections" header="Collections" width="w-64"
      items={[
        { id: "none", label: "No collections yet", description: "Group documents to ask across a set", disabled: true },
        { id: "div", divider: true },
        { id: "new", label: "New collection", icon: FolderPlus, description: "Needs server support (coming soon)" },
      ]}
      onSelect={(id) => id === "new" && toast("Collections aren't available yet: the server has no collections API. Use the scope selector to answer from one document.", "info")}
      renderButton={({ ref, props }) => (
        <button ref={ref} type="button" className="inline-flex h-8 items-center gap-1 rounded-lg px-2 text-[0.8125rem] font-medium text-ink hover:bg-subtle" {...props}>
          <Plus className="h-3.5 w-3.5" aria-hidden="true" /> Collection <ChevronDown className="h-3.5 w-3.5 text-muted" aria-hidden="true" />
        </button>
      )} />
  );
}

function Breadcrumb({ children }) {
  return (
    <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1.5 text-[0.8125rem]">
      <a href="#/overview" className="shrink-0 text-muted hover:text-ink">SmartLab AI</a>
      <span className="text-faint" aria-hidden="true">/</span>
      <span className="truncate font-medium text-ink" aria-current="page">{children}</span>
    </nav>
  );
}

/** Command-palette style global search (documents, reagents, error codes) plus page jump. */
function SearchPalette({ open, onClose }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  useEffect(() => { if (open) { setQ(""); setSel(0); } }, [open]);
  const pages = useMemo(() => {
    const t = q.trim().toLowerCase();
    return ALL_NAV.filter((n) => n.id !== "search" && (!t || n.label.toLowerCase().includes(t))).slice(0, 6);
  }, [q]);
  const options = [
    ...(q.trim() ? [{ key: "search", label: `Search documents, reagents and error codes for "${q.trim()}"`, go: () => navigate(`search?q=${encodeURIComponent(q.trim())}`), icon: Search }] : []),
    ...pages.map((p) => ({ key: p.id, label: `Go to ${p.label}`, go: () => navigate(p.id), icon: p.icon ?? FileText })),
  ];
  const choose = (o) => { if (!o) return; onClose(); o.go(); };
  return (
    <Dialog open={open} onClose={onClose} title="Search" size="max-w-xl">
      <div className="-mt-1">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-faint" aria-hidden="true" />
          <input className="input h-10 pl-9" value={q} autoFocus role="combobox" aria-expanded="true" aria-controls="palette-list"
            aria-activedescendant={options[sel] ? `palette-${options[sel].key}` : undefined}
            placeholder="Search documents, reagents, error codes" aria-label="Search documents, reagents and error codes"
            onChange={(e) => { setQ(e.target.value); setSel(0); }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, options.length - 1)); }
              if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
              if (e.key === "Enter") { e.preventDefault(); choose(options[sel]); }
            }} />
        </div>
        <ul id="palette-list" role="listbox" aria-label="Results" className="mt-3 space-y-0.5">
          {options.map((o, i) => (
            <li key={o.key} id={`palette-${o.key}`} role="option" aria-selected={i === sel}
              onMouseEnter={() => setSel(i)} onClick={() => choose(o)}
              className={`flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm ${i === sel ? "bg-subtle" : ""}`}>
              <o.icon className="h-4 w-4 shrink-0 text-muted" strokeWidth={1.6} aria-hidden="true" />
              <span className="truncate">{o.label}</span>
            </li>
          ))}
        </ul>
        <p className="mt-3 text-2xs text-muted">↑ ↓ to move · Enter to open · Esc to close · Ctrl K to reopen</p>
      </div>
    </Dialog>
  );
}

/** Sign-up link for this deployment; keeps the pathname so sub-path hosting still resolves. */
const inviteLink = () => `${window.location.origin}${window.location.pathname}#/signup`;

/** Share a sign-up link to this workspace by copying it to the clipboard. */
function InviteDialog({ open, onClose }) {
  const { toast } = useApp();
  const [copied, setCopied] = useState(false);
  const inputRef = useRef(null);
  const resetTimer = useRef(null);
  const link = useMemo(() => (open ? inviteLink() : ""), [open]);

  useEffect(() => { if (open) setCopied(false); }, [open]);
  useEffect(() => () => clearTimeout(resetTimer.current), []);

  const copy = async () => {
    clearTimeout(resetTimer.current);
    if (await copyText(link)) {
      setCopied(true);
      toast("Invite link copied to clipboard.", "success");
      resetTimer.current = setTimeout(() => setCopied(false), 2000);
    } else {
      // Clipboard API is blocked (insecure origin or denied permission): select the text for a manual copy.
      inputRef.current?.select();
      toast("Couldn't copy automatically. The link is selected: press Ctrl C to copy it.", "error");
    }
  };

  return (
    <Dialog open={open} onClose={onClose} title="Invite to SmartLab" description="Share this link with a colleague so they can create an account."
      footer={<button className="btn-secondary" onClick={onClose}>Done</button>}>
      <Field label="Invite link">
        {(id) => (
          <div className="flex gap-2">
            <div className="relative min-w-0 flex-1">
              <Link2 className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" aria-hidden="true" />
              <input ref={inputRef} id={id} type="text" readOnly value={link} onFocus={(e) => e.target.select()}
                className="input bg-subtle pl-9 font-mono text-xs text-ink" spellCheck={false} />
            </div>
            <button type="button" onClick={copy} aria-label={copied ? "Invite link copied" : "Copy invite link"}
              className={`btn-primary w-28 shrink-0 justify-center ${copied ? "!bg-success !text-white" : ""}`}>
              {copied ? <Check className="h-4 w-4" aria-hidden="true" /> : <Copy className="h-4 w-4" aria-hidden="true" />}
              {copied ? "Copied!" : "Copy"}
            </button>
          </div>
        )}
      </Field>
      <p className="sr-only" role="status" aria-live="polite">{copied ? "Invite link copied to clipboard" : ""}</p>
      <p className="mt-3 text-xs text-muted">Anyone with this link can sign up. New accounts get their own sign-in; workspace roles aren&rsquo;t assigned from the link.</p>
    </Dialog>
  );
}

function TopBar({ current, route, label }) {
  const { threadTitle } = useApp();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);

  useEffect(() => {
    const onKey = (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPaletteOpen(true); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const inThread = current === "ask" && route.path[1];
  const home = current === "overview" || (current === "ask" && !route.path[1]);
  let center = null;
  if (home) center = <CollectionButton />;
  else if (inThread) center = <Breadcrumb>{threadTitle || "New thread"}</Breadcrumb>;
  else if (label) center = <Breadcrumb>{label}</Breadcrumb>;

  return (
    <header className="flex h-[52px] shrink-0 items-center gap-2 border-b border-line bg-surface px-3 sm:px-4">
      <div className="flex shrink-0 items-center gap-1.5">
        <ScopePill />
        <ModePill />
      </div>
      <div className="hidden min-w-0 flex-1 justify-center px-2 sm:flex">{center}</div>
      <div className="flex-1 sm:hidden" />
      <div className="flex shrink-0 items-center gap-1.5">
        <HandsFreeToggle />
        <button type="button" className="pill" onClick={() => setPaletteOpen(true)} aria-label="Search thread and records (Ctrl K)">
          <Search aria-hidden="true" /><span className="hidden lg:inline">Search thread</span>
        </button>
        <button type="button" className="pill" onClick={() => setInviteOpen(true)} aria-label="Invite">
          <UserPlus aria-hidden="true" /><span className="hidden lg:inline">Invite</span>
        </button>
        <button type="button" className="btn-dark" onClick={() => navigate("overview")} aria-label="New thread">
          <Plus className="h-3.5 w-3.5" aria-hidden="true" /><span className="hidden sm:inline">New thread</span>
        </button>
      </div>
      <SearchPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
      <InviteDialog open={inviteOpen} onClose={() => setInviteOpen(false)} />
    </header>
  );
}

/* ------------------------------------------------------------------------- the shell */
export default function AppShell({ current, route, title, children, aside }) {
  useEffect(() => { document.title = title ? `${title} · SmartLab AI` : "SmartLab AI"; }, [title]);
  return (
    <VoiceProvider pages={ALL_NAV}>
      <div className="h-full p-0 sm:p-3 lg:p-4">
        <SkipLink target="main" />
        <div className="flex h-full overflow-hidden border-line bg-surface shadow-panel sm:rounded-frame sm:border">
          <Rail current={current} />
          <div className="flex min-w-0 flex-1 flex-col">
            <TopBar current={current} route={route} label={title} />
            <div className="flex min-h-0 flex-1 bg-canvas">
              <main id="main" tabIndex={-1} className="min-w-0 flex-1 overflow-y-auto outline-none">{children}</main>
              {aside}
            </div>
          </div>
        </div>
        <VoiceDock />
      </div>
    </VoiceProvider>
  );
}
