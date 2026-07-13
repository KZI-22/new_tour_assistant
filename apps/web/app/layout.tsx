import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "远行 · AI 旅行助手",
  description: "用可配置的大语言模型规划下一段旅程。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}

