"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { getBrowserSupabase } from "@/lib/supabase";

async function getAccessToken(): Promise<string | null> {
  const supabase = getBrowserSupabase();
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

export default function ConnectMondayPage() {
  const [error, setError] = useState<string | null>(null);
  const apiBaseUrl = process.env.NEXT_PUBLIC_FASTAPI_BASE_URL?.replace(/\/$/, "");

  const router = useRouter();
  const searchParams = useSearchParams();
  const returnTo = searchParams.get("returnTo") || "/";

  const handleConnect = async () => {
    try {
      if (!apiBaseUrl) throw new Error("NEXT_PUBLIC_FASTAPI_BASE_URL is not set");
      const token = await getAccessToken();
      if (!token) {
        router.push(`/login?returnTo=${encodeURIComponent(returnTo)}`);
        return;
      }

      const response = await fetch(`${apiBaseUrl}/auth/monday/login`, {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
        redirect: "manual",
      });

      const redirectUrl = response.headers.get("Location") || response.url;
      if (!redirectUrl) throw new Error("Missing redirect URL");

      window.location.href = redirectUrl;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to connect");
    }
  };

  return (
    <main className="mx-auto mt-10 max-w-lg px-4">
      <h1 className="text-2xl font-semibold">Connect monday</h1>
      <p className="text-muted-foreground mt-2">
        Link your monday account to continue.
      </p>
      <button
        className="mt-4 rounded bg-black px-4 py-2 text-white"
        onClick={handleConnect}
      >
        Connect monday
      </button>
      {error ? <p className="mt-3 text-red-600">{error}</p> : null}
    </main>
  );
}