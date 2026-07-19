// SPDX-License-Identifier: AGPL-3.0-or-later
// The size formatter feeds every number shown beside a destructive control, so its
// conventions are pinned: binary units with the unit named honestly (GiB, not GB),
// one decimal below 100, none above, and nothing negative ever rendered.
import { describe, expect, it } from "vitest";
import { bytes, count, coverage, itemBytes } from "./format";

describe("bytes", () => {
  it("renders binary units with honest labels", () => {
    expect(bytes(1024)).toBe("1.0 KiB");
    expect(bytes(5.9 * 1024 ** 3)).toBe("5.9 GiB");
    expect(bytes(214 * 1024 ** 3)).toBe("214 GiB");
  });

  it("drops the decimal at 100 and above", () => {
    expect(bytes(99.9 * 1024 ** 3)).toBe("99.9 GiB");
    expect(bytes(100 * 1024 ** 3)).toBe("100 GiB");
  });

  it("never renders a negative or zero size as anything but 0 B", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(-5)).toBe("0 B");
  });

  it("caps at the largest unit instead of inventing one", () => {
    expect(bytes(1024 ** 6)).toBe("1024 PiB");
  });
});

describe("itemBytes", () => {
  it("says a size is unknown rather than claiming the item is empty", () => {
    // Every candidate holds files -- both scan paths skip anything that does not -- so a
    // stored 0 means the *arr declined to report a size. "0 B" beside a delete control
    // would be a false statement about the file the operator is deciding on.
    expect(itemBytes(0)).toBe("Size unknown");
  });

  it("formats a real size exactly as bytes does", () => {
    expect(itemBytes(5.9 * 1024 ** 3)).toBe(bytes(5.9 * 1024 ** 3));
    expect(itemBytes(1024)).toBe("1.0 KiB");
  });
});

describe("count", () => {
  it("localizes whole numbers", () => {
    expect(count(1)).toBe((1).toLocaleString());
    expect(count(4260)).toBe((4260).toLocaleString());
  });
});

describe("coverage", () => {
  it("turns basis points into a rounded percentage", () => {
    expect(coverage(10_000)).toBe("100%");
    expect(coverage(7_550)).toBe("76%");
  });
});
