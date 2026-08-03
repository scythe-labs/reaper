// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The frontend formatter. Python has had `ruff format` as a CI gate since the beginning;
// TypeScript had nothing, so style here was whatever the last hand felt like -- and every
// agent session spent attention on it that belonged on the deletion path instead.
//
// printWidth is 100 because that is `line-length` in pyproject.toml: one number for the
// whole repository rather than a Python answer and a TypeScript answer. It also happens to
// be what this tree already wrote. Reformatting src/ at each candidate width moved 8457
// lines at prettier's default 80, 5011 at 90, 2973 at 100, and 3930 at 120 -- 100 is the
// minimum, so the setting that matches ruff is also the one that fights the code least.
//
// Everything else is prettier's default on purpose. Semicolons, double quotes, two-space
// indent, trailing commas, and always-parenthesized arrow params are all what the tree
// already did, and each alternative was measured to move MORE lines, not fewer. There is
// no house style to encode here beyond the width.
//
// No eslint-config-prettier: eslint.config.js extends only the `recommended` sets, which
// carry no stylistic rules for a formatter to collide with. Add it if a stylistic rule is
// ever enabled, not before.
export default {
  printWidth: 100,
};
