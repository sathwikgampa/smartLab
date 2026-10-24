// Web Speech API helpers for the hands-free voice co-pilot: feature detection, turning answer
// markdown into speakable text, chunking for speechSynthesis, and parsing spoken commands.

export const Recognition = typeof window === "undefined" ? undefined : window.SpeechRecognition || window.webkitSpeechRecognition;
export const synth = typeof window !== "undefined" && "speechSynthesis" in window ? window.speechSynthesis : null;

/** What this browser can do. Chrome and Edge support both; Firefox has synthesis only. */
export function speechSupport() {
  return {
    recognition: !!Recognition,
    synthesis: !!synth,
    // The microphone is only available on https:// or localhost.
    secure: typeof window === "undefined" ? false : window.isSecureContext,
  };
}

// Lab units read the way a scientist would say them ("37 °C" -> "37 degrees Celsius").
const UNITS = [
  [/°\s?C\b/g, " degrees Celsius"], [/°\s?F\b/g, " degrees Fahrenheit"],
  [/(\d)\s*[µu]L\b/g, "$1 microliters"], [/(\d)\s*mL\b/g, "$1 milliliters"],
  [/(\d)\s*[µu]g\b/g, "$1 micrograms"], [/(\d)\s*mg\b/g, "$1 milligrams"],
  [/(\d)\s*[µu]M\b/g, "$1 micromolar"], [/(\d)\s*mM\b/g, "$1 millimolar"], [/(\d)\s*nM\b/g, "$1 nanomolar"],
  [/(\d)\s*rpm\b/gi, "$1 R P M"], [/(\d)\s*[x×]\s?g\b/g, "$1 times g"],
  [/\bPPE\b/g, "P P E"], [/\bSOPs?\b/g, (m) => (m.endsWith("s") ? "S O Ps" : "S O P")],
];

