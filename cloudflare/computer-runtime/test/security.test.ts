import { describe, expect, it } from "vitest";

import { safeAbsolutePath, safeRelativePath, shellQuote } from "../src/security";

describe("runtime path policy", () => {
  it("keeps writes inside declared roots", () => {
    expect(safeAbsolutePath("/workspace/result.json", ["/workspace", "/outbox"])).toBe(true);
    expect(safeAbsolutePath("/outbox/final.json", ["/workspace", "/outbox"])).toBe(true);
    expect(safeAbsolutePath("/.memseek/context.md", ["/workspace", "/outbox"])).toBe(false);
    expect(safeAbsolutePath("/workspace/../.memseek/context.md", ["/workspace"])).toBe(false);
  });

  it("accepts only normalized Program bundle paths", () => {
    expect(safeRelativePath("lib/parser.js")).toBe(true);
    expect(safeRelativePath("../secret")).toBe(false);
    expect(safeRelativePath("/absolute.js")).toBe(false);
  });

  it("quotes immutable container argv", () => {
    expect(shellQuote("hello world")).toBe("'hello world'");
    expect(shellQuote("it's")).toBe("'it'\\''s'");
  });
});
