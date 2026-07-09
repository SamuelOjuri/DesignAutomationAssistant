import { Suspense } from "react";
import ConfirmClickClient from "./confirm-click-client";

type PageProps = {
  searchParams?: Promise<{
    token?: string;
    type?: string;
    redirect_to?: string;
    returnTo?: string;
  }>;
};

export default async function ConfirmPage({ searchParams }: PageProps) {
  const resolvedSearchParams = await searchParams;
  const token = resolvedSearchParams?.token;
  const type = resolvedSearchParams?.type;
  const returnTo = resolvedSearchParams?.redirect_to || resolvedSearchParams?.returnTo || "/";

  return (
    <Suspense fallback={null}>
      <ConfirmClickClient token={token} type={type} returnTo={returnTo} />
    </Suspense>
  );
}