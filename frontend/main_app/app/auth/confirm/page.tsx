import { Suspense } from "react";
import ConfirmClickClient from "./confirm-click-client";

type PageProps = {
  searchParams?: {
    token?: string;
    type?: string;
    redirect_to?: string;
    returnTo?: string;
  };
};

export default function ConfirmPage({ searchParams }: PageProps) {
  const token = searchParams?.token;
  const type = searchParams?.type;
  const returnTo = searchParams?.redirect_to || searchParams?.returnTo || "/";

  return (
    <Suspense fallback={null}>
      <ConfirmClickClient token={token} type={type} returnTo={returnTo} />
    </Suspense>
  );
}