"""CLI management commands for channels and local agents."""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

from argusai.cli.main import _register_commands, app

_register_commands()
runner = CliRunner()


class _FakeApi:
    def __init__(self) -> None:
        self.channels: dict[str, dict[str, Any]] = {}
        self.runtimes: dict[str, dict[str, Any]] = {
            "argusai-native": {
                "id": "argusai-native",
                "name": "ArgusAI Agent",
                "backend": "native",
                "enabled": True,
                "command": "",
                "preset": True,
            }
        }
        self.pairings: list[dict[str, Any]] = []
        self.identities: list[dict[str, Any]] = []
        self.ai_endpoints: list[dict[str, Any]] = []
        self.ai_enabled: bool = True

    @classmethod
    def for_headless(cls) -> "_FakeApi":
        return cls()

    def shutdown_background_services(self) -> None:
        return None

    def list_channel_providers(self) -> dict[str, Any]:
        return {
            "ok": True,
            "providers": [
                {
                    "id": "telegram",
                    "name": "Telegram",
                    "transport": "long_polling",
                    "secrets": ["bot_token"],
                },
                {
                    "id": "feishu",
                    "name": "Feishu",
                    "transport": "websocket",
                    "secrets": ["app_id", "app_secret", "verification_token"],
                },
            ],
        }

    def list_channels(self) -> dict[str, Any]:
        rows = []
        for ch in self.channels.values():
            rows.append(
                {
                    **ch,
                    "status": {"connected": False, "last_error": ""},
                    "configured_secrets": {
                        k: True for k in (ch.get("secrets") or {})
                    },
                }
            )
        return {"ok": True, "channels": rows}

    def save_channel(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        import uuid

        payload = dict(data or {})
        secrets = payload.pop("secrets", {}) or {}
        provider = str(payload.get("provider") or "channel")
        cid = str(payload.get("id") or f"{provider}-{uuid.uuid4().hex[:10]}")
        row = {
            "id": cid,
            "provider": provider,
            "name": payload.get("name") or provider,
            "enabled": bool(payload.get("enabled")),
            "transport": payload.get("transport") or "",
            "settings": dict(payload.get("settings") or {}),
            "secrets": dict(secrets),
        }
        self.channels[cid] = row
        public = {k: v for k, v in row.items() if k != "secrets"}
        public["configured_secrets"] = {k: True for k in secrets}
        return {"ok": True, "channel": public}

    def set_channel_enabled(self, channel_id: str, enabled: bool) -> dict[str, Any]:
        row = self.channels.get(channel_id)
        if not row:
            return {"ok": False, "error": "channel not found"}
        row["enabled"] = bool(enabled)
        return {"ok": True, "channel": {k: v for k, v in row.items() if k != "secrets"}}

    def delete_channel(self, channel_id: str) -> dict[str, Any]:
        if channel_id not in self.channels:
            return {"ok": False, "error": "channel not found"}
        del self.channels[channel_id]
        return {"ok": True, "channel_id": channel_id}

    def test_channel(self, channel_id: str) -> dict[str, Any]:
        if channel_id not in self.channels:
            return {"ok": False, "error": "channel not found"}
        return {"ok": True, "detail": "pong"}

    def create_channel_pairing(
        self, channel_id: str, session_id: str = ""
    ) -> dict[str, Any]:
        if channel_id not in self.channels:
            return {"ok": False, "error": "channel not found"}
        payload = {
            "ok": True,
            "code": "123456",
            "channel_id": channel_id,
            "expires_at": 1,
        }
        self.pairings.append(payload)
        return payload

    def list_channel_identities(self, channel_id: str = "") -> dict[str, Any]:
        rows = self.identities
        if channel_id:
            rows = [r for r in rows if r.get("channel_id") == channel_id]
        return {"ok": True, "identities": rows}

    def bind_channel_identity(self, identity_id: str, session_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "identity": {
                "id": identity_id,
                "bound_session_id": session_id,
            },
        }

    def list_agent_runtimes(self) -> dict[str, Any]:
        rows = []
        for item in self.runtimes.values():
            rows.append(
                {
                    **item,
                    "probe": {
                        "available": item.get("backend") == "native",
                        "resolved_command": item.get("command") or "",
                    },
                }
            )
        return {"ok": True, "runtimes": rows, "available": ["argusai-native"], "missing": []}

    def save_agent_runtime(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(data or {})
        rid = str(payload.get("id") or "custom-demo")
        prev = dict(self.runtimes.get(rid) or {})
        merged = {**prev, **payload, "id": rid}
        if not merged.get("name") or (
            merged.get("backend") == "acp" and not merged.get("command") and rid not in self.runtimes
        ):
            if rid not in self.runtimes:
                return {"ok": False, "error": "runtime configuration is invalid"}
        self.runtimes[rid] = merged
        return {"ok": True, "runtime": merged}

    def delete_agent_runtime(self, runtime_id: str) -> dict[str, Any]:
        if runtime_id == "argusai-native":
            return {"ok": False, "error": "built-in runtime presets can be disabled but not deleted"}
        if runtime_id not in self.runtimes:
            return {"ok": False, "error": "runtime not found"}
        del self.runtimes[runtime_id]
        return {"ok": True, "runtime_id": runtime_id}

    def test_agent_runtime(self, runtime_id: str) -> dict[str, Any]:
        if runtime_id not in self.runtimes:
            return {"ok": False, "error": "unknown"}
        return {
            "ok": True,
            "runtime_id": runtime_id,
            "protocol_version": "native",
            "agent_info": {"name": "argusai", "title": "ArgusAI"},
        }

    def list_ai_provider_presets(self) -> dict[str, Any]:
        return {
            "ok": True,
            "categories": [{"id": "cn", "label": "CN"}],
            "presets": [
                {
                    "id": "ali-bailian",
                    "name": "阿里云百炼",
                    "protocol": "openai",
                    "default_model": "qwen3.7-plus",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                }
            ],
        }

    def list_ai_endpoints(self) -> dict[str, Any]:
        rows = []
        ranked = sorted(
            enumerate(self.ai_endpoints),
            key=lambda item: int(item[1].get("priority") or 0),
            reverse=True,
        )
        active_index = None
        for index, ep in ranked:
            if ep.get("enabled", True):
                active_index = index
                break
        for index, ep in enumerate(self.ai_endpoints):
            rows.append({**ep, "index": index, "active": index == active_index})
        return {"ok": True, "ai_enabled": self.ai_enabled, "endpoints": rows}

    def save_ai_endpoint(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = dict(payload or {})
        index = data.get("index")
        if index is None and data.get("name"):
            folded = str(data["name"]).casefold()
            for i, ep in enumerate(self.ai_endpoints):
                if str(ep.get("name") or "").casefold() == folded:
                    index = i
                    break
        if index is not None:
            index = int(index)
            if index < 0 or index >= len(self.ai_endpoints):
                return {"ok": False, "error": "endpoint index out of range"}
            prev = dict(self.ai_endpoints[index])
            merged = {**prev, **data, "index": index}
            if not data.get("api_key"):
                merged["api_key"] = prev.get("api_key")
            self.ai_endpoints[index] = merged
        else:
            if not data.get("base_url") and data.get("provider_id") == "ali-bailian":
                data["base_url"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
                data.setdefault("model", "qwen3.7-plus")
                data.setdefault("name", "阿里云百炼")
            if not data.get("base_url") or not data.get("model") or not data.get("api_key"):
                return {"ok": False, "error": "base_url/model/api_key required"}
            index = len(self.ai_endpoints)
            data["index"] = index
            data.setdefault("enabled", True)
            data.setdefault("priority", 0)
            data["api_key_set"] = True
            self.ai_endpoints.append(data)
            merged = data
        merged = dict(self.ai_endpoints[index])
        merged["api_key_set"] = bool(merged.get("api_key"))
        merged["api_key"] = "********" + str(merged.get("api_key") or "")[-4:]
        return {"ok": True, "index": index, "endpoint": merged}

    def delete_ai_endpoint(self, selector: str | int) -> dict[str, Any]:
        text = str(selector)
        if text.isdigit():
            index = int(text)
        else:
            folded = text.casefold()
            index = next(
                (i for i, ep in enumerate(self.ai_endpoints) if str(ep.get("name") or "").casefold() == folded),
                None,
            )
        if index is None or index < 0 or index >= len(self.ai_endpoints):
            return {"ok": False, "error": "endpoint not found"}
        removed = self.ai_endpoints.pop(index)
        return {"ok": True, "index": index, "name": removed.get("name") or ""}

    def activate_ai_endpoint(self, selector: str | int, model: str = "") -> dict[str, Any]:
        listed = self.list_ai_endpoints()
        rows = listed["endpoints"]
        text = str(selector)
        match = None
        if text.isdigit():
            match = next((r for r in rows if r["index"] == int(text)), None)
        else:
            folded = text.casefold()
            match = next((r for r in rows if str(r.get("name") or "").casefold() == folded), None)
        if match is None:
            return {"ok": False, "error": "endpoint not found"}
        top = max((int(ep.get("priority") or 0) for ep in self.ai_endpoints), default=0)
        ep = self.ai_endpoints[match["index"]]
        ep["priority"] = top + 10
        ep["enabled"] = True
        if model:
            ep["model"] = model
        return {"ok": True, "index": match["index"], "endpoint": {**ep, "active": True, "index": match["index"]}}

    def test_ai_endpoint(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        if not endpoint.get("api_key"):
            return {"ok": False, "error": "no key"}
        return {"ok": True, "response": "pong"}


def test_channels_providers_help() -> None:
    result = runner.invoke(app, ["channels", "providers", "--help"])
    assert result.exit_code == 0


def test_agent_help() -> None:
    result = runner.invoke(app, ["agent", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "add" in result.stdout


def test_channels_add_and_enable(monkeypatch) -> None:
    from argusai.cli.commands import channels as channels_mod

    fake = _FakeApi()
    monkeypatch.setattr(channels_mod, "headless_api", lambda: fake)

    result = runner.invoke(
        app,
        [
            "channels",
            "add",
            "telegram",
            "--name",
            "TG Bot",
            "-s",
            "bot_token=secret-token",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert fake.channels, "id should be auto-assigned by save_channel"
    cid = next(iter(fake.channels))
    assert fake.channels[cid]["name"] == "TG Bot"
    assert fake.channels[cid]["secrets"]["bot_token"] == "secret-token"

    result = runner.invoke(app, ["channels", "enable", cid, "--json"])
    assert result.exit_code == 0
    assert fake.channels[cid]["enabled"] is True

    result = runner.invoke(app, ["channels", "pair", cid, "--json"])
    assert result.exit_code == 0
    assert "123456" in result.stdout


def test_ai_help() -> None:
    result = runner.invoke(app, ["ai", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "add" in result.stdout
    assert "providers" in result.stdout


def test_ai_add_enable_activate_remove(monkeypatch) -> None:
    from argusai.cli.commands import ai as ai_mod

    fake = _FakeApi()
    monkeypatch.setattr(ai_mod, "headless_api", lambda: fake)

    result = runner.invoke(
        app,
        [
            "ai",
            "add",
            "--provider",
            "ali-bailian",
            "--api-key",
            "sk-demo-key",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert len(fake.ai_endpoints) == 1
    assert fake.ai_endpoints[0]["provider_id"] == "ali-bailian"
    assert fake.ai_endpoints[0]["api_key"] == "sk-demo-key"

    result = runner.invoke(app, ["ai", "disable", "0", "--json"])
    assert result.exit_code == 0, result.stdout
    assert fake.ai_endpoints[0]["enabled"] is False

    result = runner.invoke(app, ["ai", "enable", "阿里云百炼", "--json"])
    assert result.exit_code == 0, result.stdout
    assert fake.ai_endpoints[0]["enabled"] is True

    result = runner.invoke(app, ["ai", "activate", "0", "--json"])
    assert result.exit_code == 0, result.stdout
    assert int(fake.ai_endpoints[0]["priority"]) >= 10

    result = runner.invoke(app, ["ai", "remove", "0", "--yes", "--json"])
    assert result.exit_code == 0, result.stdout
    assert fake.ai_endpoints == []


def test_agent_add_enable_remove(monkeypatch) -> None:
    from argusai.cli.commands import agent as agent_mod

    fake = _FakeApi()
    monkeypatch.setattr(agent_mod, "headless_api", lambda: fake)

    result = runner.invoke(
        app,
        [
            "agent",
            "add",
            "--id",
            "custom-demo",
            "--name",
            "Demo",
            "--command",
            "demo-cli",
            "-a",
            "acp",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert fake.runtimes["custom-demo"]["command"] == "demo-cli"
    assert fake.runtimes["custom-demo"]["args"] == ["acp"]

    result = runner.invoke(app, ["agent", "disable", "custom-demo", "--json"])
    assert result.exit_code == 0
    assert fake.runtimes["custom-demo"]["enabled"] is False

    result = runner.invoke(app, ["agent", "remove", "custom-demo", "--yes", "--json"])
    assert result.exit_code == 0
    assert "custom-demo" not in fake.runtimes


def test_parse_kv_pairs() -> None:
    from argusai.cli.commands._host import parse_kv_pairs

    assert parse_kv_pairs(["a=1", "b=x=y"]) == {"a": "1", "b": "x=y"}
