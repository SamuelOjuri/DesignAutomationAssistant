"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { getBrowserSupabase } from "@/lib/supabase/client";

type Props = {
  token?: string;
  type?: string;
  returnTo: string;
};

export default function ConfirmClickClient({ token, type, returnTo }: Props) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleConfirm = async () => {
    setError(null);

    const params = new URLSearchParams(window.location.search);

    const effectiveToken = token || params.get("token");
    const effectiveType = type || params.get("type");
    const effectiveReturnTo = returnTo || params.get("redirect_to") || "/";

    if (!effectiveToken || !effectiveType) {
      setError("Missing token or type.");
      return;
    }

    const supabase = getBrowserSupabase();
    const { error } = await supabase.auth.verifyOtp({
      token_hash: effectiveToken,
      type: effectiveType as "invite" | "recovery" | "magiclink" | "signup",
    });

    if (!error) {
      if (effectiveType === "invite" || effectiveType === "recovery") {
        router.replace(`/auth/set-password?returnTo=${encodeURIComponent(effectiveReturnTo)}`);
      } else {
        router.replace(effectiveReturnTo);
      }
    }
  };

  return (
    <main className="mx-auto mt-10 max-w-md px-4">
      <h1 className="text-2xl font-semibold">Confirm your email</h1>
      <p className="mt-2 text-muted-foreground">
        Click below to verify and continue.
      </p>
      <button
        className="mt-4 rounded bg-black px-4 py-2 text-white"
        onClick={handleConfirm}
        disabled={loading}
      >
        {loading ? "Verifying..." : "Confirm"}
      </button>
      {error ? <p className="mt-3 text-red-600">{error}</p> : null}
    </main>
  );
}