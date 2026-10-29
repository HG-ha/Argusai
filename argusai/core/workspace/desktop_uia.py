"""Windows UI Automation helpers (semantic controls, not pixel clicks)."""

from __future__ import annotations

import sys
import threading
from typing import Any

_INIT_LOCK = threading.Lock()
_UIA = None
_UIA_MOD = None

# Stable control-type names for tool IO (UIA ControlType ids).
_CONTROL_TYPES: dict[str, int] = {
    "button": 50000,
    "calendar": 50001,
    "checkbox": 50002,
    "combobox": 50003,
    "edit": 50004,
    "hyperlink": 50005,
    "image": 50006,
    "listitem": 50007,
    "list": 50008,
    "menu": 50009,
    "menubar": 50010,
    "menuitem": 50011,
    "progressbar": 50012,
    "radiobutton": 50013,
    "scrollbar": 50014,
    "slider": 50015,
    "spinner": 50016,
    "statusbar": 50017,
    "tab": 50018,
    "tabitem": 50019,
    "text": 50020,
    "toolbar": 50021,
    "tooltip": 50022,
    "tree": 50023,
    "treeitem": 50024,
    "custom": 50025,
    "group": 50026,
    "thumb": 50027,
    "datagrid": 50028,
    "dataitem": 50029,
    "document": 50030,
    "splitbutton": 50031,
    "window": 50032,
    "pane": 50033,
    "header": 50034,
    "headeritem": 50035,
    "table": 50036,
    "titlebar": 50037,
    "separator": 50038,
}
_CONTROL_TYPE_NAMES = {v: k for k, v in _CONTROL_TYPES.items()}


def uia_available() -> dict[str, Any]:
    if not sys.platform.startswith("win"):
        return {"ok": False, "available": False, "error": "UI Automation is only available on Windows"}
    try:
        _ensure_uia()
        return {"ok": True, "available": True}
    except Exception as exc:
        return {"ok": False, "available": False, "error": str(exc)}


def _ensure_uia():
    global _UIA, _UIA_MOD
    if _UIA is not None:
        return _UIA, _UIA_MOD
    with _INIT_LOCK:
        if _UIA is not None:
            return _UIA, _UIA_MOD
        if not sys.platform.startswith("win"):
            raise RuntimeError("UI Automation is only available on Windows")
        try:
            import comtypes.client
            from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation
        except ImportError as exc:
            # Generate typelib on first use.
            try:
                import comtypes.client

                comtypes.client.GetModule("UIAutomationCore.dll")
                from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation
            except Exception as gen_exc:
                raise RuntimeError(
                    "comtypes UIAutomation is required on Windows "
                    "(dependency: comtypes). "
                    f"import={exc}; generate={gen_exc}"
                ) from gen_exc
        from comtypes.gen import UIAutomationClient as uia_mod

        uia = comtypes.client.CreateObject(CUIAutomation, interface=IUIAutomation)
        _UIA = uia
        _UIA_MOD = uia_mod
        return _UIA, _UIA_MOD


def _root_element(hwnd: int = 0):
    uia, _mod = _ensure_uia()
    handle = int(hwnd or 0)
    if handle > 0:
        return uia.ElementFromHandle(handle)
    return uia.GetRootElement()


def _runtime_id(el) -> str:
    try:
        rid = el.GetRuntimeId()
        if rid is None:
            return ""
        # comtypes may expose SAFEARRAY as list-like
        vals = list(rid)
        return ".".join(str(int(v)) for v in vals)
    except Exception:
        return ""


def _rect_dict(el) -> dict[str, int]:
    try:
        r = el.CurrentBoundingRectangle
        left = int(r.left)
        top = int(r.top)
        right = int(r.right)
        bottom = int(r.bottom)
        return {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "width": max(0, right - left),
            "height": max(0, bottom - top),
        }
    except Exception:
        return {"left": 0, "top": 0, "right": 0, "bottom": 0, "width": 0, "height": 0}


def _pattern_iface_name(pattern_key: str) -> str:
    return {
        "invoke": "IUIAutomationInvokePattern",
        "value": "IUIAutomationValuePattern",
        "legacy": "IUIAutomationLegacyIAccessiblePattern",
        "selection_item": "IUIAutomationSelectionItemPattern",
        "toggle": "IUIAutomationTogglePattern",
        "text": "IUIAutomationTextPattern",
        "window": "IUIAutomationWindowPattern",
    }.get(pattern_key, "")


