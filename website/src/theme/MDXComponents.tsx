// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The site's half of the shared-source contract.
//
// `frontend/src/docs/toMdx.ts` emits `<Callout>`, `<Steps>`, `<Diagram>` and friends into the
// generated pages. Registering them here makes each tag resolve with NO import line in the page
// itself, which is what lets one file be read by two renderers: the app draws these blocks with
// `DocBody`, the site draws them with the components below, and neither needs to know about the
// other. A per-file import would have to name a path that only one of the two can resolve.
//
// The tag names must stay capitalized. MDX renders a lowercase tag as a plain HTML element and
// never consults this map, so `<callout>` would ship as an unknown element with no error.

import MDXComponents from "@theme-original/MDXComponents";
import Tabs from "@theme/Tabs";
import TabItem from "@theme/TabItem";
import { Callout } from "@site/src/components/Callout";
import { Diagram } from "@site/src/components/Diagram";
import { Step, Steps } from "@site/src/components/Steps";

export default {
  ...MDXComponents,
  Callout,
  Steps,
  Step,
  Diagram,
  // Docusaurus ships these but does not register them, so every page that wants tabs
  // normally opens with two import lines. Registering them keeps the install pages
  // readable, and matters more than convenience for the generated pages: those are
  // written by `toMdx.ts`, which has no way to emit an import that resolves here.
  Tabs,
  TabItem,
};
