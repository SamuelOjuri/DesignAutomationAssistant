import { Suspense } from "react";
import ConnectMondayClient from "./connect-monday-client";

type PageProps = {
  searchParams?: Promise<{ returnTo?: string | string[] }>;
};

export default async function ConnectMondayPage({ searchParams }: PageProps) {
  const resolvedSearchParams = await searchParams;
  const rawReturnTo = resolvedSearchParams?.returnTo;
  const returnTo = Array.isArray(rawReturnTo) ? rawReturnTo[0] : rawReturnTo;
  const safeReturnTo = returnTo || "/";

  return (
    <Suspense fallback={null}>
      <ConnectMondayClient returnTo={safeReturnTo} />
    </Suspense>
  );
}