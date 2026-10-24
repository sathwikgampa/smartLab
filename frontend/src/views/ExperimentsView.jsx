import { useDeferredValue, useEffect, useState } from "react";
import {
  ArrowDown, ArrowUp, CheckCircle2, ClipboardCheck, Eye, FlaskConical, ListChecks, Pencil, Plus, Save, Trash2, Variable, XCircle,
} from "lucide-react";
import { useApp } from "../context/AppContext";
import { api } from "../lib/api";
import CommentThread from "../components/CommentThread";
import { EmptyState, Markdown, PageHeader, SkeletonRows, StatusBadge, useAsync } from "../components/ui";

const CHECKPOINTS = ["Protocol approval", "Safety review", "Data review", "Final sign-off"];

function ProgressBar({ value }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-subtle">
      <div className="h-full rounded-full bg-gradient-to-r from-accent to-accent-strong transition-all" style={{ width: `${value}%` }} />
    </div>
  );
}

function ExperimentDetail({ id, onChanged }) {
  const { toast } = useApp();
  const exp = useAsync(() => api.experiment(id), [id]);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const deferredDraft = useDeferredValue(draft);
  const [step, setStep] = useState("");
  const [variable, setVariable] = useState({ name: "", value: "", unit: "", kind: "controlled" });
  const [decisionNotes, setDecisionNotes] = useState({});
  const [newReview, setNewReview] = useState({ checkpoint: CHECKPOINTS[0], reviewer: "" });
  const profiles = useAsync(() => api.profiles(), []);

  useEffect(() => { if (exp.data) setDraft(exp.data.protocol_markdown); }, [exp.data]);

  const act = async (fn, msg) => {
    try {
      exp.setData(await fn());
      onChanged();
      if (msg) toast(msg, "success");
    } catch (err) { toast(err.message, "error"); }
  };

  if (exp.loading) return <div className="panel p-5"><SkeletonRows rows={8} /></div>;
  if (!exp.data) return null;
  const e = exp.data;
  const move = (idx, dir) => {
    const ids = e.steps.map((s) => s.id);
    const j = idx + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[idx], ids[j]] = [ids[j], ids[idx]];
    act(() => api.reorderSteps(e.id, ids));
  };

  return (
    <div className="space-y-4">
      <div className="panel p-5">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="flex-1 text-base font-semibold">{e.title}</h2>
          <StatusBadge status={e.status} />
          <select className="input w-auto py-1 text-xs" value={e.status}
            onChange={(ev) => act(() => api.updateExperiment(e.id, { status: ev.target.value }), "Status updated.")}>
            {["draft", "in_progress", "in_review", "completed", "archived"].map((s) => <option key={s} value={s}>{s.replace("_", " ")}</option>)}
          </select>
        </div>
        <p className="mt-1 text-sm text-muted">{e.objective}</p>
        <p className="mt-1 text-xs text-muted">{e.project_name} · Owner: {e.owner_name || "-"}</p>
        <div className="mt-3 flex items-center gap-3"><ProgressBar value={e.progress} /><span className="text-xs tabular-nums">{e.progress}%</span></div>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <div className="panel p-5">
          <div className="mb-3 flex items-center justify-between">
            <p className="section-title flex items-center gap-2"><Pencil className="h-4 w-4 text-accent" /> Protocol</p>
            {editing ? (
              <button className="btn-primary py-1 text-xs" onClick={async () => { await act(() => api.updateExperiment(e.id, { protocol_markdown: draft }), "Protocol saved."); setEditing(false); }}>
                <Save className="h-3.5 w-3.5" /> Save
              </button>
            ) : <button className="btn-secondary py-1 text-xs" onClick={() => setEditing(true)}><Pencil className="h-3.5 w-3.5" /> Edit</button>}
          </div>
          {editing ? (
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              <textarea className="input min-h-[16rem] font-mono text-xs" value={draft} onChange={(ev) => setDraft(ev.target.value)} />
              <div className="panel-muted overflow-auto p-3"><p className="label flex items-center gap-1"><Eye className="h-3 w-3" /> Preview</p><Markdown>{deferredDraft}</Markdown></div>
            </div>
          ) : <Markdown>{e.protocol_markdown || "_No protocol drafted yet._"}</Markdown>}
          <p className="mt-2 text-[11px] text-muted">Markdown and LaTeX ($...$) supported.</p>
        </div>

        <div className="panel p-5">
          <p className="section-title mb-3 flex items-center gap-2"><ListChecks className="h-4 w-4 text-accent" /> Checklist</p>
          <ul className="space-y-1">
            {e.steps.map((s, idx) => (
              <li key={s.id} className="group flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-canvas">
                <input type="checkbox" className="h-4 w-4 accent-accent" checked={s.done}
                  onChange={() => act(() => api.updateStep(e.id, s.id, { done: !s.done }))} />
                <span className={`flex-1 text-sm ${s.done ? "text-faint line-through" : ""}`}>{s.text}</span>
                <div className="flex opacity-0 transition group-hover:opacity-100">
                  <button className="btn-ghost p-1" onClick={() => move(idx, -1)} aria-label="Move up"><ArrowUp className="h-3 w-3" /></button>
                  <button className="btn-ghost p-1" onClick={() => move(idx, 1)} aria-label="Move down"><ArrowDown className="h-3 w-3" /></button>
                  <button className="btn-ghost p-1" onClick={() => act(() => api.deleteStep(e.id, s.id))} aria-label="Delete step"><Trash2 className="h-3 w-3 text-critical" /></button>
                </div>
              </li>
            ))}
          </ul>
          <form className="mt-3 flex gap-2" onSubmit={(ev) => { ev.preventDefault(); if (step.trim()) { act(() => api.addStep(e.id, step)); setStep(""); } }}>
            <input className="input" value={step} onChange={(ev) => setStep(ev.target.value)} placeholder="Add a step..." />
            <button className="btn-secondary" aria-label="Add step"><Plus className="h-4 w-4" /></button>
          </form>
        </div>

        <div className="panel p-5">
          <p className="section-title mb-3 flex items-center gap-2"><Variable className="h-4 w-4 text-accent" /> Variable log</p>
          <table className="w-full">
            <thead><tr><th className="table-head">Variable</th><th className="table-head">Value</th><th className="table-head">Type</th><th className="table-head">Logged</th></tr></thead>
            <tbody className="divide-y divide-line">
              {e.variables.map((v) => (
                <tr key={v.id}><td className="table-cell font-medium">{v.name}</td><td className="table-cell tabular-nums">{v.value} {v.unit}</td>
                  <td className="table-cell text-xs capitalize">{v.kind}</td><td className="table-cell text-xs text-muted">{v.logged_at}</td></tr>
              ))}
            </tbody>
          </table>
          <form className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-5" onSubmit={(ev) => {
            ev.preventDefault();
            act(() => api.logVariable(e.id, variable), "Variable logged.");
            setVariable({ name: "", value: "", unit: "", kind: "controlled" });
          }}>
            <input className="input" required placeholder="Name" value={variable.name} onChange={(ev) => setVariable({ ...variable, name: ev.target.value })} />
            <input className="input" required placeholder="Value" value={variable.value} onChange={(ev) => setVariable({ ...variable, value: ev.target.value })} />
            <input className="input" placeholder="Unit" value={variable.unit} onChange={(ev) => setVariable({ ...variable, unit: ev.target.value })} />
            <select className="input" value={variable.kind} onChange={(ev) => setVariable({ ...variable, kind: ev.target.value })}>
              <option value="independent">Independent</option><option value="dependent">Dependent</option><option value="controlled">Controlled</option>
            </select>
            <button className="btn-secondary">Log</button>
          </form>
        </div>

        <div className="panel p-5">
          <p className="section-title mb-3 flex items-center gap-2"><ClipboardCheck className="h-4 w-4 text-warning-ink" /> Expert review checkpoints</p>
          <ol className="space-y-2">
            {e.reviews.map((r) => (
              <li key={r.id} className="panel-muted p-3">
                <div className="flex items-center gap-2">
                  <p className="flex-1 text-sm font-medium">{r.checkpoint}</p><StatusBadge status={r.status} />
                </div>
                <p className="text-xs text-muted">Reviewer: {r.reviewer_name || "Unassigned"}{r.decided_at ? ` · ${r.decided_at}` : ""}</p>
                {r.notes && <p className="mt-1 text-xs italic">"{r.notes}"</p>}
                {r.status === "pending" && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    <input className="input flex-1 py-1 text-xs" placeholder="Reviewer notes (required to reject)"
                      value={decisionNotes[r.id] || ""} onChange={(ev) => setDecisionNotes({ ...decisionNotes, [r.id]: ev.target.value })} />
                    <button className="btn-primary py-1 text-xs" onClick={() => act(() => api.decideReview(e.id, r.id, { decision: "approved", notes: decisionNotes[r.id] }), "Checkpoint approved.")}>
                      <CheckCircle2 className="h-3.5 w-3.5" /> Approve
                    </button>
                    <button className="btn-secondary py-1 text-xs" onClick={() => act(() => api.decideReview(e.id, r.id, { decision: "changes_requested", notes: decisionNotes[r.id] }), "Changes requested.")}>
                      <XCircle className="h-3.5 w-3.5" /> Request changes
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ol>
          <div className="mt-3 flex gap-2">
            <select className="input" value={newReview.checkpoint} onChange={(ev) => setNewReview({ ...newReview, checkpoint: ev.target.value })}>{CHECKPOINTS.map((c) => <option key={c}>{c}</option>)}</select>
            <select className="input" value={newReview.reviewer} onChange={(ev) => setNewReview({ ...newReview, reviewer: ev.target.value })}>
              <option value="">Reviewer...</option>
              {profiles.data?.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.role})</option>)}
            </select>
            <button className="btn-secondary shrink-0" onClick={() => {
              act(() => api.requestReview(e.id, {
                checkpoint: newReview.checkpoint, reviewer_id: newReview.reviewer ? Number(newReview.reviewer) : null,
              }), "Review requested.");
            }}>Request review</button>
          </div>
        </div>
      </div>

      <div className="panel p-5"><CommentThread entityType="experiment" entityId={e.id} /></div>
    </div>
  );
}

