from __future__ import annotations

import asyncio
import locale
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import flet as ft

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from common import app_context
from common.app_context import config, log

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_SCRIPT = PROJECT_ROOT / "scripts" / "renpy_translate_pipeline.py"
DEFAULT_CONFIG_PATH = "app1/res/config.yml"
MAX_LOG_CHARS = 200_000


def _quote_command(cmd: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return " ".join(shlex.quote(part) for part in cmd)


def _to_int(name: str, value: str, minimum: int | None = None) -> int:
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数: {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{name} 必须 >= {minimum}, 当前为 {parsed}")
    return parsed


def _to_float(name: str, value: str, minimum: float | None = None) -> float:
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字: {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{name} 必须 >= {minimum}, 当前为 {parsed}")
    return parsed


def _cfg_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _cfg_lookup(section: dict[str, Any], paths: tuple[tuple[str, ...], ...], default: Any) -> Any:
    for path in paths:
        cur: Any = section
        found = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                found = False
                break
        if found and cur is not None:
            return cur
    return default


def _cfg_str_paths(section: dict[str, Any], default: str, *paths: tuple[str, ...]) -> str:
    value = _cfg_lookup(section, paths, default)
    if value is None:
        return default
    return str(value)


def _cfg_bool_paths(section: dict[str, Any], default: bool, *paths: tuple[str, ...]) -> bool:
    value = _cfg_lookup(section, paths, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _cfg_int_paths(section: dict[str, Any], default: int, *paths: tuple[str, ...]) -> int:
    value = _cfg_lookup(section, paths, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _init_context() -> None:
    if app_context.is_initialized():
        return
    app_context.init(DEFAULT_CONFIG_PATH, "renpy_translate_studio")
    log.info("App context initialized with config: %s", DEFAULT_CONFIG_PATH)


def _load_gui_config() -> dict[str, Any]:
    extra = getattr(config, "__pydantic_extra__", None) or {}
    section = _cfg_dict(extra.get("renpy_gui"))
    if not section:
        log.warning("No 'renpy_gui' section found in config; fallback defaults will be used.")
    return section


def _with_help(
    control: ft.Control,
    text: str,
    *,
    show_marker: bool = True,
) -> ft.Control:
    control.tooltip = text
    if show_marker and isinstance(control, ft.TextField):
        control.suffix_icon = ft.Icons.HELP_OUTLINE
    if show_marker and isinstance(control, ft.Checkbox):
        label = (control.label or "").strip()
        if label and not label.endswith("ⓘ"):
            control.label = f"{label} ⓘ"
    if show_marker and isinstance(control, ft.Dropdown):
        control.helper_text = "悬停查看说明"
    return control


def _section_card(title: str, controls: list[ft.Control]) -> ft.Control:
    return ft.Container(
        bgcolor="#FFFFFF",
        border_radius=10,
        padding=10,
        content=ft.Column(
            spacing=6,
            tight=True,
            controls=[ft.Text(title, size=14, weight=ft.FontWeight.W_600), *controls],
        ),
    )


def _detect_launcher_cmd(game_root: Path, launcher_override: str) -> list[str]:
    launcher_value = launcher_override.strip()
    if launcher_value:
        launcher_path = Path(launcher_value)
        if not launcher_path.is_absolute():
            launcher_path = (game_root / launcher_path).resolve()
        suffix = launcher_path.suffix.lower()
        if suffix == ".py":
            return [sys.executable, str(launcher_path)]
        if suffix == ".sh":
            return ["bash", str(launcher_path)]
        return [str(launcher_path)]

    if os.name == "nt":
        exes = sorted(game_root.glob("*.exe"))
        if exes:
            return [str(exes[0])]

    pys = sorted(game_root.glob("*.py"))
    if pys:
        return [sys.executable, str(pys[0])]

    shs = sorted(game_root.glob("*.sh"))
    if shs:
        return ["bash", str(shs[0])]

    raise ValueError("未检测到可启动文件，请填写“启动器路径（可选）”。")


def _has_hardcoded_english_language_button(game_root: Path) -> bool:
    screens_path = game_root / "game" / "screens.rpy"
    if not screens_path.exists():
        return False
    try:
        content = screens_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    has_english_only = "Language(None)" in content
    has_specific_language = re.search(r"Language\(\s*[\"']", content) is not None
    return has_english_only and not has_specific_language


def main(page: ft.Page) -> None:
    gui_cfg = _load_gui_config()
    quick_fix_cfg = _cfg_dict(gui_cfg.get("quick-fix"))

    page.title = _cfg_str_paths(gui_cfg, "Ren'Py 翻译工作台", ("ui", "page-title"), ("page-title",))
    page.window.min_width = _cfg_int_paths(gui_cfg, 1120, ("ui", "min-width"))
    page.window.min_height = _cfg_int_paths(gui_cfg, 760, ("ui", "min-height"))
    page.scroll = ft.ScrollMode.AUTO
    page.padding = _cfg_int_paths(gui_cfg, 12, ("ui", "padding"))
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
    page.bgcolor = "#F8F6F0"

    process: asyncio.subprocess.Process | None = None
    is_running = False
    stop_requested = False
    log_follow_tail = True
    log_chars = 0
    log_line_lengths: list[int] = []
    log_encoding = locale.getpreferredencoding(False) or "utf-8"

    game_dir = ft.TextField(
        label="游戏目录",
        value=_cfg_str_paths(gui_cfg, ".", ("pipeline", "game-dir"), ("game-dir",)),
    )
    launcher = ft.TextField(
        label="启动器路径（可选）",
        value=_cfg_str_paths(gui_cfg, "", ("pipeline", "launcher"), ("launcher",)),
    )
    tools_dir = ft.TextField(
        label="工具目录",
        value=_cfg_str_paths(gui_cfg, "_tools", ("pipeline", "tools-dir"), ("tools-dir",)),
        width=210,
    )
    cache_file = ft.TextField(
        label="缓存文件",
        value=_cfg_str_paths(gui_cfg, ".translation_cache.json", ("pipeline", "cache-file"), ("cache-file",)),
        width=250,
    )

    language = ft.TextField(
        label="语言目录名",
        value=_cfg_str_paths(gui_cfg, "chinese", ("pipeline", "language"), ("language",)),
        width=170,
    )
    target_language = ft.TextField(
        label="目标语言",
        value=_cfg_str_paths(gui_cfg, "Simplified Chinese", ("pipeline", "target-language"), ("target-language",)),
        width=250,
    )
    auto_language_file = ft.TextField(
        label="自动启用语言脚本",
        value=_cfg_str_paths(
            gui_cfg,
            "_renpy_translate_autolang.rpy",
            ("pipeline", "auto-language-file"),
            ("auto-language-file",),
        ),
        width=300,
    )

    provider = ft.Dropdown(
        label="翻译后端",
        value=_cfg_str_paths(gui_cfg, "openai", ("pipeline", "provider"), ("provider",)),
        options=[
            ft.DropdownOption(key="none", text="不使用（仅跑流程）"),
            ft.DropdownOption(key="openai", text="OpenAI 兼容接口"),
            ft.DropdownOption(key="deepl", text="DeepL"),
        ],
        width=220,
    )

    rpa_pattern = ft.TextField(
        label="RPA 匹配",
        value=_cfg_str_paths(gui_cfg, "*.rpa", ("pipeline", "rpa-pattern"), ("rpa-pattern",)),
        width=170,
    )
    batch_size = ft.TextField(
        label="批大小",
        value=_cfg_str_paths(gui_cfg, "20", ("pipeline", "batch-size"), ("batch-size",)),
        width=90,
    )
    sleep_seconds = ft.TextField(
        label="批间休眠",
        value=_cfg_str_paths(gui_cfg, "0", ("pipeline", "sleep-seconds"), ("sleep-seconds",)),
        width=100,
    )
    max_lines = ft.TextField(
        label="每文件最大行",
        value=_cfg_str_paths(gui_cfg, "0", ("pipeline", "max-lines"), ("max-lines",)),
        width=120,
    )
    retries = ft.TextField(
        label="重试次数",
        value=_cfg_str_paths(gui_cfg, "3", ("pipeline", "retries"), ("retries",)),
        width=100,
    )
    timeout = ft.TextField(
        label="超时(秒)",
        value=_cfg_str_paths(gui_cfg, "180", ("pipeline", "timeout"), ("timeout",)),
        width=100,
    )

    skip_extract = ft.Checkbox(
        label="跳过解包",
        value=_cfg_bool_paths(
            gui_cfg,
            False,
            ("pipeline", "flags", "skip-extract"),
            ("pipeline", "skip-extract"),
            ("skip-extract",),
        ),
    )
    skip_decompile = ft.Checkbox(
        label="跳过反编译",
        value=_cfg_bool_paths(
            gui_cfg,
            False,
            ("pipeline", "flags", "skip-decompile"),
            ("pipeline", "skip-decompile"),
            ("skip-decompile",),
        ),
    )
    skip_template = ft.Checkbox(
        label="跳过模板生成",
        value=_cfg_bool_paths(
            gui_cfg,
            False,
            ("pipeline", "flags", "skip-template"),
            ("pipeline", "skip-template"),
            ("skip-template",),
        ),
    )
    skip_mt = ft.Checkbox(
        label="跳过机器翻译",
        value=_cfg_bool_paths(
            gui_cfg,
            False,
            ("pipeline", "flags", "skip-mt"),
            ("pipeline", "skip-mt"),
            ("skip-mt",),
        ),
    )
    dry_run = ft.Checkbox(
        label="仅演练（不落盘）",
        value=_cfg_bool_paths(
            gui_cfg,
            False,
            ("pipeline", "flags", "dry-run"),
            ("pipeline", "dry-run"),
            ("dry-run",),
        ),
    )
    overwrite = ft.Checkbox(
        label="覆盖已翻译",
        value=_cfg_bool_paths(
            gui_cfg,
            False,
            ("pipeline", "flags", "overwrite"),
            ("pipeline", "overwrite"),
            ("overwrite",),
        ),
    )
    resume_untranslated = ft.Checkbox(
        label="仅续翻未翻译",
        value=_cfg_bool_paths(
            gui_cfg,
            True,
            ("pipeline", "flags", "resume-untranslated"),
            ("pipeline", "resume-untranslated"),
            ("resume-untranslated",),
        ),
    )
    stop_on_quota = ft.Checkbox(
        label="配额不足即停止",
        value=_cfg_bool_paths(
            gui_cfg,
            True,
            ("pipeline", "flags", "stop-on-quota"),
            ("pipeline", "stop-on-quota"),
            ("stop-on-quota",),
        ),
    )
    auto_enable_language = ft.Checkbox(
        label="自动启用语言",
        value=_cfg_bool_paths(
            gui_cfg,
            True,
            ("pipeline", "flags", "auto-enable-language"),
            ("pipeline", "auto-enable-language"),
            ("auto-enable-language",),
        ),
    )
    extract_all_rpa = ft.Checkbox(
        label="解包全部 RPA",
        value=_cfg_bool_paths(
            gui_cfg,
            False,
            ("pipeline", "flags", "extract-all-rpa"),
            ("pipeline", "extract-all-rpa"),
            ("extract-all-rpa",),
        ),
    )
    unrpyc_no_init_offset = ft.Checkbox(
        label="unrpyc 无 init-offset",
        value=_cfg_bool_paths(
            gui_cfg,
            False,
            ("pipeline", "flags", "unrpyc-no-init-offset"),
            ("pipeline", "unrpyc-no-init-offset"),
            ("unrpyc-no-init-offset",),
        ),
    )
    unrpyc_clobber = ft.Checkbox(
        label="unrpyc 覆盖已有",
        value=_cfg_bool_paths(
            gui_cfg,
            False,
            ("pipeline", "flags", "unrpyc-clobber"),
            ("pipeline", "unrpyc-clobber"),
            ("unrpyc-clobber",),
        ),
    )

    model = ft.TextField(
        label="模型",
        value=_cfg_str_paths(gui_cfg, "dolphin3:latest", ("openai", "model"), ("model",)),
        width=210,
    )
    base_url = ft.TextField(
        label="接口地址",
        value=_cfg_str_paths(
            gui_cfg,
            os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:11434"),
            ("openai", "base-url"),
            ("base-url",),
        ),
        width=260,
    )
    api_key = ft.TextField(
        label="API Key",
        value=_cfg_str_paths(gui_cfg, os.getenv("OPENAI_API_KEY", ""), ("openai", "api-key"), ("openai-api-key",)),
        password=True,
        can_reveal_password=True,
        width=260,
    )
    temperature = ft.TextField(
        label="温度",
        value=_cfg_str_paths(gui_cfg, "0.1", ("openai", "temperature"), ("temperature",)),
        width=110,
    )
    openai_format = ft.TextField(
        label="格式",
        value=_cfg_str_paths(gui_cfg, os.getenv("OPENAI_FORMAT", "json"), ("openai", "format"), ("openai-format",)),
        width=110,
    )

    deepl_url = ft.TextField(
        label="DeepL 地址",
        value=_cfg_str_paths(
            gui_cfg,
            os.getenv("DEEPL_API_URL", "https://api-free.deepl.com"),
            ("deepl", "url"),
            ("deepl-url",),
        ),
        width=260,
    )
    deepl_auth_key = ft.TextField(
        label="DeepL Key",
        value=_cfg_str_paths(gui_cfg, os.getenv("DEEPL_AUTH_KEY", ""), ("deepl", "auth-key"), ("deepl-auth-key",)),
        password=True,
        can_reveal_password=True,
        width=260,
    )
    deepl_target_lang = ft.TextField(
        label="目标语言码",
        value=_cfg_str_paths(
            gui_cfg,
            os.getenv("DEEPL_TARGET_LANG", "ZH-HANS"),
            ("deepl", "target-lang"),
            ("deepl-target-lang",),
        ),
        width=140,
    )
    deepl_source_lang = ft.TextField(
        label="源语言码",
        value=_cfg_str_paths(
            gui_cfg,
            os.getenv("DEEPL_SOURCE_LANG", ""),
            ("deepl", "source-lang"),
            ("deepl-source-lang",),
        ),
        width=140,
    )

    extra_args = ft.TextField(
        label="额外命令参数",
        value=_cfg_str_paths(gui_cfg, "", ("pipeline", "extra-args"), ("extra-args",)),
    )

    command_preview = ft.TextField(
        label="将执行命令",
        read_only=True,
        multiline=True,
        min_lines=2,
        max_lines=2,
        expand=True,
    )

    log_lines = _cfg_int_paths(gui_cfg, 10, ("ui", "log-lines"), ("log-lines",))
    log_list = ft.ListView(
        spacing=2,
        expand=True,
        auto_scroll=False,
        scroll=ft.ScrollMode.AUTO,
    )
    logs = ft.Container(
        height=max(180, 20 * max(8, log_lines + 2)),
        border=ft.border.all(1, ft.Colors.BLUE_GREY_300),
        border_radius=8,
        padding=8,
        content=log_list,
        expand=True,
    )
    status = ft.Text("状态：空闲", color=ft.Colors.BLUE_GREY_700)

    run_button = ft.Button(content="开始运行", icon=ft.Icons.PLAY_ARROW)
    stop_button = ft.Button(content="停止", icon=ft.Icons.STOP, disabled=True)
    clear_logs_button = ft.Button(content="清空日志", icon=ft.Icons.CLEAR_ALL)
    quick_fix_run_button = ft.Button(content="快速修复并启动", icon=ft.Icons.BUILD_CIRCLE)

    openai_settings = ft.Column(
        spacing=6,
        tight=True,
        controls=[
            ft.Row(
                spacing=6,
                wrap=True,
                controls=[
                    _with_help(model, "OpenAI 兼容后端的模型名，例如 gpt-4.1-mini 或本地模型名。"),
                    _with_help(temperature, "采样温度。越低越稳定，通常建议 0~0.3。"),
                    _with_help(openai_format, "可选格式字段。使用 Ollama 时建议 json。"),
                ],
            ),
            ft.Row(
                spacing=6,
                wrap=True,
                controls=[
                    _with_help(base_url, "OpenAI 兼容接口地址。本地 Ollama 常用 http://127.0.0.1:11434。"),
                    _with_help(api_key, "OpenAI 兼容 API Key。本地服务一般可留空。"),
                ],
            ),
        ],
    )

    deepl_settings = ft.Column(
        spacing=6,
        tight=True,
        visible=False,
        controls=[
            ft.Row(
                spacing=6,
                wrap=True,
                controls=[
                    _with_help(deepl_target_lang, "DeepL 的 target_lang，例如 ZH-HANS。"),
                    _with_help(deepl_source_lang, "DeepL 的 source_lang，可选。为空时自动检测。"),
                ],
            ),
            ft.Row(
                spacing=6,
                wrap=True,
                controls=[
                    _with_help(deepl_url, "DeepL API 地址，免费版通常为 https://api-free.deepl.com。"),
                    _with_help(deepl_auth_key, "DeepL 鉴权密钥。使用 DeepL 时必填。"),
                ],
            ),
        ],
    )

    def on_log_scroll(event: ft.OnScrollEvent) -> None:
        nonlocal log_follow_tail
        remain = event.max_scroll_extent - event.pixels
        threshold = max(24.0, event.viewport_dimension * 0.08)
        log_follow_tail = remain <= threshold

    log_list.on_scroll = on_log_scroll

    def append_log(message: str, level: str | None = None) -> None:
        nonlocal log_chars, log_follow_tail
        line = message.rstrip() or " "
        color = ft.Colors.BLUE_GREY_900
        if level == "error":
            color = ft.Colors.RED_700
        elif level == "warning":
            color = ft.Colors.ORANGE_700
        log_list.controls.append(ft.Text(line, size=12, color=color, selectable=True))
        line_len = len(line) + 1
        log_chars += line_len
        log_line_lengths.append(line_len)
        while log_chars > MAX_LOG_CHARS and log_list.controls:
            log_list.controls.pop(0)
            removed = log_line_lengths.pop(0)
            log_chars -= removed

        if level == "error":
            log.error(line)
        elif level == "warning":
            log.warning(line)
        elif level == "info":
            log.info(line)

        if log_follow_tail:
            page.run_task(scroll_logs_to_tail)
        page.update()

    async def scroll_logs_to_tail() -> None:
        try:
            result = log_list.scroll_to(offset=-1, duration=0)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            log.debug("Ignored log auto-scroll error: %s", exc)

    def set_running(value: bool) -> None:
        nonlocal is_running
        is_running = value
        run_button.disabled = value
        quick_fix_run_button.disabled = value
        stop_button.disabled = not value
        status.value = "状态：运行中" if value else "状态：空闲"
        status.color = ft.Colors.GREEN_700 if value else ft.Colors.BLUE_GREY_700
        page.update()

    def refresh_provider_panel(_: ft.ControlEvent | None = None) -> None:
        selected = provider.value or "none"
        openai_settings.visible = selected == "openai"
        deepl_settings.visible = selected == "deepl"
        page.update()

    def build_command() -> list[str]:
        if not PIPELINE_SCRIPT.exists():
            raise ValueError(f"未找到流水线脚本: {PIPELINE_SCRIPT}")

        selected_provider = provider.value or "none"
        command = [sys.executable, str(PIPELINE_SCRIPT)]

        command.extend(["--game-dir", game_dir.value.strip() or "."])
        command.extend(["--tools-dir", tools_dir.value.strip() or "_tools"])
        command.extend(["--language", language.value.strip() or "chinese"])
        command.extend(["--target-language", target_language.value.strip() or "Simplified Chinese"])
        command.extend(["--rpa-pattern", rpa_pattern.value.strip() or "*.rpa"])
        command.extend(["--provider", selected_provider])
        command.extend(["--batch-size", str(_to_int("batch-size", batch_size.value, minimum=1))])
        command.extend(["--sleep-seconds", str(_to_float("sleep-seconds", sleep_seconds.value, minimum=0.0))])
        command.extend(["--max-lines", str(_to_int("max-lines", max_lines.value, minimum=0))])
        command.extend(["--cache-file", cache_file.value.strip() or ".translation_cache.json"])
        command.extend(["--retries", str(_to_int("retries", retries.value, minimum=1))])
        command.extend(["--timeout", str(_to_int("timeout", timeout.value, minimum=1))])
        command.extend(["--auto-language-file", auto_language_file.value.strip() or "_renpy_translate_autolang.rpy"])

        launcher_value = launcher.value.strip()
        if launcher_value:
            command.extend(["--launcher", launcher_value])

        if skip_extract.value:
            command.append("--skip-extract")
        if skip_decompile.value:
            command.append("--skip-decompile")
        if skip_template.value:
            command.append("--skip-template")
        if skip_mt.value:
            command.append("--skip-mt")
        if dry_run.value:
            command.append("--dry-run")
        if overwrite.value:
            command.append("--overwrite")
        if extract_all_rpa.value:
            command.append("--extract-all-rpa")
        if unrpyc_no_init_offset.value:
            command.append("--unrpyc-no-init-offset")
        if unrpyc_clobber.value:
            command.append("--unrpyc-clobber")
        if not resume_untranslated.value:
            command.append("--no-resume-untranslated")
        if not stop_on_quota.value:
            command.append("--no-stop-on-quota")
        if not auto_enable_language.value:
            command.append("--no-auto-enable-language")

        if selected_provider == "openai":
            command.extend(["--model", model.value.strip() or "dolphin3:latest"])
            command.extend(["--base-url", base_url.value.strip() or "http://127.0.0.1:11434"])
            command.extend(["--temperature", str(_to_float("temperature", temperature.value, minimum=0.0))])
            if api_key.value.strip():
                command.extend(["--api-key", api_key.value.strip()])
            if openai_format.value.strip():
                command.extend(["--openai-format", openai_format.value.strip()])

        if selected_provider == "deepl":
            if not deepl_auth_key.value.strip():
                raise ValueError("使用 DeepL 时必须填写 DeepL Key。")
            command.extend(["--deepl-url", deepl_url.value.strip() or "https://api-free.deepl.com"])
            command.extend(["--deepl-auth-key", deepl_auth_key.value.strip()])
            command.extend(["--deepl-target-lang", deepl_target_lang.value.strip() or "ZH-HANS"])
            if deepl_source_lang.value.strip():
                command.extend(["--deepl-source-lang", deepl_source_lang.value.strip()])

        if extra_args.value.strip():
            try:
                parsed = shlex.split(extra_args.value.strip(), posix=os.name != "nt")
            except ValueError as exc:
                raise ValueError(f"额外命令参数格式错误: {exc}") from exc
            command.extend(parsed)

        return command

    def build_quick_fix_command() -> list[str]:
        if not PIPELINE_SCRIPT.exists():
            raise ValueError(f"未找到流水线脚本: {PIPELINE_SCRIPT}")

        quick_provider = _cfg_str_paths(quick_fix_cfg, "none", ("provider",))
        command = [sys.executable, str(PIPELINE_SCRIPT)]

        command.extend(["--game-dir", game_dir.value.strip() or "."])
        command.extend(["--tools-dir", tools_dir.value.strip() or "_tools"])
        command.extend(["--language", language.value.strip() or "chinese"])
        command.extend(["--target-language", target_language.value.strip() or "Simplified Chinese"])
        command.extend(["--rpa-pattern", rpa_pattern.value.strip() or "*.rpa"])
        command.extend(["--provider", quick_provider])
        command.extend(["--batch-size", str(_to_int("batch-size", batch_size.value, minimum=1))])
        command.extend(["--sleep-seconds", str(_to_float("sleep-seconds", sleep_seconds.value, minimum=0.0))])
        command.extend(["--max-lines", str(_to_int("max-lines", max_lines.value, minimum=0))])
        command.extend(["--cache-file", cache_file.value.strip() or ".translation_cache.json"])
        command.extend(["--retries", str(_to_int("retries", retries.value, minimum=1))])
        command.extend(["--timeout", str(_to_int("timeout", timeout.value, minimum=1))])
        command.extend(["--auto-language-file", auto_language_file.value.strip() or "_renpy_translate_autolang.rpy"])

        launcher_value = launcher.value.strip()
        if launcher_value:
            command.extend(["--launcher", launcher_value])

        quick_skip_extract = _cfg_bool_paths(quick_fix_cfg, True, ("skip-extract",))
        quick_skip_decompile = _cfg_bool_paths(quick_fix_cfg, True, ("skip-decompile",))
        quick_skip_template = _cfg_bool_paths(quick_fix_cfg, True, ("skip-template",))
        quick_skip_mt = _cfg_bool_paths(quick_fix_cfg, True, ("skip-mt",))
        quick_auto_language = _cfg_bool_paths(quick_fix_cfg, True, ("auto-enable-language",))
        quick_extract_all = _cfg_bool_paths(quick_fix_cfg, False, ("extract-all-rpa",))
        quick_no_init = _cfg_bool_paths(quick_fix_cfg, False, ("unrpyc-no-init-offset",))
        quick_clobber = _cfg_bool_paths(quick_fix_cfg, False, ("unrpyc-clobber",))
        quick_resume_untranslated = _cfg_bool_paths(quick_fix_cfg, True, ("resume-untranslated",))
        quick_stop_on_quota = _cfg_bool_paths(quick_fix_cfg, True, ("stop-on-quota",))
        quick_overwrite = _cfg_bool_paths(quick_fix_cfg, False, ("overwrite",))
        quick_dry_run = _cfg_bool_paths(quick_fix_cfg, False, ("dry-run",))

        if quick_skip_extract:
            command.append("--skip-extract")
        if quick_skip_decompile:
            command.append("--skip-decompile")
        if quick_skip_template:
            command.append("--skip-template")
        if quick_skip_mt:
            command.append("--skip-mt")
        if quick_dry_run:
            command.append("--dry-run")
        if quick_overwrite:
            command.append("--overwrite")
        if quick_extract_all:
            command.append("--extract-all-rpa")
        if quick_no_init:
            command.append("--unrpyc-no-init-offset")
        if quick_clobber:
            command.append("--unrpyc-clobber")
        if not quick_resume_untranslated:
            command.append("--no-resume-untranslated")
        if not quick_stop_on_quota:
            command.append("--no-stop-on-quota")
        if not quick_auto_language:
            command.append("--no-auto-enable-language")

        return command

    def launch_game() -> None:
        target_game_dir = Path(game_dir.value.strip() or ".").resolve()
        if not (target_game_dir / "game").exists():
            raise ValueError(f"游戏目录无效: {target_game_dir}")
        launch_cmd = _detect_launcher_cmd(target_game_dir, launcher.value)
        subprocess.Popen(launch_cmd, cwd=str(target_game_dir))
        append_log(f"[信息] 已启动游戏: {_quote_command(launch_cmd)}", level="info")

    async def run_pipeline(command: list[str], launch_after_success: bool = False) -> None:
        nonlocal process, stop_requested
        no_tl_generated = False
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(PROJECT_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert process.stdout is not None
            append_log(f"[信息] 已启动进程: pid={process.pid}", level="info")

            while True:
                chunk = await process.stdout.readline()
                if not chunk:
                    break
                line = chunk.decode(log_encoding, errors="replace").rstrip("\r\n")
                append_log(line)
                log.info("[PIPELINE] %s", line)
                if "No tl files found at game/tl/" in line:
                    no_tl_generated = True

            code = await process.wait()
            if code == 0:
                append_log(f"[信息] 运行结束，退出码: {code}", level="info")
                if launch_after_success:
                    try:
                        launch_game()
                    except Exception as exc:  # noqa: BLE001
                        append_log(f"[警告] 自动启动游戏失败: {exc}", level="warning")
            elif stop_requested:
                append_log(f"[警告] 任务已停止，退出码: {code}", level="warning")
            else:
                append_log(f"[错误] 运行异常结束，退出码: {code}", level="error")
            if no_tl_generated:
                append_log(
                    "[警告] 未检测到翻译模板文件。建议将“启动器路径”设为 .py 启动器（例如 DMD.CH1.py）后重试。",
                    level="warning",
                )
            game_root = Path(game_dir.value.strip() or ".").resolve()
            if _has_hardcoded_english_language_button(game_root):
                append_log(
                    "[警告] 检测到此游戏设置页语言按钮写死为 English（Language(None)）。"
                    "这是游戏脚本限制，不影响自动启用中文。",
                    level="warning",
                )
        except Exception:
            log.exception("Pipeline execution failed.")
            append_log("[错误] 运行失败，请查看日志文件。", level="error")
        finally:
            process = None
            set_running(False)

    def on_run_click(_: ft.Event[ft.Button]) -> None:
        nonlocal stop_requested
        if is_running:
            return
        try:
            cmd = build_command()
        except Exception as exc:  # noqa: BLE001
            append_log(f"[错误] {exc}", level="error")
            return

        command_preview.value = _quote_command(cmd)
        page.update()
        log.info("Pipeline run requested. command=%s", command_preview.value)
        append_log(f"[信息] 使用脚本: {PIPELINE_SCRIPT}", level="info")
        stop_requested = False
        set_running(True)
        page.run_task(run_pipeline, cmd)

    def on_quick_fix_run_click(_: ft.Event[ft.Button]) -> None:
        nonlocal stop_requested
        if is_running:
            return
        try:
            cmd = build_quick_fix_command()
        except Exception as exc:  # noqa: BLE001
            append_log(f"[错误] {exc}", level="error")
            return

        command_preview.value = _quote_command(cmd)
        page.update()
        log.info("Quick-fix run requested. command=%s", command_preview.value)
        append_log("[信息] 已启用“快速修复并启动”模式。", level="info")
        append_log(f"[信息] 使用脚本: {PIPELINE_SCRIPT}", level="info")
        stop_requested = False
        set_running(True)
        page.run_task(run_pipeline, cmd, True)

    async def on_stop_click(_: ft.Event[ft.Button]) -> None:
        nonlocal process, stop_requested
        if not process or process.returncode is not None:
            return

        append_log("[信息] 已请求停止进程。", level="warning")
        stop_requested = True
        process.terminate()
        await asyncio.sleep(1.2)
        if process.returncode is None:
            append_log("[警告] 进程未及时退出，已强制结束。", level="warning")
            process.kill()

    def on_clear_logs_click(_: ft.Event[ft.Button]) -> None:
        nonlocal log_chars, log_follow_tail
        log_list.controls.clear()
        log_line_lengths.clear()
        log_chars = 0
        log_follow_tail = True
        log.info("UI logs cleared by user.")
        page.update()

    provider.on_select = refresh_provider_panel
    run_button.on_click = on_run_click
    quick_fix_run_button.on_click = on_quick_fix_run_click
    stop_button.on_click = on_stop_click
    clear_logs_button.on_click = on_clear_logs_click

    path_card = _section_card(
        "路径设置",
        [
            _with_help(game_dir, "Ren'Py 游戏根目录。该目录下必须包含 game/ 子目录。"),
            _with_help(launcher, "可选，手动指定启动器文件（.exe / .py / .sh）。"),
            ft.Row(
                controls=[
                    _with_help(tools_dir, "工具目录。用于下载和缓存 unrpa/unrpyc。"),
                    _with_help(cache_file, "翻译缓存文件路径。用于断点续跑和复用历史结果。"),
                ],
                wrap=True,
                spacing=6,
            ),
        ],
    )

    provider_card = _section_card(
        "翻译后端",
        [
            _with_help(provider, "选择翻译服务：不翻译 / OpenAI 兼容 / DeepL。"),
            openai_settings,
            deepl_settings,
            _with_help(extra_args, "附加到命令末尾的自定义参数。请按命令行格式填写。"),
        ],
    )

    def flag_item(control: ft.Control, tip: str) -> ft.Control:
        return ft.Container(content=_with_help(control, tip), width=250)

    pipeline_card = _section_card(
        "流水线参数",
        [
            ft.Row(
                controls=[
                    _with_help(language, "Ren'Py 的语言目录名，对应 game/tl/<language>。"),
                    _with_help(target_language, "提供给翻译模型的目标语言描述。"),
                ],
                wrap=True,
                spacing=6,
            ),
            ft.Row(
                controls=[
                    _with_help(rpa_pattern, "RPA 文件匹配模式，如 *.rpa。"),
                    _with_help(batch_size, "单次请求包含的文本条数。越大越快，但更容易失败。"),
                    _with_help(sleep_seconds, "批次之间休眠秒数。可用于限速和稳定性。"),
                    _with_help(max_lines, "每个文件最多翻译多少行，0 表示不限制。"),
                    _with_help(retries, "接口失败后的重试次数。"),
                    _with_help(timeout, "请求基础超时秒数。"),
                ],
                wrap=True,
                spacing=6,
            ),
            _with_help(auto_language_file, "在 game/ 下生成的自动切换语言脚本文件名。"),
            ft.Row(
                wrap=True,
                spacing=6,
                run_spacing=4,
                controls=[
                    flag_item(skip_extract, "跳过 .rpa 解包阶段。"),
                    flag_item(skip_decompile, "跳过 .rpyc 反编译阶段。"),
                    flag_item(skip_template, "跳过 Ren'Py 翻译模板生成阶段。"),
                    flag_item(skip_mt, "跳过机器翻译阶段。"),
                    flag_item(dry_run, "只检查流程和参数，不写入任何文件。"),
                    flag_item(overwrite, "覆盖已经翻译过的行。"),
                    flag_item(resume_untranslated, "只处理未翻译行，适合中断后续跑。"),
                    flag_item(stop_on_quota, "遇到配额不足时立即停止，避免无效重试。"),
                    flag_item(auto_enable_language, "自动生成语言启用钩子，启动游戏后默认切到目标语言。"),
                    flag_item(extract_all_rpa, "解包所有匹配的 RPA，而不是仅包含 .rpyc 的归档。"),
                    flag_item(unrpyc_no_init_offset, "反编译时传递 --no-init-offset，用于兼容部分旧游戏。"),
                    flag_item(unrpyc_clobber, "反编译时覆盖已存在的 .rpy 文件。"),
                ],
            ),
        ],
    )

    run_card = _section_card(
        "执行与日志",
        [
            ft.Row(
                controls=[run_button, quick_fix_run_button, stop_button, clear_logs_button, status],
                wrap=True,
                spacing=8,
            ),
            ft.Row(
                controls=[_with_help(command_preview, "根据当前表单生成的最终命令，可复制用于命令行排障。", show_marker=False)],
                spacing=0,
            ),
            ft.Row(
                controls=[_with_help(logs, "脚本实时输出日志。完整日志会额外写入日志文件。", show_marker=False)],
                spacing=0,
            ),
        ],
    )

    top_layout = ft.Row(
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.START,
        controls=[
            ft.Container(
                expand=5,
                content=ft.Column(controls=[path_card, provider_card], spacing=8, tight=True),
            ),
            ft.Container(
                expand=7,
                content=pipeline_card,
            ),
        ],
    )

    page.add(
        ft.Column(
            spacing=10,
            controls=[
                ft.Text(page.title, size=24, weight=ft.FontWeight.W_700),
                ft.Text(f"流水线脚本: {PIPELINE_SCRIPT}", color=ft.Colors.BLUE_GREY_700, selectable=True),
                top_layout,
                ft.Row(controls=[ft.Container(expand=1, content=run_card)], spacing=0),
            ],
            expand=True,
        )
    )

    log.info("Flet desktop UI initialized.")
    refresh_provider_panel()


def run() -> None:
    _init_context()
    ft.run(main)


if __name__ == "__main__":
    run()
