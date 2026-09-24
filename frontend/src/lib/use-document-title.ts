import { useEffect } from "react";

/** Per-route document titles ("Runs · Axiom"). Restores nothing on unmount —
 * the next route always sets its own title. */
export function useDocumentTitle(title: string): void {
  useEffect(() => {
    document.title = title;
  }, [title]);
}
