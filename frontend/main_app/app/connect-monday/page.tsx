import { Suspense } from "react";
import ConnectMondayClient from "./connect-monday-client";

type PageProps = {
  searchParams?: { returnTo?: string | string[] };
};

export default function ConnectMondayPage({ searchParams }: PageProps) {
  const rawReturnTo = searchParams?.returnTo;
  const returnTo = Array.isArray(rawReturnTo) ? rawReturnTo[0] : rawReturnTo;
  const safeReturnTo = returnTo || "/";

  return (
    <Suspense fallback={null}>
      <ConnectMondayClient returnTo={safeReturnTo} />
    </Suspense>
  );
}