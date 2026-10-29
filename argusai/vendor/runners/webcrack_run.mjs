// webcrack runner — invoked by argusai.core.runtime_manager.webcrack_run.
//
// Input  (JSON on stdin):  { source: string, options?: object,
//                            webcrack_root: string }
// Output (JSON on stdout): { ok: bool, code: string, bundle: object|null,
//                            markers: array, error: string,
//                            deobfuscated: bool, unminified: bool,
//                            unpacked: bool }
//
// Why a wrapper instead of running `webcrack` CLI directly:
//
// - The CLI shape changes across versions (2.16 broke some flags vs 2.13).
// - We want the structured ``bundle`` result, not the on-disk
//   ``output-dir`` layout the CLI produces.
// - Passing 4–5 MB of source over stdin is faster than writing it to a
//   tmp file and exec'ing the CLI.
//
// Failures emit ``{ok: false, error: "..."}`` on stdout, NOT non-zero
// exit. Non-zero exit is reserved for "the runner itself blew up"
// (e.g. couldn't import webcrack). The Python wrapper distinguishes
// these to give the user a useful error.
//
// We use a dynamic ``import()`` of an explicit file:// URL because Node's
// ESM resolver doesn't honour NODE_PATH the way CJS does — we can't
// just ``import { webcrack } from "webcrack"`` and rely on env. The
// path comes from the ``webcrack_root`` field in the JSON payload
// (set by runtime_manager.webcrack_run from the vendor status).

import { pathToFileURL } from "url";
import path from "path";

async function readStdin() {
    const chunks = [];
    for await (const chunk of process.stdin) {
        chunks.push(chunk);
    }
    return Buffer.concat(chunks).toString("utf-8");
}

function summariseBundle(b) {
    if (!b) return null;
    const out = {
        type: b.type || "",
        size: b.size || 0,
        modules: [],
        entry_id: b.entryId || null,
    };
    if (b.modules instanceof Map) {
        for (const [id, mod] of b.modules) {
            out.modules.push({
                id: String(id),
                path: mod.path || null,
                size: (mod.code || "").length,
                is_entry: !!mod.isEntry,
            });
            if (out.modules.length >= 2000) break;
        }
    } else if (Array.isArray(b.modules)) {
        for (const mod of b.modules) {
            out.modules.push({
                id: String(mod.id ?? ""),
                path: mod.path || null,
                size: (mod.code || "").length,
                is_entry: !!mod.isEntry,
            });
            if (out.modules.length >= 2000) break;
        }
    }
    return out;
}

async function main() {
    const raw = await readStdin();
    let payload;
    try {
        payload = JSON.parse(raw);
    } catch (e) {
        console.log(JSON.stringify({
            ok: false,
            error: "invalid JSON on stdin: " + e.message,
        }));
        return;
    }

    const source = String(payload.source || "");
    if (!source) {
        console.log(JSON.stringify({
            ok: false,
            error: "source is required",
        }));
        return;
    }
    const webcrackRoot = String(payload.webcrack_root || "");
    if (!webcrackRoot) {
        console.log(JSON.stringify({
            ok: false,
            error: "webcrack_root is required (absolute path to the managed Node tools directory)",
        }));
        return;
    }

    // Dynamically import webcrack from the vendor location. ESM resolver
    // doesn't honour NODE_PATH so we build a file:// URL directly.
    let webcrack;
    try {
        const distIndex = path.join(
            webcrackRoot, "node_modules", "webcrack", "dist", "index.js",
        );
        const mod = await import(pathToFileURL(distIndex).href);
        webcrack = mod.webcrack;
        if (typeof webcrack !== "function") {
            console.log(JSON.stringify({
                ok: false,
                error: `webcrack export not found at ${distIndex}`,
            }));
            return;
        }
    } catch (e) {
        console.log(JSON.stringify({
            ok: false,
            error: "could not load webcrack: " + (e?.message || e),
            stack: e && e.stack ? String(e.stack).split("\n").slice(0, 6) : [],
        }));
        return;
    }

    const userOpts = payload.options && typeof payload.options === "object"
        ? payload.options
        : {};

    // Defaults — equivalent to webcrack CLI defaults with timing budget.
    const options = {
        // 30s timeout on the slowest stage; webcrack 2.16 default is
        // 10 minutes, way too long for an interactive AI agent.
        sandboxTimeoutMs: 30_000,
        ...userOpts,
    };

    let result;
    try {
        result = await webcrack(source, options);
    } catch (e) {
        console.log(JSON.stringify({
            ok: false,
            error: "webcrack threw: " + (e && e.message ? e.message : String(e)),
            stack: e && e.stack ? String(e.stack).split("\n").slice(0, 6) : [],
        }));
        return;
    }

    const out = {
        ok: true,
        code: typeof result.code === "string" ? result.code : "",
        bundle: summariseBundle(result.bundle),
        markers: [],
        deobfuscated: !!(result.applied && result.applied.deobfuscate),
        unminified: !!(result.applied && result.applied.unminify),
        unpacked: !!result.bundle,
        error: "",
    };

    process.stdout.write(JSON.stringify(out));
}

main().catch(e => {
    process.stderr.write("webcrack_run.mjs uncaught: " + (e?.stack || e) + "\n");
    process.exit(2);
});
