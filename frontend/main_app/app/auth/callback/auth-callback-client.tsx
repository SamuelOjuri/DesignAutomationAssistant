"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getBrowserSupabase } from "@/lib/supabase/client";

type Props = {
  returnTo: string;
};

export default function AuthCallbackClient({ returnTo }: Props) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const run = async () => {
      const supabase = getBrowserSupabase();

      const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
      const query = new URLSearchParams(window.location.search);

      const errorParam = hash.get("error") || query.get("error");
      const errorDescription =
        hash.get("error_description") || query.get("error_description");

      if (errorParam) {
        setError(errorDescription || errorParam);
        return;
      }

      const access_token = hash.get("access_token");
      const refresh_token = hash.get("refresh_token");
      const type = hash.get("type") || query.get("type");
      const code = query.get("code");

      if (access_token && refresh_token) {
        const { error } = await supabase.auth.setSession({
          access_token,
          refresh_token,
        });
        if (error) {
          setError(error.message);
          return;
        }
      } else if (code) {
        const { error } = await supabase.auth.exchangeCodeForSession(code);
        if (error) {
          setError(error.message);
          return;
        }
      } else {
        setError("Missing auth credentials in redirect.");
        return;
      }

      const finalReturnTo = query.get("returnTo") || returnTo || "/";
      if (type === "invite" || type === "recovery") {
        router.replace(
          `/auth/set-password?returnTo=${encodeURIComponent(finalReturnTo)}`
        );
      } else {
        router.replace(finalReturnTo);
      }
    };

    run().catch((err) =>
      setError(err instanceof Error ? err.message : "Auth callback failed")
    );
  }, [router, returnTo]);

  return (
    <main className="mx-auto mt-10 max-w-md px-4">
      <h1 className="text-2xl font-semibold">Signing you in...</h1>
      {error ? <p className="mt-3 text-red-600">{error}</p> : null}
    </main>
  );
}