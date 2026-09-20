import { ProjectDeliveryPage } from "@/components/project-delivery/root";
import type { Route } from "./+types/page";

export default function DeliveryPage({ params }: Route.ComponentProps) {
  return <ProjectDeliveryPage workspaceSlug={params.workspaceSlug} projectId={params.projectId} view="releases" />;
}
