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
// The site header and the footer say this same line, so it is declared once.
const TAGLINE = "Grave decisions, clearly explained";

const config: Config = {
  title: "Reaper",
  tagline: TAGLINE,
  favicon: "img/favicon.svg",

  // Cloudflare Pages serves this from its own subdomain, so it sits at the root rather than
  // under /<repo>/. The project builds this directory out of the repository it already lives
  // in, and its four settings are in the Cloudflare dashboard rather than in any file here:
  //
  //   Root directory           website
  //   Build command            git fetch --unshallow || true && npm ci && npm run build
  //   Build output directory   build
  //   Custom domain            docs.scythelabs.dev
  //
  // The `--unshallow` is load-bearing. Pages clones one commit deep, and `showLastUpdateTime`
  // below reads each page's git history, so without it every page reports the same date.
  url: "https://docs.scythelabs.dev",
  baseUrl: "/",
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

  // Search is built into the site rather than served from one. Algolia DocSearch is the usual
  // answer and it crawls the deployed site, which means the index only covers what is public
  // and every query leaves the reader's browser. Reaper is self-hosted, and an operator
  // reading this while deciding what to delete may be on a box with no route to the internet,
  // so the index is generated at build time and shipped with the pages it describes.
  //
  // `hashed` puts a content hash in the index filename, so a reader who has the old site
  // cached does not search yesterday's manual. `docsRouteBasePath` has to track the preset's
  // `routeBasePath` above: the manual is served from the root, and the default here is
  // "/docs", which indexes nothing.
  themes: [
    [
      "@easyops-cn/docusaurus-search-local",
      {
        hashed: true,
        // Both of these track the preset above: the manual is not in this directory, and it
        // is served from the root. The defaults are "docs" and "/docs", and leaving either
        // wrong costs a warning on every build, which is how a real one goes unread.
        docsDir: "../manual",
        docsRouteBasePath: "/",
        indexBlog: false,
        highlightSearchTermsOnTargetPage: true,
      },
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
          href: `https://github.com/${ORG}/${REPO}/issues`,
          label: "Issues",
          position: "right",
        },
        {
          href: `https://github.com/${ORG}/${REPO}/discussions`,
          label: "Discussions",
          position: "right",
        },
        {
          href: "https://hosted.weblate.org/engage/reaper/",
          label: "Translate",
          position: "right",
        },
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
      copyright: `Reaper is AGPL-3.0-or-later. ${TAGLINE}.`,
    },
    prism: {
      theme: themes.github,
      darkTheme: themes.dracula,
      additionalLanguages: ["bash", "yaml", "ini"],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
