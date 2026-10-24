import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  AudioLines, ChevronDown, Maximize2, Mic, MicOff, Minimize2, Pause, Play, SkipBack, SkipForward, Square, Volume2, X,
} from "lucide-react";
import { usePref } from "../context/AppContext";
import { navigate } from "../lib/router";
import { Recognition, chunkForSpeech, matchPage, parseCommand, speechSupport, synth, toSpeakable } from "../lib/speech";
import { Spinner } from "./ui";

/*
 * Voice co-pilot / hands-free wet-lab mode, built on the browser's Web Speech API.
 *
 * One VoiceProvider (mounted by AppShell) owns a single SpeechRecognition session and the speech
 * synthesis queue, so listening survives page changes. Components plug into it:
 *   - Composer registers itself as the dictation target (useDictationTarget) and shows <MicButton>.
 *   - Answers register what can be read aloud (useReadable) and show <ReadAloudButton>.
 *   - <HandsFreeToggle> (top bar) turns on <VoiceDock>, the large glove-friendly control panel.
 *
 * @typedef {{ key: string, label?: string, text: string, quiet?: boolean }} ReadablePart
 *   One unit the user can step through (a checklist step, the summary...). `quiet` skips speaking the label.
 * @typedef {{ id: string, title: string, parts: ReadablePart[] }} Readable
 * @typedef {{ append: (text: string) => void, submit: () => void, clear: () => void }} DictationTarget
 */

const VoiceContext = createContext(null);
const TranscriptContext = createContext({ interim: "", last: null });

export const useVoice = () => useContext(VoiceContext);
export const useVoiceTranscript = () => useContext(TranscriptContext);

const RATE_MIN = 0.6;
const RATE_MAX = 1.5;
const AUTO_READ_WINDOW_MS = 3 * 60 * 1000;
// Speech heard this soon after a readout ends is assumed to be the speakers, not the user.
const ECHO_GUARD_MS = 800;
const MAX_FAILURES = 5;
const NO_ANSWER = "There's no answer to read yet. Ask a question first.";
const HELP_TEXT = "You can say: read answer, next step, previous step, repeat, step 3, pause, resume, stop, faster, or slower. "
  + "To ask a question, just speak it, then say send. You can also say go to reagents, new thread, or stop listening.";

const FATAL = {
  "not-allowed": "Microphone access is blocked. Allow it from the site settings in the address bar, then tap the microphone.",
  "service-not-allowed": "This browser's speech service is turned off. Voice input works in Chrome and Edge.",
  "audio-capture": "No microphone was found. Connect one, then tap the microphone.",
  "language-not-supported": "Voice input doesn't support this browser's language. Set the browser language to English and try again.",
};

/** Sentences joined with one terminal punctuation mark each. */
export const joinSentences = (...items) => items.filter(Boolean).map((s) => String(s).trim().replace(/([^.!?:;])$/, "$1.")).join(" ");

/** A readable built from answer markdown: one part per paragraph or list item; numbered items become steps. */
export function markdownReadable(id, title, md = "") {
  const parts = String(md).split(/\n\s*\n|\n(?=\s*(?:[-*+]|\d+[.)])\s)/).map((b) => b.trim()).filter(Boolean).map((block, i) => {
    const n = block.match(/^(\d+)[.)]\s/)?.[1];
    return n ? { key: `step-${n}`, label: `Step ${n}`, text: block.replace(/^\d+[.)]\s+/, "") } : { key: `p-${i}`, label: `Part ${i + 1}`, text: block, quiet: true };
  });
  return { id, title, parts };
}

function pickDefaultVoice(voices) {
  const norm = (l) => (l || "").toLowerCase().replace("_", "-");
  const lang = norm(navigator.language || "en-US");
  const same = voices.filter((v) => norm(v.lang) === lang);
  const family = voices.filter((v) => norm(v.lang).startsWith(lang.split("-")[0]));
  const pool = same.length ? same : family.length ? family : voices;
  // Edge's "Natural"/"Online" and Chrome's Google voices are far clearer over fume-hood noise.
  return pool.find((v) => /natural|neural|online/i.test(v.name)) ?? pool.find((v) => /google/i.test(v.name))
    ?? pool.find((v) => v.default) ?? pool[0] ?? null;
}

