import { Suspense } from "react";
import AuthCallbackClient from "./auth-callback-client";

type PageProps = {
  searchParams?: Promise<{ returnTo?: string | string[] }>;
};

export default async function AuthCallbackPage({ searchParams }: PageProps) {
  const resolvedSearchParams = await searchParams;
  const rawReturnTo = resolvedSearchParams?.returnTo;
  const returnTo = Array.isArray(rawReturnTo) ? rawReturnTo[0] : rawReturnTo;

  return (
    <Suspense fallback={null}>
      <AuthCallbackClient returnTo={returnTo || "/"} />
    </Suspense>
  );
}