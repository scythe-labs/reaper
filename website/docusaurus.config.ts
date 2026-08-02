// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The manual site. It owns no words: every page it serves lives in `../manual`, half of them
// generated from the app's own help pages by `frontend/src/docs/toMdx.ts`. Keeping the content
// outside this directory is what lets the app and the site share one source, and it is the shape
// Seerr runs in production (`path: '../docs'` from `gen-docs/`).
//
// Never symlink the content in. Docusaurus has had open bugs against symlinked docs directories
// for years (facebook/docusaurus#6257, #10751); the relative `path` below is the supported route.

import type { Config } from "@docusaurus/types";
import type * as Preset from "@docusaurus/preset-classic";
import { themes } from "prism-react-renderer";

const ORG = "scythe-labs";
const REPO = "reaper";

const config: Config = {
  title: "Reaper",
  tagline: "Explainable media library pruning for Plex",
  favicon: "img/favicon.svg",

  // GitHub Pages for a project repository serves under /<repo>/. Both halves are read by the
  // deploy workflow and by every absolute link the theme builds.
  url: `https://${ORG}.github.io`,
  baseUrl: `/${REPO}/`,
  organizationName: ORG,
  projectName: REPO,
  trailingSlash: false,

  // A broken link is a page an operator lands on while deciding what to delete. Fail the build.
  onBrokenLinks: "throw",
  onBrokenMarkdownLinks: "throw",

  // No `future: { v4: true }`. It pulls in `@docusaurus/faster` (rspack) as a required
  // dependency, and v4 has not shipped, so there is nothing yet to ease a migration onto. Revisit
  // when it does, or earlier if this build gets slow enough in CI to be worth the dependency.

  presets: [
    [
      "classic",
      {
        docs: {
          // The content lives outside this directory on purpose. See the header.
          path: "../manual",
          routeBasePath: "/",
          sidebarPath: "./sidebars.ts",
          // A generated page must not invite an edit that the next `gen-manual` overwrites, so
          // the pencil points at the manual and the banner in each generated file says where the
          // words actually come from.
          editUrl: `https://github.com/${ORG}/${REPO}/edit/dev/manual/`,
          showLastUpdateTime: true,
        },
        blog: false,
        theme: { customCss: "./src/css/custom.css" },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: { respectPrefersColorScheme: true },
    navbar: {
      title: "Reaper",
      logo: { alt: "Reaper", src: "img/favicon.svg" },
      items: [
        { type: "docSidebar", sidebarId: "manual", position: "left", label: "Manual" },
        {
          href: `https://github.com/${ORG}/${REPO}`,
          label: "GitHub",
          position: "right",
        },
      ],
    },
    footer: {
      style: "dark",
      links: [
        {
          title: "Manual",
          items: [
            { label: "What Reaper does", to: "/getting-started/overview" },
            { label: "Install", to: "/getting-started/install" },
            { label: "Understanding policy", to: "/policy/understanding-policy" },
          ],
        },
        {
          title: "Project",
          items: [
            { label: "GitHub", href: `https://github.com/${ORG}/${REPO}` },
            { label: "Issues", href: `https://github.com/${ORG}/${REPO}/issues` },
          ],
        },
      ],
      copyright: "Reaper is AGPL-3.0-or-later. Every ambiguity resolves toward keeping the file.",
    },
    prism: {
      theme: themes.github,
      darkTheme: themes.dracula,
      additionalLanguages: ["bash", "yaml", "ini"],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
