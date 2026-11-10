import type { Metadata } from "next";
import ReviewWorkspace from "@/components/specification-review/ReviewWorkspace";

export const metadata: Metadata = {
  title: "Specification Review",
  description: "Review the performance specification for this Monday item",
  robots: { index: false, follow: false },
};

export default function SpecificationReviewPage() {
  return <ReviewWorkspace />;
}
