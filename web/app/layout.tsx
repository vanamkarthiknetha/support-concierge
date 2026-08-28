import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Support Concierge",
  description:
    "Multi-agent support ticket triage where the safety rules live in code, not in prompts.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen grid-bg">
        <header className="border-b border-edge bg-panel/70 backdrop-blur sticky top-0 z-20">
          <div className="mx-auto max-w-6xl px-5 h-14 flex items-center gap-6">
            <Link href="/" className="font-semibold tracking-tight">
              Support<span className="text-accent">Concierge</span>
            </Link>
            <nav className="flex items-center gap-5 text-sm text-muted">
              <Link href="/queues" className="hover:text-text transition-colors">
                Queues
              </Link>
              <Link href="/stats" className="hover:text-text transition-colors">
                Stats
              </Link>
              <Link
                href="/tickets/TCK-1013"
                className="hover:text-escalate transition-colors"
              >
                Injection demo
              </Link>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-5 py-8">{children}</main>
        <footer className="mx-auto max-w-6xl px-5 py-10 text-xs text-dim">
          Take-home prototype. Outbound sending is a logged no-op — approving a draft
          records the decision, it does not email anyone.
        </footer>
      </body>
    </html>
  );
}
