"use client";

import {
  ArrowRight,
  Check,
  Compass,
  LoaderCircle,
  MessageCircleMore,
  ShieldCheck,
  Smartphone,
} from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import type { FormEvent, ReactNode } from "react";

import {
  AUTH_EXPIRED_EVENT,
  fetchCurrentUser,
  loginWithPhone,
  logoutAuthentication,
  refreshAuthentication,
  requestSmsCode,
} from "@/lib/api";
import type { AuthUser, SmsCodeChallenge } from "@/lib/api";

type AuthContextValue = {
  user: AuthUser;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthGate.");
  return value;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null | undefined>(undefined);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const restore = async () => {
      try {
        const current = await fetchCurrentUser(controller.signal);
        if (!active) return;
        if (current) {
          setUser(current);
          return;
        }
        const refreshed = await refreshAuthentication();
        if (active) setUser(refreshed?.user ?? null);
      } catch (reason: unknown) {
        if (!active || (reason instanceof DOMException && reason.name === "AbortError")) return;
        setBootstrapError(reason instanceof Error ? reason.message : "无法检查登录状态。");
        setUser(null);
      }
    };
    const expire = () => setUser(null);
    window.addEventListener(AUTH_EXPIRED_EVENT, expire);
    void restore();
    return () => {
      active = false;
      controller.abort();
      window.removeEventListener(AUTH_EXPIRED_EVENT, expire);
    };
  }, []);

  const signOut = useCallback(async () => {
    await logoutAuthentication();
    setUser(null);
  }, []);

  if (user === undefined) return <AuthLoading />;
  if (user === null) {
    return (
      <LoginScreen
        initialError={bootstrapError}
        onAuthenticated={(authenticatedUser) => {
          setBootstrapError(null);
          setUser(authenticatedUser);
        }}
      />
    );
  }
  return <AuthContext.Provider value={{ user, signOut }}>{children}</AuthContext.Provider>;
}

function AuthLoading() {
  return (
    <main className="grid min-h-screen place-items-center bg-[var(--canvas)] text-[var(--ink)]">
      <div className="flex items-center gap-3 text-sm text-[var(--muted)]">
        <span className="grid size-10 place-items-center rounded-2xl bg-[var(--brand)] text-white">
          <Compass size={20} />
        </span>
        <LoaderCircle className="animate-spin" size={18} />
        正在恢复登录状态…
      </div>
    </main>
  );
}

