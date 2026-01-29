import { Suspense } from "react";
import SetPasswordClient from "./set-password-client";

type PageProps = {
  searchParams?: { returnTo?: string | string[] };
};

export default function SetPasswordPage({ searchParams }: PageProps) {
  const rawReturnTo = searchParams?.returnTo;
  const returnTo = Array.isArray(rawReturnTo) ? rawReturnTo[0] : rawReturnTo;

  return (
    <Suspense fallback={null}>
      <SetPasswordClient returnTo={returnTo || "/"} />
    </Suspense>
  );
}