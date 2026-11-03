"""Generate/check the exact CLI parameter reference without invoking commands.

Run with the project's dependencies: uv run python scripts/cli_docs.py --write
"""
from __future__ import annotations

import argparse
from enum import Enum
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "wiki/CLI-Options.md"
TRANSLATIONS = ROOT / "scripts/cli_docs_zh.json"


def _cell(value: object) -> str:
    return " ".join(str(value or "").split()).replace("|", r"\|")


def _type_name(parameter) -> str:
    kind = parameter.type
    if getattr(parameter, "is_flag", False):
        return "开关"
    choices = getattr(kind, "choices", None)
    if choices is not None:
        return " / ".join(str(v.value if isinstance(v, Enum) else v) for v in choices)
    name = str(getattr(kind, "name", "value"))
    if name == "path":
        traits = ["路径"]
        if getattr(kind, "exists", False):
            traits.append("须已存在")
        if not getattr(kind, "dir_okay", True):
            traits.append("文件")
        if not getattr(kind, "file_okay", True):
            traits.append("目录")
        name = "，".join(traits)
    name = {"str": "文本", "string": "文本", "int": "整数", "integer": "整数",
            "float": "数值", "boolean": "布尔值", "value": "值"}.get(name, name)
    lower, upper = getattr(kind, "min", None), getattr(kind, "max", None)
    if lower is not None or upper is not None:
        name += f" [{lower if lower is not None else '-∞'}, {upper if upper is not None else '∞'}]"
    return name


def _default(parameter) -> str:
    value = parameter.default
    if value is None or getattr(value, "name", None) == "UNSET":
        return "—"
    if callable(value):
        return "运行时决定"
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "空列表"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value == "":
        return "空字符串"
    return str(value)


def command_tree():
    # Load declarations only: no parse/invoke, callbacks, defaults or probes.
    sys.path.insert(0, str(ROOT))
    from typer.main import get_command
    from argusai.cli.main import app, _register_commands

    _register_commands()
    root_command = get_command(app)
    rows = []

    def visit(command, name, parent=None):
        context = command.context_class(command, info_name=name, parent=parent)
        children = getattr(command, "commands", {})
        rows.append((command, context, bool(children)))
        for child_name in sorted(children):
            child = children[child_name]
            if not getattr(child, "hidden", False):
                visit(child, child_name, context)

    visit(root_command, "argusai")
    return rows


def _translated_help(value: object, translations: dict[str, str]) -> str:
    source = str(value or "").strip()
    if not source:
        return ""
    if source not in translations:
        raise ValueError(f"Missing Chinese CLI help; update scripts/cli_docs_zh.json: {source}")
    translation = translations[source]
    if not isinstance(translation, str) or not translation.strip():
        raise ValueError(f"Empty Chinese CLI help: {source}")
    return translation.strip()


def render_reference() -> tuple[str, int, int]:
    rows = command_tree()
    translations = json.loads(TRANSLATIONS.read_text(encoding="utf-8"))
    if not isinstance(translations, dict):
        raise ValueError("scripts/cli_docs_zh.json must contain a Chinese help mapping")
    leaves = sum(not group for _, _, group in rows)
    groups = sum(group for _, _, group in rows)
    result = [
        "# 命令行参数参考", "",
        "[文档首页](Home.md) · [命令手册与完整示例](CLI-Reference.md) · [快速开始](Quick-Start.md)", "",
        "本页从当前版本的实际命令声明生成，逐项列出参数、类型、必填状态、默认值和中文说明。"
        "从配置 AI 到渠道、任务与产物投递的可复制示例见[命令手册](CLI-Reference.md)。", "",
        f"覆盖 **{leaves} 个末端命令**、**{groups} 个命令分组或全局入口**。"
        "`proxy` 分组自身还可前台运行；交互控制台的斜杠指令见命令手册。", "",
        "参数的“必填”表示解析层要求；服务账号、模型、路径和平台等条件仍由命令执行时检查。"
        "默认值不求值，也不读取用户配置；运行时默认与全局设置可影响实际行为。"
        "命令、参数、状态值和产品名称保留实际写法，说明统一使用中文。"
        "`--help` 本身不在每张表重复。", "",
        "## 命令导航", "",
    ]
    for command, context, group in rows:
        path = context.command_path
        if group:
            anchor = path.replace(" ", "-")
            result.append(f"- [{path}](#{anchor})")
    result.append("")
    for command, context, group in rows:
        path = context.command_path
        result += [("## " if group else "### ") + path, ""]
        help_text = _translated_help(command.help, translations)
        if help_text:
            result += [help_text, ""]
        usage = command.get_usage(context).strip()
        usage = (usage.replace("Usage: ", "用法：")
                 .replace("[OPTIONS]", "[选项]").replace("COMMAND", "子命令")
                 .replace("[ARGS]", "[参数]"))
        result += ["```text", usage, "```", ""]
        parameters = [p for p in command.params if not getattr(p, "hidden", False)]
        if not parameters:
            result += ["无额外参数。使用 `--help` 查看命令帮助。", ""]
            continue
        result += ["| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |",
                   "| --- | --- | --- | --- | --- | --- |"]
        for parameter in parameters:
            options = list(getattr(parameter, "opts", [])) + list(getattr(parameter, "secondary_opts", []))
            label = " / ".join(f"`{_cell(value)}`" for value in options) or f"`{parameter.name}`"
            # Required/default metadata have their own columns. Do not append
            # Click's English annotations to the translated description.
            description = _translated_help(getattr(parameter, "help", ""), translations)
            arity = []
            if getattr(parameter, "multiple", False):
                arity.append("可重复")
            if parameter.nargs == -1:
                arity.append("可变数量")
            elif parameter.nargs > 1:
                arity.append(f"每次 {parameter.nargs} 项")
            result.append("| " + " | ".join((
                label, _cell(_type_name(parameter)), "是" if parameter.required else "否",
                _cell(_default(parameter)), " / ".join(arity) or "—", _cell(description) or "—",
            )) + " |")
        result.append("")
    result += ["## 维护参数参考", "",
               "新增、删除或修改命令声明后，在仓库根目录重新生成并检查：", "",
               "```sh", "uv run python scripts/cli_docs.py --write",
               "uv run python scripts/cli_docs.py --check", "```", "",
               "生成器只遍历命令声明，不执行登录、网络探测、扫描、服务安装或配置写入。"
               "新增或修改帮助文案时，需同步更新 `scripts/cli_docs_zh.json`；缺少译文时生成会报错。"
               "新增命令的操作实例仍需同步补入命令手册。", ""]
    return "\n".join(result), leaves, groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Regenerate wiki/CLI-Options.md")
    mode.add_argument("--check", action="store_true", help="Check the reference against actual declarations")
    args = parser.parse_args()
    try:
        text, leaves, groups = render_reference()
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.write:
        OUTPUT.write_text(text, encoding="utf-8", newline="\n")
        print(f"Generated {OUTPUT.relative_to(ROOT)}: {leaves} commands, {groups} groups")
    elif not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != text:
        print("CLI parameter reference is stale; run scripts/cli_docs.py --write", file=sys.stderr)
        return 1
    else:
        print(f"CLI parameter reference verified: {leaves} commands, {groups} groups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
