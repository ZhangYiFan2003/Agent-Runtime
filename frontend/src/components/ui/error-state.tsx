import { cn } from "../../lib/utils";

/** Standard inline error block — normalized message only, never a raw stack. */
export function ErrorState({
  title = "Something went wrong",
  message,
  className,
}: {
  title?: string;
  message: string;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "rounded-md border border-danger/40 bg-danger/5 px-3 py-2.5 text-13",
        className,
      )}
    >
      <div className="font-medium text-danger">{title}</div>
      <div className="mt-0.5 text-fg-1">{message}</div>
    </div>
  );
}
