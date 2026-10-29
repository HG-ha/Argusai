"""System-browser discovery + CDP attach/detach + Playwright engine installs."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class BrowserMixin:
    """Browser-related bridge methods used by the settings panel.

    Three concerns live here:

    1. **Discovery** — surface the Chromium-family browsers found on
       this machine (``scan_system_browsers``) plus the Playwright
       bundle status, so the settings UI can populate a picker without
       calling assistant tools.
    2. **Attach/detach** — wire a global ``BrowserHub`` to the user's
       real browser via CDP. After attach, every assistant tool that
       drives Playwright reuses that session instead of spawning a
       fresh isolated context.
    3. **One-click chrome launch** — ``launch_chrome`` is the simpler
       sibling: starts Chrome with ``--remote-debugging-port`` and
       returns the resulting CDP URL, without touching the global hub.
    """

    def scan_system_browsers(self) -> dict[str, Any]:
        """Return Chromium-family browsers found on this machine + Playwright bundle status."""
        try:
            from argusai.core.browser.browser_locator import (
                playwright_bundle_status,
                resolve_browser_binary,
                scan_system_browsers,
            )

            return {
                "ok": True,
                "system_browsers": scan_system_browsers(),
                "playwright_bundle": playwright_bundle_status(),
                "recommended": resolve_browser_binary(),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def attach_browser_status(self) -> dict[str, Any]:
        """Mirror :func:`browser_attach_status` for the settings panel.

        Returns the currently attached CDP endpoint (if any), whether
        the DevTools port still responds, and a list of probable
        endpoints under common ports.
        """
        try:
            from argusai.core.browser.browser_attach import (
                cdp_endpoint_alive,
                discover_running_cdp_endpoints,
            )
            from argusai.core.browser.browser_pool import get_attached_hub

            hub = get_attached_hub()
            attached: dict[str, Any] = {"attached": False}
            if hub:
                st = hub.status()
                endpoint = st.get("attached_endpoint") or ""
                port = 0
                if endpoint.startswith("http"):
                    try:
                        port = int(endpoint.rsplit(":", 1)[-1])
                    except (ValueError, IndexError):
                        port = 0
                live = cdp_endpoint_alive(port) if port else {"ok": False}
                attached = {
                    "attached": True,
                    "endpoint": endpoint,
                    "alive": bool(live.get("ok")),
                    "browser": live.get("browser", ""),
                }
            return {
                "ok": True,
                **attached,
                "discovered": discover_running_cdp_endpoints(9222, 9230),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def launch_browser_for_attach(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Spawn the user's real browser with ``--remote-debugging-port`` and attach.

        One-click flow used by the settings panel: kill the running
        Edge/Chrome of the requested channel, relaunch it with the user's
        existing profile + the debug port, then call ``connect_over_cdp``
        and install the global attached hub so AI assistant tools start
        operating on the real session.
        """
        try:
            from argusai.core.browser.browser_attach import (
                argusai_cdp_profile_path,
                channel_friendly_label,
                find_default_profile_path,
                kill_browser_processes,
                launch_with_debug_port,
            )
            from argusai.core.browser.browser_locator import (
                find_channel_executable,
                resolve_browser_binary,
            )
            from argusai.core.browser.browser_pool import (
                BrowserHub,
                clear_attached_hub,
                set_attached_hub,
            )

            p = dict(params or {})
            channel = (p.get("channel") or "msedge").strip().lower()
            port = int(p.get("port") or 9222)
            use_my_profile = bool(p.get("use_my_profile", True))
            user_data_dir = (p.get("user_data_dir") or "").strip()
            executable_path = (p.get("executable_path") or "").strip()
            kill_existing = bool(p.get("kill_existing", True))
            # Default hidden: the agent drives the real browser off-screen and
            # the human watches via the in-app live view. Pass ``show_window``
            # to pop a visible window instead.
            headless = not bool(p.get("show_window", False))

            if not executable_path:
                # ``resolve_browser_binary`` may return a channel without an exe
                # path (it's fine for Playwright .launch but not for
                # subprocess.Popen). Fall back to the system scan.
                hint = resolve_browser_binary(explicit_channel=channel)
                executable_path = hint.get("executable_path") or find_channel_executable(channel)
            if not executable_path:
                return {
                    "ok": False,
                    "error": f"找不到 {channel_friendly_label(channel)} 的可执行文件，请在“设置 → 浏览器”里手动填写路径",
                }
            if not user_data_dir and use_my_profile:
                user_data_dir = find_default_profile_path(channel)

            kill_result: dict[str, Any] = {}
            if kill_existing:
                kill_result = kill_browser_processes(channel)

            launch = launch_with_debug_port(
                executable_path=executable_path,
                user_data_dir=user_data_dir,
                port=port,
                headless=headless,
            )
            fallback_profile = ""
            if not launch.get("ok"):
                fallback_profile = argusai_cdp_profile_path(channel)
                retry = launch_with_debug_port(
                    executable_path=executable_path,
                    user_data_dir=fallback_profile,
                    port=port,
                    headless=headless,
                )
                if retry.get("ok"):
                    launch = retry
                    user_data_dir = fallback_profile
                else:
                    return {
                        "ok": False,
                        "error": retry.get("error") or launch.get("error") or "启动浏览器失败",
                        "kill_result": kill_result,
                        "argv": retry.get("argv") or launch.get("argv"),
                        "fallback_profile": fallback_profile,
                        "first_error": launch.get("error", ""),
                    }

            clear_attached_hub(shutdown=True)
            hub = BrowserHub(attached_endpoint=launch["endpoint"])

            import asyncio as _asyncio

            try:
                tabs = _asyncio.run(hub.alist_pages())
            except Exception as e:
                try:
                    hub.shutdown()
                except Exception:
                    pass
                return {
                    "ok": False,
                    "error": f"connect_over_cdp 成功之后枚举标签页失败: {e}",
                    "endpoint": launch["endpoint"],
                }

            set_attached_hub(hub)
            return {
                "ok": True,
                "channel": channel,
                "channel_label": channel_friendly_label(channel),
                "executable_path": executable_path,
                "user_data_dir": user_data_dir,
                "endpoint": launch["endpoint"],
                "pid": launch.get("pid"),
                "browser": launch.get("browser", ""),
                "headless": headless,
                "tabs": tabs,
                "kill_result": kill_result,
                "fallback_profile": fallback_profile,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def detach_browser(self) -> dict[str, Any]:
        """Release the CDP attachment (browser keeps running)."""
        try:
            from argusai.core.browser.browser_pool import clear_attached_hub, get_attached_hub

            hub = get_attached_hub()
            if not hub:
                return {"ok": True, "attached": False}
            endpoint = hub.status().get("attached_endpoint", "")
            clear_attached_hub(shutdown=True)
            return {"ok": True, "attached": False, "previous_endpoint": endpoint}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def attach_existing_browser(
        self, endpoint: str = "http://127.0.0.1:9222"
    ) -> dict[str, Any]:
        """Attach to a CDP endpoint the user already brought up themselves."""
        try:
            from argusai.core.browser.browser_attach import cdp_endpoint_alive
            from argusai.core.browser.browser_pool import (
                BrowserHub,
                clear_attached_hub,
                set_attached_hub,
            )

            ep = (endpoint or "").strip()
            if not ep:
                return {"ok": False, "error": "endpoint 不能为空"}
            try:
                port = int(ep.rsplit(":", 1)[-1])
            except (ValueError, IndexError):
                return {"ok": False, "error": "endpoint 必须形如 http://127.0.0.1:9222"}
            info = cdp_endpoint_alive(port)
            if not info.get("ok"):
                return {"ok": False, "error": f"{ep} 没有响应; 浏览器是否带 --remote-debugging-port 启动?"}

            clear_attached_hub(shutdown=True)
            hub = BrowserHub(attached_endpoint=ep)
            import asyncio as _asyncio

            try:
                tabs = _asyncio.run(hub.alist_pages())
            except Exception as e:
                try:
                    hub.shutdown()
                except Exception:
                    pass
                return {"ok": False, "error": f"connect_over_cdp 失败: {e}"}
            set_attached_hub(hub)
            return {"ok": True, "endpoint": ep, "browser": info.get("browser", ""), "tabs": tabs}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def install_playwright_engine(self, engine: str = "chromium") -> dict[str, Any]:
        """Run ``playwright install <engine>`` synchronously and return the result."""
        import subprocess
        from argusai.core.infra.win_stdio import quiet_subprocess_kwargs

        eng = (engine or "chromium").strip().lower()
        if eng not in ("chromium", "firefox", "webkit"):
            return {"ok": False, "error": f"unknown engine: {engine}"}
        try:
            from argusai.core.browser.browser_locator import playwright_install_argv

            cmd = playwright_install_argv(eng)
        except (RuntimeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "engine": eng}
        try:
            r = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=900,
                **quiet_subprocess_kwargs(),
            )
            output = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
            if len(output) > 8000:
                output = output[:4000] + "\n...\n" + output[-3500:]
            from argusai.core.browser.browser_locator import playwright_bundle_status

            return {
                "ok": r.returncode == 0,
                "exit_code": r.returncode,
                "engine": eng,
                "log": output.strip(),
                "playwright_bundle": playwright_bundle_status(),
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "playwright install 超过 15 分钟未完成"}
        except FileNotFoundError as e:
            return {"ok": False, "error": f"无法启动 playwright: {e}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def launch_chrome(self, params: dict[str, Any]) -> dict[str, Any]:
        """Launch system Chrome with ``--remote-debugging-port``.

        Used as a quick start: the caller still has to ``attach_existing_browser``
        afterwards if they want the assistant to drive it.
        """
        port = params.get("port", 9222)
        try:
            import platform
            import shutil
            import subprocess

            candidates: list[str | None] = []
            system = platform.system()
            if system == "Windows":
                candidates = [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    shutil.which("chrome"),
                    shutil.which("google-chrome"),
                ]
            elif system == "Darwin":
                candidates = [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    shutil.which("google-chrome-stable"),
                ]
            else:
                candidates = [
                    shutil.which("google-chrome-stable"),
                    shutil.which("google-chrome"),
                    shutil.which("chromium-browser"),
                    shutil.which("chromium"),
                ]

            chrome: str | None = None
            for c in candidates:
                if c and Path(c).exists():
                    chrome = c
                    break

            if not chrome:
                return {
                    "ok": False,
                    "error": (
                        "Chrome not found. Install Chrome or launch it manually: "
                        f"chrome --remote-debugging-port={port}"
                    ),
                }

            headless = not bool(params.get("show_window", False))
            argv = [chrome, f"--remote-debugging-port={port}"]
            if headless:
                argv.append("--headless=new")
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"ok": True, "port": port, "cdp_url": f"http://127.0.0.1:{port}", "headless": headless}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- live browser viewer (CDP screencast + hand-off) ------------

    def _live_hubs(self) -> list[Any]:
        """Every browser the panel should list: the attached one and each member hub."""
        from argusai.core.browser.browser_pool import watchable_browser_hubs

        return watchable_browser_hubs()

    def _live_hub(self):
        """Resolve the hub the agent is currently driving (or None)."""
        hubs = self._live_hubs()
        return hubs[0] if hubs else None

    def browser_live_status(self) -> dict[str, Any]:
        """Summary for the live-browser panel: is there anything to watch?"""
        try:
            from argusai.core.browser.browser_live import CONTROL

            hubs = self._live_hubs()
            ctrl = CONTROL.snapshot()
            if not hubs:
                return {"ok": True, "available": False, "browsers": 0, **ctrl}
            st = hubs[0].status()
            return {
                "ok": True,
                "available": True,
                "browsers": len(hubs),
                "attached": any(bool(hub.status().get("attached")) for hub in hubs),
                "headless": bool(st.get("headless")),
                "resolved_label": st.get("resolved_label", ""),
                **ctrl,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_live_targets(self) -> dict[str, Any]:
        """List the tabs of the active browser so the user can pick one."""
        try:
            import asyncio as _asyncio

            hub = self._live_hub()
            if hub is None:
                return {"ok": True, "available": False, "tabs": []}
            tabs = _asyncio.run(hub.alist_pages())
            return {"ok": True, "available": True, "tabs": tabs}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_live_thumbnails(self) -> dict[str, Any]:
        """Still screenshot of every open tab across the attached browser and member hubs."""
        try:
            hubs = self._live_hubs()
            if not hubs:
                return {"ok": True, "available": False, "tabs": []}
            tabs: list[dict[str, Any]] = []
            for hub in hubs:
                st = hub.status()
                label = str(st.get("resolved_label") or st.get("attached_endpoint") or st.get("profile_name") or st.get("engine") or "browser")
                part = hub.live_thumbnails()
                for tab in list(part.get("tabs") or []):
                    if isinstance(tab, dict):
                        row = dict(tab)
                        row["browser"] = label
                        tabs.append(row)
            return {"ok": True, "available": True, "tabs": tabs, "browsers": len(hubs)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_live_start(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start streaming a CDP screencast of the agent's browser."""
        try:
            hub = self._live_hub()
            if hub is None:
                return {"ok": False, "available": False, "error": "Agent 尚未开始操作浏览器，暂无可观看的会话。"}
            p = dict(params or {})
            return hub.start_live_view(
                target_id=str(p.get("target_id") or ""),
                index=int(p.get("index", -1)),
                max_width=int(p.get("max_width", 1280)),
                max_height=int(p.get("max_height", 900)),
                quality=int(p.get("quality", 55)),
                every_nth=int(p.get("every_nth", 1)),
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_live_stop(self) -> dict[str, Any]:
        try:
            hub = self._live_hub()
            if hub is None:
                return {"ok": True}
            return hub.stop_live_view()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_live_frame(self) -> dict[str, Any]:
        """Hot path polled by the panel ~10fps for the latest frame."""
        try:
            hub = self._live_hub()
            if hub is None:
                return {"ok": True, "available": False, "running": False, "frame": "", "seq": 0}
            out = hub.live_frame()
            out["available"] = True
            return out
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_live_input(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Forward one batch of viewer mouse/key events over CDP."""
        try:
            hub = self._live_hub()
            if hub is None:
                return {"ok": False, "error": "no active browser"}
            events = (payload or {}).get("events") or []
            return hub.dispatch_live_input(events)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_live_take_control(self, force: bool = False) -> dict[str, Any]:
        """Hand the browser to the human. ``force`` grabs it mid-operation."""
        try:
            from argusai.core.browser.browser_live import CONTROL
            from argusai.core.browser.browser_pool import notify_browser_ui

            out = CONTROL.take(force=bool(force))
            notify_browser_ui({"reason": "control"})
            return out
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_live_release_control(self) -> dict[str, Any]:
        """Hand control back to the agent."""
        try:
            from argusai.core.browser.browser_live import CONTROL
            from argusai.core.browser.browser_pool import notify_browser_ui

            out = CONTROL.release()
            notify_browser_ui({"reason": "control"})
            return out
        except Exception as e:
            return {"ok": False, "error": str(e)}