/** Plain, speakable text from answer markdown: no markup, citation markers, URLs or LaTeX delimiters. */
export function toSpeakable(md = "") {
  let text = String(md)
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/\$\$([\s\S]*?)\$\$/g, "$1")
    .replace(/\$([^$\n]+)\$/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\[\d+(?:\s*[,–-]\s*\d+)*\]/g, "")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/\\[a-z]+\{([^}]*)\}/gi, "$1");
  text = text.split("\n")
    .map((line) => line
      .replace(/^\s{0,3}#{1,6}\s+/, "")
      .replace(/^\s*(?:[-*+]|\d+[.)])\s+/, "")
      .replace(/^\s*\|?[\s:|-]+\|?\s*$/, "")
      .replace(/\|/g, ", ")
      .replace(/[*_`~>]+/g, "")
      .trim())
    .filter(Boolean)
    // Each line becomes a sentence so list items and headings get a pause.
    .map((line) => (/[.!?:;]$/.test(line) ? line : `${line}.`))
    .join(" ");
  for (const [re, to] of UNITS) text = text.replace(re, to);
  return text.replace(/\s+([.,;:!?])/g, "$1").replace(/\s{2,}/g, " ").trim();
}

/**
 * Split text into utterance-sized chunks. Chrome silently stops a single utterance after roughly
 * 15 seconds, so long passages are queued as sentences (and long sentences split at commas).
 */
export function chunkForSpeech(text, max = 180) {
  const out = [];
  for (const sentence of text.split(/(?<=[.!?;:])\s+/)) {
    if (sentence.length <= max) { if (sentence.trim()) out.push(sentence); continue; }
    let buf = "";
    for (const word of sentence.split(/(?<=,)\s+|\s+/)) {
      if (buf && buf.length + word.length + 1 > max) { out.push(buf); buf = word; } else buf = buf ? `${buf} ${word}` : word;
    }
    if (buf) out.push(buf);
  }
  return out;
}

const NUMBER_WORDS = {
  one: 1, first: 1, won: 1, two: 2, second: 2, to: 2, too: 2, three: 3, third: 3, four: 4, fourth: 4, for: 4,
  five: 5, fifth: 5, six: 6, sixth: 6, seven: 7, seventh: 7, eight: 8, eighth: 8, ate: 8, nine: 9, ninth: 9,
  ten: 10, tenth: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16,
  seventeen: 17, eighteen: 18, nineteen: 19, twenty: 20,
};
const toNumber = (w) => (/^\d+$/.test(w) ? Number(w) : NUMBER_WORDS[w] ?? null);

// Exact phrases only, so ordinary dictation ("stop the centrifuge after five minutes") is never
// mistaken for a command. An optional wake word ("SmartLab, next step") is stripped first.
const PHRASES = [
  ["stop", ["stop", "stop reading", "stop talking", "quiet", "silence", "cancel", "shut up", "be quiet"]],
  ["pause", ["pause", "pause reading", "hold on", "wait"]],
  ["resume", ["resume", "continue", "keep going", "go on", "carry on", "unpause"]],
  ["next", ["next", "next step", "next one", "go on to the next step", "forward", "skip"]],
  ["prev", ["previous", "previous step", "back", "go back", "last step", "step back"]],
  ["repeat", ["repeat", "again", "say again", "say that again", "repeat that", "repeat step", "repeat the step", "what was that"]],
  ["readAll", ["read", "read it", "read aloud", "read answer", "read the answer", "read that", "read it out", "read everything", "read from the start", "start over", "from the top"]],
  ["send", ["send", "send it", "submit", "send question", "send the question", "ask it", "go", "ask smartlab"]],
  ["clear", ["clear", "clear text", "clear that", "clear it", "delete that", "scratch that", "erase"]],
  ["faster", ["faster", "speed up", "read faster", "talk faster"]],
  ["slower", ["slower", "slow down", "read slower", "talk slower"]],
  ["stopListening", ["stop listening", "mic off", "microphone off", "go to sleep", "sleep", "mute"]],
  ["exit", ["exit hands free", "exit hands-free", "hands free off", "turn off hands free", "turn off hands-free"]],
  ["help", ["help", "what can i say", "commands", "list commands", "voice commands"]],
  ["newThread", ["new thread", "new question", "new conversation", "start a new thread"]],
];
const PHRASE_TO_TYPE = new Map(PHRASES.flatMap(([type, list]) => list.map((p) => [p, type])));
const WAKE_RE = /^(?:(?:hey|ok|okay)\s+)?(?:smart\s?lab|smartlab|computer|co-?pilot)[\s,]+/;

export const COMMAND_LABELS = {
  stop: "Stop reading", pause: "Pause", resume: "Resume", next: "Next step", prev: "Previous step", repeat: "Repeat",
  readAll: "Read answer", send: "Send question", clear: "Clear text", faster: "Faster", slower: "Slower",
  stopListening: "Microphone off", exit: "Hands-free off", help: "Help", newThread: "New thread", step: "Read step", go: "Go to page",
  dictateSend: "Dictate and send",
};

export const normalizeSpeech = (s) => s.toLowerCase().replace(/[^\p{L}\p{N}\s'-]/gu, " ").replace(/\s+/g, " ").trim();

/**
 * Parse one final transcript into a command, or null when it is dictation.
 * Returns { type, label, step?, target?, text? }.
 */
export function parseCommand(raw) {
  const t = normalizeSpeech(raw).replace(WAKE_RE, "").replace(/^please\s+|\s+please$/g, "");
  if (!t) return null;
  const make = (type, extra = {}) => ({ type, label: COMMAND_LABELS[type], ...extra });

  const type = PHRASE_TO_TYPE.get(t);
  if (type) return make(type);

  let m = t.match(/^(?:read |go to |jump to |show )?step (?:number )?(\w+)$/);
  if (m && toNumber(m[1]) != null) return make("step", { step: toNumber(m[1]), label: `Step ${toNumber(m[1])}` });

  m = t.match(/^(?:go to|open|show|show me|navigate to|take me to)\s+(?:the\s+)?(.+?)(?:\s+page)?$/);
  if (m) return make("go", { target: m[1], label: `Go to ${m[1]}` });

  // "How long should trypsin incubate, send it": dictate the question and send it in one breath.
  m = t.match(/^(.{3,})\s+(?:and\s+)?(?:send it|send question|send the question)$/);
  if (m) {
    const text = raw.trim().replace(/[\s,.;:!?]*(?:and\s+)?send(?: it| (?:the )?question)?[\s.!?]*$/i, "");
    return make("dictateSend", { text });
  }
  return null;
}

/** Find a navigation entry for a spoken page name ("reagents", "the safety page", "home"). */
export function matchPage(target, pages) {
  const t = normalizeSpeech(target).replace(/^(?:the|my)\s+/, "").replace(/\s+(?:page|screen|tab)$/, "");
  if (["home", "overview", "start", "dashboard"].includes(t)) return pages.find((p) => p.id === "overview");
  if (["ask", "chat", "assistant", "co-pilot", "copilot"].includes(t)) return pages.find((p) => p.id === "ask");
  const singular = (w) => w.replace(/s$/, "");
  const squash = (s) => normalizeSpeech(s).replace(/[\s-]/g, ""); // "trouble shooting" -> "troubleshooting"
  return pages.find((p) => squash(p.label) === squash(t) || p.id === squash(t))
    ?? pages.find((p) => normalizeSpeech(p.label).split(" ").some((w) => singular(w) === singular(t)))
    ?? pages.find((p) => normalizeSpeech(p.label).includes(t));
}

/** Join dictated phrases into an existing draft with sensible spacing and capitalisation. */
export function appendDictation(draft, phrase) {
  const p = phrase.trim();
  if (!p) return draft;
  if (!draft.trim()) return p.charAt(0).toUpperCase() + p.slice(1);
  return `${draft.replace(/\s+$/, "")} ${p}`;
}
