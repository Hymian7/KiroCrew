"use strict";

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");
const { borrowSessionToken } = require("../mochi-session-token");

describe("borrowSessionToken", () => {
  it("returns the mc_token_<port> cookie value for the backend's port", async () => {
    const seen = [];
    const electronSession = {
      cookies: {
        get(filter) {
          seen.push(filter);
          return Promise.resolve([{ name: "mc_token_5476", value: "session-cookie-value" }]);
        },
      },
    };

    const token = await borrowSessionToken({
      electronSession,
      backendUrl: "http://localhost:5476",
    });

    assert.equal(token, "session-cookie-value");
    assert.deepEqual(seen, [{ url: "http://localhost:5476", name: "mc_token_5476" }]);
  });

  it("names the cookie after a scheme's default port instead of an empty one", async () => {
    // The gateway names the cookie `mc_token_<port>` after the port the browser
    // reached, falling back to its own listen port when the Host header carries
    // none -- which is what a browser sends for a scheme default. `URL.port` is
    // "" there, so the raw property asked for `mc_token_` and borrowed nothing.
    for (const [backendUrl, expected] of [
      ["http://localhost:80", "mc_token_80"],
      ["http://localhost", "mc_token_80"],
      ["https://localhost", "mc_token_443"],
      ["https://127.0.0.1:443", "mc_token_443"],
      ["http://localhost:5476", "mc_token_5476"],
    ]) {
      const seen = [];
      const electronSession = {
        cookies: {
          get(filter) {
            seen.push(filter.name);
            return Promise.resolve([{ name: filter.name, value: "v" }]);
          },
        },
      };
      const token = await borrowSessionToken({ electronSession, backendUrl });
      assert.deepEqual(seen, [expected], backendUrl);
      assert.equal(token, "v", backendUrl);
    }
  });

  it("returns empty when no session was ever established (no matching cookie)", async () => {
    const electronSession = { cookies: { get: () => Promise.resolve([]) } };

    const token = await borrowSessionToken({
      electronSession,
      backendUrl: "http://localhost:5476",
    });

    assert.equal(token, "");
  });

  it("fails closed when there is no session/cookie API at all", async () => {
    assert.equal(
      await borrowSessionToken({ electronSession: null, backendUrl: "http://localhost:5476" }),
      "",
    );
    assert.equal(
      await borrowSessionToken({ electronSession: {}, backendUrl: "http://localhost:5476" }),
      "",
    );
  });

  it("fails closed on an unparsable backend URL rather than throwing", async () => {
    const electronSession = { cookies: { get: () => Promise.resolve([{ value: "x" }]) } };
    const token = await borrowSessionToken({ electronSession, backendUrl: "not-a-url" });
    assert.equal(token, "");
  });

  it("fails closed when the cookie store rejects", async () => {
    const electronSession = { cookies: { get: () => Promise.reject(new Error("boom")) } };
    const token = await borrowSessionToken({
      electronSession,
      backendUrl: "http://localhost:5476",
    });
    assert.equal(token, "");
  });

  it("never fabricates a value: a non-string cookie value resolves to empty", async () => {
    const electronSession = {
      cookies: { get: () => Promise.resolve([{ value: undefined }]) },
    };
    const token = await borrowSessionToken({
      electronSession,
      backendUrl: "http://localhost:5476",
    });
    assert.equal(token, "");
  });

  it("keys the cookie name off the backend's own port, not a hardcoded one", async () => {
    const seen = [];
    const electronSession = {
      cookies: {
        get(filter) {
          seen.push(filter.name);
          return Promise.resolve([{ value: "t" }]);
        },
      },
    };
    await borrowSessionToken({ electronSession, backendUrl: "http://localhost:7778" });
    assert.deepEqual(seen, ["mc_token_7778"]);
  });
});
