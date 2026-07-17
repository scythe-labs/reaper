// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The lint gate. Exists above all for the two react-hooks rules: a conditional hook or
// a stale-closure dependency in the reap confirmation is a safety bug, not a style nit,
// so both run as errors. The handful of deliberate exceptions in the codebase carry
// inline eslint-disable comments naming the rule -- which this config is what makes
// truthful (they were dead markers before ESLint was wired in).
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules"] },
  {
    files: ["src/**/*.{ts,tsx}", "*.config.ts", "eslint.config.js"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      globals: { ...globals.browser },
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      // The two classic hook rules only, as errors: a violation in the execute-gating
      // components is a correctness bug. The plugin's newer compiler-era rules (purity,
      // set-state-in-effect, ...) are deliberately NOT enabled: they flag long-standing
      // legitimate patterns here, and this gate must stay green to stay meaningful.
      // Intentional exceptions remain visible as inline disables.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
      // House idiom: an intentionally-unused value is named with a leading underscore.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrors: "none" },
      ],
      // House idiom: `cond ? doA() : doB()` as a statement in small handlers.
      "@typescript-eslint/no-unused-expressions": ["error", { allowTernary: true }],
    },
  },
);
