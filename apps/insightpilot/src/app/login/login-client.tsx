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

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      configApi.getMe().then(() => true).catch(() => false),
      localLoginEnabled
        ? Promise.resolve({ registrationEnabled: false, publicDemo: false })
        : configApi.getAuthStatus().catch(() => ({ registrationEnabled: true, publicDemo: false })),
    ]).then(([signedIn, status]) => {
      if (cancelled) return;
      if (signedIn) {
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
    return <GuestLogin onAuthenticated={() => router.replace("/data-tasks")} />;
  }

  return (
    <AuthFlow
      initialMode="login"
      registrationEnabled={registrationEnabled}
      onAuthenticated={() => router.replace("/data-tasks")}
    />
  );
}

function GuestLogin({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function enter() {
    setSubmitting(true);
    setError(null);
    try {
      await configApi.loginGuest();
      onAuthenticated();
    } catch {
      setError("暂时无法进入试用，请稍后重试。");
      setSubmitting(false);
    }
  }
  return (
    <PasswordAuthShell title="体验 InsightPilot" subtitle="从查数的“是多少”，走向分析的“为什么”。">
      <div className="flex flex-col gap-4">
        <p className="text-sm text-neutral-600">使用演示数据探索指标变化，无需注册。每位访客每天可发起 2 次分析；全站额度用完后次日恢复。</p>
        <p className="text-sm text-neutral-500">请勿输入个人隐私或公司机密。试用记录可能随服务重启清除，请及时导出报告。</p>
        {error ? <p role="alert" className="text-sm text-rose-700">{error}</p> : null}
        <button className={AUTH_BUTTON_CLASS} disabled={submitting} onClick={() => void enter()}>
          {submitting ? "正在进入…" : "开始体验"}
        </button>
      </div>
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
