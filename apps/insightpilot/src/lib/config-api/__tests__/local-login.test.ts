import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { configApi } from "../client";

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_CONFIG_API_URL", "");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("local one-click login", () => {
  it("creates a cookie session without browser credentials and verifies the signed-in identity", async () => {
    const identity = {
      user: { id: "existing-user", displayName: "Local workspace" },
      workspace: { id: "existing-workspace" },
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(Response.json({ success: true, data: identity }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(configApi.loginLocal()).resolves.toEqual(identity);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/local-dev-session", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
    });
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/me", expect.objectContaining({
      credentials: "same-origin",
    }));
  });

  it("does not silently recreate a session when checking a signed-out user", async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      success: false,
      error: { code: "UNAUTHORIZED", message: "Authentication required" },
    }, { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(configApi.getMe()).rejects.toMatchObject({ status: 401 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/me", expect.any(Object));
  });

  it("reports a failed local login without proceeding to the workspace identity check", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("Local API is unavailable.", { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(configApi.loginLocal()).rejects.toMatchObject({ status: 503 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("requires a usable authenticated session before considering login successful", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(Response.json({
        success: false,
        error: { code: "UNAUTHORIZED", message: "Authentication required" },
      }, { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(configApi.loginLocal()).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
