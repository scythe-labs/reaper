import { readFileSync } from "node:fs";
import { join } from "node:path";
import { JSDOM } from "/opt/reaper_1/.claude/worktrees/agent-a7cb46be9e5088c69/frontend/node_modules/jsdom/lib/api.js";

const root = process.argv[2];
const barrel = readFileSync(join(root, "index.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const files = [...barrel.matchAll(/@import\s+"\.\/([^"]+)"/g)].map((m) => m[1]);
const CSS = files.map((f) => readFileSync(join(root, f), "utf8")).join("");

const CONTROLS = {
  ".filter-chip button": `<span class="filter-chip">a<button type="button">x</button></span>`,
  ".fchip-x":
    `<span class="filter-anchor"><span class="fchip">` +
    `<button type="button" class="fchip-body">a</button>` +
    `<button type="button" class="fchip-x">x</button></span></span>`,
  ".tag-chip button":
    `<div class="tag-editor"><div class="tag-chips"><span class="tag-chip">a` +
    `<button type="button">x</button></span></div></div>`,
  ".inst-chip .chip-x":
    `<span class="inst-chip"><span class="tick">t</span>` +
    `<button type="button" class="chip-edit">a</button>` +
    `<button type="button" class="chip-x"><span>x</span></button></span>`,
  ".inst-chip .chip-edit":
    `<span class="inst-chip"><span class="tick">t</span>` +
    `<button type="button" class="chip-x"><span>x</span></button>` +
    `<button type="button" class="chip-edit">a</button></span>`,
  ".bar-x":
    `<div class="bar-line"><span class="bar-src">a</span><span class="bar-set">b</span>` +
    `<button type="button" class="bar-x">x</button></div>`,
  ".nudge-x":
    `<div class="scan-nudge"><span class="nudge-text">a</span><span class="nudge-actions">` +
    `<button type="button" class="primary sm">Show latest</button>` +
    `<button type="button" class="nudge-x">x</button></span></div>`,
};

const PROPS = [
  "display",
  "align-items",
  "justify-content",
  "place-items",
  "width",
  "height",
  "min-width",
  "min-height",
  "background-color",
  "background-image",
  "background",
  "border-top-style",
  "border-top-width",
  "border-top-color",
  "border-radius",
  "box-shadow",
  "color",
  "cursor",
  "font-family",
  "font-size",
  "font-style",
  "font-variant",
  "font-weight",
  "font-stretch",
  "line-height",
  "padding-top",
  "padding-right",
  "padding-bottom",
  "padding-left",
  "opacity",
  "overflow-wrap",
  "transition",
  "transform",
  "outline",
  "text-align",
  "white-space",
  "flex-shrink",
  "gap",
];

const dom = new JSDOM(`<!doctype html><html><head><style>${CSS}</style></head><body></body></html>`);
const { document, getComputedStyle } = dom.window;
const out = {};
for (const [name, html] of Object.entries(CONTROLS)) {
  const host = document.createElement("div");
  host.id = "root";
  host.innerHTML = html;
  document.body.appendChild(host);
  const buttons = host.querySelectorAll("button");
  const target = name === ".inst-chip .chip-edit" ? host.querySelector(".chip-edit") : buttons[buttons.length - 1];
  const style = getComputedStyle(target);
  out[name] = Object.fromEntries(PROPS.map((p) => [p, style.getPropertyValue(p)]));
}
console.log(JSON.stringify(out, null, 1));
