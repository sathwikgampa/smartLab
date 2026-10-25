import { useEffect, useId, useState } from "react";
import { ArrowLeft, Check, Eye, EyeOff, Mail, X } from "lucide-react";
import SmartLabOrb from "../components/SmartLabOrb";
import { Spinner } from "../components/ui";
import { useApp } from "../context/AppContext";
import { api } from "../lib/api";
import { checkPassword, isEmail } from "../lib/password";
import { navigate } from "../lib/router";

/* ------------------------------------------------------------------ layout & fields */
function AuthCard({ title, subtitle, children, footer }) {
  return (
    <div className="flex min-h-full flex-col items-center justify-center bg-page px-4 py-10">
      <main id="site-main" tabIndex={-1} className="w-full max-w-[420px] outline-none">
        <div className="rounded-2xl border border-line bg-surface px-6 pb-7 pt-8 shadow-pop sm:px-8">
          <div className="flex flex-col items-center text-center">
            <SmartLabOrb size={44} />
            <h1 className="mt-4 font-serif text-[1.75rem] font-medium leading-tight text-ink">{title}</h1>
            {subtitle && <p className="mt-1.5 text-sm text-muted">{subtitle}</p>}
          </div>
          <div className="mt-7">{children}</div>
        </div>
        {footer && <p className="mt-5 text-center text-sm text-muted">{footer}</p>}
        <a href="#/" className="mx-auto mt-4 flex w-fit items-center gap-1.5 rounded-md text-sm text-muted hover:text-ink">
          <ArrowLeft className="h-4 w-4" aria-hidden="true" /> Back to home
        </a>
      </main>
    </div>
  );
}

/** Labelled input with an error message tied to it through aria-describedby. */
function TextField({ label, error, hint, id: givenId, children, ...input }) {
  const auto = useId();
  const id = givenId ?? auto;
  const describedBy = [error && `${id}-err`, hint && `${id}-hint`].filter(Boolean).join(" ") || undefined;
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-[0.8125rem] font-medium text-ink">{label}</label>
      {children ? children({ id, describedBy, invalid: !!error }) : (
        <input id={id} className={`input h-10 ${error ? "border-critical/60" : ""}`} aria-invalid={!!error || undefined} aria-describedby={describedBy} {...input} />
      )}
      {hint && !error && <p id={`${id}-hint`} className="mt-1 text-xs text-muted">{hint}</p>}
      {error && <p id={`${id}-err`} className="mt-1 text-xs text-critical">{error}</p>}
    </div>
  );
}

