# Ghidra headless post-script (Jython 2.7). Disassembles one function or a
# bounded range starting at an address, then writes JSON for the Python bridge.
# @category ArgusAI
import json


_args = getScriptArgs()  # noqa: F821 - provided by Ghidra runtime
target = _args[0] if len(_args) > 0 else "entry"
try:
    count = max(0, min(int(_args[1]), 4000)) if len(_args) > 1 else 0
except Exception:
    count = 0
out_path = _args[2] if len(_args) > 2 else ""

program = currentProgram  # noqa: F821 - Ghidra global
fm = program.getFunctionManager()
listing = program.getListing()


def _entry_function():
    try:
        it = program.getSymbolTable().getExternalEntryPointIterator()
        while it.hasNext():
            func = fm.getFunctionContaining(it.next())
            if func is not None:
                return func
    except Exception:
        pass
    it = fm.getFunctions(True)
    return it.next() if it.hasNext() else None


def _target_address(text):
    if not text or text in ("entry", "entry0"):
        return None
    try:
        return program.getAddressFactory().getAddress(text)
    except Exception:
        return None


def _find_function(text, address):
    if not text or text in ("entry", "entry0"):
        return _entry_function()
    if address is not None:
        func = fm.getFunctionContaining(address)
        if func is not None:
            return func
    it = fm.getFunctions(True)
    while it.hasNext():
        func = it.next()
        if func.getName() == text:
            return func
    return None


def _line(instruction):
    return "%s  %s" % (instruction.getAddress(), instruction)


def _run():
    address = _target_address(target)
    func = _find_function(target, address)
    lines = []
    truncated = False

    if func is not None:
        iterator = listing.getInstructions(func.getBody(), True)
        while iterator.hasNext():
            instruction = iterator.next()
            if address is not None and instruction.getAddress().compareTo(address) < 0:
                continue
            if count and len(lines) >= count:
                truncated = True
                break
            lines.append(_line(instruction))
        return {
            "ok": bool(lines),
            "function": str(func.getName()),
            "entry": str(func.getEntryPoint()),
            "mode": "function" if count == 0 else "%d instructions" % count,
            "instruction_count": len(lines),
            "disasm": "\n".join(lines),
            "truncated": truncated,
        }

    if address is None:
        return {"ok": False, "error": "function not found: " + str(target)}
    if count == 0:
        return {
            "ok": False,
            "error": "address is not inside an analyzed function; provide count for a bounded range",
        }

    instruction = listing.getInstructionContaining(address)
    while instruction is not None and len(lines) < count:
        lines.append(_line(instruction))
        instruction = instruction.getNext()
    return {
        "ok": bool(lines),
        "function": "",
        "entry": str(address),
        "mode": "%d instructions" % count,
        "instruction_count": len(lines),
        "disasm": "\n".join(lines),
        "truncated": instruction is not None,
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
