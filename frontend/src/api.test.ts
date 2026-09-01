import { describe, expect, it } from "vitest";
import { formatApiErrorDetail } from "./api";

describe("formatApiErrorDetail", () => {
  it("keeps plain server errors readable", () => {
    expect(formatApiErrorDetail("Provider unavailable", 503)).toBe(
      "Provider unavailable",
    );
  });

  it("formats FastAPI validation errors instead of object coercion", () => {
    expect(
      formatApiErrorDetail(
        [{ loc: ["body", "history", 1, "content"], msg: "String too long" }],
        422,
      ),
    ).toBe("history.1.content: String too long");
  });

  it("serializes structured details and falls back to status", () => {
    expect(formatApiErrorDetail({ provider: "openrouter" }, 502)).toBe(
      '{"provider":"openrouter"}',
    );
    expect(formatApiErrorDetail(undefined, 500)).toBe(
      "Request failed with status 500",
    );
  });
});
