# Ghidra headless post-script (Jython 2.7). Lists analyzed functions and writes
# a bounded JSON result for the Python bridge.
# @category ArgusAI
import json


_args = getScriptArgs()  # noqa: F821 - provided by Ghidra runtime
try:
    limit = max(1, min(int(_args[0]), 5000)) if len(_args) > 0 else 400
except Exception:
    limit = 400
out_path = _args[1] if len(_args) > 1 else ""

program = currentProgram  # noqa: F821 - Ghidra global
fm = program.getFunctionManager()


def _run():
    functions = []
    it = fm.getFunctions(True)
    while it.hasNext():
        func = it.next()
        try:
            entry = func.getEntryPoint()
            body = func.getBody()
            functions.append({
                "name": str(func.getName()),
                "offset": int(entry.getOffset()),
                "vaddr": int(entry.getOffset()),
                "entry": str(entry),
                "size": int(body.getNumAddresses()),
                "nargs": int(func.getParameterCount()),
                "nlocals": len(func.getLocalVariables()),
                "calltype": str(func.getCallingConventionName() or ""),
            })
        except Exception:
            continue

    functions.sort(key=lambda item: item.get("size", 0), reverse=True)
    total = len(functions)
    return {
        "ok": total > 0,
        "function_count": total,
        "functions": functions[:limit],
        "truncated": total > limit,
    }


result = _run()
payload = json.dumps(result)
if out_path:
    fh = open(out_path, "w")
    try:
        fh.write(payload)
    finally:
        fh.close()
else:
    print(payload)
