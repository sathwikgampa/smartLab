import { useEffect, useRef, useState } from "react";
import { Download, FolderUp, ImageIcon, NotebookPen, Sparkles } from "lucide-react";
import { useApp } from "../context/AppContext";
import { api } from "../lib/api";
import { navigate } from "../lib/router";
import { Badge, Dialog, EmptyState, ErrorState, Field, Notice, Page, PageHeader, SegmentedControl, Skeleton, Spinner, useAsync } from "../components/ui";

const SUGGESTIONS = [
  "CRISPR-Cas9 gene editing mechanism at the target DNA strand",
  "Fume hood chemical vapor flow with sash at working height",
  "Phospholipid bilayer cross-section with embedded membrane proteins",
  "Western blot transfer stack, exploded view",
  "Mitochondrial electron transport chain complexes I-IV",
  "Rotary evaporator setup with condenser and receiving flask",
];
const STYLE_LABELS = { diagram: "Schematic", illustration: "Illustration", photorealistic: "Photo-real" };
const RATIO_LABELS = { "1:1": "Square", "16:9": "Wide", "9:16": "Tall" };
// Shown in turn while the provider renders (typically 10-40 s); the request itself reports no progress.
const STAGES = [
  "Parsing scientific description…", "Composing figure layout…", "Rendering molecular schematic…",
  "Resolving structural detail…", "Balancing colour coding…", "Finalising high-resolution render…",
];

function useStage(active) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!active) return undefined;
    setTick(0);
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [active]);
  return { seconds: tick, label: STAGES[Math.min(Math.floor(tick / 4), STAGES.length - 1)] };
}

/** Pick a project or experiment, then run `onPick(id)`. */
function PickerDialog({ open, onClose, title, description, label, load, optionLabel, onPick, empty }) {
  const items = useAsync(() => (open ? load() : Promise.resolve([])), [open]);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (items.data?.length && !value) setValue(String(items.data[0].id)); }, [items.data, value]);
  const submit = async () => {
    setBusy(true);
    try { await onPick(Number(value)); onClose(); } finally { setBusy(false); }
  };
  return (
    <Dialog open={open} onClose={onClose} title={title} description={description}
      footer={<>
        <button className="btn-secondary" onClick={onClose}>Cancel</button>
        <button className="btn-primary" disabled={!value || busy} onClick={submit}>{busy && <Spinner />} Confirm</button>
      </>}>
      {items.loading ? <Skeleton className="h-10 w-full" /> : items.error ? <ErrorState error={items.error} onRetry={items.reload} /> :
        !items.data.length ? <p className="text-sm text-muted">{empty}</p> : (
          <Field label={label}>{(id) => (
            <select id={id} className="input" value={value} onChange={(e) => setValue(e.target.value)}>
              {items.data.map((it) => <option key={it.id} value={it.id}>{optionLabel(it)}</option>)}
            </select>
          )}</Field>
        )}
    </Dialog>
  );
}

function Preview({ image, onSave, onAttach }) {
  const [loaded, setLoaded] = useState(false);
  useEffect(() => setLoaded(false), [image.id]);
  return (
    <div className="card overflow-hidden">
      <div className="relative flex items-center justify-center bg-canvas">
        {!loaded && <Skeleton className="absolute inset-0 rounded-none" />}
        <img src={api.generatedImageUrl(image.id)} alt={image.prompt} onLoad={() => setLoaded(true)}
          className="max-h-[32rem] w-full object-contain" style={{ aspectRatio: `${image.width} / ${image.height}` }} />
      </div>
      <div className="space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="info"><Sparkles className="h-3 w-3" aria-hidden="true" /> AI-generated</Badge>
          <span className="meta">#{image.id} · {image.model} · {image.width}×{image.height} · {(image.duration_ms / 1000).toFixed(1)} s · {image.created_at} UTC</span>
        </div>
        <p className="text-sm">{image.prompt}</p>
        <p className="text-xs text-muted">{image.notice}</p>
        <div className="flex flex-wrap gap-2">
          <a className="btn-secondary btn-sm" href={api.generatedImageUrl(image.id, { download: true })} download>
            <Download className="h-3.5 w-3.5" /> Download PNG
          </a>
          <button className="btn-secondary btn-sm" onClick={onAttach}><NotebookPen className="h-3.5 w-3.5" /> Insert into lab notes</button>
          <button className="btn-secondary btn-sm" onClick={onSave}><FolderUp className="h-3.5 w-3.5" /> Save to team workspace</button>
        </div>
      </div>
    </div>
  );
}

