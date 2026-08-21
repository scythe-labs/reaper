// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// Stage 4's missing-key gate (docs/history/I18N_PLAN.md): every catalog key the tree references
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
import { leaves } from "./test/catalog";
import { shippedSource, sourceText, srcRelative } from "./test/sources";

// The files allowed to hand t() a computed key, and the catalog namespaces they own.
// A namespace listed here is proven by that file's own tests, not by this gate.
// why.ts composes under any namespace (composeIn). "chip." and "warning." are #868 phase 2/3
// (status chip text, policy warnings). Phase 5's four one-off server sentences add three
// leaf keys with no id variant beside them ("lists.plexError", "services.modal.mapError") or
// with a whole small namespace of their own ("policySim.staleReason.", "services.discord.
// testResult."), registered the same way.
// PolicyRuleEditors.tsx reads a field's label, help paragraph and unit by its server key
// (`why.field.<key>`, `policyRules.fieldHelp.<key>`, `policyRules.fieldUnit.<key>`) now that
// the vocabulary endpoint no longer ships them (#868 phase 4); proven complete by
// `test_review_chips.py::test_every_field_and_fixed_check_has_catalog_copy` for the label and
// by `tests/test_review_chips.py`'s pinned help/unit sets for the other two.
const DYNAMIC: Record<string, string[]> = {
  "why.ts": [
    "why.",
    "chip.",
    "warning.",
    "policySim.staleReason.",
    "lists.plexError",
    "services.discord.testResult.",
    "services.modal.mapError",
  ],
  "components/PolicyRuleEditors.tsx": [
    "why.field.",
    "policyRules.fieldHelp.",
    "policyRules.fieldUnit.",
  ],
};

const T_CALLEES = new Set(["t", "i18next.t", "i18n.t"]);

// A `.ts` file parsed as TSX misreads generics as JSX, and a t() call inside the
// misparsed region can drop out of the walk (see the twin note in i18n-extraction.test.ts).
const scriptKind = (fileName: string) =>
  fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;

type Ref = {
  file: string;
  line: number;
  key: string;
  /** How the message will render: t() interpolates text only, Trans can map tags. */
  via: "t" | "trans";
  /** Trans only: the tag names its `components` prop maps; null when the prop is
   *  absent; "opaque" when it is an expression this file-local reader cannot resolve. */
  components: string[] | "opaque" | null;
};
type Refs = { literal: Ref[]; dynamic: Ref[] };

