import { getServerSupabase } from "@/lib/supabase/server";
import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";

type PageProps = {
  params: Promise<{ code: string }>;
};

async function getServerAccessToken(): Promise<string | null> {
  const supabase = await getServerSupabase();
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

export default async function HandoffResolvePage({ params }: PageProps) {
  const { code } = await params;

  const baseUrl = process.env.FASTAPI_BASE_URL?.replace(/\/$/, "");
  if (!baseUrl) {
    throw new Error("FASTAPI_BASE_URL is not set");
  }

  const accessToken = await getServerAccessToken();

  if (!accessToken) {
    redirect(`/connect-monday?returnTo=/monday-handoff/${code}`);
  }

  const response = await fetch(`${baseUrl}/api/monday/handoff/resolve`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify({ code }),
    cache: "no-store",
  });

  if (response.status === 401 || response.status === 403) {
    redirect(`/connect-monday?returnTo=/monday-handoff/${code}`);
  }

  if (!response.ok) {
    throw new Error(`Resolve failed (${response.status})`);
  }

  const data = (await response.json()) as { externalTaskKey: string };

  redirect(`/tasks/${data.externalTaskKey}`);
}