import { Suspense } from "react";
import LoginClient from "./login-client";

type PageProps = {
  searchParams?: Promise<{ returnTo?: string | string[] }>;
};

export default async function LoginPage({ searchParams }: PageProps) {
  const resolvedSearchParams = await searchParams;
  const rawReturnTo = resolvedSearchParams?.returnTo;
  const returnTo = Array.isArray(rawReturnTo) ? rawReturnTo[0] : rawReturnTo;
  const safeReturnTo = returnTo || "/";

  return (
    <Suspense fallback={null}>
      <LoginClient returnTo={safeReturnTo} />
    </Suspense>
  );
}