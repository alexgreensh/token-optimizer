// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

// Deploy target. Currently GitHub Pages project site:
//   https://alexgreensh.github.io/token-optimizer/
// When a custom (sub)domain is added later, this is a one-line switch:
//   1. set `site` to the custom domain (e.g. https://docs.tokenoptimizer.xyz)
//   2. set BASE = "" below
//   3. add a CNAME file to docs-site/public/ with the domain
// Everything that references BASE (base + the icon <link>s) updates with it.
const SITE = "https://alexgreensh.github.io";
const BASE = "/token-optimizer";

// https://astro.build/config
export default defineConfig({
  site: SITE,
  base: BASE,
  trailingSlash: "ignore",
  integrations: [
    starlight({
      title: "Token Optimizer",
      description:
        "Cut the tokens you waste. Keep the work you'd lose. A fully local context optimizer for Claude Code, Codex, OpenCode, OpenClaw, Hermes, and Copilot.",
      tagline: "Cut the tokens you waste. Keep the work you'd lose.",
      logo: {
        src: "./src/assets/logo.png",
      },
      favicon: "/favicon.ico",
      components: {
        // Enables Astro view transitions so navigation swaps content in place
        // instead of a full page reload (kills the white flash between pages).
        Head: "./src/components/Head.astro",
      },
      customCss: ["./src/styles/theme.css"],
      lastUpdated: true,
      pagination: true,
      editLink: {
        baseUrl:
          "https://github.com/alexgreensh/token-optimizer/edit/main/docs-site/",
      },
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/alexgreensh/token-optimizer",
        },
        {
          icon: "linkedin",
          label: "LinkedIn",
          href: "https://linkedin.com/in/alexgreensh",
        },
      ],
      head: [
        // Rounded T.O mark, served from public/. Starlight injects the .ico via the
        // `favicon` option above; these add the high-res PNG + Apple touch icon.
        {
          tag: "link",
          attrs: { rel: "apple-touch-icon", sizes: "180x180", href: `${BASE}/apple-touch-icon.png` },
        },
        {
          tag: "link",
          attrs: { rel: "icon", type: "image/png", sizes: "32x32", href: `${BASE}/favicon-32x32.png` },
        },
        {
          tag: "link",
          attrs: { rel: "icon", type: "image/png", sizes: "16x16", href: `${BASE}/favicon-16x16.png` },
        },
        {
          tag: "link",
          attrs: { rel: "preconnect", href: "https://fonts.googleapis.com" },
        },
        {
          tag: "link",
          attrs: {
            rel: "preconnect",
            href: "https://fonts.gstatic.com",
            crossorigin: true,
          },
        },
        {
          tag: "link",
          attrs: {
            rel: "stylesheet",
            href: "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap",
          },
        },
      ],
      sidebar: [
        {
          label: "Getting started",
          items: [
            { label: "Introduction", slug: "index" },
            { label: "Quickstart", slug: "start/quickstart" },
            { label: "Reading your first audit", slug: "start/first-audit" },
            { label: "All features at a glance", slug: "start/feature-map" },
          ],
        },
        {
          label: "Core concepts",
          items: [
            { label: "How it works", slug: "concepts/how-it-works" },
            { label: "Why install this first", slug: "concepts/why-first" },
            { label: "The compaction problem", slug: "concepts/compaction-model" },
            { label: "Quality scoring", slug: "concepts/quality-scoring" },
            { label: "Prompt cache economics", slug: "concepts/cache-economics" },
            { label: "Trust and safety", slug: "concepts/trust-and-safety" },
          ],
        },
        {
          label: "Install",
          items: [
            { label: "Choosing your install", slug: "install/overview" },
            { label: "Claude Code (CLI)", slug: "install/claude-code-cli" },
            { label: "Claude Code (VS Code)", slug: "install/claude-code-vscode" },
            { label: "Codex (CLI)", slug: "install/codex-cli" },
            { label: "Codex (Desktop)", slug: "install/codex-desktop" },
            { label: "GitHub Copilot (CLI)", slug: "install/copilot-cli" },
            { label: "GitHub Copilot (VS Code)", slug: "install/copilot-vscode" },
            { label: "OpenCode", slug: "install/opencode" },
            { label: "OpenClaw", slug: "install/openclaw" },
            { label: "Hermes", slug: "install/hermes" },
          ],
        },
        {
          label: "Platforms",
          items: [
            { label: "Platform support", slug: "platforms/overview" },
            { label: "Claude Code", slug: "platforms/claude-code" },
            { label: "Codex", slug: "platforms/codex" },
            { label: "GitHub Copilot", slug: "platforms/copilot" },
            { label: "OpenCode", slug: "platforms/opencode" },
            { label: "OpenClaw", slug: "platforms/openclaw" },
            { label: "Hermes", slug: "platforms/hermes" },
          ],
        },
        {
          label: "Features",
          items: [
            { label: "Setup audit", slug: "features/audit" },
            { label: "The dashboard", slug: "features/dashboard" },
            { label: "Managing skills and MCP", slug: "features/manage-tab" },
            { label: "Active compression", slug: "features/active-compression" },
            { label: "Read cache", slug: "features/read-cache" },
            { label: "Bash output compression", slug: "features/bash-compression" },
            { label: "Quality nudges and loop detection", slug: "features/quality-signals" },
            { label: "Tool result archive", slug: "features/tool-result-archive" },
            { label: "Smart compaction", slug: "features/smart-compaction" },
            { label: "Session continuity", slug: "features/session-continuity" },
            { label: "Keep-Warm", slug: "features/keep-warm" },
            { label: "Cache TTL watchdog", slug: "features/cache-watchdog" },
            { label: "Token Coach", slug: "features/token-coach" },
            { label: "Waste detectors", slug: "features/waste-detectors" },
            { label: "Fleet Auditor", slug: "features/fleet-auditor" },
            { label: "CLAUDE.md injection", slug: "features/routing-and-coach-injection" },
            { label: "Memory health", slug: "features/memory-health" },
            { label: "Attention optimizer", slug: "features/attention-optimizer" },
            { label: "Usage and session analytics", slug: "features/usage-analytics" },
            { label: "The quality status line", slug: "features/status-line" },
          ],
        },
        {
          label: "Reference",
          items: [
            { label: "CLI", slug: "reference/cli" },
            { label: "Configuration", slug: "reference/configuration" },
            { label: "Automatic hooks", slug: "reference/hooks" },
            { label: "Capability matrix", slug: "reference/capability-matrix" },
            { label: "Health and diagnostics", slug: "reference/diagnostics" },
            { label: "Your data and privacy", slug: "reference/data-and-privacy" },
            { label: "Benchmarks", slug: "reference/benchmarks" },
            { label: "How it compares", slug: "reference/comparison" },
            { label: "License and pricing", slug: "reference/license" },
          ],
        },
      ],
    }),
  ],
});