def _get_pattern(el, pattern_id: int, pattern_key: str = ""):
    """GetCurrentPattern returns IUnknown; cast to the concrete pattern iface."""
    try:
        raw = el.GetCurrentPattern(pattern_id)
    except Exception:
        return None
    if raw is None:
        return None
    iface_name = _pattern_iface_name(pattern_key)
    if not iface_name:
        return raw
    try:
        _, mod = _ensure_uia()
        iface = getattr(mod, iface_name, None)
        if iface is None:
            return raw
        return raw.QueryInterface(iface)
    except Exception:
        return raw


def _patterns(el) -> list[str]:
    uia, mod = _ensure_uia()
    out: list[str] = []
    checks = [
        ("invoke", mod.UIA_InvokePatternId),
        ("value", mod.UIA_ValuePatternId),
        ("legacy", mod.UIA_LegacyIAccessiblePatternId),
        ("selection_item", getattr(mod, "UIA_SelectionItemPatternId", 10010)),
        ("toggle", getattr(mod, "UIA_TogglePatternId", 10015)),
        ("text", getattr(mod, "UIA_TextPatternId", 10014)),
        ("window", getattr(mod, "UIA_WindowPatternId", 10009)),
    ]
    for name, pid in checks:
        try:
            if _get_pattern(el, pid, name) is not None:
                out.append(name)
        except Exception:
            continue
    return out


def _element_public(el, *, index: int = 0) -> dict[str, Any]:
    try:
        ctype = int(el.CurrentControlType)
    except Exception:
        ctype = 0
    try:
        name = str(el.CurrentName or "")
    except Exception:
        name = ""
    try:
        auto_id = str(el.CurrentAutomationId or "")
    except Exception:
        auto_id = ""
    try:
        class_name = str(el.CurrentClassName or "")
    except Exception:
        class_name = ""
    try:
        enabled = bool(el.CurrentIsEnabled)
    except Exception:
        enabled = False
    try:
        offscreen = bool(el.CurrentIsOffscreen)
    except Exception:
        offscreen = True
    rect = _rect_dict(el)
    return {
        "index": index,
        "runtime_id": _runtime_id(el),
        "name": name[:300],
        "automation_id": auto_id[:200],
        "control_type": _CONTROL_TYPE_NAMES.get(ctype, str(ctype)),
        "control_type_id": ctype,
        "class_name": class_name[:120],
        "enabled": enabled,
        "offscreen": offscreen,
        "rect": rect,
        "patterns": _patterns(el),
    }


def _build_condition(
    *,
    name: str = "",
    name_contains: str = "",
    automation_id: str = "",
    control_type: str = "",
):
    uia, mod = _ensure_uia()
    conds: list[Any] = []
    ct = str(control_type or "").strip().lower()
    if ct:
        if ct not in _CONTROL_TYPES:
            raise ValueError(
                f"unknown control_type={control_type!r}; "
                f"known={', '.join(sorted(_CONTROL_TYPES))}"
            )
        conds.append(uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, _CONTROL_TYPES[ct]))
    aid = str(automation_id or "").strip()
    if aid:
        conds.append(uia.CreatePropertyCondition(mod.UIA_AutomationIdPropertyId, aid))
    exact = str(name or "").strip()
    if exact:
        conds.append(uia.CreatePropertyCondition(mod.UIA_NamePropertyId, exact))
    # name_contains is filtered in Python after FindAll (UIA has no contains condition).
    if not conds:
        return uia.CreateTrueCondition(), str(name_contains or "").strip()
    cond = conds[0]
    for extra in conds[1:]:
        cond = uia.CreateAndCondition(cond, extra)
    return cond, str(name_contains or "").strip()


def _count_control_type(root, control_type: str) -> int:
    uia, mod = _ensure_uia()
    ct = _CONTROL_TYPES.get(control_type)
    if ct is None:
        return 0
    try:
        arr = root.FindAll(
            mod.TreeScope_Descendants,
            uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, ct),
        )
        return int(getattr(arr, "Length", 0) or 0)
    except Exception:
        return 0


