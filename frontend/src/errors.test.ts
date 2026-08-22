// SPDX-License-Identifier: AGPL-3.0-or-later
//
// describeError, proven against the real catalog the same way why.test.ts proves composeIn:
// a coded ApiError composes through error.*, a 422's several items compose each and join with
// a space the same way api.ts already joins their English `msg`, and anything without a code
// (or without a catalog entry for it) falls back to the English it already carried.

import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { describeError } from "./errors";

describe("describeError", () => {
  it("composes a coded ApiError through the real error.* catalog", () => {
    const err = new ApiError(422, "Use at least 8 characters.", "error.password.too_short", {
      min_length: 8,
    });
    expect(describeError(err)).toBe("Use at least 8 characters.");
  });

  it("derives a field param the same way why.ts does for a why.* reason", () => {
    const err = new ApiError(
      422,
      '"recent_watchers" needs a value.',
      "error.policy.field_needs_value",
      { field: "recent_watchers" },
    );
    expect(describeError(err)).toBe('"People who watched it recently" needs a value.');
  });

  it("falls back to the English message when the code has no catalog entry", () => {
    // A code this build's catalog does not carry -- a newer server, or one this build
    // predates. describeError must not print the raw dotted id in its place.
    const err = new ApiError(500, "Something new broke.", "error.not_a_real.code");
    expect(describeError(err)).toBe("Something new broke.");
  });

  it("falls back to the English message when there is no code at all", () => {
    const err = new ApiError(500, "Reaper couldn't do that. Try again.");
    expect(describeError(err)).toBe("Reaper couldn't do that. Try again.");
  });

  it("composes each coded item of a 422 list and joins them with a space", () => {
    const err = new ApiError(
      422,
      "Use at least 8 characters. Wrong username or password.",
      null,
      {},
      [
        {
          code: "error.password.too_short",
          params: { min_length: 8 },
          msg: "Use at least 8 characters.",
        },
        { code: "error.auth.wrong_credentials", params: {}, msg: "Wrong username or password." },
      ],
    );
    expect(describeError(err)).toBe("Use at least 8 characters. Wrong username or password.");
  });

  it("keeps an uncoded item's own msg beside a coded sibling's composed sentence", () => {
    // A plain pydantic type error carries no code; validation_error_items leaves it as-is.
    const err = new ApiError(422, "Use at least 8 characters. field required", null, {}, [
      {
        code: "error.password.too_short",
        params: { min_length: 8 },
        msg: "Use at least 8 characters.",
      },
      { code: null, params: {}, msg: "field required" },
    ]);
    expect(describeError(err)).toBe("Use at least 8 characters. field required");
  });

  it("falls back to the plain Error message for a non-ApiError", () => {
    expect(describeError(new Error("boom"))).toBe("boom");
  });

  it("stringifies anything that isn't even an Error", () => {
    expect(describeError("boom")).toBe("boom");
    expect(describeError(null)).toBe("null");
  });
});
