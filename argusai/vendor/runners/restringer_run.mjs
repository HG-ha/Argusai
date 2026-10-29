// restringer runner — invoked by argusai.core.runtime_manager.restringer_run.
//
// Input  (JSON on stdin):  { source: string, maxIterations?: number }
// Output (JSON on stdout): { ok: bool, code: string, iterations: number,
//                            error: string }
//
// Restringer is a complementary deobfuscator: where webcrack is fast and
// conservative, restringer is slower and willing to evaluate suspicious
// constants in a (host) sandbox to constant-fold them away. We use it
// as a SECOND pass on samples where webcrack didn't fully recover the
// string array — typically obfuscator.io variants with rotated arrays.
//
// API surface (restringer ≥ 2.0):
//
//   const REstringer = require('restringer').REstringer;
//   const r = new REstringer(source);
//   r.logger.setLogLevel(REstringer.logger.LOG_LEVELS.ERROR);
//   r.run();   // mutates r.script to be the deobfuscated source
//
// Restringer is written as CommonJS so we import via createRequire.

import { createRequire } from "module";

const require = createRequire(import.meta.url);

async function readStdin() {
    const chunks = [];
    for await (const chunk of process.stdin) {
        chunks.push(chunk);
    }
    return Buffer.concat(chunks).toString("utf-8");
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
    const maxIter = Math.max(1, Math.min(20, Number(payload.maxIterations) || 6));

    let REstringer;
    try {
        const mod = require("restringer");
        REstringer = mod.REstringer || mod.default || mod;
    } catch (e) {
        console.log(JSON.stringify({
            ok: false,
            error: "could not require('restringer'): " + (e?.message || e),
        }));
        return;
    }

    let inst;
    try {
        inst = new REstringer(source);
    } catch (e) {
        console.log(JSON.stringify({
            ok: false,
            error: "new REstringer(source) threw: " + (e?.message || e),
        }));
        return;
    }

    // Restringer logs prolifically by default; turn it down so stderr
    // doesn't blow up the Python pipe buffer.
    try {
        const lvls = REstringer.logger?.LOG_LEVELS || inst.logger?.LOG_LEVELS;
        if (lvls && inst.logger && inst.logger.setLogLevel) {
            inst.logger.setLogLevel(lvls.ERROR || lvls.WARN || 0);
        }
    } catch { /* nothing */ }

    if (typeof inst.run !== "function") {
        console.log(JSON.stringify({
            ok: false,
            error: "restringer instance does not have a run() method",
        }));
        return;
    }

    let iter = 0;
    try {
        // Restringer's run is single-shot (it loops internally up to
        // maxIterations, set in the constructor in older versions).
        if (typeof inst.runLoop === "function") {
            inst.runLoop(maxIter);
        } else {
            inst.run();
        }
        iter = inst.totalChangesCounter || maxIter;
    } catch (e) {
        console.log(JSON.stringify({
            ok: false,
            error: "restringer.run() threw: " + (e?.message || e),
            stack: e && e.stack ? String(e.stack).split("\n").slice(0, 6) : [],
        }));
        return;
    }

    const code = typeof inst.script === "string" ? inst.script : "";
    process.stdout.write(JSON.stringify({
        ok: true,
        code,
        iterations: iter,
        error: "",
    }));
}

main().catch(e => {
    process.stderr.write("restringer_run.mjs uncaught: " + (e?.stack || e) + "\n");
    process.exit(2);
});
