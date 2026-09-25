"use strict";
// Which machine must the user mint a dashboard token on? Three outcomes, not
// two: "could not determine" is a different thing to tell the user than "not
// this app". The probe returns "unknown" on any platform without lsof, so
// collapsing that into "foreign" would state an inference as fact and send a
// purely-local user hunting for another machine.

const { test } = require("node:test");
const assert = require("node:assert");
const { classifyAuthBlock, defaultedPort } = require("../gateway-auth-hint");

test("our own local gateway points at THIS machine", () => {
  assert.equal(classifyAuthBlock({ localOwner: "kirocrew" }), "local");
});

test("a confirmed tunnel (ssh owns the socket) points at the OTHER machine", () => {
  // The reported scenario: `ssh -NL 5476:localhost:5476 dev-host`. The remote
  // gateway's access key is its own, so our CLI can only mint from there.
  assert.equal(classifyAuthBlock({ localOwner: "foreign" }), "foreign");
});

test("a configured remote host is decisive, whatever the socket says", () => {
  assert.equal(
    classifyAuthBlock({ localOwner: "kirocrew", remoteHost: "dev-host.example.com" }),
    "foreign",
  );
});

test("an unconfirmed owner is 'unknown' — never asserted as foreign", () => {
  // Regression pin: on Windows there is no lsof, so the probe yields "unknown".
  // Reporting that as "foreign" told every such user their own gateway belonged
  // to someone else. "none" (nothing listening, yet something answered) is
  // equally unconfirmed.
  for (const owner of ["none", "unknown", "", undefined, "weird-new-value"]) {
    assert.equal(classifyAuthBlock({ localOwner: owner }), "unknown", String(owner));
  }
});

test("no facts at all yields the hedged verdict, not a guess", () => {
  assert.equal(classifyAuthBlock(), "unknown");
  assert.equal(classifyAuthBlock({}), "unknown");
});

test("an empty remoteHost does not force the foreign verdict", () => {
  // Guard against treating "" as "a host was configured" — that would mislabel
  // every local-gateway failure as a tunnel.
  assert.equal(classifyAuthBlock({ localOwner: "kirocrew", remoteHost: "" }), "local");
});

// ── defaultedPort: a default-port URL must not read as "no port" ────────────

test("defaultedPort resolves a scheme-default URL to a real port", () => {
  // URL.port is "" for http://host/ and https://host/. Left empty it would key
  // the remote-host lookup wrong, probe no port, and let the page fall back to
  // :5476 — describing and submitting to a different gateway than the one that
  // just returned 403.
  assert.equal(defaultedPort("http://localhost/"), "80");
  assert.equal(defaultedPort("https://localhost/"), "443");
});

test("defaultedPort passes an explicit port through unchanged", () => {
  assert.equal(defaultedPort("http://localhost:5476/"), "5476");
  assert.equal(defaultedPort("http://127.0.0.1:7811/?x=1"), "7811");
  assert.equal(defaultedPort("https://example.com:8443/"), "8443");
});

test("defaultedPort returns '' for an unparseable URL rather than guessing", () => {
  for (const bad of ["", "not a url", undefined, null]) {
    assert.equal(defaultedPort(bad), "", String(bad));
  }
});

// ── No shell module may read a port off a URL without this normalizer ───────

test("no shell module keys anything off the raw URL.port property", () => {
  // `URL.port` is "" for a scheme's default, so a port-keyed lookup written with
  // the raw property misses on :80 and :443. One of those keys the remote-host
  // map that decides whether a window's gateway runs on this machine, and the
  // host-presence heartbeat sends this machine's internal secret whenever that
  // answer is "local" -- so a tunnelled crew read as local receives the secret.
  //
  // The fix is a property of the whole shell rather than of the sites that
  // happened to be found, so this sweeps every module instead of a list that a
  // new consumer can be added outside of.
  const fs = require("node:fs");
  const path = require("node:path");
  const root = path.join(__dirname, "..");

  // `data-home.js` reads the port out of a stored `dashboard.url` to pick a
  // LAUNCH TARGET, not to key a lookup. Resolving a scheme default there would
  // newly admit :80 as a target, which `isSelectablePort` in host-config.js
  // exists to refuse -- so that decision belongs with port selection.
  const ALLOWED = new Set(["data-home.js"]);

  const offenders = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === "node_modules" || entry.name === "test" || entry.name === "dist") continue;
        walk(full);
        continue;
      }
      if (!entry.name.endsWith(".js")) continue;
      const rel = path.relative(root, full);
      if (ALLOWED.has(rel)) continue;
      // Strip comments first: a module is allowed to DESCRIBE the erasure, and
      // host-config.js does exactly that where it refuses to select port 80.
      const code = fs.readFileSync(full, "utf8")
        .replace(/\r\n/g, "\n")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
      if (/new URL\([^)]*\)\s*\.port\b/.test(code)) offenders.push(rel);
    }
  };
  walk(root);

  assert.deepEqual(
    offenders,
    [],
    "read the port with defaultedPort(url) instead of new URL(url).port",
  );
});
