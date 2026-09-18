import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "../../lib/utils";

/**
 * Sheet — side/bottom panel built on the Dialog primitive (shadcn pattern).
 * `bottom` is the mobile inspector; `left`/`right` are desktop drawers.
 */
export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;
export const SheetClose = DialogPrimitive.Close;
export const SheetTitle = DialogPrimitive.Title;

const SIDE_CLASSES: Record<"right" | "left" | "bottom", string> = {
  right: "inset-y-0 right-0 w-80 max-w-[85vw] border-l",
  left: "inset-y-0 left-0 w-80 max-w-[85vw] border-r",
  bottom: "inset-x-0 bottom-0 h-[75dvh] w-full rounded-t-lg border-t",
};

export const SheetContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
    side?: "right" | "left" | "bottom";
  }
>(({ className, children, side = "right", ...props }, ref) => (
  <DialogPrimitive.Portal>
    <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/60" />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        "fixed z-50 border-border bg-bg-1 p-4 shadow-pop",
        SIDE_CLASSES[side],
        className,
      )}
      {...props}
    >
      {children}
      <DialogPrimitive.Close
        className="absolute top-3 right-3 rounded p-0.5 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
        aria-label="Close"
      >
        <X className="size-3.5" />
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPrimitive.Portal>
));
SheetContent.displayName = "SheetContent";