let audioCtx = null;
/** Short tone so a recognised command (or a miss) is acknowledged without looking at the screen. */
function chime(ok = true) {
  try {
    audioCtx ??= new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    const t = audioCtx.currentTime;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.frequency.setValueAtTime(ok ? 880 : 330, t);
    osc.frequency.setValueAtTime(ok ? 1320 : 260, t + 0.07);
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.08, t + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(t);
    osc.stop(t + 0.2);
  } catch { /* no audio output available */ }
}

/* ------------------------------------------------------------------------------ provider */
/** @param {{ pages?: { id: string, label: string }[], children: import("react").ReactNode }} props */
export function VoiceProvider({ pages = [], children }) {
  const support = useMemo(speechSupport, []);
  const [handsFree, setHandsFreePref] = usePref("smartlab-voice-hands-free", false);
  const [rate, setRate] = usePref("smartlab-voice-rate", 0.95);
  const [voiceURI, setVoiceURI] = usePref("smartlab-voice", "");
  const [voices, setVoices] = useState([]);
  const [mic, setMic] = useState("idle"); // idle | starting | listening | error
  const [micError, setMicError] = useState(null);
  const [reading, setReading] = useState(null); // { id, key, label, part, total, title, paused, single }
  const [transcript, setTranscript] = useState({ interim: "", last: null });
  const [latestId, setLatestId] = useState(null);
  const [hasTarget, setHasTarget] = useState(false);

  const recRef = useRef(null);
  const wantRef = useRef(false);
  const failRef = useRef(0);
  const startedAt = useRef(0);
  const retryTimer = useRef(null);
  const readables = useRef(new Map());
  const latestRef = useRef(null);
  const cursor = useRef(null); // { id, part }: the part read most recently, for next / previous / repeat
  const genRef = useRef(0); // bumps on every cancel, so stale utterance events are ignored
  const queueRef = useRef([]); // Chrome drops onend for utterances that get garbage-collected
  const nowRef = useRef(null); // { readable, single } being read, for resume
  const readingRef = useRef(null);
  const lastSpeechEnd = useRef(0);
  const targetRef = useRef(null);
  const expectAnswer = useRef(0);

  readingRef.current = reading;
  const voice = useMemo(() => voices.find((v) => v.voiceURI === voiceURI) ?? pickDefaultVoice(voices), [voices, voiceURI]);
  const settings = useRef({});
  settings.current = { rate, voice, handsFree };

  useEffect(() => {
    if (!synth) return undefined;
    const load = () => setVoices(synth.getVoices());
    load();
    synth.addEventListener?.("voiceschanged", load);
    return () => synth.removeEventListener?.("voiceschanged", load);
  }, []);

  /* ---------------------------------------------------------------- speech synthesis */
  const speak = useCallback((readable, start = 0, { single = false, transient = false } = {}) => {
    if (!synth || !readable?.parts.length) return;
    const gen = ++genRef.current;
    synth.cancel();
    if (synth.paused) synth.resume();
    const { rate: r, voice: v } = settings.current;
    const end = single ? start + 1 : readable.parts.length;
    const show = (i) => {
      const p = readable.parts[i];
      if (!transient) cursor.current = { id: readable.id, part: i };
      setReading({ id: readable.id, key: p.key, label: p.label || readable.title, part: i, total: readable.parts.length, title: readable.title, paused: false, single });
    };
    const queue = [];
    for (let i = start; i < end; i++) {
      const p = readable.parts[i];
      chunkForSpeech(toSpeakable(p.label && !p.quiet ? `${p.label}. ${p.text}` : p.text)).forEach((chunk, ci) => {
        const u = new SpeechSynthesisUtterance(chunk);
        u.rate = r;
        if (v) { u.voice = v; u.lang = v.lang; }
        if (ci === 0) u.onstart = () => { if (gen === genRef.current) show(i); };
        queue.push(u);
      });
    }
    if (!queue.length) return;
    const finish = () => {
      if (gen !== genRef.current) return;
      genRef.current++;
      lastSpeechEnd.current = Date.now();
      nowRef.current = null;
      setReading(null);
    };
    queue.forEach((u) => { u.onerror = (e) => { if (e.error !== "interrupted" && e.error !== "canceled") { synth.cancel(); finish(); } }; });
    queue[queue.length - 1].onend = finish;
    queueRef.current = queue;
    nowRef.current = { readable, single };
    show(start); // show progress straight away; a network voice can take a moment to start
    queue.forEach((u) => synth.speak(u));
  }, []);

  const say = useCallback((text) => speak({ id: "voice-feedback", title: "SmartLab", parts: [{ key: "msg", text }] }, 0, { transient: true }), [speak]);

  const stopSpeaking = useCallback(() => {
    genRef.current++;
    synth?.cancel();
    lastSpeechEnd.current = Date.now();
    nowRef.current = null;
    setReading(null);
  }, []);

  // Pause stops at the current part and resume restarts that part: synthesis pause() is unreliable
  // with Chrome's network voices, and hearing a whole step again is what you want at the bench anyway.
  const pause = useCallback(() => {
    if (!readingRef.current || readingRef.current.paused) return;
    genRef.current++;
    synth?.cancel();
    lastSpeechEnd.current = Date.now();
    setReading((r) => r && { ...r, paused: true });
  }, []);

  const resume = useCallback(() => {
    const r = readingRef.current;
    if (r?.paused && nowRef.current) speak(nowRef.current.readable, r.part, { single: nowRef.current.single });
  }, [speak]);

  const speakReadable = useCallback((id, start = 0, opts) => {
    const r = readables.current.get(id);
    if (!r) return;
    const i = typeof start === "string" ? r.parts.findIndex((p) => p.key === start) : start;
    if (i >= 0 && i < r.parts.length) speak(r, i, opts);
  }, [speak]);

  const speakText = useCallback((id, text, title = "SmartLab") => speak({ id, title, parts: [{ key: "text", text, quiet: true }] }, 0), [speak]);

  /* -------------------------------------------------------------- what can be read / typed */
  const registerReadable = useCallback((readable) => {
    if (cursor.current && cursor.current.id !== readable.id && !readables.current.has(readable.id)) cursor.current = null;
    readables.current.set(readable.id, readable);
    latestRef.current = readable.id;
    setLatestId(readable.id);
    // In hands-free mode, read a new answer to the question that was just asked.
    if (settings.current.handsFree && expectAnswer.current && Date.now() - expectAnswer.current < AUTO_READ_WINDOW_MS) {
      expectAnswer.current = 0;
      speak(readable, 0);
    }
    return () => {
      if (readables.current.get(readable.id) !== readable) return;
      readables.current.delete(readable.id);
      if (latestRef.current === readable.id) {
        latestRef.current = [...readables.current.keys()].pop() ?? null;
        setLatestId(latestRef.current);
      }
    };
  }, [speak]);

  const registerTarget = useCallback((target) => {
    targetRef.current = target;
    setHasTarget(true);
    return () => { if (targetRef.current === target) { targetRef.current = null; setHasTarget(false); } };
  }, []);

  /** Called by the composer on every send, so the answer is read out when it arrives. */
  const noteQuestion = useCallback(() => {
    expectAnswer.current = Date.now();
    if (readingRef.current) stopSpeaking();
  }, [stopSpeaking]);

  /* ------------------------------------------------------------------------ commands */
  const activeReadable = () => {
    const id = cursor.current?.id;
    return (id && readables.current.get(id)) || readables.current.get(latestRef.current) || null;
  };

  const runCommand = (cmd) => {
    const target = targetRef.current;
    const r = activeReadable();
    const cur = r && cursor.current?.id === r.id ? cursor.current.part : -1;
    switch (cmd.type) {
      case "stop": return stopSpeaking();
      case "pause": return pause();
      case "resume": return readingRef.current?.paused ? resume() : r ? speak(r, Math.max(0, cur)) : say(NO_ANSWER);
      case "readAll": {
        const latest = readables.current.get(latestRef.current);
        return latest ? speak(latest, 0) : say(NO_ANSWER);
      }
      case "next":
        if (!r) return say(NO_ANSWER);
        return cur + 1 >= r.parts.length ? say("That was the last step.") : speak(r, cur + 1, { single: true });
      case "prev": return r ? speak(r, Math.max(0, cur - 1), { single: true }) : say(NO_ANSWER);
      case "repeat": return r ? speak(r, Math.max(0, cur), { single: true }) : say(NO_ANSWER);
      case "step": {
        if (!r) return say(NO_ANSWER);
        const i = r.parts.findIndex((p) => p.key === `step-${cmd.step}`);
        return i < 0 ? say(`There is no step ${cmd.step} in this answer.`) : speak(r, i, { single: true });
      }
      case "send": return target ? target.submit() : say("Open Ask SmartLab to send a question.");
      case "dictateSend":
        if (!target) return say("Open Ask SmartLab to send a question.");
        target.append(cmd.text);
        return target.submit();
      case "clear": return target?.clear();
      case "faster":
      case "slower": {
        const next = Math.min(RATE_MAX, Math.max(RATE_MIN, Math.round((settings.current.rate + (cmd.type === "faster" ? 0.15 : -0.15)) * 100) / 100));
        setRate(next);
        settings.current.rate = next;
        const now = readingRef.current;
        if (now && !now.paused && nowRef.current) speak(nowRef.current.readable, now.part, { single: nowRef.current.single });
        return undefined;
      }
      case "stopListening": return stopListening(); // eslint-disable-line no-use-before-define
      case "exit": return setHandsFree(false); // eslint-disable-line no-use-before-define
      case "help": return say(HELP_TEXT);
      case "newThread": return navigate("overview");
      case "go": {
        const page = matchPage(cmd.target, pages);
        return page ? navigate(page.id) : say(`I couldn't find a page called ${cmd.target}.`);
      }
      default: return undefined;
    }
  };

  const onFinal = (raw) => {
    const text = raw.trim();
    if (!text) return;
    const cmd = parseCommand(text);
    if (cmd) {
      chime(true);
      setTranscript({ interim: "", last: { text, command: cmd.label } });
      runCommand(cmd);
      return;
    }
    // While (or just after) SmartLab is talking, the microphone hears the speakers: commands only.
    const echo = (synth?.speaking && !readingRef.current?.paused) || Date.now() - lastSpeechEnd.current < ECHO_GUARD_MS;
    if (echo) { setTranscript((t) => ({ ...t, interim: "" })); return; }
    if (targetRef.current) {
      targetRef.current.append(text);
      setTranscript({ interim: "", last: { text, command: null } });
    } else {
      chime(false);
      setTranscript({ interim: "", last: { text, command: null, unrouted: true } });
    }
  };

  // Recognition callbacks are bound once; they reach the current render's handlers through this ref.
  const handlers = useRef(null);
  handlers.current = { onFinal, runCommand };
  const command = useCallback((cmd) => handlers.current.runCommand(cmd), []);

  /* -------------------------------------------------------------- speech recognition */
  const begin = useCallback(() => {
    clearTimeout(retryTimer.current);
    if (!Recognition || recRef.current || !wantRef.current) return;
    const rec = new Recognition();
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    rec.lang = navigator.language || "en-US";
    rec.onstart = () => { setMic("listening"); setMicError(null); };
    rec.onresult = (e) => {
      failRef.current = 0;
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) handlers.current.onFinal(res[0].transcript);
        else interim += res[0].transcript;
      }
      if (interim) setTranscript((t) => ({ ...t, interim }));
    };
    rec.onerror = (e) => {
      if (e.error === "no-speech" || e.error === "aborted") return; // silence: onend restarts
      if (FATAL[e.error]) {
        wantRef.current = false;
        setMicError(FATAL[e.error]);
        setMic("error");
        chime(false);
        return;
      }
      failRef.current += 1; // "network" and other transient failures: retried with backoff in onend
      if (e.error === "network") setMicError("Speech service unreachable. Retrying...");
    };
    rec.onend = () => {
      recRef.current = null;
      setTranscript((t) => (t.interim ? { ...t, interim: "" } : t));
      if (!wantRef.current) { setMic((m) => (m === "error" ? m : "idle")); return; }
      // Chrome ends continuous sessions after silence or about a minute: restart. A session that ends
      // straight away (mic taken by another tab, service down) counts as a failure.
      if (Date.now() - startedAt.current < 1000) failRef.current += 1;
      if (failRef.current > MAX_FAILURES) {
        wantRef.current = false;
        setMic("error");
        setMicError("Voice input keeps stopping. Chrome and Edge transcribe online: check the connection, then tap the microphone.");
        return;
      }
      setMic("starting");
      retryTimer.current = setTimeout(begin, failRef.current ? Math.min(500 * 2 ** failRef.current, 8000) : 150);
    };
    recRef.current = rec;
    startedAt.current = Date.now();
    setMic("starting");
    try { rec.start(); } catch { recRef.current = null; retryTimer.current = setTimeout(begin, 500); }
  }, []);

  const startListening = useCallback(() => {
    if (!support.recognition) return;
    if (!support.secure) {
      setMic("error");
      setMicError("The microphone only works on a secure page (https:// or localhost).");
      return;
    }
    wantRef.current = true;
    failRef.current = 0;
    setMicError(null);
    chime(true); // also unlocks audio output on this user gesture
    begin();
  }, [begin, support]);

  const stopListening = useCallback(() => {
    wantRef.current = false;
    clearTimeout(retryTimer.current);
    if (recRef.current) recRef.current.stop(); // stop() still delivers the last phrase; abort() would drop it
    else setMic("idle");
    setMicError(null);
    setTranscript((t) => ({ ...t, interim: "" }));
  }, []);

  const toggleListening = useCallback(() => {
    if (wantRef.current) stopListening(); else startListening();
  }, [startListening, stopListening]);

  const setHandsFree = useCallback((on) => {
    setHandsFreePref(on);
    if (on) startListening();
    else { stopListening(); stopSpeaking(); }
  }, [setHandsFreePref, startListening, stopListening, stopSpeaking]);

  // After a reload in hands-free mode, resume listening if the microphone was already allowed.
  useEffect(() => {
    if (!handsFree || !support.recognition || !support.secure) return;
    navigator.permissions?.query({ name: "microphone" })
      .then((p) => { if (p.state === "granted" && !wantRef.current) startListening(); })
      .catch(() => { /* permission query unsupported: wait for a tap */ });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Release the microphone and silence the speakers when the shell unmounts (sign-out) or the page goes away.
  useEffect(() => {
    const onHide = () => synth?.cancel();
    window.addEventListener("pagehide", onHide);
    return () => {
      window.removeEventListener("pagehide", onHide);
      wantRef.current = false;
      clearTimeout(retryTimer.current);
      recRef.current?.abort();
      recRef.current = null;
      genRef.current++;
      synth?.cancel();
    };
  }, []);

  const value = useMemo(() => ({
    support, handsFree, setHandsFree, rate, setRate, voices, voice, setVoiceURI,
    mic, micError, reading, latestId, hasTarget,
    startListening, stopListening, toggleListening, command,
    speakReadable, speakText, stopSpeaking, pause, resume,
    registerReadable, registerTarget, noteQuestion,
  }), [support, handsFree, setHandsFree, rate, setRate, voices, voice, setVoiceURI, mic, micError, reading, latestId, hasTarget,
    startListening, stopListening, toggleListening, command, speakReadable, speakText, stopSpeaking, pause, resume,
    registerReadable, registerTarget, noteQuestion]);

  return (
    <VoiceContext.Provider value={value}>
      <TranscriptContext.Provider value={transcript}>{children}</TranscriptContext.Provider>
    </VoiceContext.Provider>
  );
}

/* ---------------------------------------------------------------------------- hooks */
/** Make an answer available to "read answer", "next step" and the dock. Last registered = latest answer. */
export function useReadable(readable) {
  const register = useVoice()?.registerReadable;
  const key = readable?.parts.length ? JSON.stringify(readable) : "";
  useEffect(() => (register && key ? register(readable) : undefined), [register, key]); // eslint-disable-line react-hooks/exhaustive-deps
}

/** Route dictated speech into an input. The handlers may change every render. */
export function useDictationTarget(target) {
  const register = useVoice()?.registerTarget;
  const ref = useRef(target);
  ref.current = target;
  useEffect(() => register?.({
    append: (t) => ref.current.append(t),
    submit: () => ref.current.submit(),
    clear: () => ref.current.clear(),
  }), [register]);
}

/* ------------------------------------------------------------------------- controls */
/** Microphone toggle for the prompt bar. Hidden when the browser has no speech recognition. */
export function MicButton() {
  const v = useVoice();
  const { interim } = useVoiceTranscript();
  if (!v?.support.recognition) return null;
  const { mic } = v;
  const on = mic === "listening" || mic === "starting";
  const hearing = mic === "listening" && interim;
  return (
    <button type="button" onClick={v.toggleListening} aria-pressed={on}
      aria-label={on ? "Stop voice input" : "Speak your question"}
      title={v.micError ?? (on ? "Listening: speak, then say “send”" : "Speak your question")}
      className={`relative flex h-9 w-9 shrink-0 items-center justify-center rounded-full transition-colors ${
        hearing ? "bg-success text-white"
          : mic === "listening" ? "voice-pulse bg-critical text-white"
            : mic === "starting" ? "bg-critical/75 text-white"
              : mic === "error" ? "bg-warning-soft text-warning-ink hover:bg-warning-soft/70"
                : "text-ink hover:bg-subtle"}`}>
      {mic === "starting" ? <Spinner className="h-4 w-4" />
        : mic === "error" ? <MicOff className="h-[18px] w-[18px]" strokeWidth={1.7} aria-hidden="true" />
          : hearing ? <AudioLines className="h-[18px] w-[18px]" strokeWidth={1.7} aria-hidden="true" />
            : <Mic className="h-[18px] w-[18px]" strokeWidth={1.7} aria-hidden="true" />}
    </button>
  );
}

/** Live transcript line under the prompt bar while the microphone is on. */
export function VoiceHint() {
  const v = useVoice();
  const { interim } = useVoiceTranscript();
  if (!v) return null;
  if (v.mic === "error" && v.micError) return <p role="alert" className="mt-2 flex items-center gap-1.5 text-xs text-warning-ink"><MicOff className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />{v.micError}</p>;
  if (v.mic !== "listening" && v.mic !== "starting") return null;
  return (
    <p className="mt-2 flex min-h-[1rem] items-center gap-1.5 text-xs text-muted">
      <span className="h-2 w-2 shrink-0 rounded-full bg-critical" aria-hidden="true" />
      {interim ? <span className="truncate italic text-ink">{interim}</span>
        : <span>Listening. Speak your question, then say &ldquo;send&rdquo;. Say &ldquo;clear&rdquo; to start over.</span>}
    </p>
  );
}

/**
 * Read-aloud toggle for a registered readable (`id`), one of its parts (`partKey`), or ad hoc `text`.
 * @param {{ id: string, partKey?: string, text?: string, label?: string, compact?: boolean }} props
 */
export function ReadAloudButton({ id, partKey, text, label = "Read aloud", compact = false }) {
  const v = useVoice();
  if (!v?.support.synthesis) return null;
  const r = v.reading;
  const active = !!r && r.id === id && (partKey == null || r.key === partKey);
  const playing = active && !r.paused;
  const onClick = () => {
    if (playing) v.stopSpeaking();
    else if (text != null) v.speakText(id, text);
    else v.speakReadable(id, partKey ?? 0, { single: partKey != null });
  };
  const Icon = playing ? Square : Volume2;
  const big = v.handsFree; // glove-sized targets in hands-free mode
  if (compact) {
    return (
      <button type="button" onClick={onClick} aria-pressed={playing} aria-label={playing ? "Stop reading" : label} title={playing ? "Stop reading" : label}
        className={`flex shrink-0 items-center justify-center rounded-full transition-colors ${big ? "h-11 w-11" : "h-7 w-7"} ${
          playing ? "bg-accent text-white hover:bg-accent-deeper" : "text-muted hover:bg-subtle hover:text-accent-strong"}`}>
        <Icon className={big ? "h-5 w-5" : "h-3.5 w-3.5"} aria-hidden="true" />
      </button>
    );
  }
  return (
    <button type="button" onClick={onClick} aria-pressed={playing}
      className={`${big ? "btn-secondary h-11 px-4" : "btn-ghost btn-sm"} ${playing ? "!text-accent-strong" : ""}`}>
      <Icon className={big ? "h-4 w-4" : "h-3.5 w-3.5"} aria-hidden="true" />{playing ? "Stop reading" : label}
    </button>
  );
}

/** Top-bar switch for hands-free mode. */
export function HandsFreeToggle() {
  const v = useVoice();
  if (!v || (!v.support.recognition && !v.support.synthesis)) return null;
  return (
    <button type="button" aria-pressed={v.handsFree} onClick={() => v.setHandsFree(!v.handsFree)}
      aria-label="Hands-free mode" title="Hands-free mode: speak questions and commands, hear answers read aloud"
      className={`pill ${v.handsFree ? "!border-accent/40 !bg-accent-soft !text-accent-strong" : ""}`}>
      <AudioLines aria-hidden="true" className={v.handsFree ? "!text-accent" : ""} /><span className="hidden lg:inline">Hands-free</span>
    </button>
  );
}

/* ----------------------------------------------------------------------------- dock */
const DOCK_BTN = "flex h-16 flex-col items-center justify-center gap-1 rounded-2xl bg-white/10 text-2xs font-medium text-white transition-colors "
  + "hover:bg-white/20 active:bg-white/25 disabled:cursor-not-allowed disabled:opacity-35 disabled:hover:bg-white/10 focus-visible:outline-white";
const ICON_BTN = "flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-white/70 hover:bg-white/10 hover:text-white focus-visible:outline-white";

const COMMAND_HELP = [
  ["Read answer", "Read the latest answer"], ["Next step / Previous step", "Move through the checklist"],
  ["Repeat · Step 3", "Hear a step again"], ["Pause · Resume · Stop", "Control the readout"],
  ["Faster · Slower", "Reading speed"], ["(speak) … Send", "Dictate and send a question"],
  ["Clear", "Empty the question box"], ["Go to reagents", "Open any page by name"],
  ["New thread", "Start a new conversation"], ["Stop listening", "Turn the microphone off"],
];

function BigMic({ size = "h-16 w-16" }) {
  const v = useVoice();
  const { interim } = useVoiceTranscript();
  const { mic } = v;
  const on = mic === "listening" || mic === "starting";
  const hearing = mic === "listening" && interim;
  return (
    <button type="button" onClick={v.toggleListening} disabled={!v.support.recognition} aria-pressed={on}
      aria-label={on ? "Stop listening" : "Start listening"}
      className={`relative flex shrink-0 items-center justify-center rounded-full transition-colors focus-visible:outline-white disabled:cursor-not-allowed disabled:opacity-40 ${size} ${
        hearing ? "bg-success text-white"
          : mic === "listening" ? "voice-pulse bg-critical text-white"
            : mic === "starting" ? "bg-critical/70 text-white"
              : mic === "error" ? "bg-warning text-ink"
                : "bg-white text-ink hover:bg-white/90"}`}>
      {mic === "starting" ? <Spinner className="h-6 w-6" />
        : mic === "error" || !v.support.recognition ? <MicOff className="h-7 w-7" aria-hidden="true" />
          : hearing ? <AudioLines className="h-7 w-7" aria-hidden="true" />
            : <Mic className="h-7 w-7" aria-hidden="true" />}
    </button>
  );
}

function dockStatus(v) {
  const { mic, reading, support } = v;
  if (reading) return reading.paused ? `Paused at ${reading.label}` : `Reading ${reading.label}${reading.total > 1 ? ` (${reading.part + 1} of ${reading.total})` : ""}`;
  if (mic === "error") return v.micError;
  if (!support.recognition) return "Voice input needs Chrome or Edge. Read-aloud still works.";
  if (mic === "listening") return v.hasTarget ? "Listening: ask a question or say a command" : "Listening for commands";
  if (mic === "starting") return v.micError ?? "Starting microphone...";
  return "Microphone off. Tap to listen.";
}

/** Floating hands-free panel: big microphone, live transcript, step controls and voice settings. */
export function VoiceDock() {
  const v = useVoice();
  const { interim, last } = useVoiceTranscript();
  const [collapsed, setCollapsed] = useState(false);
  if (!v?.handsFree) return null;
  const { reading } = v;
  const canRead = !!v.latestId || !!reading;
  const playing = reading && !reading.paused;
  const position = "fixed bottom-24 left-3 z-30 sm:left-[4.75rem]";

  if (collapsed) {
    return (
      <div className={`${position} flex items-center gap-1.5 rounded-full border border-white/10 bg-primary p-2 text-white shadow-pop`} role="region" aria-label="Hands-free voice controls">
        <BigMic />
        {reading && (
          <button type="button" className={ICON_BTN} onClick={v.stopSpeaking} aria-label="Stop reading"><Square className="h-5 w-5" aria-hidden="true" /></button>
        )}
        <button type="button" className={ICON_BTN} onClick={() => setCollapsed(false)} aria-label="Expand voice controls"><Maximize2 className="h-5 w-5" aria-hidden="true" /></button>
      </div>
    );
  }

  return (
    <section className={`${position} w-[min(21rem,calc(100vw-1.5rem))] rounded-2xl border border-white/10 bg-primary p-3.5 text-white shadow-pop`} aria-label="Hands-free voice controls">
      <div className="flex items-start gap-3">
        <BigMic />
        <div className="min-w-0 flex-1 pt-1">
          <p className="text-2xs font-semibold uppercase tracking-wider text-white/60">Hands-free</p>
          <p className="text-sm font-medium leading-5" role="status" aria-live="polite">{dockStatus(v)}</p>
        </div>
        <button type="button" className={ICON_BTN} onClick={() => setCollapsed(true)} aria-label="Minimise voice controls"><Minimize2 className="h-5 w-5" aria-hidden="true" /></button>
        <button type="button" className={`${ICON_BTN} -ml-1.5`} onClick={() => v.setHandsFree(false)} aria-label="Turn off hands-free mode"><X className="h-5 w-5" aria-hidden="true" /></button>
      </div>

      <div className="mt-3 min-h-[3.25rem] rounded-xl bg-white/[0.06] px-3 py-2 text-sm leading-5">
        {interim ? <p className="italic text-white/75">{interim}&hellip;</p>
          : last ? (
            <>
              <p className="line-clamp-2">&ldquo;{last.text}&rdquo;</p>
              {last.command && <p className="mt-1 text-xs font-semibold text-accent-soft">&rarr; {last.command}</p>}
              {last.unrouted && <p className="mt-1 text-xs text-white/60">Not a command. Open Ask SmartLab to dictate a question.</p>}
            </>
          ) : <p className="text-white/55">Say &ldquo;help&rdquo; to hear what you can say.</p>}
      </div>
      {/* Announce finished phrases only; interim words would flood a screen reader. */}
      <p className="sr-only" aria-live="polite">{last ? `Heard: ${last.text}${last.command ? `. ${last.command}.` : ""}` : ""}</p>

      <div className="mt-3 grid grid-cols-4 gap-2">
        <button type="button" className={DOCK_BTN} disabled={!canRead} onClick={() => v.command({ type: "prev" })}>
          <SkipBack className="h-6 w-6" aria-hidden="true" />Previous
        </button>
        <button type="button" className={DOCK_BTN} disabled={!canRead}
          onClick={() => v.command({ type: playing ? "pause" : reading?.paused ? "resume" : "readAll" })}>
          {playing ? <Pause className="h-6 w-6" aria-hidden="true" /> : <Play className="h-6 w-6" aria-hidden="true" />}
          {playing ? "Pause" : reading?.paused ? "Resume" : "Read"}
        </button>
        <button type="button" className={DOCK_BTN} disabled={!canRead} onClick={() => v.command({ type: "next" })}>
          <SkipForward className="h-6 w-6" aria-hidden="true" />Next
        </button>
        <button type="button" className={DOCK_BTN} disabled={!reading} onClick={v.stopSpeaking}>
          <Square className="h-6 w-6" aria-hidden="true" />Stop
        </button>
      </div>

      <details className="group mt-3 rounded-xl bg-white/[0.06]">
        <summary className="flex h-11 cursor-pointer list-none items-center justify-between px-3 text-xs font-semibold text-white/80 focus-visible:outline-white">
          Voice settings <ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" aria-hidden="true" />
        </summary>
        <div className="space-y-2.5 px-3 pb-3">
          <label className="flex items-center gap-3 text-xs">
            <span className="w-12 text-white/70">Speed</span>
            <input type="range" min={RATE_MIN} max={RATE_MAX} step={0.05} value={v.rate} onChange={(e) => v.setRate(Number(e.target.value))}
              className="h-8 flex-1 cursor-pointer" style={{ accentColor: "rgb(var(--sl-accent))" }} />
            <span className="w-10 text-right tabular-nums">{v.rate.toFixed(2)}&times;</span>
          </label>
          {v.voices.length > 0 && (
            <label className="flex items-center gap-3 text-xs">
              <span className="w-12 text-white/70">Voice</span>
              <select value={v.voice?.voiceURI ?? ""} onChange={(e) => v.setVoiceURI(e.target.value)}
                className="h-10 min-w-0 flex-1 rounded-lg border border-white/15 bg-white/10 px-2 text-sm text-white">
                {v.voices.map((o) => <option key={o.voiceURI} value={o.voiceURI} className="text-ink">{o.name} ({o.lang})</option>)}
              </select>
            </label>
          )}
          <button type="button" className="h-10 w-full rounded-lg bg-white/10 text-xs font-medium hover:bg-white/20 focus-visible:outline-white"
            onClick={() => v.speakText("voice-test", "Step 1. Put on nitrile gloves and safety glasses. Then centrifuge at 300 x g for 5 minutes at 4 °C.", "Voice test")}>
            Test voice
          </button>
        </div>
      </details>

      <details className="group mt-2 rounded-xl bg-white/[0.06]">
        <summary className="flex h-11 cursor-pointer list-none items-center justify-between px-3 text-xs font-semibold text-white/80 focus-visible:outline-white">
          What can I say? <ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" aria-hidden="true" />
        </summary>
        <dl className="max-h-48 space-y-1.5 overflow-y-auto px-3 pb-3 text-xs">
          {COMMAND_HELP.map(([say, what]) => (
            <div key={say} className="flex justify-between gap-3">
              <dt className="font-medium">&ldquo;{say}&rdquo;</dt><dd className="text-right text-white/60">{what}</dd>
            </div>
          ))}
          <p className="pt-1 text-white/50">Start with &ldquo;SmartLab&rdquo; if you like: &ldquo;SmartLab, next step&rdquo;.</p>
        </dl>
      </details>
    </section>
  );
}
