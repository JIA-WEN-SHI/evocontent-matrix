import type { Metadata } from "next";
import AdminShell from "@/components/AdminShell";

import "./globals.css";

export const metadata: Metadata = {
  title: "单账号内容系统 | 数字员工中台",
  description: "面向日本赛道的可复用、可扩展内容运营与自进化系统。"
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body suppressHydrationWarning>
        <AdminShell>{children}</AdminShell>
      </body>
    </html>
  );
}