def assess_window_uia(hwnd: int = 0) -> dict[str, Any]:
    """Classify how much semantic UIA a window exposes (for chat/IM identity safety)."""
    handle = int(hwnd or 0)
    try:
        root = _root_element(handle)
    except Exception as exc:
        return {"ok": False, "hwnd": handle, "error": str(exc)}

    root_pub = _element_public(root, index=-1)
    rect = root_pub.get("rect") or {}
    width = max(0, int(rect.get("width") or 0))
    height = max(0, int(rect.get("height") or 0))

    semantic = {
        "edit": _count_control_type(root, "edit"),
        "listitem": _count_control_type(root, "listitem"),
        "text": _count_control_type(root, "text"),
        "button": _count_control_type(root, "button"),
        "document": _count_control_type(root, "document"),
    }
    semantic_total = sum(semantic.values())

    uia, mod = _ensure_uia()
    named_sample: list[str] = []
    descendant_estimate = 0
    try:
        arr = root.FindAll(mod.TreeScope_Descendants, uia.CreateTrueCondition())
        descendant_estimate = int(getattr(arr, "Length", 0) or 0)
        for i in range(min(descendant_estimate, 40)):
            try:
                el = arr.GetElement(i)
                n = str(el.CurrentName or "").strip()
            except Exception:
                continue
            if n and n not in named_sample:
                named_sample.append(n[:80])
            if len(named_sample) >= 12:
                break
    except Exception:
        descendant_estimate = semantic_total

    if semantic_total >= 3 or semantic["edit"] > 0 or semantic["listitem"] > 0:
        surface = "rich"
    elif descendant_estimate <= 8 and semantic_total == 0:
        surface = "shallow"
    else:
        surface = "partial"

    out: dict[str, Any] = {
        "ok": True,
        "hwnd": handle,
        "uia_surface": surface,
        "descendant_estimate": descendant_estimate,
        "semantic_controls": semantic,
        "named_sample": named_sample,
        "root": {
            "name": root_pub.get("name"),
            "control_type": root_pub.get("control_type"),
            "rect": rect,
        },
    }
    if width > 0 and height > 0:
        # Composer is usually bottom-center of the chat pane; keep clear of left session list.
        out["chat_input_relative"] = {
            "x": max(1, int(width * 0.62)),
            "y": max(1, int(height * 0.90)),
        }
        out["avoid_left_fraction"] = 0.30
    if surface == "shallow":
        out["warning"] = (
            "This window exposes almost no semantic UIA controls (custom-rendered UI). "
            "Do not trust a vision answer that merely confirms a contact name you put in the query. "
            "Open the contact via in-app search (Ctrl+F / Ctrl+K), then ask vision to quote the "
            "chat header verbatim without naming the expected contact. "
            "Prefer chat_input_relative for composer clicks; never click the left session list "
            "unless you intend to change conversation, and re-verify the header afterward."
        )
    elif surface == "partial":
        out["warning"] = (
            "UIA coverage is partial. Prefer named/listitem controls when present; "
            "otherwise use in-app search + unprimed header quote before typing/sending."
        )
    return out


