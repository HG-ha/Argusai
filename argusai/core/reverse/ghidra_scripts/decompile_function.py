# Ghidra headless post-script (Jython 2.7). Decompiles one function to C and
# writes a JSON result to an output file so the Python caller never has to parse
# the headless log stream.
#
# Invoked by ghidra_runtime via:
#   analyzeHeadless <proj> <name> (-import <bin> | -process <bin>) \
#       -scriptPath <this dir> -postScript decompile_function.py <target> <out_json>
#
# <target>: a function name, an address (0x...), or "" / "entry" for the entry point.
# @category ArgusAI
import json

from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor

_args = getScriptArgs()  # noqa: F821 - provided by Ghidra runtime
target = _args[0] if len(_args) > 0 else ""
out_path = _args[1] if len(_args) > 1 else ""

program = currentProgram  # noqa: F821 - Ghidra global
fm = program.getFunctionManager()


def _entry_function():
    try:
        it = program.getSymbolTable().getExternalEntryPointIterator()
        while it.hasNext():
            f = fm.getFunctionContaining(it.next())
            if f is not None:
                return f
    except Exception:
        pass
    it = fm.getFunctions(True)
    if it.hasNext():
        return it.next()
    return None


def _find_function(t):
    if not t or t == "entry" or t == "entry0":
        return _entry_function()
    try:
        addr = program.getAddressFactory().getAddress(t)
        if addr is not None:
            f = fm.getFunctionContaining(addr)
            if f is not None:
                return f
    except Exception:
        pass
    it = fm.getFunctions(True)
    while it.hasNext():
        f = it.next()
        if f.getName() == t:
            return f
    return None


def _run():
    func = _find_function(target)
    if func is None:
        return {"ok": False, "error": "function not found: " + str(target)}
    ifc = DecompInterface()
    try:
        ifc.openProgram(program)
        res = ifc.decompileFunction(func, 120, ConsoleTaskMonitor())
    finally:
        try:
            ifc.dispose()
        except Exception:
            pass
    if res is not None and res.decompileCompleted():
        return {
            "ok": True,
            "function": func.getName(),
            "entry": str(func.getEntryPoint()),
            "c": res.getDecompiledFunction().getC(),
        }
    err = res.getErrorMessage() if res is not None else "no result"
    return {"ok": False, "error": "decompile failed: " + str(err)}


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
