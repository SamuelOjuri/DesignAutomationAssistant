import { Suspense } from "react";
import SetPasswordClient from "./set-password-client";

type PageProps = {
  searchParams?: Promise<{ returnTo?: string | string[] }>;
};

export default async function SetPasswordPage({ searchParams }: PageProps) {
  const resolvedSearchParams = await searchParams;
  const rawReturnTo = resolvedSearchParams?.returnTo;
  const returnTo = Array.isArray(rawReturnTo) ? rawReturnTo[0] : rawReturnTo;

  return (
    <Suspense fallback={null}>
      <SetPasswordClient returnTo={returnTo || "/"} />
    </Suspense>
  );
}