export function catalogRefs(fileName: string, text: string): Refs {
  const sf = ts.createSourceFile(
    fileName,
    text,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(fileName),
  );
  const refs: Refs = { literal: [], dynamic: [] };

  // A component whose own data is named `t` destructures the translator under another
  // name (`const { t: tr } = useTranslation()`), so the callee set is per-file: the three
  // canonical spellings plus every alias this file binds that way (rule 147).
  const callees = new Set(T_CALLEES);
  const collectAliases = (node: ts.Node): void => {
    if (
      ts.isVariableDeclaration(node) &&
      node.initializer &&
      ts.isCallExpression(node.initializer) &&
      node.initializer.expression.getText(sf) === "useTranslation" &&
      ts.isObjectBindingPattern(node.name)
    ) {
      for (const el of node.name.elements) {
        const prop = el.propertyName?.getText(sf) ?? el.name.getText(sf);
        if (prop === "t") callees.add(el.name.getText(sf));
      }
    }
    node.forEachChild(collectAliases);
  };
  collectAliases(sf);

  const record = (
    node: ts.Node,
    key: string | null,
    via: "t" | "trans" = "t",
    components: string[] | "opaque" | null = null,
  ) => {
    const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
    const at = { file: fileName, line: line + 1, via, components };
    if (key === null) refs.dynamic.push({ ...at, key: "<computed>" });
    else refs.literal.push({ ...at, key });
  };

  // Object-literal bindings in this file (`const shared = { exact: <span/> }`), so a
  // `components={shared}` spelling resolves instead of hiding the map (rule 147).
  const objectBindings = new Map<string, string[]>();
  const collectBindings = (node: ts.Node): void => {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer &&
      ts.isObjectLiteralExpression(node.initializer)
    ) {
      const names: string[] = [];
      for (const prop of node.initializer.properties) {
        if (prop.name) names.push(prop.name.getText(sf).replace(/^["']|["']$/g, ""));
      }
      objectBindings.set(node.name.getText(sf), names);
    }
    node.forEachChild(collectBindings);
  };
  collectBindings(sf);

  // The tag names a Trans element's `components` prop maps: read off an inline object
  // literal, resolved through a same-file binding, or "opaque" when neither reading
  // applies -- an opaque map cannot be verified, only a missing or readable one can.
  const componentsOf = (attr: ts.JsxAttribute): string[] | "opaque" | null => {
    const siblings = attr.parent.properties;
    for (const p of siblings) {
      if (ts.isJsxAttribute(p) && p.name.getText(sf) === "components" && p.initializer) {
        let expr: ts.Expression | undefined;
        if (ts.isJsxExpression(p.initializer)) expr = p.initializer.expression;
        if (expr && ts.isObjectLiteralExpression(expr)) {
          const names: string[] = [];
          for (const prop of expr.properties) {
            if (prop.name) names.push(prop.name.getText(sf).replace(/^["']|["']$/g, ""));
          }
          return names;
        }
        if (expr && ts.isIdentifier(expr)) {
          return objectBindings.get(expr.getText(sf)) ?? "opaque";
        }
        return "opaque";
      }
    }
    return null;
  };
  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node) && callees.has(node.expression.getText(sf))) {
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
      if (ts.isStringLiteral(node.initializer))
        record(node.initializer, node.initializer.text, "trans", componentsOf(node));
      else record(node.initializer, null, "trans", componentsOf(node));
    }
    node.forEachChild(visit);
  };
  visit(sf);
  return refs;
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

  it("every tag a message uses is one its render site can draw (#852)", () => {
    // Trans auto-renders only br/strong/i/p (react-i18next's transKeepBasicHtmlNodesFor
    // default); any other tag needs the `components` prop, or the operator reads the tag
    // itself: "Use at least 12 characters. <b>7 so far.</b>". And a t() call renders text
    // only, so a tagged message behind t() is always wrong.
    const AUTO = new Set(["br", "strong", "i", "p"]);
    const tagsIn = (message: string): Set<string> => {
      const tags = new Set<string>();
      for (const m of message.matchAll(/<\/?([A-Za-z]\w*)\s*\/?>/g)) tags.add(m[1]!);
      return tags;
    };
    const broken: string[] = [];
    for (const r of allRefs().literal) {
      const message = CATALOG[r.key];
      if (message === undefined) continue; // direction 1 reports it
      const tags = tagsIn(message);
      if (tags.size === 0) continue;
      if (r.via === "t") {
        broken.push(
          `${r.file}:${r.line}: t("${r.key}") renders tags as text: <${[...tags].join(">, <")}>`,
        );
        continue;
      }
      if (r.components === "opaque") continue; // an unreadable map is not a missing one
      const drawn = new Set([...AUTO, ...(r.components ?? [])]);
      const missing = [...tags].filter((tag) => !drawn.has(tag));
      if (missing.length > 0) {
        broken.push(
          `${r.file}:${r.line}: <Trans i18nKey="${r.key}"> has no components entry for ` +
            `<${missing.join(">, <")}>, so the tag prints literally`,
        );
      }
    }
    expect(
      broken,
      `messages whose tags the render site cannot draw:\n${broken.join("\n")}`,
    ).toEqual([]);

    // The matcher's own spellings (rule 147): flagged and clean forms of both render paths.
    expect(tagsIn("Keep <btn>going</btn> to <strong>win</strong>")).toEqual(
      new Set(["btn", "strong"]),
    );
    expect(tagsIn("a < b and {n} of {m}")).toEqual(new Set());
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

    // The alias spelling: a component whose data is named `t` binds the translator as
    // `tr`, and the gate reads it exactly like `t`.
    const aliased = catalogRefs(
      "probe.tsx",
      `const { t: tr } = useTranslation();
       const a = tr("aliased.literal");
       const b = tr(key);`,
    );
    expect(aliased.literal.map((r) => r.key)).toEqual(["aliased.literal"]);
    expect(aliased.dynamic).toHaveLength(1);

    // A .ts module: a generic call cannot swallow the t() beside it as phantom JSX.
    const tsRefs = catalogRefs(
      "probe.ts",
      `const a = pick<Item>(rows); const b = t("ts.literal");`,
    );
    expect(tsRefs.literal.map((r) => r.key)).toEqual(["ts.literal"]);
  });
});
