// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// Stage 4's missing-key gate (docs/I18N_PLAN.md): every catalog key the tree references
// resolves in `locales/en/ui.json`, and every catalog message is a message a reader can
// receive. Stage 4's fan-out is why this exists: extraction agents convert files while
// the orchestrator merges their key maps into the catalog in a separate step, so the
// failure this catches is precisely "a key an agent used but did not return", which
// would otherwise render as a raw dotted id on an operator's screen.
//
// Three directions, because each fails differently:
//   1. A referenced key missing from the catalog renders as the key itself.
//   2. A catalog key nothing references is a merge typo or dead copy, and dead copy
//      still gets sent to translators.
//   3. A key present but unparseable as ICU garbles at format time, which no
//      existence check sees.
//
// Keys are read from the AST: the first argument of `t()`/`i18next.t()`/`i18n.t()` and
// the `i18nKey` attribute. A non-literal first argument is a runtime-composed key the
// gate cannot resolve, so composition is confined to the files in DYNAMIC, each claiming
// the namespaces it composes over; those namespaces are exempt from direction 2 and are
// covered by their own suites (why.test.ts drives the composer over the real catalog).

import { IntlMessageFormat } from "intl-messageformat";
import ts from "typescript";
import { describe, expect, it } from "vitest";

import ui from "./locales/en/ui.json";
import { shippedSource, sourceText, srcRelative } from "./test/sources";

// The files allowed to hand t() a computed key, and the catalog namespaces they own.
// A namespace listed here is proven by that file's own tests, not by this gate.
const DYNAMIC: Record<string, string[]> = {
  "why.ts": ["why."],
};

const T_CALLEES = new Set(["t", "i18next.t", "i18n.t"]);

type Ref = { file: string; line: number; key: string };
type Refs = { literal: Ref[]; dynamic: Ref[] };

export function catalogRefs(fileName: string, text: string): Refs {
  const sf = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const refs: Refs = { literal: [], dynamic: [] };
  const record = (node: ts.Node, key: string | null) => {
    const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
    if (key === null) refs.dynamic.push({ file: fileName, line: line + 1, key: "<computed>" });
    else refs.literal.push({ file: fileName, line: line + 1, key });
  };
  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node) && T_CALLEES.has(node.expression.getText(sf))) {
      const key = node.arguments[0];
      if (key === undefined) {
        // no key at all: let it fall through as computed, someone is doing something odd
        record(node, null);
      } else if (ts.isStringLiteralLike(key)) {
        record(key, key.text);
      } else {
        record(key, null);
      }
    } else if (ts.isJsxAttribute(node) && node.name.getText(sf) === "i18nKey" && node.initializer) {
      if (ts.isStringLiteral(node.initializer)) record(node.initializer, node.initializer.text);
      else record(node.initializer, null);
    }
    node.forEachChild(visit);
  };
  visit(sf);
  return refs;
}

/** The catalog's leaf keys, dot-joined the way t() spells them. */
function leaves(node: unknown, prefix = ""): Record<string, string> {
  if (typeof node === "string") return { [prefix]: node };
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
    Object.assign(out, leaves(v, prefix ? `${prefix}.${k}` : k));
  }
  return out;
}

const CATALOG = leaves(ui);

function allRefs(): Refs {
  const all: Refs = { literal: [], dynamic: [] };
  for (const path of shippedSource()) {
    const refs = catalogRefs(srcRelative(path), sourceText(srcRelative(path)));
    all.literal.push(...refs.literal);
    all.dynamic.push(...refs.dynamic);
  }
  return all;
}

describe("the i18n key gate", () => {
  it("every key the tree references exists in the catalog", () => {
    const missing = allRefs()
      .literal.filter((r) => !(r.key in CATALOG))
      .map((r) => `${r.file}:${r.line}: t("${r.key}")`);
    expect(
      missing,
      `keys referenced but absent from locales/en/ui.json (an operator would see the raw id):\n${missing.join("\n")}`,
    ).toEqual([]);
  });

  it("computed keys live only in the files DYNAMIC names", () => {
    const strays = allRefs()
      .dynamic.filter((r) => !(r.file in DYNAMIC))
      .map((r) => `${r.file}:${r.line}`);
    expect(
      strays,
      `t() handed a computed key outside DYNAMIC, which this gate cannot resolve:\n${strays.join("\n")}`,
    ).toEqual([]);
  });

  it("every catalog key is referenced, or sits in a namespace a DYNAMIC file owns", () => {
    const used = new Set(allRefs().literal.map((r) => r.key));
    const ownedPrefixes = Object.values(DYNAMIC).flat();
    const orphans = Object.keys(CATALOG).filter(
      (key) => !used.has(key) && !ownedPrefixes.some((p) => key.startsWith(p)),
    );
    expect(
      orphans,
      `catalog keys nothing references (a merge typo, or dead copy headed to translators):\n${orphans.join("\n")}`,
    ).toEqual([]);
  });

  it("every catalog message parses as ICU", () => {
    // The parser must be able to fail, or the loop below proves nothing (rule 145).
    expect(() => new IntlMessageFormat("{n, plural", "en-US")).toThrow();
    const broken: string[] = [];
    for (const [key, message] of Object.entries(CATALOG)) {
      try {
        new IntlMessageFormat(message, "en-US");
      } catch (err) {
        broken.push(`${key}: ${String(err)}`);
      }
    }
    expect(broken, `catalog messages ICU cannot parse:\n${broken.join("\n")}`).toEqual([]);
  });

  it("reads every spelling a reference is written in (rule 147)", () => {
    const refs = catalogRefs(
      "probe.tsx",
      `const a = t("a.literal");
       const b = i18next.t("b.literal", { n });
       const c = <Trans i18nKey="c.literal" />;
       const d = i18n.t(\`d.\${shape}\`);
       const e = t(key);
       const f = <Trans i18nKey={key} />;`,
    );
    expect(refs.literal.map((r) => r.key)).toEqual(["a.literal", "b.literal", "c.literal"]);
    expect(refs.dynamic.map((r) => r.line)).toEqual([4, 5, 6]);
  });
});
