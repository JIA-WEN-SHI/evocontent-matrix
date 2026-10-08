"use client";

import { useEffect, useState } from "react";
import { MoonStar, Sun } from "lucide-react";

import { Button } from "@/components/ui/button";

type ThemeMode = "light" | "dark";

export default function ThemeToggle() {
  const [mode, setMode] = useState<ThemeMode>("light");

  useEffect(() => {
    const stored = localStorage.getItem("theme-mode") as ThemeMode | null;
    const preferred = stored === "dark" || stored === "light" ? stored : "dark";
    document.documentElement.classList.toggle("dark", preferred === "dark");
    setMode(preferred);
  }, []);

  function onToggle() {
    const next: ThemeMode = mode === "light" ? "dark" : "light";
    setMode(next);
    localStorage.setItem("theme-mode", next);
    document.documentElement.classList.toggle("dark", next === "dark");
  }

  return (
    <Button variant="outline" size="sm" onClick={onToggle} className="gap-2 border-cyan-300/35 bg-slate-950/55 text-cyan-100">
      {mode === "light" ? <MoonStar className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
      <span>{mode === "light" ? "深色模式" : "浅色模式"}</span>
    </Button>
  );
}
