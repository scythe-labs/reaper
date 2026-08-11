import { readFileSync } from "node:fs";
import { join } from "node:path";
import { JSDOM } from "/opt/reaper_1/.claude/worktrees/agent-a7cb46be9e5088c69/frontend/node_modules/jsdom/lib/api.js";

const root = process.argv[2];
const barrel = readFileSync(join(root, "index.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const files = [...barrel.matchAll(/@import\s+"\.\/([^"]+)"/g)].map((m) => m[1]);
const segs = [];
let CSS = "";
for (const f of files) {
  segs.push({ f, at: CSS.length });
  CSS += readFileSync(join(root, f), "utf8");
}
function siteOf(off) {
  let s = segs[0];
  for (const x of segs) {
    if (x.at > off) break;
    s = x;
  }
  return `${s.f}:${CSS.slice(s.at, off).split("\n").length}`;
}

// Strip comments but keep offsets by replacing with spaces.
const code = CSS.replace(/\/\*[\s\S]*?\*\//g, (m) => " ".repeat(m.length));

// Naive rule walker: collect (selectorText, body, offset, atContext)
const rules = [];
let i = 0;
const stack = [];
while (i < code.length) {
  const open = code.indexOf("{", i);
  if (open === -1) break;
  const prelude = code.slice(i, open).trim();
  const preludeStart = i + (code.slice(i, open).length - code.slice(i, open).trimStart().length);
  if (prelude.startsWith("@")) {
    // at-rule with a block: descend
    stack.push(prelude);
    i = open + 1;
    // find matching close later via a counter approach below
    continue;
  }
  // find matching close brace
  let depth = 1;
  let j = open + 1;
  while (j < code.length && depth > 0) {
    if (code[j] === "{") depth++;
    else if (code[j] === "}") depth--;
    j++;
  }
  rules.push({ sel: prelude, body: code.slice(open + 1, j - 1), off: preludeStart, at: [...stack] });
  i = j;
  // pop any at-rule blocks that closed
  while (i < code.length && /^\s*\}/.test(code.slice(i, i + 5))) {
    stack.pop();
    i = code.indexOf("}", i) + 1;
  }
}

function specificity(sel) {
  let s = sel;
  // :not(...)/:is(...) -> use inner max (approximation: count inner)
  let a = 0,
    b = 0,
    c = 0;
  s = s.replace(/::[a-zA-Z-]+/g, () => {
    c++;
    return " ";
  });
  s = s.replace(/:(not|is|has)\(([^()]*)\)/g, (_m, _k, inner) => {
    const [ia, ib, ic] = specificity(inner.split(",")[0]);
    a += ia;
    b += ib;
    c += ic;
    return " ";
  });
  s = s.replace(/:where\([^()]*\)/g, " ");
  s = s.replace(/#[\w-]+/g, () => {
    a++;
    return " ";
  });
  s = s.replace(/\.[\w-]+/g, () => {
    b++;
    return " ";
  });
  s = s.replace(/\[[^\]]*\]/g, () => {
    b++;
    return " ";
  });
  s = s.replace(/:[a-zA-Z-]+(\([^()]*\))?/g, () => {
    b++;
    return " ";
  });
  s.replace(/(^|[\s>+~])([a-zA-Z][\w-]*)/g, (_m, _p, t) => {
    c++;
    return " ";
  });
  return [a, b, c];
}

const CONTROLS = {
  ".filter-chip button": [`<span class="filter-chip">a<button type="button">x</button></span>`, "button"],
  ".fchip-x": [
    `<span class="filter-anchor"><span class="fchip">` +
      `<button type="button" class="fchip-body">a</button>` +
      `<button type="button" class="fchip-x">x</button></span></span>`,
    ".fchip-x",
  ],
  ".tag-chip button": [
    `<div class="tag-editor"><div class="tag-chips"><span class="tag-chip">a` +
      `<button type="button">x</button></span></div></div>`,
    ".tag-chip button",
  ],
  ".inst-chip .chip-x": [
    `<span class="inst-chip"><span class="tick">t</span>` +
      `<button type="button" class="chip-edit">a</button>` +
      `<button type="button" class="chip-x"><span>x</span></button></span>`,
    ".chip-x",
  ],
  ".inst-chip .chip-edit": [
    `<span class="inst-chip"><span class="tick">t</span>` +
      `<button type="button" class="chip-x"><span>x</span></button>` +
      `<button type="button" class="chip-edit">a</button></span>`,
    ".chip-edit",
  ],
  ".bar-x": [
    `<div class="bar-line"><span class="bar-src">a</span><span class="bar-set">b</span>` +
      `<button type="button" class="bar-x">x</button></div>`,
    ".bar-x",
  ],
};

const dom = new JSDOM(`<!doctype html><html><body></body></html>`);
const { document } = dom.window;

const out = {};
for (const [name, [html, pick]] of Object.entries(CONTROLS)) {
  const host = document.createElement("div");
  host.id = "root";
  host.innerHTML = html;
  document.body.appendChild(host);
  const el = host.querySelector(pick);
  const hits = [];
  for (const r of rules) {
    for (const one of r.sel.split(",")) {
      const sel = one.trim();
      if (!sel) continue;
      // strip state pseudo-classes for matching; record them
      const states = [...sel.matchAll(/:{1,2}(hover|focus|focus-visible|focus-within|active|disabled|first-child|last-child|before|after|not\([^)]*\))/g)].map((m) => m[0]);
      const probe = sel
        .replace(/::[a-zA-Z-]+/g, "")
        .replace(/:(not|is)\([^()]*\)/g, "")
        .replace(/:[a-zA-Z-]+(\([^()]*\))?/g, "");
      let m = false;
      try {
        m = probe.trim() ? el.matches(probe.trim()) : false;
      } catch {
        m = false;
      }
      if (m) {
        hits.push({
          sel,
          spec: specificity(sel).join(","),
          site: siteOf(r.off),
          at: r.at.join("|"),
          props: r.body
            .split(";")
            .map((d) => d.split(":")[0].trim())
            .filter(Boolean)
            .join(" "),
        });
      }
    }
  }
  out[name] = hits;
}
console.log(JSON.stringify(out, null, 1));
