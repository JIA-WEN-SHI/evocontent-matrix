import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center rounded-lg border text-sm font-medium transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default:
          "border-primary bg-primary text-primary-foreground hover:bg-primary/90 dark:border-cyan-300/60 dark:bg-cyan-400/20 dark:text-cyan-50 dark:hover:bg-cyan-300/30",
        secondary:
          "border-border bg-secondary text-secondary-foreground hover:bg-secondary/80 dark:border-sky-300/35 dark:bg-slate-900/75 dark:text-cyan-100",
        outline:
          "border-border bg-background text-foreground hover:bg-accent dark:border-cyan-300/35 dark:bg-slate-950/55 dark:text-cyan-100 dark:hover:bg-cyan-900/30",
        destructive:
          "border-destructive/50 bg-destructive/10 text-destructive hover:bg-destructive/20 dark:text-red-100",
        ghost: "border-transparent bg-transparent text-foreground hover:bg-accent dark:text-cyan-100 dark:hover:bg-cyan-900/25"
      },
      size: {
        default: "h-10 px-4 py-2",
        sm: "h-9 px-3",
        lg: "h-11 px-8"
      }
    },
    defaultVariants: {
      variant: "default",
      size: "default"
    }
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />;
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
