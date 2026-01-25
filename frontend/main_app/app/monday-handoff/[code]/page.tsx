import { getServerSupabase } from "@/lib/supabase";
import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";

type PageProps = {
  params: { code: string };
};

async function getServerAccessToken(): Promise<string | null> {
  const supabase = await getServerSupabase();
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

export default async function HandoffResolvePage({ params }: PageProps) {
  const baseUrl = process.env.FASTAPI_BASE_URL?.replace(/\/$/, "");
  if (!baseUrl) {
    throw new Error("FASTAPI_BASE_URL is not set");
  }

  const accessToken = await getServerAccessToken();

  if (!accessToken) {
    redirect(`/connect-monday?returnTo=/monday-handoff/${params.code}`);
  }

  const response = await fetch(`${baseUrl}/api/monday/handoff/resolve`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify({ code: params.code }),
    cache: "no-store",
  });

  if (response.status === 401 || response.status === 403) {
    redirect(`/connect-monday?returnTo=/monday-handoff/${params.code}`);
  }

  if (!response.ok) {
    throw new Error(`Resolve failed (${response.status})`);
  }

  const data = (await response.json()) as { externalTaskKey: string };

  // // Fire-and-forget background sync
  // try {
  //   await fetch(`${baseUrl}/api/tasks/${data.externalTaskKey}/sync`, {
  //     method: "POST",
  //     headers: {
  //       "Content-Type": "application/json",
  //       Authorization: `Bearer ${accessToken}`,
  //     },
  //     body: JSON.stringify({ runAsync: true }),
  //     cache: "no-store",
  //   });
  // } catch {
  //   // Ignore sync failures here; user can retry manually
  // }

  redirect(`/tasks/${data.externalTaskKey}`);
}