def find_elements(
    *,
    hwnd: int = 0,
    name: str = "",
    name_contains: str = "",
    automation_id: str = "",
    control_type: str = "",
    limit: int = 30,
    include_offscreen: bool = False,
) -> dict[str, Any]:
    """Find UIA elements under a window (or desktop root when hwnd=0)."""
    try:
        root = _root_element(hwnd)
    except Exception as exc:
        return {"ok": False, "error": f"failed to bind UIA root: {exc}", "hwnd": int(hwnd or 0)}
    try:
        cond, contains = _build_condition(
            name=name,
            name_contains=name_contains,
            automation_id=automation_id,
            control_type=control_type,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_code": "invalid_arguments"}

    uia, mod = _ensure_uia()
    try:
        arr = root.FindAll(mod.TreeScope_Descendants, cond)
    except Exception as exc:
        return {"ok": False, "error": f"UIA FindAll failed: {exc}", "hwnd": int(hwnd or 0)}

    max_n = max(1, min(int(limit or 30), 80))
    contains_l = contains.lower()
    items: list[dict[str, Any]] = []
    total = int(getattr(arr, "Length", 0) or 0)
    for i in range(total):
        if len(items) >= max_n:
            break
        try:
            el = arr.GetElement(i)
        except Exception:
            continue
        pub = _element_public(el, index=len(items))
        if not include_offscreen and pub.get("offscreen") and (pub.get("rect") or {}).get("width", 0) <= 0:
            continue
        if contains_l and contains_l not in str(pub.get("name") or "").lower():
            continue
        items.append(pub)

    root_pub = _element_public(root, index=-1)
    out: dict[str, Any] = {
        "ok": True,
        "hwnd": int(hwnd or 0),
        "root": {
            "name": root_pub.get("name"),
            "control_type": root_pub.get("control_type"),
            "rect": root_pub.get("rect"),
        },
        "count": len(items),
        "scanned": total,
        "elements": items,
        "hint": (
            "Prefer desktop_uia_action(action='focus'|'invoke'|'set_value'|'select', …) "
            "with runtime_id / automation_id / name from this list instead of pixel clicks."
        ),
    }
    # When a filtered find is empty, surface assessment so the agent does not
    # silently fall back to primed vision + blind clicks (common on WeChat/QQ).
    if len(items) == 0 and int(hwnd or 0) > 0:
        try:
            assess = assess_window_uia(int(hwnd or 0))
        except Exception as exc:
            assess = {"ok": False, "error": str(exc)}
        if assess.get("ok"):
            out["uia_surface"] = assess.get("uia_surface")
            out["window_action_hints"] = assess
            if assess.get("warning"):
                out["warning"] = assess["warning"]
            if assess.get("chat_input_relative"):
                out["hint"] = (
                    f"{out['hint']} Filtered find returned 0 controls. "
                    f"uia_surface={assess.get('uia_surface')}. "
                    "Use in-app search + unprimed header quote before send; "
                    f"composer click hint={assess.get('chat_input_relative')}."
                )
    return out


def _match_element(
    root,
    *,
    runtime_id: str = "",
    name: str = "",
    name_contains: str = "",
    automation_id: str = "",
    control_type: str = "",
):
    rid = str(runtime_id or "").strip()
    if rid:
        # Walk with true condition and match runtime id (bounded).
        uia, mod = _ensure_uia()
        arr = root.FindAll(mod.TreeScope_Descendants, uia.CreateTrueCondition())
        total = int(getattr(arr, "Length", 0) or 0)
        for i in range(min(total, 4000)):
            el = arr.GetElement(i)
            if _runtime_id(el) == rid:
                return el
        return None

    cond, contains = _build_condition(
        name=name,
        name_contains=name_contains,
        automation_id=automation_id,
        control_type=control_type,
    )
    uia, mod = _ensure_uia()
    if contains:
        arr = root.FindAll(mod.TreeScope_Descendants, cond)
        contains_l = contains.lower()
        for i in range(int(getattr(arr, "Length", 0) or 0)):
            el = arr.GetElement(i)
            try:
                n = str(el.CurrentName or "")
            except Exception:
                n = ""
            if contains_l in n.lower():
                return el
        return None
    return root.FindFirst(mod.TreeScope_Descendants, cond)


def element_action(
    *,
    action: str,
    hwnd: int = 0,
    runtime_id: str = "",
    name: str = "",
    name_contains: str = "",
    automation_id: str = "",
    control_type: str = "",
    value: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Act on one UIA element: focus | invoke | set_value | select | toggle | click."""
    act = str(action or "").strip().lower()
    if act not in {"focus", "invoke", "set_value", "select", "toggle", "click"}:
        return {
            "ok": False,
            "error": "action must be one of focus|invoke|set_value|select|toggle|click",
            "error_code": "invalid_arguments",
        }
    if not any([runtime_id, name, name_contains, automation_id]):
        return {
            "ok": False,
            "error": "provide runtime_id or name/name_contains/automation_id to locate the element",
            "error_code": "invalid_arguments",
        }

    try:
        root = _root_element(hwnd)
        el = _match_element(
            root,
            runtime_id=runtime_id,
            name=name,
            name_contains=name_contains,
            automation_id=automation_id,
            control_type=control_type,
        )
    except Exception as exc:
        return {"ok": False, "error": f"UIA locate failed: {exc}", "hwnd": int(hwnd or 0)}

    if el is None:
        return {
            "ok": False,
            "error": "element not found",
            "error_code": "not_found",
            "hwnd": int(hwnd or 0),
            "query": {
                "runtime_id": runtime_id,
                "name": name,
                "name_contains": name_contains,
                "automation_id": automation_id,
                "control_type": control_type,
            },
        }

    before = _element_public(el)
    uia, mod = _ensure_uia()
    click_info: dict[str, Any] | None = None
    try:
        if act == "focus":
            el.SetFocus()
        elif act == "click":
            rect = before.get("rect") or {}
            left = int(rect.get("left") or 0)
            top = int(rect.get("top") or 0)
            width = int(rect.get("width") or 0)
            height = int(rect.get("height") or 0)
            if width <= 0 or height <= 0:
                return {
                    "ok": False,
                    "error": "element has no usable bounding rect for click",
                    "element": before,
                }
            cx = left + width // 2
            cy = top + height // 2
            from argusai.core.workspace import desktop_control as dc

            click_info = dc.mouse_click(str(session_id or "default"), x=cx, y=cy, button="left")
            if not click_info.get("ok"):
                return {
                    "ok": False,
                    "error": click_info.get("error") or "mouse click failed",
                    "element": before,
                    "click": click_info,
                }
        elif act == "invoke":
            pat = _get_pattern(el, mod.UIA_InvokePatternId, "invoke")
            if pat is None:
                return {
                    "ok": False,
                    "error": "element has no Invoke pattern; try action='click' on the same runtime_id",
                    "element": before,
                }
            pat.Invoke()
        elif act == "set_value":
            pat = _get_pattern(el, mod.UIA_ValuePatternId, "value")
            if pat is None:
                # Fallback: focus then caller can desktop_type — still try LegacyIAccessible.
                legacy = _get_pattern(el, mod.UIA_LegacyIAccessiblePatternId, "legacy")
                if legacy is None:
                    return {
                        "ok": False,
                        "error": "element has no Value pattern; focus it then use desktop_type",
                        "element": before,
                    }
                el.SetFocus()
                try:
                    legacy.SetValue(str(value or ""))
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": f"LegacyIAccessible SetValue failed: {exc}",
                        "element": before,
                    }
            else:
                if getattr(pat, "CurrentIsReadOnly", False):
                    return {"ok": False, "error": "value pattern is read-only", "element": before}
                pat.SetValue(str(value or ""))
        elif act == "select":
            pid = getattr(mod, "UIA_SelectionItemPatternId", 10010)
            pat = _get_pattern(el, pid, "selection_item")
            if pat is None:
                # Many list items expose Invoke instead.
                inv = _get_pattern(el, mod.UIA_InvokePatternId, "invoke")
                if inv is None:
                    return {
                        "ok": False,
                        "error": "element has no SelectionItem/Invoke pattern; try action='click'",
                        "element": before,
                    }
                inv.Invoke()
            else:
                pat.Select()
        elif act == "toggle":
            pid = getattr(mod, "UIA_TogglePatternId", 10015)
            pat = _get_pattern(el, pid, "toggle")
            if pat is None:
                return {"ok": False, "error": "element has no Toggle pattern", "element": before}
            pat.Toggle()
    except Exception as exc:
        return {"ok": False, "error": f"UIA action failed: {exc}", "action": act, "element": before}

    after = _element_public(el)
    out: dict[str, Any] = {
        "ok": True,
        "action": act,
        "hwnd": int(hwnd or 0),
        "element": after,
        "value_set": str(value or "") if act == "set_value" else None,
    }
    if click_info is not None:
        out["click"] = click_info
        out["hint"] = (
            "Clicked element bounding-rect center. Prefer this over invoke for "
            "Chromium/QQ/WeChat search hits when invoke returns ok but UI is unchanged."
        )
    elif act in {"invoke", "select"}:
        out["hint"] = (
            "If the UI did not change after invoke/select, retry with action='click' "
            "on the same runtime_id (common for Chromium/Electron list items)."
        )
    else:
        out["hint"] = "Re-check with desktop_observe(screenshot=true, monitor=…) after mutating actions."
    return out
