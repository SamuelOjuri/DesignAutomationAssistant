import HandoffResolveClient from "./handoff-resolve-client";

export const dynamic = "force-dynamic";

type PageProps = {
  params: Promise<{ code: string }>;
};

export default async function HandoffResolvePage({ params }: PageProps) {
  const { code } = await params;
  return <HandoffResolveClient code={code} />;
}