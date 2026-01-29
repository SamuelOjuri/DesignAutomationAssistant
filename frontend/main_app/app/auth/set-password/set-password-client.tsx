"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { getBrowserSupabase } from "@/lib/supabase/client";

type Props = {
  returnTo: string;
};

export default function SetPasswordClient({ returnTo }: Props) {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }

    setLoading(true);
    try {
      const supabase = getBrowserSupabase();
      const { data } = await supabase.auth.getSession();
      if (!data.session) {
        throw new Error("Invite session missing or expired.");
      }

      const { error } = await supabase.auth.updateUser({ password });
      if (error) throw error;

      router.replace(returnTo || "/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to set password");
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="mx-auto mt-10 max-w-md px-4">
      <h1 className="text-2xl font-semibold">Set your password</h1>
      <form onSubmit={handleSubmit} className="mt-6 space-y-4">
        <input
          type="password"
          className="w-full rounded border px-3 py-2"
          placeholder="New password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        <input
          type="password"
          className="w-full rounded border px-3 py-2"
          placeholder="Confirm password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          required
        />
        <button
          type="submit"
          className="w-full rounded bg-black px-4 py-2 text-white"
          disabled={loading}
        >
          {loading ? "Saving..." : "Save password"}
        </button>
        {error ? <p className="text-red-600">{error}</p> : null}
      </form>
    </main>
  );
}