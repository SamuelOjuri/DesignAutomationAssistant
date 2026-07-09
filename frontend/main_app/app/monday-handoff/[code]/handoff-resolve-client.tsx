"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

const CSRF_COOKIE_NAME = "daa_csrf";

function getCookie(name: string): string | null {
  const value = document.cookie
    .split("; ")
    .find((part) => part.startsWith(`${name}=`));
  return value ? decodeURIComponent(value.split("=").slice(1).join("=")) : null;
}

function csrfHeaders(): HeadersInit {
  const token = getCookie(CSRF_COOKIE_NAME);
  return token ? { "X-CSRF-Token": token } : {};
}

export default function HandoffResolveClient({ code }: { code: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const baseUrl = process.env.NEXT_PUBLIC_FASTAPI_BASE_URL?.replace(/\/$/, "");
    if (!baseUrl) {
      setError("FASTAPI base URL is not configured.");
      return;
    }

    let cancelled = false;

    const resolve = async () => {
      try {
        const response = await fetch(`${baseUrl}/api/monday/handoff/resolve`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...csrfHeaders(),
          },
          body: JSON.stringify({ code }),
          credentials: "include",
          cache: "no-store",
        });

        if (cancelled) return;

        if (response.status === 401) {
          const loginUrl = new URL(`${baseUrl}/auth/monday/login`);
          loginUrl.searchParams.set("mode", "monday_first");
          loginUrl.searchParams.set("handoff_code", code);
          loginUrl.searchParams.set("return_to", `/monday-handoff/${code}`);
          window.location.href = loginUrl.toString();
          return;
        }

        if (!response.ok) {
          setError(`Resolve failed (${response.status})`);
          return;
        }

        const data = (await response.json()) as { externalTaskKey: string };
        router.replace(`/tasks/${encodeURIComponent(data.externalTaskKey)}`);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Unable to resolve handoff.");
        }
      }
    };

    void resolve();

    return () => {
      cancelled = true;
    };
  }, [code, router]);

  return (
    <main className="mx-auto min-h-screen max-w-lg px-5 py-10">
      <h1 className="text-2xl font-semibold text-foreground">Opening task</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Connecting your monday session to this task...
      </p>
      {error ? <p className="mt-4 text-sm text-red-500">{error}</p> : null}
    </main>
  );
}