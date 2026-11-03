"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AUTH_BUTTON_CLASS, AuthFlow, PasswordAuthShell } from "../../components/auth/auth-flow";
import { configApi } from "../../lib/config-api/client";

export function LoginClient({ localLoginEnabled = false }: { localLoginEnabled?: boolean }) {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [registrationEnabled, setRegistrationEnabled] = useState(true);
  const [publicDemo, setPublicDemo] = useState(false);
  const [adminRequested, setAdminRequested] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const switchToAdmin = new URLSearchParams(window.location.search).get("mode") === "admin";
    setAdminRequested(switchToAdmin);
    Promise.all([
      configApi.getMe().then(() => true).catch(() => false),
      localLoginEnabled
        ? Promise.resolve({ registrationEnabled: false, publicDemo: false })
        : configApi.getAuthStatus().catch(() => ({ registrationEnabled: true, publicDemo: false })),
    ]).then(([signedIn, status]) => {
      if (cancelled) return;
      if (signedIn && !switchToAdmin) {
        router.replace("/data-tasks");
        return;
      }
      setRegistrationEnabled(status.registrationEnabled);
      setPublicDemo(Boolean(status.publicDemo));
      setChecking(false);
    });
    return () => {
      cancelled = true;
    };
  }, [localLoginEnabled, router]);

  if (checking) {
    return <PasswordAuthShell title="Loading account..." />;
  }

  if (localLoginEnabled) {
    return <LocalLogin onAuthenticated={() => router.replace("/data-tasks")} />;
  }

  if (publicDemo) {
    return <PublicLogin initialAdmin={adminRequested} onAuthenticated={() => { window.location.assign("/data-tasks"); }} />;
  }

  return (
    <AuthFlow
      initialMode="login"
      registrationEnabled={registrationEnabled}
      onAuthenticated={() => router.replace("/data-tasks")}
    />
  );
}

function PublicLogin({ initialAdmin, onAuthenticated }: { initialAdmin: boolean; onAuthenticated: () => void }) {
  const [admin, setAdmin] = useState(initialAdmin);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function enter() {
    setSubmitting(true);
    setError(null);
    try {
      if (admin) await configApi.login({ email: username, password });
      else await configApi.loginGuest();
      onAuthenticated();
    } catch {
      setError(admin ? "登录失败，请检查管理员账号和密码后重试。" : "暂时无法进入试用，请稍后重试。");
      setSubmitting(false);
    }
  }
  return (
    <PasswordAuthShell title="体验 InsightPilot" subtitle="从查数的“是多少”，走向分析的“为什么”。">
      <div className="mb-5 flex rounded-lg bg-neutral-100 p-1" role="group" aria-label="登录方式">
        {[false, true].map((isAdmin) => (
          <button key={String(isAdmin)} type="button" disabled={submitting} aria-pressed={admin === isAdmin}
            className={`flex-1 rounded-md px-4 py-2 text-sm ${admin === isAdmin ? "bg-white font-semibold shadow-sm" : "text-neutral-500"}`}
            onClick={() => { setAdmin(isAdmin); setError(null); setPassword(""); }}>
            {isAdmin ? "管理员登录" : "游客体验"}
          </button>
        ))}
      </div>
      <form className="flex flex-col gap-4" onSubmit={(event) => { event.preventDefault(); if (!submitting) void enter(); }}>
        {admin ? <>
          <p className="text-sm text-neutral-600">管理员不受每天 2 次的游客限制；全站每天 ¥10 的费用上限仍然有效。</p>
          <label className="text-sm">账号
            <input className="mt-1 w-full rounded-lg border border-neutral-300 p-3" autoComplete="username" required
              value={username} onChange={(event) => setUsername(event.target.value)} />
          </label>
          <label className="text-sm">密码
            <input className="mt-1 w-full rounded-lg border border-neutral-300 p-3" type="password" autoComplete="current-password" required maxLength={256}
              value={password} onChange={(event) => setPassword(event.target.value)} />
          </label>
        </> : <>
          <p className="text-sm text-neutral-600">使用演示数据探索指标变化，无需注册。每位游客每天可发起 2 次分析；全站额度用完后次日恢复。</p>
          <p className="text-sm text-neutral-500">请勿输入个人隐私或公司机密。试用记录可能随服务重启清除，请及时导出报告。</p>
        </>}
        {error ? <p role="alert" className="text-sm text-rose-700">{error}</p> : null}
        <button type="submit" className={AUTH_BUTTON_CLASS} disabled={submitting}>
          {submitting ? "正在进入…" : admin ? "登录管理员账号" : "开始体验"}
        </button>
      </form>
    </PasswordAuthShell>
  );
}

function LocalLogin({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function signIn() {
    setSubmitting(true);
    setError(null);
    try {
      await configApi.loginLocal();
      onAuthenticated();
    } catch {
      setError("暂时无法进入工作区，请确认服务已启动后重试。");
      setSubmitting(false);
    }
  }

  return (
    <PasswordAuthShell title="欢迎使用 DataFoundry" subtitle="无需账号和密码，点击登录即可进入工作区。">
      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!submitting) void signIn();
        }}
      >
        {error ? <p role="alert" className="text-sm text-rose-700">{error}</p> : null}
        <button className={AUTH_BUTTON_CLASS} disabled={submitting} type="submit">
          {submitting ? "正在进入…" : "登录"}
        </button>
      </form>
    </PasswordAuthShell>
  );
}