function PasswordField({ label, value, onChange, error, autoComplete, placeholder, describedExtra, name }) {
  const [show, setShow] = useState(false);
  return (
    <TextField label={label} error={error}>
      {({ id, describedBy, invalid }) => (
        <div className="relative">
          <input id={id} name={name} type={show ? "text" : "password"} value={value} onChange={onChange} autoComplete={autoComplete} placeholder={placeholder}
            className={`input h-10 pr-10 ${invalid ? "border-critical/60" : ""}`} aria-invalid={invalid || undefined}
            aria-describedby={[describedBy, describedExtra].filter(Boolean).join(" ") || undefined} />
          <button type="button" onClick={() => setShow((s) => !s)} aria-label={show ? "Hide password" : "Show password"} aria-pressed={show}
            className="absolute right-1 top-1 flex h-8 w-8 items-center justify-center rounded-md text-muted hover:bg-subtle hover:text-ink">
            {show ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
          </button>
        </div>
      )}
    </TextField>
  );
}

/** Live strength meter and rule checklist (meaning carried by icons and text, not colour). */
function PasswordStrength({ id, result, touched }) {
  const colors = ["bg-critical", "bg-critical", "bg-warning", "bg-success", "bg-success"];
  return (
    <div id={id} className="-mt-1 space-y-2" aria-live="polite">
      <div className="flex items-center gap-2">
        <div className="grid flex-1 grid-cols-4 gap-1" aria-hidden="true">
          {[1, 2, 3, 4].map((n) => (
            <span key={n} className={`h-1 rounded-full ${touched && result.score >= n ? colors[result.score] : "bg-line"}`} />
          ))}
        </div>
        <span className="w-20 text-right text-xs text-muted">{touched ? result.label : ""}</span>
      </div>
      <ul className="space-y-1">
        {result.rules.map((r) => (
          <li key={r.id} className={`flex items-center gap-1.5 text-xs ${r.ok ? "text-success" : "text-muted"}`}>
            {r.ok ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : <X className="h-3.5 w-3.5" aria-hidden="true" />}
            <span>{r.label}<span className="sr-only">{r.ok ? " (met)" : " (not met)"}</span></span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function FormError({ children }) {
  if (!children) return null;
  return <p role="alert" className="rounded-lg border border-critical/30 bg-critical-soft px-3 py-2 text-sm text-critical">{children}</p>;
}

function SubmitButton({ busy, children }) {
  return (
    <button type="submit" disabled={busy} aria-busy={busy || undefined} className="btn-primary h-10 w-full">
      {busy && <Spinner />}{children}
    </button>
  );
}

const friendly = (err, fallback) => (err?.status === 429 ? "Too many attempts. Wait a few minutes and try again." : err?.status === 0 ? err.message : fallback ?? err?.message);
const useTitle = (t) => useEffect(() => { document.title = `${t} · SmartLab AI`; }, [t]);

/* --------------------------------------------------------------------------- login */
export function LoginPage({ route }) {
  useTitle("Log in");
  const { setAccount } = useApp();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState({});
  const [formError, setFormError] = useState(null);
  const [busy, setBusy] = useState(false);
  const notice = route.query.reset === "1" ? "Your password was changed. Log in with the new password." : route.query.expired === "1" ? "Your session ended. Please log in again." : null;

  const submit = async (e) => {
    e.preventDefault();
    const next = {};
    if (!isEmail(email)) next.email = "Enter a valid email address.";
    if (!password) next.password = "Enter your password.";
    setErrors(next);
    setFormError(null);
    if (Object.keys(next).length) return;
    setBusy(true);
    try {
      const { user } = await api.login(email.trim(), password);
      setAccount(user);
      navigate(route.query.next || "overview", { replace: true });
    } catch (err) {
      setFormError(friendly(err, "Email or password is incorrect."));
    } finally { setBusy(false); }
  };

  return (
    <AuthCard title="Welcome back" subtitle="Log in to your SmartLab AI account"
      footer={<>Don&rsquo;t have an account? <a className="link" href="#/signup">Sign up</a></>}>
      <form className="space-y-4" onSubmit={submit} noValidate>
        {notice && <p role="status" className="rounded-lg border border-accent/20 bg-accent-soft px-3 py-2 text-sm text-ink">{notice}</p>}
        <FormError>{formError}</FormError>
        <TextField label="Email" type="email" autoComplete="email" placeholder="you@lab.org" value={email} onChange={(e) => { setEmail(e.target.value); setErrors((x) => ({ ...x, email: undefined })); }} error={errors.email} />
        <PasswordField label="Password" autoComplete="current-password" placeholder="Enter your password" value={password} onChange={(e) => { setPassword(e.target.value); setErrors((x) => ({ ...x, password: undefined })); }} error={errors.password} />
        <div className="flex justify-end"><a href="#/forgot-password" className="link text-[0.8125rem]">Forgot password?</a></div>
        <SubmitButton busy={busy}>Log in</SubmitButton>
      </form>
    </AuthCard>
  );
}

/* -------------------------------------------------------------------------- sign up */
export function SignupPage({ route }) {
  useTitle("Create your account");
  const { setAccount } = useApp();
  const inviteToken = route?.query?.invite || route?.query?.token || "";
  const [inviteInfo, setInviteInfo] = useState(null);
  const [form, setForm] = useState({ name: "", email: "", organization: "", password: "", confirm: "", accepted: false });
  const [errors, setErrors] = useState({});
  const [formError, setFormError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (inviteToken) {
      api.invitationDetails(inviteToken).then((data) => {
        setInviteInfo(data);
        if (data?.email) setForm((f) => ({ ...f, email: data.email, organization: data.organization_name || f.organization }));
      }).catch(() => {});
    }
  }, [inviteToken]);

  const set = (k) => (e) => {
    setForm((f) => ({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
    setErrors((errs) => ({ ...errs, [k]: undefined }));
  };
  const strength = checkPassword(form.password, { email: form.email, name: form.name });
  const strengthId = useId();

  const submit = async (e) => {
    e.preventDefault();
    const next = {};
    if (!form.name.trim()) next.name = "Enter your full name.";
    if (!isEmail(form.email)) next.email = "Enter a valid email address.";
    if (!strength.valid) next.password = "Choose a password that meets the rules below.";
    if (!form.confirm) next.confirm = "Confirm your password.";
    else if (form.confirm !== form.password) next.confirm = "Passwords don't match.";
    if (!form.accepted) next.accepted = "Please confirm to continue.";
    setErrors(next);
    setFormError(null);
    if (Object.keys(next).length) return;
    setBusy(true);
    try {
      const { user } = await api.signup({
        name: form.name.trim(),
        email: form.email.trim(),
        organization: form.organization.trim(),
        password: form.password,
        accepted: true,
        invite_token: inviteToken || undefined,
      });
      setAccount(user);
      navigate("overview", { replace: true });
    } catch (err) {
      if (err.data?.field) setErrors({ [err.data.field]: err.message });
      else setFormError(friendly(err));
    } finally { setBusy(false); }
  };

  return (
    <AuthCard title="Create your account" subtitle={inviteInfo ? `You've been invited to join ${inviteInfo.organization_name}` : "Join SmartLab AI to get started"}
      footer={<>Already have an account? <a className="link" href="#/login">Log in</a></>}>
      {inviteInfo && (
        <div className="mb-4 rounded-lg border border-accent/30 bg-accent/10 p-3 text-xs text-ink">
          <p className="font-semibold text-accent-strong">Team Invitation</p>
          <p className="mt-0.5 text-muted">
            Joining <strong className="text-ink">{inviteInfo.organization_name}</strong>
            {inviteInfo.team_name ? ` (Team: ${inviteInfo.team_name})` : ""} as <strong className="text-ink">{inviteInfo.role}</strong>.
          </p>
        </div>
      )}
      <form className="space-y-4" onSubmit={submit} noValidate>
        <FormError>{formError}</FormError>
        <TextField label="Full name" autoComplete="name" placeholder="Your full name" value={form.name} onChange={set("name")} error={errors.name} />
        <TextField label="Email" type="email" autoComplete="email" placeholder="you@lab.org" value={form.email} onChange={set("email")} error={errors.email} />
        <TextField label="Organization or lab name" autoComplete="organization" placeholder="Your lab or organization" value={form.organization} onChange={set("organization")} hint="Optional" />
        <PasswordField label="Password" autoComplete="new-password" placeholder="Create a password" value={form.password} onChange={set("password")} error={errors.password} describedExtra={strengthId} />
        <PasswordStrength id={strengthId} result={strength} touched={form.password.length > 0} />
        <PasswordField label="Confirm password" autoComplete="new-password" placeholder="Confirm your password" value={form.confirm} onChange={set("confirm")} error={errors.confirm} />
        <div>
          <label className="flex items-start gap-2.5 text-[0.8125rem] leading-snug text-ink">
            <input type="checkbox" checked={form.accepted} onChange={set("accepted")} className="mt-0.5 h-4 w-4 shrink-0 accent-accent"
              aria-invalid={!!errors.accepted || undefined} aria-describedby={errors.accepted ? "accept-err" : undefined} />
            <span>
              I understand SmartLab AI supports, and does not replace, my lab&rsquo;s safety procedures, and I agree to the{" "}
              <a className="link" href="#/terms" target="_blank" rel="noreferrer">Terms</a> and <a className="link" href="#/privacy" target="_blank" rel="noreferrer">Privacy</a> notice.
            </span>
          </label>
          {errors.accepted && <p id="accept-err" className="mt-1 text-xs text-critical">{errors.accepted}</p>}
        </div>
        <SubmitButton busy={busy}>Create account</SubmitButton>
      </form>
    </AuthCard>
  );
}

/* ------------------------------------------------------------------ forgot password */
export function ForgotPasswordPage() {
  useTitle("Forgot your password");
  const [email, setEmail] = useState("");
  const [error, setError] = useState(null);
  const [formError, setFormError] = useState(null);
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setFormError(null);
    if (!isEmail(email)) { setError("Enter a valid email address."); return; }
    setError(null);
    setBusy(true);
    try {
      await api.forgotPassword(email.trim());
      setSent(true);
    } catch (err) {
      setFormError(friendly(err));
    } finally { setBusy(false); }
  };

  return (
    <AuthCard title="Forgot your password?" subtitle="Enter your email and we'll send you instructions to reset your password."
      footer={<>Remembered it? <a className="link" href="#/login">Log in</a></>}>
      <form className="space-y-4" onSubmit={submit} noValidate>
        <FormError>{formError}</FormError>
        <TextField label="Email" type="email" autoComplete="email" placeholder="you@lab.org" value={email} onChange={(e) => { setEmail(e.target.value); setSent(false); setError(null); }} error={error} />
        {sent && (
          <p role="status" className="flex items-start gap-2.5 rounded-lg border border-accent/20 bg-accent-soft px-3 py-2.5 text-sm text-ink">
            <Mail className="mt-0.5 h-4 w-4 shrink-0 text-accent-strong" aria-hidden="true" />
            If an account exists, we&rsquo;ve sent instructions to reset your password to that email.
          </p>
        )}
        <SubmitButton busy={busy}>{sent ? "Send again" : "Send instructions"}</SubmitButton>
      </form>
    </AuthCard>
  );
}

/* ------------------------------------------------------------------- reset password */
export function ResetPasswordPage({ route }) {
  useTitle("Reset your password");
  const token = route.query.token ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState({});
  const [formError, setFormError] = useState(null);
  const [busy, setBusy] = useState(false);
  const strength = checkPassword(password);
  const strengthId = useId();

  if (!token) {
    return (
      <AuthCard title="Reset your password" footer={<><a className="link" href="#/login">Back to log in</a></>}>
        <p role="alert" className="text-center text-sm text-muted">This reset link is incomplete. Open the link from your email again, or <a className="link" href="#/forgot-password">request a new one</a>.</p>
      </AuthCard>
    );
  }

  const submit = async (e) => {
    e.preventDefault();
    const next = {};
    if (!strength.valid) next.password = "Choose a password that meets the rules below.";
    if (!confirm) next.confirm = "Confirm your new password.";
    else if (confirm !== password) next.confirm = "Passwords don't match.";
    setErrors(next);
    setFormError(null);
    if (Object.keys(next).length) return;
    setBusy(true);
    try {
      await api.resetPassword(token, password);
      navigate("login?reset=1", { replace: true });
    } catch (err) {
      if (err.data?.field === "password") setErrors({ password: err.message });
      else setFormError(err.status === 400 ? <>This reset link is invalid or has expired. <a className="link" href="#/forgot-password">Request a new one</a>.</> : friendly(err));
    } finally { setBusy(false); }
  };

  return (
    <AuthCard title="Reset your password" subtitle="Enter a new password for your account."
      footer={<><a className="link" href="#/login">Back to log in</a></>}>
      <form className="space-y-4" onSubmit={submit} noValidate>
        <FormError>{formError}</FormError>
        <PasswordField label="New password" autoComplete="new-password" placeholder="Create a new password" value={password} onChange={(e) => { setPassword(e.target.value); setErrors((x) => ({ ...x, password: undefined })); }} error={errors.password} describedExtra={strengthId} />
        <PasswordStrength id={strengthId} result={strength} touched={password.length > 0} />
        <PasswordField label="Confirm new password" autoComplete="new-password" placeholder="Confirm your new password" value={confirm} onChange={(e) => { setConfirm(e.target.value); setErrors((x) => ({ ...x, confirm: undefined })); }} error={errors.confirm} />
        <SubmitButton busy={busy}>Reset password</SubmitButton>
      </form>
    </AuthCard>
  );
}
