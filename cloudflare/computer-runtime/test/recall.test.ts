import { describe, expect, it, vi } from "vitest";
import { limitRecallMatches } from "../src/protocol";

const size = (value: unknown) => new TextEncoder().encode(JSON.stringify(value)).length;

describe("recall exposure budget", () => {
  it("preserves exact prefixes at UTF-8, escaping, and comma boundaries", () => {
    const matches = [{ text: 'é漢🙂"\\\n' }, { text: "second" }, { text: "third" }];
    for (let budget = 0; budget <= size(matches) + 1; budget++) {
      let expected = matches;
      while (expected.length && size(expected) > budget) expected = expected.slice(0, -1);
      expect(limitRecallMatches(matches, budget)).toEqual(expected);
    }
    expect(limitRecallMatches([], 0)).toEqual([]);
    expect(limitRecallMatches(matches, size(matches))).toBe(matches);
  });

  it("serializes each candidate at most once", () => {
    const matches = Array.from({ length: 50 }, (_, n) => ({ text: "x".repeat(500), n }));
    const stringify = vi.spyOn(JSON, "stringify");
    try {
      limitRecallMatches(matches, 1024);
      expect(stringify).toHaveBeenCalledTimes(2);
    } finally {
      stringify.mockRestore();
    }
  });
});
