// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
// The size formatter feeds every number shown beside a destructive control, so its
// conventions are pinned: binary units with the unit named honestly (GiB, not GB),
// one decimal below 100, none above, and nothing negative ever rendered.
import { describe, expect, it } from "vitest";
import {
  bytes,
  carriesYear,
  count,
  coverage,
  itemBytes,
  spareRemaining,
  titleWithYear,
  totalBytes,
} from "./format";

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
    // The server sends null when nothing would report a size, so the client no longer
    // has to infer it. "0 B" beside a delete control would be a false statement about
    // the file the operator is deciding on.
    expect(itemBytes(null)).toBe("Size unknown");
  });

  it("renders a real zero honestly, now that null carries the unknown", () => {
    // Regression on the heuristic this replaced: a `value > 0` test could not tell an
    // empty thing from an unmeasured one, so it called both unknown.
    expect(itemBytes(0)).toBe("0 B");
  });

  it("formats a real size exactly as bytes does", () => {
    expect(itemBytes(5.9 * 1024 ** 3)).toBe(bytes(5.9 * 1024 ** 3));
    expect(itemBytes(1024)).toBe("1.0 KiB");
  });
});

describe("totalBytes", () => {
  it("shows nothing extra when every item was measured", () => {
    // The whole-library case. An operator whose sources all answer must see no new pixels.
    expect(totalBytes(1024 ** 4, 0)).toBe(bytes(1024 ** 4));
  });

  it("says how many the total could not include", () => {
    // The sum is of what IS known, so it reads low. Saying so is the difference between
    // an incomplete number and a wrong one.
    expect(totalBytes(1024 ** 4, 3)).toBe(`${bytes(1024 ** 4)}, 3 sizes unknown`);
  });

  it("reads naturally for a single unknown", () => {
    expect(totalBytes(1024, 1)).toBe("1.0 KiB, 1 size unknown");
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

describe("spareRemaining", () => {
  const inDays = (n: number) => new Date(Date.now() + n * 86_400_000).toISOString();

  it("reads a null expiry as a forever spare", () => {
    const r = spareRemaining(null);
    expect(r.forever).toBe(true);
    expect(r.short).toBe("");
    expect(r.phrase).toBe("");
  });

  it("counts whole days left, rounding up so a partial day still shows", () => {
    const r = spareRemaining(inDays(26.4));
    expect(r.forever).toBe(false);
    expect(r.days).toBe(27);
    expect(r.short).toBe("27d");
    expect(r.phrase).toBe("27 days left");
  });

  it("says a single day in the singular", () => {
    expect(spareRemaining(inDays(0.5)).phrase).toBe("1 day left");
  });

  it("absorbs a small server clock lead so a fresh whole-day spare reads its own length", () => {
    // The server sets the expiry; if its clock runs a few minutes ahead of the browser's, a
    // just-made "90 days" spare would otherwise round up to 91d. Up to an hour of lead reads 90.
    const r = spareRemaining(inDays(90 + 5 / 1440)); // 90 days + 5 minutes
    expect(r.days).toBe(90);
    expect(r.short).toBe("90d");
    expect(r.phrase).toBe("90 days left");
  });

  it("reads a past expiry as expired, floored at zero (realized only at the next scan)", () => {
    const r = spareRemaining(inDays(-3));
    expect(r.expired).toBe(true);
    expect(r.days).toBe(0);
  });

  it("names the expired state, and empties the one field that would lie about it", () => {
    // The item is still kept until a scan realizes the expiry, so every surface goes on saying
    // it is spared -- it just says WHICH spare. `until` is the field that cannot survive here:
    // "Kept until Jul 22" past Jul 22 is a promise about a day already gone, so it empties and
    // `note` carries the whole sentence instead.
    const r = spareRemaining(inDays(-3));
    expect(r.short).toBe("0d");
    expect(r.phrase).toBe("expired");
    expect(r.until).toBe("");
    expect(r.note).toMatch(
      /^Your spare expired on .+\. Still kept until the next scan judges it again$/,
    );
  });

  it("leaves `note` to the expired state alone", () => {
    // It is the sentence that explains a spare which has stopped counting but is still keeping
    // the file. A live or forever spare has nothing to explain, and a surface that prints
    // `note` unconditionally must render nothing for them.
    expect(spareRemaining(null).note).toBe("");
    expect(spareRemaining(inDays(27)).note).toBe("");
  });
});

describe("titleWithYear", () => {
  // What a jump seeds the review search box with. The queue's search understands a year on the
  // end of a title (`list_candidates`), so the seeded string is the title as the queue prints
  // it -- and a title that already names its own year must not be handed a second one, which is
  // the same question the Scales row asks before printing the year in its own span.
  it("joins a title to its year the way the queue prints it", () => {
    expect(titleWithYear("Example Alpha", 1979)).toBe("Example Alpha 1979");
  });

  it("leaves a title that already carries its year alone", () => {
    expect(titleWithYear("Example Show (2019)", 2019)).toBe("Example Show (2019)");
    expect(carriesYear("Example Show (2019)", 2019)).toBe(true);
  });

  it("appends a year the title carries a DIFFERENT one of", () => {
    // "(2019)" in the name is not this item's year, so the year still has to be said.
    expect(titleWithYear("Example Show (2019)", 2021)).toBe("Example Show (2019) 2021");
    expect(carriesYear("Example Show (2019)", 2021)).toBe(false);
  });

  it("says nothing about a year it does not have", () => {
    expect(titleWithYear("Example Alpha", null)).toBe("Example Alpha");
    expect(titleWithYear("Example Alpha", undefined)).toBe("Example Alpha");
    expect(carriesYear("Example Alpha", null)).toBe(false);
  });

  it("trims, so the seeded search is not a term with an edge no title has", () => {
    expect(titleWithYear("  Example Alpha  ", 1979)).toBe("Example Alpha 1979");
  });
});