export default function ExperimentsView({ route }) {
  const { toast, currentUser } = useApp();
  const list = useAsync(() => api.experiments(), []);
  const projects = useAsync(() => api.projects(), []);
  // "#/experiments/12" opens experiment 12 (e.g. after the Visualizer inserts a figure into its notes).
  const linked = Number(route?.path[1]) || null;
  const [selected, setSelected] = useState(linked);
  useEffect(() => { if (linked) setSelected(linked); }, [linked]);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ title: "", objective: "", project_id: "", steps: "" });

  useEffect(() => { if (!selected && list.data?.length) setSelected(list.data[0].id); }, [list.data, selected]);

  const create = async (ev) => {
    ev.preventDefault();
    try {
      const exp = await api.createExperiment({
        title: form.title, objective: form.objective, owner_id: currentUser.id,
        project_id: form.project_id ? Number(form.project_id) : null,
        steps: form.steps.split("\n").map((s) => s.trim()).filter(Boolean),
        protocol_markdown: "## Protocol\n1. ",
      });
      setCreating(false);
      setForm({ title: "", objective: "", project_id: "", steps: "" });
      await list.reload();
      setSelected(exp.id);
      toast("Experiment created.", "success");
    } catch (err) { toast(err.message, "error"); }
  };

  return (
    <div className="mx-auto max-w-7xl p-6">
      <PageHeader title="Experiment planning" subtitle="Protocols, checklists, variable logs and review checkpoints."
        actions={<button className="btn-primary" onClick={() => setCreating((c) => !c)}><Plus className="h-4 w-4" /> New experiment</button>} />
      {creating && (
        <form onSubmit={create} className="panel mb-5 grid grid-cols-1 gap-3 p-5 md:grid-cols-2">
          <div><label className="label">Title</label><input className="input" required value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></div>
          <div><label className="label">Project</label>
            <select className="input" value={form.project_id} onChange={(e) => setForm({ ...form, project_id: e.target.value })}>
              <option value="">-</option>{projects.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select></div>
          <div className="md:col-span-2"><label className="label">Objective</label><input className="input" value={form.objective} onChange={(e) => setForm({ ...form, objective: e.target.value })} /></div>
          <div className="md:col-span-2"><label className="label">Checklist steps (one per line)</label>
            <textarea className="input" rows={4} value={form.steps} onChange={(e) => setForm({ ...form, steps: e.target.value })} /></div>
          <div className="flex justify-end gap-2 md:col-span-2">
            <button type="button" className="btn-secondary" onClick={() => setCreating(false)}>Cancel</button><button className="btn-primary">Create</button>
          </div>
        </form>
      )}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <div className="space-y-2">
          {list.loading ? <SkeletonRows rows={3} /> : list.data?.length === 0 ? <EmptyState icon={FlaskConical} title="No experiments" /> :
            list.data.map((x) => (
              <button key={x.id} onClick={() => setSelected(x.id)}
                className={`panel w-full p-3 text-left transition ${selected === x.id ? "ring-2 ring-accent/60" : "hover:border-accent/40"}`}>
                <div className="flex items-start justify-between gap-2"><p className="text-sm font-medium">{x.title}</p><StatusBadge status={x.status} /></div>
                <p className="mt-1 text-xs text-muted">{x.project_name || "No project"}</p>
                <div className="mt-2"><ProgressBar value={x.progress} /></div>
              </button>
            ))}
        </div>
        {selected && <ExperimentDetail key={selected} id={selected} onChanged={list.reload} />}
      </div>
    </div>
  );
}
