"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AUTH_BUTTON_CLASS, AuthFlow, PasswordAuthShell } from "../../components/auth/auth-flow";
import { configApi } from "../../lib/config-api/client";

export function LoginClient({ localLoginEnabled = false }: { localLoginEnabled?: boolean }) {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [registrationEnabled, setRegistrationEnabled] = useState(true);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      configApi.getMe().then(() => true).catch(() => false),
      localLoginEnabled
        ? Promise.resolve(false)
        : configApi.getAuthStatus().then((status) => status.registrationEnabled).catch(() => true),
    ]).then(([signedIn, canRegister]) => {
      if (cancelled) return;
      if (signedIn) {
        router.replace("/data-tasks");
        return;
      }
      setRegistrationEnabled(canRegister);
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

  return (
    <AuthFlow
      initialMode="login"
      registrationEnabled={registrationEnabled}
      onAuthenticated={() => router.replace("/data-tasks")}
    />
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
