import { Suspense } from "react";
import LoginClient from "./login-client";

type PageProps = {
  searchParams?: { returnTo?: string | string[] };
};

export default function LoginPage({ searchParams }: PageProps) {
  const rawReturnTo = searchParams?.returnTo;
  const returnTo = Array.isArray(rawReturnTo) ? rawReturnTo[0] : rawReturnTo;
  const safeReturnTo = returnTo || "/";

  return (
    <Suspense fallback={null}>
      <LoginClient returnTo={safeReturnTo} />
    </Suspense>
  );
}