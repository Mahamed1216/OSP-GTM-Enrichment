import { afterEach, describe, expect, it, vi } from "vitest";

import { callApi } from "./api";

/**
 * callApi is the single choke point for every error the console shows, so its
 * failure paths matter as much as the happy one. It must never throw: a panel
 * renders whatever it returns.
 */

function mockFetch({ status = 200, contentType = "application/json", body = "{}" }) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (name.toLowerCase() === "content-type" ? contentType : null) },
    text: () => Promise.resolve(body),
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("callApi", () => {
  it("parses a JSON success response", async () => {
    vi.stubGlobal("fetch", mockFetch({ body: '{"total": 3}' }));
    const result = await callApi("/api/v1/leads");
    expect(result).toMatchObject({ ok: true, status: 200, data: { total: 3 } });
  });

  it("sends cookies so the admin session travels with the request", async () => {
    const fetchMock = mockFetch({});
    vi.stubGlobal("fetch", fetchMock);
    await callApi("/api/v1/leads");
    expect(fetchMock.mock.calls[0][1].credentials).toBe("same-origin");
  });

  it("never puts a credential in the request", async () => {
    const fetchMock = mockFetch({});
    vi.stubGlobal("fetch", fetchMock);
    await callApi("/api/v1/leads");
    const headers = fetchMock.mock.calls[0][1].headers;
    expect(headers.Authorization).toBeUndefined();
  });

  it("names the route when the API returns plain text instead of JSON", async () => {
    vi.stubGlobal("fetch", mockFetch({
      status: 500,
      contentType: "text/plain; charset=utf-8",
      body: "Internal Server Error",
    }));
    const result = await callApi("/api/v1/dashboard/summary");
    expect(result.ok).toBe(false);
    expect(result.error).toBe(
      "API returned 500 text/plain from /api/v1/dashboard/summary",
    );
  });

  it("surfaces the API's message and hint on a JSON error", async () => {
    vi.stubGlobal("fetch", mockFetch({
      status: 503,
      body: JSON.stringify({
        error: "database_unavailable",
        message: "OperationalError: connection refused",
        hint: "Check DATABASE_URL.",
      }),
    }));
    const result = await callApi("/api/v1/leads");
    expect(result.ok).toBe(false);
    expect(result.error).toContain("connection refused");
    expect(result.error).toContain("Check DATABASE_URL.");
  });

  it("falls back to detail for FastAPI-style errors", async () => {
    vi.stubGlobal("fetch", mockFetch({
      status: 401,
      body: '{"detail": "Invalid or missing API key."}',
    }));
    const result = await callApi("/api/v1/leads");
    expect(result.error).toContain("Invalid or missing API key.");
  });

  it("returns a result object when the network is down, and does not throw", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const result = await callApi("/api/v1/leads");
    expect(result.ok).toBe(false);
    expect(result.unreachable).toBe(true);
    expect(result.error).toContain("Network error");
  });

  it("handles an HTML error page without throwing", async () => {
    vi.stubGlobal("fetch", mockFetch({
      status: 502,
      contentType: "text/html",
      body: "<!doctype html><title>502</title>",
    }));
    const result = await callApi("/api/info");
    expect(result.ok).toBe(false);
    expect(result.error).toContain("502 text/html");
    expect(result.raw).toContain("doctype");
  });

  it("serialises a body and sets the JSON content type", async () => {
    const fetchMock = mockFetch({});
    vi.stubGlobal("fetch", fetchMock);
    await callApi("/api/auth/login", { method: "POST", body: { password: "x" } });
    const [, options] = fetchMock.mock.calls[0];
    expect(options.method).toBe("POST");
    expect(options.headers["Content-Type"]).toBe("application/json");
    expect(options.body).toBe('{"password":"x"}');
  });
});