export default function VisualizerView() {
  const { toast } = useApp();
  const info = useAsync(() => api.visualizerInfo(), []);
  const history = useAsync(() => api.generatedImages({ limit: 24 }), []);
  const [prompt, setPrompt] = useState("");
  const [style, setStyle] = useState("diagram");
  const [ratio, setRatio] = useState("1:1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [current, setCurrent] = useState(null);
  const [dialog, setDialog] = useState(null); // "project" | "experiment"
  const promptRef = useRef(null);
  const stage = useStage(busy);

  const max = info.data?.max_prompt_chars ?? 1000;
  const enabled = info.data?.enabled;
  const trimmed = prompt.trim();
  const canSubmit = enabled && !busy && trimmed.length >= 8 && prompt.length <= max;

  useEffect(() => { if (!current && history.data?.length) setCurrent(history.data[0]); }, [history.data, current]);

  const generate = async (e) => {
    e?.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const image = await api.generateImage({ prompt: trimmed, style, aspect_ratio: ratio });
      setCurrent(image);
      history.setData([image, ...(history.data ?? [])]);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const save = async (projectId) => {
    try {
      await api.saveImageToProject(current.id, projectId);
      toast("Saved to the project's shared files.", "success");
    } catch (err) { toast(err.message, "error"); throw err; }
  };
  const attach = async (experimentId) => {
    try {
      await api.attachImageToExperiment(current.id, experimentId);
      toast("Figure inserted into the experiment notes.", "success");
      navigate(`experiments/${experimentId}`);
    } catch (err) { toast(err.message, "error"); throw err; }
  };

  return (
    <Page>
      <PageHeader title="SmartLab Visualizer"
        description="Describe a mechanism, apparatus or structure and generate a figure for protocols, reviews and team discussion." />

      {info.data && !enabled && (
        <Notice tone="warning" title="Image generation is not configured" className="mb-4">
          An administrator needs to set <code>OPENAI_API_KEY</code> or <code>GOOGLE_API_KEY</code> on the server. Earlier generations remain available below.
        </Notice>
      )}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
        <form className="card-pad space-y-4 self-start" onSubmit={generate}>
          <Field label="Scientific description" hint={`${prompt.length}/${max} characters. Figures are rendered without text labels; annotate them after.`}>
            {(id) => (
              <textarea id={id} ref={promptRef} className="input min-h-[8rem] resize-y py-2" value={prompt} maxLength={max}
                placeholder="e.g. Cross-section of a fume hood showing laminar airflow drawing solvent vapour away from the operator"
                onChange={(e) => setPrompt(e.target.value)} disabled={busy}
                onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) generate(e); }} />
            )}
          </Field>
          <div>
            <p className="label">Suggestions</p>
            <div className="flex flex-wrap gap-1.5">
              {SUGGESTIONS.map((s) => (
                <button key={s} type="button" className={`chip ${prompt === s ? "chip-active" : ""}`} disabled={busy}
                  onClick={() => { setPrompt(s); promptRef.current?.focus(); }}>{s}</button>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap gap-4">
            <div><p className="label">Style</p>
              <SegmentedControl label="Style" value={style} onChange={setStyle}
                options={(info.data?.styles ?? Object.keys(STYLE_LABELS)).map((id) => ({ id, label: STYLE_LABELS[id] ?? id }))} /></div>
            <div><p className="label">Aspect ratio</p>
              <SegmentedControl label="Aspect ratio" value={ratio} onChange={setRatio}
                options={(info.data?.aspect_ratios ?? Object.keys(RATIO_LABELS)).map((id) => ({ id, label: RATIO_LABELS[id] ?? id }))} /></div>
          </div>
          <button type="submit" className="btn-primary w-full" disabled={!canSubmit}>
            {busy ? <Spinner /> : <Sparkles className="h-4 w-4" />} {busy ? "Generating…" : "Generate figure"}
          </button>
          {error && (
            <Notice tone={error.status === 422 ? "warning" : "critical"} title={error.status === 422 ? "Prompt not accepted" : "Generation failed"}>
              {error.message}
            </Notice>
          )}
        </form>

        <section aria-label="Preview" aria-live="polite" className="min-w-0">
          {busy ? (
            <div className="card flex min-h-[22rem] flex-col items-center justify-center gap-3 p-6 text-center" role="status">
              <Spinner className="h-6 w-6 text-accent" />
              <p className="font-medium">{stage.label}</p>
              <p className="meta tabular-nums">{stage.seconds} s elapsed · usually 10–40 s</p>
            </div>
          ) : current ? (
            <Preview image={current} onSave={() => setDialog("project")} onAttach={() => setDialog("experiment")} />
          ) : (
            <div className="card"><EmptyState icon={ImageIcon} title="No figure yet">Pick a suggestion or describe what you need, then generate.</EmptyState></div>
          )}
        </section>
      </div>

      <section className="mt-8" aria-label="Your recent figures">
        <h2 className="section-title mb-3">Your recent figures</h2>
        {history.loading && !history.data ? (
          <div className="grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-6">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="aspect-square" />)}</div>
        ) : history.error ? <ErrorState error={history.error} onRetry={history.reload} /> : !history.data?.length ? (
          <p className="text-sm text-muted">Figures you generate appear here.</p>
        ) : (
          <ul className="grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-6">
            {history.data.map((img) => (
              <li key={img.id}>
                <button type="button" onClick={() => setCurrent(img)} title={img.prompt} aria-pressed={current?.id === img.id}
                  className={`block w-full overflow-hidden rounded-lg border bg-canvas transition ${current?.id === img.id ? "border-accent ring-2 ring-accent/30" : "border-line hover:border-accent"}`}>
                  <img src={api.generatedImageUrl(img.id)} alt={img.prompt} loading="lazy" className="aspect-square w-full object-cover" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <PickerDialog open={dialog === "project"} onClose={() => setDialog(null)} title="Save to team workspace"
        description="Adds the PNG to a project's shared files, where the whole team can download it."
        label="Project" load={api.projects} optionLabel={(p) => p.name} onPick={save} empty="No projects yet. Create one under Collaboration." />
      <PickerDialog open={dialog === "experiment"} onClose={() => setDialog(null)} title="Insert into lab notes"
        description="Appends the figure, with an AI-generated caption, to the end of the experiment's protocol."
        label="Experiment" load={api.experiments} optionLabel={(e) => `${e.title}${e.status ? ` (${e.status.replace(/_/g, " ")})` : ""}`}
        onPick={attach} empty="No experiments yet." />
    </Page>
  );
}