function LoginScreen({
  initialError,
  onAuthenticated,
}: {
  initialError: string | null;
  onAuthenticated: (user: AuthUser) => void;
}) {
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState<SmsCodeChallenge | null>(null);
  const [countdown, setCountdown] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(initialError);

  useEffect(() => {
    if (countdown <= 0) return;
    const timer = window.setInterval(() => {
      setCountdown((current) => Math.max(current - 1, 0));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [countdown]);

  const sendCode = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const result = await requestSmsCode(phone);
      setChallenge(result);
      setCountdown(result.resend_after);
      setCode(result.debug_code ?? "");
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "验证码发送失败，请稍后重试。");
    } finally {
      setSubmitting(false);
    }
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!challenge) {
      await sendCode();
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = await loginWithPhone(phone, challenge.challenge_id, code);
      onAuthenticated(result.user);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "登录失败，请重新获取验证码。");
    } finally {
      setSubmitting(false);
    }
  };

  const resetPhone = () => {
    setChallenge(null);
    setCode("");
    setCountdown(0);
    setError(null);
  };

  return (
    <main className="min-h-screen bg-[var(--canvas)] px-4 py-8 text-[var(--ink)] sm:grid sm:place-items-center">
      <div className="mx-auto grid w-full max-w-5xl overflow-hidden rounded-[28px] border border-black/[0.07] bg-white shadow-2xl shadow-black/[0.08] lg:grid-cols-[1.05fr_0.95fr]">
        <section className="hidden min-h-[620px] flex-col justify-between bg-[var(--brand)] p-10 text-white lg:flex">
          <div>
            <span className="grid size-12 place-items-center rounded-2xl bg-white/15">
              <Compass size={25} />
            </span>
            <h1 className="mt-8 max-w-md text-4xl font-semibold leading-tight tracking-[-0.04em]">
              从一句旅行想法，走到一份可执行的行程。
            </h1>
            <p className="mt-4 max-w-md text-sm leading-7 text-white/70">
              登录后，你的对话、工具查询和旅行计划只会出现在自己的账号中。
            </p>
          </div>
          <div className="space-y-4 text-sm text-white/80">
            <Feature icon={<MessageCircleMore size={17} />} text="跨设备继续历史规划会话" />
            <Feature icon={<ShieldCheck size={17} />} text="按登录用户隔离会话与行程数据" />
            <Feature icon={<Check size={17} />} text="验证码登录，无需记忆密码" />
          </div>
        </section>

        <section className="flex min-h-[620px] items-center p-6 sm:p-12">
          <form className="mx-auto w-full max-w-sm" onSubmit={(event) => void submit(event)}>
            <div className="grid size-12 place-items-center rounded-2xl bg-[var(--brand-soft)] text-[var(--brand)] lg:hidden">
              <Compass size={23} />
            </div>
            <p className="mt-8 text-xs font-semibold uppercase tracking-[0.18em] text-[var(--brand)]">
              Tour Assistant
            </p>
            <h2 className="mt-3 text-3xl font-semibold tracking-[-0.035em]">
              {challenge ? "输入验证码" : "手机号登录"}
            </h2>
            <p className="mt-3 text-sm leading-6 text-[var(--muted)]">
              {challenge
                ? `验证码已发送至 ${phone}，首次登录会自动创建账号。`
                : "当前为本地模拟短信流程，首次验证成功即完成注册。"}
            </p>

            <label className="mt-8 block text-sm font-medium" htmlFor="auth-phone">
              手机号
            </label>
            <div className="mt-2 flex h-12 items-center rounded-xl border border-black/10 bg-black/[0.018] px-3 focus-within:border-[var(--brand)]">
              <Smartphone size={17} className="mr-2 text-[var(--muted)]" />
              <span className="mr-2 border-r border-black/10 pr-2 text-sm text-[var(--muted)]">+86</span>
              <input
                id="auth-phone"
                autoComplete="tel"
                className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-[var(--muted-light)] disabled:text-[var(--muted)]"
                disabled={Boolean(challenge) || submitting}
                inputMode="numeric"
                maxLength={11}
                onChange={(event) => setPhone(event.target.value.replace(/\D/g, ""))}
                placeholder="请输入 11 位手机号"
                value={phone}
              />
              {challenge && (
                <button className="text-xs text-[var(--brand)]" onClick={resetPhone} type="button">
                  更换
                </button>
              )}
            </div>

            {challenge && (
              <>
                <label className="mt-5 block text-sm font-medium" htmlFor="auth-code">
                  六位验证码
                </label>
                <div className="mt-2 flex h-12 items-center rounded-xl border border-black/10 bg-black/[0.018] px-3 focus-within:border-[var(--brand)]">
                  <input
                    id="auth-code"
                    autoComplete="one-time-code"
                    autoFocus
                    className="min-w-0 flex-1 bg-transparent font-mono text-lg tracking-[0.28em] outline-none placeholder:font-sans placeholder:text-sm placeholder:tracking-normal placeholder:text-[var(--muted-light)]"
                    disabled={submitting}
                    inputMode="numeric"
                    maxLength={6}
                    onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))}
                    placeholder="输入验证码"
                    value={code}
                  />
                  <button
                    className="text-xs text-[var(--brand)] disabled:text-[var(--muted-light)]"
                    disabled={countdown > 0 || submitting}
                    onClick={() => void sendCode()}
                    type="button"
                  >
                    {countdown > 0 ? `${countdown}s` : "重新发送"}
                  </button>
                </div>
                {challenge.debug_code && (
                  <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
                    本地模拟验证码：<span className="font-mono font-semibold">{challenge.debug_code}</span>
                  </p>
                )}
              </>
            )}

            {error && (
              <p className="mt-4 rounded-xl border border-red-200 bg-red-50 px-3 py-2.5 text-xs leading-5 text-red-700">
                {error}
              </p>
            )}

            <button
              className="mt-6 flex h-12 w-full items-center justify-center gap-2 rounded-xl bg-[var(--brand)] text-sm font-medium text-white shadow-lg shadow-[var(--brand-shadow)] transition-transform hover:-translate-y-0.5 disabled:translate-y-0 disabled:cursor-not-allowed disabled:opacity-50"
              disabled={
                submitting ||
                phone.length !== 11 ||
                (Boolean(challenge) && code.length !== 6)
              }
              type="submit"
            >
              {submitting ? (
                <LoaderCircle className="animate-spin" size={17} />
              ) : (
                <>
                  {challenge ? "登录并继续" : "获取验证码"}
                  <ArrowRight size={16} />
                </>
              )}
            </button>
            <p className="mt-5 text-center text-[11px] leading-5 text-[var(--muted-light)]">
              登录即表示你同意仅将手机号用于账号识别和登录验证。
            </p>
          </form>
        </section>
      </div>
    </main>
  );
}

function Feature({ icon, text }: { icon: ReactNode; text: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="grid size-8 place-items-center rounded-xl bg-white/10">{icon}</span>
      {text}
    </div>
  );
}
