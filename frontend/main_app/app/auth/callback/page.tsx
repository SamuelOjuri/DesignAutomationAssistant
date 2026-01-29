import { Suspense } from "react";
import AuthCallbackClient from "./auth-callback-client";

type PageProps = {
  searchParams?: { returnTo?: string | string[] };
};

export default function AuthCallbackPage({ searchParams }: PageProps) {
  const rawReturnTo = searchParams?.returnTo;
  const returnTo = Array.isArray(rawReturnTo) ? rawReturnTo[0] : rawReturnTo;

  return (
    <Suspense fallback={null}>
      <AuthCallbackClient returnTo={returnTo || "/"} />
    </Suspense>
  );
}