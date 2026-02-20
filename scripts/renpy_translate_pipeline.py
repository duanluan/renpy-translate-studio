#!/usr/bin/env python3
"""
Ren'Py translation pipeline:
1) Extract .rpyc from .rpa archives (via unrpa)
2) Decompile .rpyc to .rpy (via unrpyc)
3) Generate/refresh Ren'Py tl files
4) Optionally machine-translate tl files (OpenAI-compatible API or DeepL)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple


PLACEHOLDER_RE = re.compile(
    r"(\[[^\]\n]+\]|\{[^{}\n]+\}|%\([^)]+\)[a-zA-Z]|%[sdif]|\\[nrt])"
)
TEMP_PLACEHOLDER_RE = re.compile(r"__RNPH_\d+__")
WARN_ONCE_KEYS: Set[str] = set()

COMMENT_SAY_RE = re.compile(
    r'^(?P<prefix>\s*)#\s*(?:[A-Za-z_]\w*\s+)?"(?P<text>(?:[^"\\]|\\.)*)"(?P<suffix>\s*)$'
)
OLD_RE = re.compile(
    r'^(?P<prefix>\s*old\s+)"(?P<text>(?:[^"\\]|\\.)*)"(?P<suffix>\s*)$'
)
NEW_RE = re.compile(
    r'^(?P<prefix>\s*new\s+)"(?P<text>(?:[^"\\]|\\.)*)"(?P<suffix>\s*)$'
)
SAY_RE = re.compile(
    r'^(?P<prefix>\s*(?:[A-Za-z_]\w*\s+)?)"(?P<text>(?:[^"\\]|\\.)*)"(?P<suffix>\s*)$'
)


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def warn_once(key: str, msg: str) -> None:
    if key in WARN_ONCE_KEYS:
        return
    WARN_ONCE_KEYS.add(key)
    warn(f"{msg} (shown once)")


def has_temp_placeholders(text: str) -> bool:
    return bool(TEMP_PLACEHOLDER_RE.search(text))


def run_cmd(
    cmd: Sequence[str],
    cwd: Optional[Path] = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    rendered = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    info(f"Run: {rendered}")
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        check=check,
        capture_output=capture,
        text=True,
    )


def detect_launcher(game_dir: Path, launcher_override: Optional[str]) -> List[str]:
    if launcher_override:
        launcher_path = Path(launcher_override)
        if not launcher_path.is_absolute():
            launcher_path = (game_dir / launcher_path).resolve()
        if launcher_path.suffix.lower() == ".py":
            return [sys.executable, str(launcher_path)]
        if launcher_path.suffix.lower() == ".sh":
            return ["bash", str(launcher_path)]
        return [str(launcher_path)]

    exes = sorted(game_dir.glob("*.exe"))
    if exes and os.name == "nt":
        return [str(exes[0])]

    pys = sorted(game_dir.glob("*.py"))
    if pys:
        return [sys.executable, str(pys[0])]

    shs = sorted(game_dir.glob("*.sh"))
    if shs:
        return ["bash", str(shs[0])]

    raise RuntimeError("Could not detect Ren'Py launcher. Use --launcher.")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def ensure_git_repo(repo_dir: Path, url: str) -> None:
    if repo_dir.exists() and (repo_dir / ".git").exists():
        return
    ensure_dir(repo_dir.parent)
    run_cmd(["git", "clone", "--depth", "1", url, str(repo_dir)])


def ensure_unrpa(tools_dir: Path) -> List[str]:
    exe = shutil.which("unrpa")
    if exe:
        return [exe]

    repo_dir = tools_dir / "unrpa"
    ensure_git_repo(repo_dir, "https://github.com/Lattyware/unrpa.git")
    run_cmd([sys.executable, "-m", "pip", "install", str(repo_dir)])

    exe = shutil.which("unrpa")
    if exe:
        return [exe]

    # Fallback in case entrypoint scripts are not on PATH.
    return [sys.executable, "-m", "unrpa"]


def ensure_unrpyc_script(tools_dir: Path) -> Path:
    repo_dir = tools_dir / "unrpyc"
    script_path = repo_dir / "unrpyc.py"
    if script_path.exists():
        return script_path
    ensure_git_repo(repo_dir, "https://github.com/CensoredUsername/unrpyc.git")
    if not script_path.exists():
        raise RuntimeError(f"unrpyc script not found: {script_path}")
    return script_path


def list_rpa_files(game_dir: Path, pattern: str) -> List[Path]:
    rpa_dir = game_dir / "game"
    return sorted(rpa_dir.glob(pattern))


def archive_has_rpyc(unrpa_cmd: Sequence[str], archive: Path) -> bool:
    try:
        cp = run_cmd([*unrpa_cmd, "-l", str(archive)], capture=True, check=True)
    except subprocess.CalledProcessError:
        warn(f"Failed to list archive, skipping: {archive}")
        return False
    for line in cp.stdout.splitlines():
        if line.strip().lower().endswith(".rpyc"):
            return True
    return False


def extract_archives(
    game_dir: Path,
    unrpa_cmd: Sequence[str],
    pattern: str,
    extract_all: bool,
) -> None:
    archives = list_rpa_files(game_dir, pattern)
    if not archives:
        warn("No .rpa archives found for extraction step.")
        return

    selected: List[Path] = []
    if extract_all:
        selected = archives
    else:
        for archive in archives:
            if archive_has_rpyc(unrpa_cmd, archive):
                selected.append(archive)

    if not selected:
        warn("No archives with .rpyc found. Extraction step skipped.")
        return

    info(f"Extracting {len(selected)} archive(s) into game directory.")
    out_dir = game_dir / "game"
    for archive in selected:
        run_cmd([*unrpa_cmd, "-s", "-m", "-p", str(out_dir), str(archive)])


def decompile_rpyc(
    game_dir: Path,
    unrpyc_script: Path,
    no_init_offset: bool,
    clobber: bool,
) -> None:
    cmd = [sys.executable, str(unrpyc_script)]
    if no_init_offset:
        cmd.append("--no-init-offset")
    if clobber:
        cmd.append("--clobber")
    cmd.append(str(game_dir / "game"))
    run_cmd(cmd)


def run_renpy_translate_template(
    game_dir: Path,
    launcher_cmd: Sequence[str],
    language: str,
    count_only: bool = False,
) -> None:
    cmd = list(launcher_cmd) + [".", "translate", language]
    if count_only:
        cmd.append("--count")
    run_cmd(cmd, cwd=game_dir)


def ensure_auto_language_bootstrap(game_dir: Path, language: str, filename: str) -> None:
    if not language.strip():
        return
    path = game_dir / "game" / filename
    target_literal = json.dumps(language, ensure_ascii=False)
    content = (
        "# Auto-generated by renpy_translate_pipeline.py\n"
        "init -100 python:\n"
        f"    _rntr_target_language = {target_literal}\n\n"
        "    if getattr(_preferences, \"language\", None) != _rntr_target_language:\n"
        "        _preferences.language = _rntr_target_language\n\n"
        "    def _rntr_apply_language_once():\n"
        "        target = _rntr_target_language\n"
        "        if getattr(_preferences, \"language\", None) != target:\n"
        "            _preferences.language = target\n"
        "        try:\n"
        "            renpy.change_language(target, force=True)\n"
        "        except Exception:\n"
        "            return\n"
        "        renpy.save_persistent()\n\n"
        "    if _rntr_apply_language_once not in config.start_callbacks:\n"
        "        config.start_callbacks.append(_rntr_apply_language_once)\n"
    )
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            compiled = path.with_suffix(".rpyc")
            if compiled.exists():
                try:
                    compiled.unlink()
                    info(f"Removed stale compiled script: game/{compiled.name}")
                except OSError as exc:
                    warn(f"Failed to remove stale compiled script {compiled}: {exc}")
            info(f"Auto-language bootstrap is up to date: game/{filename}")
            return
    path.write_text(content, encoding="utf-8")
    compiled = path.with_suffix(".rpyc")
    if compiled.exists():
        try:
            compiled.unlink()
            info(f"Removed stale compiled script: game/{compiled.name}")
        except OSError as exc:
            warn(f"Failed to remove stale compiled script {compiled}: {exc}")
    info(f"Wrote auto-language bootstrap: game/{filename}")


def _remove_compiled_rpy(path: Path) -> None:
    compiled = path.with_suffix(".rpyc")
    if not compiled.exists():
        return
    try:
        compiled.unlink()
        info(f"Removed stale compiled script: game/{compiled.name}")
    except OSError as exc:
        warn(f"Failed to remove stale compiled script {compiled}: {exc}")


def _normalized_language(language: str) -> str:
    return language.strip().lower().replace("_", "-")


def _is_cjk_language(language: str) -> bool:
    norm = _normalized_language(language)
    return norm in {
        "chinese",
        "zh",
        "zh-cn",
        "zh-hans",
        "zh-hant",
        "japanese",
        "ja",
        "korean",
        "ko",
    }


def _default_language_label(language: str, target_language: str) -> str:
    norm = _normalized_language(language)
    labels = {
        "chinese": "\u7b80\u4f53\u4e2d\u6587",
        "zh": "\u7b80\u4f53\u4e2d\u6587",
        "zh-cn": "\u7b80\u4f53\u4e2d\u6587",
        "zh-hans": "\u7b80\u4f53\u4e2d\u6587",
        "zh-hant": "\u7e41\u9ad4\u4e2d\u6587",
        "japanese": "\u65e5\u672c\u8a9e",
        "ja": "\u65e5\u672c\u8a9e",
        "korean": "\ud55c\uad6d\uc5b4",
        "ko": "\ud55c\uad6d\uc5b4",
    }
    return labels.get(norm, (target_language or language).strip() or language)


def _ensure_language_font(game_dir: Path, language: str) -> Optional[str]:
    if not _is_cjk_language(language):
        return None

    game_path = game_dir / "game"
    preferred = [
        "simhei.ttf",
        "msyh.ttc",
        "msyh.ttf",
        "simsun.ttc",
        "NotoSansCJK-Regular.ttc",
        "NotoSansCJKSC-Regular.otf",
        "SourceHanSansSC-Regular.otf",
    ]

    for name in preferred:
        existing = game_path / name
        if existing.exists():
            return name

    candidates: List[Path] = []
    if os.name == "nt":
        windir = Path(os.environ.get("WINDIR", "C:/Windows"))
        fonts_dir = windir / "Fonts"
        candidates.extend(fonts_dir / name for name in preferred)
    candidates.extend(
        Path(p)
        for p in [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKSC-Regular.otf",
            "/usr/share/fonts/opentype/adobe-source-han-sans/SourceHanSansSC-Regular.otf",
        ]
    )

    for source in candidates:
        if not source.exists():
            continue
        target = game_path / source.name
        try:
            shutil.copy2(source, target)
            info(f"Copied CJK font asset: game/{target.name}")
            return target.name
        except OSError as exc:
            warn(f"Failed to copy CJK font from {source}: {exc}")

    warn("No suitable CJK font asset found. Some translated text may render as squares.")
    return None


def _patch_options_language_defaults(game_dir: Path, language: str, font_name: Optional[str]) -> None:
    path = game_dir / "game" / "options.rpy"
    if not path.exists():
        return

    original = path.read_text(encoding="utf-8", errors="ignore")
    updated = original
    changed = False

    if re.search(r"^\s*config\.language\s*=", updated, flags=re.MULTILINE) is None:
        m = re.search(r"^(?P<indent>\s*)config\.version\s*=.*$", updated, flags=re.MULTILINE)
        if m:
            insert_line = f'{m.group("indent")}config.language = "{language}"'
            updated = updated[: m.end()] + "\n" + insert_line + updated[m.end() :]
            changed = True
            info("Patched options.rpy: set config.language.")
        else:
            warn("Cannot find config.version in options.rpy, skipped config.language patch.")

    if font_name:
        font_re = re.compile(r"^(\s*)style\.default\.font\s*=\s*['\"].*?['\"]\s*$", re.MULTILINE)
        if font_re.search(updated):
            new_updated, replaced = font_re.subn(
                lambda m: f'{m.group(1)}style.default.font = "{font_name}"',
                updated,
                count=1,
            )
            if replaced > 0 and new_updated != updated:
                updated = new_updated
                changed = True
                info(f"Patched options.rpy: set style.default.font to {font_name}.")
        else:
            updated = updated.rstrip() + f'\n\nstyle.default.font = "{font_name}"\n'
            changed = True
            info(f"Patched options.rpy: appended style.default.font = {font_name}.")

    if not changed:
        return

    path.write_text(updated, encoding="utf-8")
    _remove_compiled_rpy(path)


def _patch_screens_language_selector(
    game_dir: Path,
    language: str,
    target_language: str,
    font_name: Optional[str],
) -> None:
    path = game_dir / "game" / "screens.rpy"
    if not path.exists():
        return

    original = path.read_text(encoding="utf-8", errors="ignore")
    has_trailing_newline = original.endswith("\n")
    lines = original.splitlines()

    language_exists_re = re.compile(rf'Language\(\s*[\'"]{re.escape(language)}[\'"]\s*\)')
    language_none_re = re.compile(r"Language\(\s*None\s*\)")

    def _find_textbutton_start(start: int) -> Optional[int]:
        for idx in range(start, -1, -1):
            stripped = lines[idx].lstrip()
            if stripped.startswith("#"):
                continue
            if re.match(r"^\s*textbutton\b", lines[idx]):
                return idx
        return None

    def _set_textbutton_font(line: str) -> str:
        if not font_name:
            return line
        if not re.match(r"^\s*textbutton\b", line):
            return line
        if 'text_font "' in line:
            return re.sub(r'text_font\s+"[^"]+"', f'text_font "{font_name}"', line, count=1)
        stripped = line.rstrip()
        trailing = line[len(stripped) :]
        if " action " in stripped:
            return stripped.replace(" action ", f' text_font "{font_name}" action ', 1) + trailing
        if stripped.endswith(":"):
            return stripped[:-1] + f' text_font "{font_name}":' + trailing
        return stripped + f' text_font "{font_name}"' + trailing

    def _patch_existing_buttons_font() -> bool:
        if not font_name:
            return False
        changed = False
        for idx, line in enumerate(lines):
            if not language_none_re.search(line) and not language_exists_re.search(line):
                continue
            start = _find_textbutton_start(idx)
            if start is None:
                continue
            patched = _set_textbutton_font(lines[start])
            if patched != lines[start]:
                lines[start] = patched
                changed = True
        return changed

    # If the target language button already exists, only normalize fonts.
    if any(language_exists_re.search(line) for line in lines):
        if _patch_existing_buttons_font():
            updated = "\n".join(lines)
            if has_trailing_newline:
                updated += "\n"
            path.write_text(updated, encoding="utf-8")
            _remove_compiled_rpy(path)
            info("Patched screens.rpy: normalized language selector font.")
        return

    action_idx = next((i for i, line in enumerate(lines) if language_none_re.search(line)), None)
    if action_idx is None:
        warn("Cannot find a Language(None) button in screens.rpy; skipped language selector patch.")
        return

    start_idx = _find_textbutton_start(action_idx)
    if start_idx is None:
        warn("Cannot locate textbutton block for Language(None) in screens.rpy; skipped language selector patch.")
        return

    base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip(" "))
    end_idx = max(start_idx, action_idx)
    if lines[start_idx].rstrip().endswith(":"):
        end_idx = start_idx
        for idx in range(start_idx + 1, len(lines)):
            stripped = lines[idx].strip()
            indent = len(lines[idx]) - len(lines[idx].lstrip(" "))
            if stripped and indent <= base_indent and not lines[idx].lstrip().startswith("#"):
                end_idx = idx - 1
                break
            end_idx = idx

    english_block = lines[start_idx : end_idx + 1]
    if not english_block:
        return
    english_block[0] = _set_textbutton_font(english_block[0])

    target_label = _default_language_label(language, target_language).replace('"', '\\"')
    language_block = list(english_block)
    language_block[0] = re.sub(
        r'(^\s*textbutton\s+)"[^"]*"',
        rf'\1"{target_label}"',
        language_block[0],
        count=1,
    )
    replaced = False
    for idx, line in enumerate(language_block):
        patched, count = language_none_re.subn(f'Language("{language}")', line, count=1)
        if count > 0:
            language_block[idx] = patched
            replaced = True
            break
    if not replaced:
        warn("Failed to rewrite Language(None) to target language button in screens.rpy.")
        return

    lines[start_idx : end_idx + 1] = english_block + language_block
    updated = "\n".join(lines)
    if has_trailing_newline:
        updated += "\n"
    if updated == original:
        return

    path.write_text(updated, encoding="utf-8")
    _remove_compiled_rpy(path)
    info("Patched screens.rpy: added explicit language selector button.")


def _has_before_main_menu_label(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return re.search(r"^\s*label\s+before_main_menu\b", text, flags=re.MULTILINE) is not None


def _ensure_before_main_menu_language_hook(game_dir: Path, language: str) -> None:
    game_path = game_dir / "game"
    hook_name = f"_renpy_force_{_normalized_language(language).replace('-', '_')}.rpy"
    hook_path = game_path / hook_name

    # If game already defines before_main_menu elsewhere, do not inject another label.
    for candidate in game_path.glob("*.rpy"):
        if candidate.name == hook_name:
            continue
        if _has_before_main_menu_label(candidate):
            warn(
                "Detected existing before_main_menu label in game scripts; "
                f"skip writing {hook_name} to avoid label conflict."
            )
            return

    lang_literal = json.dumps(language, ensure_ascii=False)
    content = (
        "# Auto-generated by renpy_translate_pipeline.py\n"
        "label before_main_menu:\n"
        f"    $ _preferences.language = {lang_literal}\n"
        f"    $ renpy.change_language({lang_literal}, force=True)\n"
        "    $ renpy.save_persistent()\n"
        "    return\n"
    )
    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8", errors="ignore")
        if existing == content:
            info(f"Language hook is up to date: game/{hook_name}")
            return

    hook_path.write_text(content, encoding="utf-8")
    _remove_compiled_rpy(hook_path)
    info(f"Wrote language hook for main menu: game/{hook_name}")


def ensure_language_ui_compatibility(game_dir: Path, language: str, target_language: str) -> None:
    if not language.strip():
        return
    font_name = _ensure_language_font(game_dir, language)
    _patch_options_language_defaults(game_dir, language, font_name)
    _patch_screens_language_selector(game_dir, language, target_language, font_name)
    _ensure_before_main_menu_language_hook(game_dir, language)


def compute_adaptive_timeout(base_timeout: int, texts: List[str]) -> int:
    safe_base = max(base_timeout, 1)
    max_chars = max((len(t) for t in texts), default=0)
    adaptive = safe_base + int(max_chars / 30)
    return max(safe_base, min(adaptive, safe_base * 4))


def build_openai_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def build_deepl_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v2/translate"):
        return base
    return base + "/v2/translate"


def http_post_json(
    url: str,
    payload: Dict,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> Dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code} from {url}: {details}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error calling {url}: {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON response from {url}: {raw[:400]}") from e


def http_post_form(
    url: str,
    form_data: Dict[str, str],
    list_data: Optional[List[Tuple[str, str]]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> Dict:
    pairs: List[Tuple[str, str]] = list(form_data.items())
    if list_data:
        pairs.extend(list_data)
    encoded = urllib.parse.urlencode(pairs, doseq=True).encode("utf-8")
    req_headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=encoded,
        headers=req_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code} from {url}: {details}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error calling {url}: {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON response from {url}: {raw[:400]}") from e


def extract_string_array(value: object) -> Optional[List[str]]:
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return value
    if isinstance(value, dict):
        for key in ("translations", "texts", "output", "result", "data"):
            nested = value.get(key)
            if isinstance(nested, list) and all(isinstance(x, str) for x in nested):
                return nested
    return None


def sanitize_json_like_text(text: str) -> str:
    cleaned = text.strip().lstrip("\ufeff")
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = re.sub(r",\s*([\]}])", r"\1", cleaned)
    return cleaned


def decode_json_string_fragment(fragment: str) -> str:
    try:
        return json.loads(f'"{fragment}"')
    except Exception:  # noqa: BLE001
        return fragment.replace('\\"', '"').replace("\\\\", "\\")


def extract_relaxed_string_array(text: str) -> Optional[List[str]]:
    candidate = sanitize_json_like_text(text)

    # Try list-like segment first to avoid accidentally picking unrelated quoted text.
    start = candidate.find("[")
    end = candidate.rfind("]")
    if start != -1 and end > start:
        segment = candidate[start : end + 1]
    else:
        segment = candidate

    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', segment)
    if quoted:
        return [decode_json_string_fragment(item) for item in quoted]

    # Handle broken single-item arrays such as: ["text...]
    m = re.search(r'\[\s*"([\s\S]+?)\]\s*$', segment)
    if m:
        value = m.group(1).strip().rstrip('",')
        if value:
            return [value]

    # Handle heavily truncated array beginnings such as: ["text...
    m = re.search(r'\[\s*"([\s\S]+)$', segment)
    if m:
        value = m.group(1).strip().rstrip('",')
        if value:
            return [value]

    return None


def extract_first_json_array(text: str) -> List[str]:
    text = text.strip()
    if not text:
        raise RuntimeError("Model response is empty.")

    decoder = json.JSONDecoder()
    candidates: List[str] = [text]
    code_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    for block in code_blocks:
        block = block.strip()
        if block:
            candidates.append(block)

    for candidate in candidates:
        parse_inputs = [candidate]
        sanitized_candidate = sanitize_json_like_text(candidate)
        if sanitized_candidate != candidate:
            parse_inputs.append(sanitized_candidate)

        for parse_input in parse_inputs:
            try:
                data = json.loads(parse_input)
                direct = extract_string_array(data)
                if direct is not None:
                    return direct
            except json.JSONDecodeError:
                pass

            for i, ch in enumerate(parse_input):
                if ch not in "[{":
                    continue
                try:
                    data, _ = decoder.raw_decode(parse_input[i:])
                except json.JSONDecodeError:
                    continue
                embedded = extract_string_array(data)
                if embedded is not None:
                    return embedded

    relaxed = extract_relaxed_string_array(text)
    if relaxed is not None:
        return relaxed

    raise RuntimeError(f"Model response is not a valid JSON string array: {text[:500]}")


def normalize_translation_count(result: List[str], source_texts: List[str]) -> List[str]:
    expected = len(source_texts)
    got = len(result)
    if got == expected:
        return result

    if expected == 0:
        return []

    if got == 0:
        warn_once(
            "count_mismatch_zero",
            "Model returned 0 translations for some requests; source text will be kept for missing entries.",
        )
        return list(source_texts)

    if got > expected:
        if expected == 1:
            src = source_texts[0].strip()
            for candidate in result:
                if candidate.strip() and candidate.strip() != src:
                    warn_once(
                        "count_mismatch_single_extra",
                        "Model sometimes returns multiple candidates for one source line; first non-identical candidate is used.",
                    )
                    return [candidate]
        warn_once(
            "count_mismatch_extra",
            "Model returned extra translations in some requests; extras will be truncated.",
        )
        return result[:expected]

    warn_once(
        "count_mismatch_missing",
        "Model returned fewer translations in some requests; source text will be kept for missing entries.",
    )
    padded = list(result)
    padded.extend(source_texts[got:expected])
    return padded


class Translator:
    provider_key: str

    def translate_batch(self, texts: List[str]) -> List[str]:
        raise NotImplementedError


class QuotaExceededError(RuntimeError):
    """Raised when provider quota is exhausted and retrying is pointless."""


class OpenAITranslator(Translator):
    def __init__(
        self,
        model: str,
        target_language: str,
        base_url: str,
        api_key: str,
        temperature: float,
        retries: int,
        timeout: int,
        openai_format: str,
    ) -> None:
        self.model = model
        self.target_language = target_language
        self.endpoint = build_openai_endpoint(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.retries = retries
        self.timeout = timeout
        self.openai_format = openai_format.strip()
        self.provider_key = f"openai|{self.endpoint}|{self.model}|{self.target_language}"

    def translate_batch(self, texts: List[str]) -> List[str]:
        system_prompt = (
            "You are a professional visual novel localizer.\n"
            "Translate to the requested target language.\n"
            "Rules:\n"
            "1) Keep placeholders like __RNPH_0__ unchanged.\n"
            "2) Preserve tone and intent.\n"
            "3) Do not censor, refuse, moralize, or change topic.\n"
            "4) Return ONLY a JSON array of strings, same order and same length."
        )
        user_prompt = json.dumps(
            {
                "target_language": self.target_language,
                "texts": texts,
            },
            ensure_ascii=False,
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
        }
        if self.openai_format:
            payload["format"] = self.openai_format

        headers: Dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        adaptive_timeout = compute_adaptive_timeout(self.timeout, texts)

        def run_request(request_timeout: int) -> List[str]:
            data = http_post_json(
                self.endpoint, payload, headers=headers, timeout=request_timeout
            )
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            parsed = extract_first_json_array(content)
            return normalize_translation_count(parsed, texts)

        last_err: Optional[Exception] = None
        timeout_boost_used = False
        for attempt in range(1, self.retries + 1):
            try:
                return run_request(adaptive_timeout)
            except Exception as e:  # noqa: BLE001
                last_err = e
                if (not timeout_boost_used) and ("timed out" in str(e).lower()):
                    timeout_boost_used = True
                    boosted_timeout = min(adaptive_timeout * 2, max(self.timeout * 8, adaptive_timeout))
                    warn_once(
                        "openai_timeout_boost",
                        f"OpenAI request timed out. The pipeline will retry once with a higher timeout ({boosted_timeout}s).",
                    )
                    try:
                        return run_request(boosted_timeout)
                    except Exception as boosted_err:  # noqa: BLE001
                        last_err = boosted_err
                warn(f"OpenAI batch failed (attempt {attempt}/{self.retries}): {last_err}")
                if attempt < self.retries:
                    time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"OpenAI translation failed: {last_err}")


class DeepLTranslator(Translator):
    def __init__(
        self,
        auth_key: str,
        target_lang: str,
        source_lang: Optional[str],
        base_url: str,
        retries: int,
        timeout: int,
    ) -> None:
        if not auth_key:
            raise RuntimeError("DeepL provider requires --deepl-auth-key or DEEPL_AUTH_KEY.")
        self.auth_key = auth_key
        self.target_lang = target_lang
        self.source_lang = source_lang
        self.endpoint = build_deepl_endpoint(base_url)
        self.retries = retries
        self.timeout = timeout
        src = self.source_lang or "auto"
        self.provider_key = f"deepl|{self.endpoint}|{src}|{self.target_lang}"

    def translate_batch(self, texts: List[str]) -> List[str]:
        form = {
            "target_lang": self.target_lang,
            "preserve_formatting": "1",
            "split_sentences": "nonewlines",
        }
        if self.source_lang:
            form["source_lang"] = self.source_lang
        list_data = [("text", text) for text in texts]
        headers = {"Authorization": f"DeepL-Auth-Key {self.auth_key}"}

        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                data = http_post_form(
                    self.endpoint,
                    form,
                    list_data=list_data,
                    headers=headers,
                    timeout=self.timeout,
                )
                translations = data.get("translations", [])
                result = [entry.get("text", "") for entry in translations]
                if len(result) != len(texts):
                    raise RuntimeError(
                        f"Expected {len(texts)} translations, got {len(result)}."
                    )
                return result
            except Exception as e:  # noqa: BLE001
                message = str(e)
                if "HTTP 456" in message or "Quota exceeded" in message:
                    raise QuotaExceededError(message) from e
                last_err = e
                warn(f"DeepL batch failed (attempt {attempt}/{self.retries}): {e}")
                if attempt < self.retries:
                    time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"DeepL translation failed: {last_err}")


class TranslationCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: Dict[str, Dict[str, str]] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    entries = data.get("entries", {})
                    if isinstance(entries, dict):
                        self.entries = entries
            except Exception as e:  # noqa: BLE001
                warn(f"Failed to load cache {path}: {e}")

    @staticmethod
    def _hash_key(provider_key: str, source_text: str) -> str:
        raw = f"{provider_key}\n{source_text}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()

    def get(self, provider_key: str, source_text: str) -> Optional[str]:
        key = self._hash_key(provider_key, source_text)
        entry = self.entries.get(key)
        if not entry:
            return None
        if entry.get("source") != source_text:
            return None
        if entry.get("provider") != provider_key:
            return None
        translation = entry.get("translation")
        if not isinstance(translation, str):
            return None
        if has_temp_placeholders(translation):
            return None
        return translation

    def set(self, provider_key: str, source_text: str, translation: str) -> None:
        key = self._hash_key(provider_key, source_text)
        self.entries[key] = {
            "provider": provider_key,
            "source": source_text,
            "translation": translation,
        }

    def save(self) -> None:
        ensure_dir(self.path.parent)
        payload = {"entries": self.entries}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def mask_placeholders(text: str) -> Tuple[str, Dict[str, str]]:
    mapping: Dict[str, str] = {}

    def repl(match: re.Match) -> str:
        token = f"__RNPH_{len(mapping)}__"
        mapping[token] = match.group(0)
        return token

    masked = PLACEHOLDER_RE.sub(repl, text)
    return masked, mapping


def restore_placeholders(text: str, mapping: Dict[str, str]) -> str:
    restored = text
    for token, original in mapping.items():
        restored = restored.replace(token, original)
    return restored


def escape_renpy_string(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return escaped


@dataclass
class LineJob:
    line_index: int
    prefix: str
    suffix: str
    source_text: str
    current_text: str


def collect_line_jobs(lines: List[str], overwrite: bool) -> List[LineJob]:
    jobs: List[LineJob] = []
    pending_old: Optional[str] = None
    pending_comment: Optional[str] = None

    for i, raw_line in enumerate(lines):
        line = raw_line.rstrip("\r\n")

        m = COMMENT_SAY_RE.match(line)
        if m:
            pending_comment = m.group("text")
            continue

        m = OLD_RE.match(line)
        if m:
            pending_old = m.group("text")
            continue

        m = NEW_RE.match(line)
        if m:
            current = m.group("text")
            source = pending_old if pending_old is not None else current
            if overwrite or current == source or has_temp_placeholders(current):
                jobs.append(
                    LineJob(
                        line_index=i,
                        prefix=m.group("prefix"),
                        suffix=m.group("suffix"),
                        source_text=source,
                        current_text=current,
                    )
                )
            pending_old = None
            pending_comment = None
            continue

        m = SAY_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            current = m.group("text")
            source = pending_comment if pending_comment is not None else current
            if overwrite or current == source or has_temp_placeholders(current):
                jobs.append(
                    LineJob(
                        line_index=i,
                        prefix=m.group("prefix"),
                        suffix=m.group("suffix"),
                        source_text=source,
                        current_text=current,
                    )
                )
            pending_comment = None
            pending_old = None
            continue

        if line.strip() and not line.lstrip().startswith("#"):
            pending_old = None
            pending_comment = None

    return jobs


def chunked(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def translate_unique_texts(
    unique_texts: List[str],
    translator: Translator,
    cache: TranslationCache,
    batch_size: int,
    sleep_seconds: float,
) -> Tuple[Dict[str, str], bool]:
    results: Dict[str, str] = {}
    pending: List[str] = []
    quota_hit = False

    for text in unique_texts:
        cached = cache.get(translator.provider_key, text)
        if cached is not None:
            results[text] = cached
        else:
            pending.append(text)

    if not pending:
        return results, quota_hit

    info(f"Translating {len(pending)} uncached text(s).")
    batches = chunked(pending, batch_size)
    done = 0
    for batch in batches:
        masked_batch: List[str] = []
        mappings: List[Dict[str, str]] = []
        translated_masked: List[Optional[str]] = [None] * len(batch)
        for text in batch:
            masked, mapping = mask_placeholders(text)
            masked_batch.append(masked)
            mappings.append(mapping)

        try:
            translated_result = translator.translate_batch(masked_batch)
            for idx, translated_item in enumerate(translated_result[: len(batch)]):
                translated_masked[idx] = translated_item
        except QuotaExceededError as quota_error:
            quota_hit = True
            warn(f"Quota exceeded. Stopping new requests for now: {quota_error}")
            cache.save()
            break
        except Exception as batch_error:  # noqa: BLE001
            warn(f"Batch failed, fallback to single requests: {batch_error}")
            for idx, single in enumerate(masked_batch):
                try:
                    single_result = translator.translate_batch([single])
                    if single_result:
                        translated_masked[idx] = single_result[0]
                except QuotaExceededError as quota_error:
                    quota_hit = True
                    warn(f"Quota exceeded during fallback. Stopping: {quota_error}")
                    break
                except Exception as single_error:  # noqa: BLE001
                    warn(f"Single request failed; deferring this line: {single_error}")

        applied_count = 0
        for idx, translated_item in enumerate(translated_masked):
            if translated_item is None:
                continue
            original = batch[idx]
            restored = restore_placeholders(translated_item, mappings[idx]).strip()
            if not restored:
                restored = original
            if has_temp_placeholders(restored):
                restored = original
            results[original] = restored
            cache.set(translator.provider_key, original, restored)
            applied_count += 1

        done += applied_count
        info(f"Translated {done}/{len(pending)} uncached text(s).")
        cache.save()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        if quota_hit:
            break

    return results, quota_hit


@dataclass
class FileStats:
    translated: int = 0
    skipped: int = 0
    failed: int = 0
    deferred: int = 0
    quota_exceeded: bool = False
    changed: bool = False


def apply_translations_to_file(
    path: Path,
    translator: Translator,
    cache: TranslationCache,
    batch_size: int,
    sleep_seconds: float,
    overwrite: bool,
    max_lines: int,
) -> FileStats:
    stats = FileStats()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    jobs = collect_line_jobs(lines, overwrite=overwrite)

    if not jobs:
        return stats

    if max_lines > 0:
        jobs = jobs[:max_lines]

    unique_sources = sorted(set(job.source_text for job in jobs))
    translated_map, quota_hit = translate_unique_texts(
        unique_sources, translator, cache, batch_size=batch_size, sleep_seconds=sleep_seconds
    )
    stats.quota_exceeded = quota_hit

    for job in jobs:
        translated = translated_map.get(job.source_text)
        if translated is None:
            stats.deferred += 1
            continue

        escaped = escape_renpy_string(translated)
        old_line = lines[job.line_index]
        ending = ""
        if old_line.endswith("\r\n"):
            ending = "\r\n"
        elif old_line.endswith("\n"):
            ending = "\n"

        new_line = f'{job.prefix}"{escaped}"{job.suffix}{ending}'
        if new_line == old_line:
            stats.skipped += 1
            continue
        lines[job.line_index] = new_line
        stats.translated += 1
        stats.changed = True

    if stats.changed:
        path.write_text("".join(lines), encoding="utf-8")
    return stats


def find_tl_files(game_dir: Path, language: str) -> List[Path]:
    base = game_dir / "game" / "tl" / language
    if not base.exists():
        return []
    return sorted(base.rglob("*.rpy"))


def create_translator(args: argparse.Namespace) -> Optional[Translator]:
    provider = args.provider.lower()
    if provider == "none":
        return None
    if provider == "openai":
        return OpenAITranslator(
            model=args.model,
            target_language=args.target_language,
            base_url=args.base_url,
            api_key=args.api_key,
            temperature=args.temperature,
            retries=args.retries,
            timeout=args.timeout,
            openai_format=args.openai_format,
        )
    if provider == "deepl":
        return DeepLTranslator(
            auth_key=args.deepl_auth_key,
            target_lang=args.deepl_target_lang,
            source_lang=args.deepl_source_lang,
            base_url=args.deepl_url,
            retries=args.retries,
            timeout=args.timeout,
        )
    raise RuntimeError(f"Unsupported provider: {args.provider}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automate Ren'Py translation workflow.")
    parser.add_argument("--game-dir", default=".", help="Ren'Py game root directory.")
    parser.add_argument("--launcher", default="", help="Path to launcher (.exe/.py/.sh).")
    parser.add_argument("--tools-dir", default="_tools", help="Directory used for unrpa/unrpyc.")

    parser.add_argument(
        "--language",
        default="chinese",
        help="Ren'Py language folder name under game/tl (example: chinese).",
    )
    parser.add_argument(
        "--target-language",
        default="Simplified Chinese",
        help="Target language description for LLM translation prompt.",
    )

    parser.add_argument("--skip-extract", action="store_true", help="Skip .rpa extraction.")
    parser.add_argument("--skip-decompile", action="store_true", help="Skip .rpyc decompile.")
    parser.add_argument("--skip-template", action="store_true", help="Skip Ren'Py template generation.")
    parser.add_argument("--skip-mt", action="store_true", help="Skip machine translation stage.")
    parser.add_argument(
        "--rpa-pattern",
        default="*.rpa",
        help="Archive glob in game directory (default: *.rpa).",
    )
    parser.add_argument(
        "--extract-all-rpa",
        action="store_true",
        help="Extract every matched .rpa without checking if it contains .rpyc.",
    )
    parser.add_argument(
        "--unrpyc-no-init-offset",
        action="store_true",
        help="Pass --no-init-offset to unrpyc (useful for some older games).",
    )
    parser.add_argument(
        "--unrpyc-clobber",
        action="store_true",
        help="Overwrite existing .rpy when decompiling.",
    )

    parser.add_argument("--provider", choices=["none", "openai", "deepl"], default="none")
    parser.add_argument("--batch-size", type=int, default=20, help="Translation batch size.")
    parser.add_argument("--sleep-seconds", type=float, default=0.0, help="Sleep between batches.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite lines that are already translated.")
    parser.add_argument(
        "--resume-untranslated",
        action="store_true",
        default=True,
        help="Resume from existing progress and only translate untranslated lines (default).",
    )
    parser.add_argument(
        "--no-resume-untranslated",
        dest="resume_untranslated",
        action="store_false",
        help="Disable resume behavior and translate lines even if already translated (same effect as --overwrite).",
    )
    parser.add_argument(
        "--stop-on-quota",
        action="store_true",
        default=True,
        help="Stop the run gracefully when provider quota is exhausted (default).",
    )
    parser.add_argument(
        "--no-stop-on-quota",
        dest="stop_on_quota",
        action="store_false",
        help="Do not stop immediately when quota errors happen.",
    )
    parser.add_argument("--max-lines", type=int, default=0, help="Translate at most N lines per file (0 = no limit).")
    parser.add_argument(
        "--cache-file",
        default=".translation_cache.json",
        help="Path to translation cache JSON file.",
    )

    parser.add_argument("--retries", type=int, default=3, help="API retry attempts.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Base API timeout in seconds. Adaptive timeout may increase this for long lines.",
    )

    parser.add_argument("--model", default="gpt-4.1-mini", help="OpenAI-compatible model name.")
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com"),
        help="OpenAI-compatible base URL (for local LLM, LM Studio, etc.).",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("OPENAI_API_KEY", ""),
        help="OpenAI-compatible API key. Can also use OPENAI_API_KEY env var.",
    )
    parser.add_argument("--temperature", type=float, default=0.1, help="OpenAI-compatible temperature.")
    parser.add_argument(
        "--openai-format",
        default=os.getenv("OPENAI_FORMAT", ""),
        help="Optional OpenAI-compatible 'format' field. For Ollama, set to 'json' for stricter JSON outputs.",
    )

    parser.add_argument(
        "--deepl-url",
        default=os.getenv("DEEPL_API_URL", "https://api-free.deepl.com"),
        help="DeepL API base URL.",
    )
    parser.add_argument(
        "--deepl-auth-key",
        default=os.getenv("DEEPL_AUTH_KEY", ""),
        help="DeepL auth key. Can also use DEEPL_AUTH_KEY env var.",
    )
    parser.add_argument(
        "--deepl-target-lang",
        default=os.getenv("DEEPL_TARGET_LANG", "ZH-HANS"),
        help="DeepL target_lang (example: ZH-HANS).",
    )
    parser.add_argument(
        "--deepl-source-lang",
        default=os.getenv("DEEPL_SOURCE_LANG", ""),
        help="Optional DeepL source_lang (example: EN).",
    )

    parser.add_argument("--dry-run", action="store_true", help="Print plan and exit without changing files.")
    parser.add_argument(
        "--auto-enable-language",
        action="store_true",
        default=True,
        help="Generate a small Ren'Py hook to auto-enable --language on first launch (default).",
    )
    parser.add_argument(
        "--no-auto-enable-language",
        dest="auto_enable_language",
        action="store_false",
        help="Do not generate the auto-language hook.",
    )
    parser.add_argument(
        "--auto-language-file",
        default="_renpy_translate_autolang.rpy",
        help="Filename (under game/) for the generated auto-language hook.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    game_dir = Path(args.game_dir).resolve()
    tools_dir = Path(args.tools_dir)
    if not tools_dir.is_absolute():
        tools_dir = game_dir / tools_dir
    cache_file = Path(args.cache_file)
    if not cache_file.is_absolute():
        cache_file = game_dir / cache_file

    if not (game_dir / "game").exists():
        raise RuntimeError(f"Invalid game directory: {game_dir} (missing game/ folder)")

    launcher_cmd = detect_launcher(game_dir, args.launcher or None)
    info(f"Game dir: {game_dir}")
    info(f"Launcher: {' '.join(launcher_cmd)}")
    info(f"Language: {args.language}")

    if args.overwrite and args.resume_untranslated:
        warn("--overwrite is set, so resume mode is disabled for this run.")

    if args.dry_run:
        info("Dry-run mode enabled. No changes will be made.")
        return 0

    if not args.skip_extract:
        unrpa_cmd = ensure_unrpa(tools_dir)
        extract_archives(
            game_dir=game_dir,
            unrpa_cmd=unrpa_cmd,
            pattern=args.rpa_pattern,
            extract_all=args.extract_all_rpa,
        )
    else:
        info("Skip extraction step.")

    if not args.skip_decompile:
        unrpyc_script = ensure_unrpyc_script(tools_dir)
        decompile_rpyc(
            game_dir=game_dir,
            unrpyc_script=unrpyc_script,
            no_init_offset=args.unrpyc_no_init_offset,
            clobber=args.unrpyc_clobber,
        )
    else:
        info("Skip decompile step.")

    if not args.skip_template:
        run_renpy_translate_template(
            game_dir=game_dir, launcher_cmd=launcher_cmd, language=args.language
        )
        run_renpy_translate_template(
            game_dir=game_dir, launcher_cmd=launcher_cmd, language=args.language, count_only=True
        )
    else:
        info("Skip template generation step.")

    if args.auto_enable_language:
        ensure_auto_language_bootstrap(game_dir, args.language, args.auto_language_file)
        ensure_language_ui_compatibility(game_dir, args.language, args.target_language)
    else:
        info("Skip auto language bootstrap step.")

    if args.skip_mt or args.provider == "none":
        info("Machine translation step skipped.")
        return 0

    translator = create_translator(args)
    if not translator:
        info("No translator configured. Done.")
        return 0

    cache = TranslationCache(cache_file)
    tl_files = find_tl_files(game_dir, args.language)
    if not tl_files:
        warn(f"No tl files found at game/tl/{args.language}")
        return 0

    total = FileStats()
    info(f"Machine-translating {len(tl_files)} file(s)...")
    for idx, path in enumerate(tl_files, start=1):
        info(f"[{idx}/{len(tl_files)}] {path.relative_to(game_dir)}")
        try:
            overwrite_effective = args.overwrite or (not args.resume_untranslated)
            stats = apply_translations_to_file(
                path=path,
                translator=translator,
                cache=cache,
                batch_size=max(args.batch_size, 1),
                sleep_seconds=max(args.sleep_seconds, 0.0),
                overwrite=overwrite_effective,
                max_lines=max(args.max_lines, 0),
            )
            total.translated += stats.translated
            total.skipped += stats.skipped
            total.failed += stats.failed
            total.deferred += stats.deferred
            if stats.changed:
                total.changed = True
            if stats.quota_exceeded and args.stop_on_quota:
                warn("Quota exhausted. Progress has been saved. Re-run later to continue from untranslated lines.")
                break
        except Exception as e:  # noqa: BLE001
            total.failed += 1
            warn(f"Failed translating file {path}: {e}")
        finally:
            cache.save()

    info(
        f"Done. translated={total.translated}, skipped={total.skipped}, deferred={total.deferred}, failed={total.failed}, "
        f"cache={cache_file}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        warn("Interrupted by user.")
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001
        warn(str(exc))
        raise SystemExit(1)
