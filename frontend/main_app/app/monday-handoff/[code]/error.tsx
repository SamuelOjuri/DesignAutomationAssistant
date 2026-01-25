"use client";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

export default function HandoffError({ error }: { error: Error }) {
  return (
    <main className="mx-auto mt-10 max-w-lg px-4">
      <Alert variant="destructive">
        <AlertTitle>Handoff failed</AlertTitle>
        <AlertDescription>{error.message}</AlertDescription>
      </Alert>
    </main>
  );
}
