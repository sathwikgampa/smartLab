import { useState } from "react";
import { Activity, ClipboardCheck, FileText, Flag, MessageSquare, Upload } from "lucide-react";
import { useApp } from "../context/AppContext";
import { api } from "../lib/api";
import { answerBody, formatDateTime, timeAgo } from "../lib/format";
import { Badge, EmptyState, ErrorState, Markdown, Page, PageHeader, SegmentedControl, SkeletonRows, Spinner, StatusBadge, useAsync } from "../components/ui";

const KIND = {
  comment: [MessageSquare, "Comment"], file: [Upload, "File shared"], document: [FileText, "Document added"],
  review: [ClipboardCheck, "Experiment review"], audit: [Activity, "Change"],
};
const RATING = { incorrect: ["warning", "Incorrect"], unsafe: ["critical", "Potentially unsafe"], outdated: ["warning", "Outdated"] };

function Feed({ scope }) {
  const feed = useAsync(() => api.activity(undefined, { mine: scope === "mine" }), [scope]);
  if (feed.error) return <ErrorState error={feed.error} onRetry={feed.reload} />;
  if (feed.loading) return <SkeletonRows rows={8} />;
  if (!feed.data.length) {
    return scope === "mine" ? (
      <EmptyState icon={Activity} title="No recent activity for your account yet.">
        Comments you post, files you share, reviews assigned to you and changes you make appear here.
      </EmptyState>
    ) : <EmptyState icon={Activity} title="No lab activity yet" />;
  }
  return (
    <ol className="card divide-y divide-line">
      {feed.data.map((i) => {
        const [Icon, label] = KIND[i.kind] ?? KIND.audit;
        return (
          <li key={`${i.kind}-${i.id}`} className="flex gap-3 px-4 py-3">
            <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <p className="text-sm">{i.summary}</p>
              <p className="meta">{label}{i.actor ? ` · ${i.actor}` : ""} · <time title={formatDateTime(i.at)}>{timeAgo(i.at)}</time></p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function ReviewItem({ item, onResolved }) {
  const { toast, openEvidence } = useApp();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [tone, label] = RATING[item.rating] ?? ["neutral", item.rating];
  const resolve = async (e) => {
    e.preventDefault();
    setBusy(true);
    try { await api.resolveFeedback(item.id, note); toast("Marked as resolved.", "success"); onResolved(); } catch (err) { toast(err.message, "error"); } finally { setBusy(false); }
  };
  return (
    <li className="card-pad space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={tone}><Flag className="h-3 w-3" /> {label}</Badge>
        <StatusBadge status={item.status} />
        <span className="meta">Reported {timeAgo(item.created_at)}</span>
        <a className="link ml-auto text-xs" href={`#/ask/${item.session_id}`}>Open conversation</a>
      </div>
      <div><p className="eyebrow mb-1">Question</p><p className="text-sm font-medium">{item.question ?? "(not recorded)"}</p></div>
      <div><p className="eyebrow mb-1">Reporter's note</p><p className="text-sm">{item.comment}</p></div>
      <details className="rounded-lg border border-line">
        <summary className="cursor-pointer px-3 py-2 text-xs font-medium">Answer that was flagged</summary>
        <div className="border-t border-line px-3 py-2">
          <Markdown citations={item.citations} onCite={(c) => openEvidence(c, item.citations)}>{answerBody(item.answer)}</Markdown>
        </div>
      </details>
      {item.status === "open" ? (
        <form onSubmit={resolve} className="flex flex-col gap-2 sm:flex-row">
          <label htmlFor={`res-${item.id}`} className="sr-only">Resolution note</label>
          <input id={`res-${item.id}`} className="input" value={note} onChange={(e) => setNote(e.target.value)} required
            placeholder="What was done, e.g. SOP corrected and re-approved as v3.2" />
          <button className="btn-primary shrink-0" disabled={busy || !note.trim()}>{busy && <Spinner />} Resolve</button>
        </form>
      ) : <p className="text-sm"><span className="text-muted">Resolution: </span>{item.resolution}</p>}
    </li>
  );
}

function ReviewQueue() {
  const [status, setStatus] = useState("open");
  const queue = useAsync(() => api.feedbackQueue(status), [status]);
  return (
    <div className="space-y-3">
      <SegmentedControl label="Filter review items" value={status} onChange={setStatus}
        options={[{ id: "open", label: "Open" }, { id: "resolved", label: "Resolved" }, { id: "all", label: "All" }]} />
      {queue.error ? <ErrorState error={queue.error} onRetry={queue.reload} /> : queue.loading ? <SkeletonRows rows={3} className="h-28" /> : queue.data.length === 0 ? (
        <EmptyState icon={Flag} title={status === "open" ? "Nothing waiting for review" : "No items"}>Answers reported as incorrect, unsafe or outdated appear here for an expert to check.</EmptyState>
      ) : <ul className="space-y-3">{queue.data.map((i) => <ReviewItem key={i.id} item={i} onResolved={queue.reload} />)}</ul>}
    </div>
  );
}

export default function ActivityView({ route }) {
  const { currentUser } = useApp();
  const [tab, setTab] = useState(route.query.tab === "review" ? "review" : "activity");
  const [scope, setScope] = useState("mine");
  // The whole lab's feed is for administrators; the API refuses mine=false for everyone else.
  const canSeeLab = currentUser.role === "Admin";
  const shownScope = canSeeLab ? scope : "mine";
  return (
    <Page width="max-w-4xl">
      <PageHeader title="Recent activity" description="Your approvals, changes, uploads and comments, plus answers reported for expert review." />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <SegmentedControl label="Activity sections" value={tab} onChange={setTab}
          options={[{ id: "activity", label: "Activity log" }, { id: "review", label: "Answer review queue" }]} />
        {tab === "activity" && canSeeLab && (
          <SegmentedControl label="Whose activity" value={scope} onChange={setScope}
            options={[{ id: "mine", label: "My Activity" }, { id: "lab", label: "Lab Activity" }]} />
        )}
      </div>
      {tab === "activity" ? <Feed scope={shownScope} /> : <ReviewQueue />}
    </Page>
  );
}
