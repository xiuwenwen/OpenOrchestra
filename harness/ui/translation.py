from __future__ import annotations

import re
import shutil
import threading
from hashlib import sha256
from typing import Any

from harness.core.misc_chat import MiscChatRunner


class DisplayTranslator:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, str]] = {}

    def translate_to_zh(self, text: str) -> dict[str, str]:
        key = sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._cache.get(key)
        if cached:
            return cached

        fallback = self._fallback_translate(text)
        backend = self._translation_backend()
        if not backend:
            result = {"text": fallback, "mode": "fallback", "error": "translation backend unavailable"}
            with self._lock:
                self._cache[key] = result
            return result

        try:
            translated = self._model_translate(text, backend)
            result = {"text": translated or fallback, "mode": "model" if translated else "fallback", "error": ""}
        except Exception as exc:
            result = {"text": fallback, "mode": "fallback", "error": str(exc)}
        with self._lock:
            self._cache[key] = result
        return result

    def _translation_backend(self) -> str | None:
        backend = str(self.config.get("agent_backend", {}).get("default", "") or "")
        candidates = ("claude", "codex", "gemini", "qwen")
        if backend in candidates and shutil.which(backend):
            return backend
        for candidate in candidates:
            if shutil.which(candidate):
                return candidate
        return None

    def _model_translate(self, text: str, backend: str) -> str:
        masked_text, placeholders = self._mask_markdown_for_translation(text)
        chunks = self._chunks(masked_text, max_chars=12000)
        translated_chunks = []
        runner = MiscChatRunner(backend, log_root="logs/ui_translate", config=self.config)
        for chunk in chunks:
            prompt = "\n".join(
                [
                    "Translate the following Harness UI display text into Simplified Chinese.",
                    "Preserve Markdown structure.",
                    "Do not translate placeholders like __KEEP_0__.",
                    "Do not translate file paths, commands, code, JSON, YAML, environment variables, or configuration keys.",
                    "Translate only prose, prompts, explanations, role instructions, and model-visible statements.",
                    "Return only the translated text, without commentary.",
                    "",
                    chunk,
                ]
            )
            translated_chunks.append(runner.ask(prompt, timeout_seconds=0).strip())
        return self._restore_placeholders("\n".join(translated_chunks), placeholders)

    def _mask_markdown_for_translation(self, text: str) -> tuple[str, list[str]]:
        in_fence = False
        masked_lines: list[str] = []
        placeholders: list[str] = []
        for line in text.splitlines():
            if re.match(r"^\s*```", line):
                in_fence = not in_fence
                masked_lines.append(line)
                continue
            if in_fence or self._should_preserve_line(line):
                masked_lines.append(line)
                continue
            masked_lines.append(self._mask_inline_preserved_tokens(line, placeholders))
        return "\n".join(masked_lines), placeholders

    def _mask_inline_preserved_tokens(self, line: str, placeholders: list[str]) -> str:
        def keep(match: re.Match[str]) -> str:
            marker = f"__KEEP_{len(placeholders)}__"
            placeholders.append(match.group(0))
            return marker

        return re.sub(
            r"`[^`]*`|https?://\S+|(?:/|~/|\.\.?/)[^\s),;]+|[A-Za-z0-9_.-]+\.(?:md|py|js|ts|tsx|json|yaml|yml|txt|log|diff|patch|html|css|sh)\b",
            keep,
            line,
        )

    def _restore_placeholders(self, text: str, placeholders: list[str]) -> str:
        for index, token in enumerate(placeholders):
            text = text.replace(f"__KEEP_{index}__", token)
        return text

    def _should_preserve_line(self, line: str) -> bool:
        trimmed = line.strip()
        if not trimmed:
            return True
        if re.match(r"^(diff --git|index |--- |\+\+\+ |@@ |[+-]{3,})", trimmed):
            return True
        if re.match(r"^[+-]\s", trimmed) and re.search(r"[`$./\\]|^\+\s*(import|from|def|class|const|let|var|function)\b", trimmed):
            return True
        if re.match(r"^(curl|python3?|pip|npm|pnpm|yarn|bun|uv|pytest|git|docker|make|cargo|go|node|claude|codex|gemini|qwen|source|cd|mkdir|cp|mv|rm|cat|sed|rg|grep|ls|open)\b", trimmed):
            return True
        if re.match(r"^\$ ", trimmed):
            return True
        if re.match(r"^(https?://|file://)", trimmed):
            return True
        if re.match(r"^(/|~/|\.\.?/)[^\s]*$", trimmed):
            return True
        if re.match(r"^[A-Za-z]:[\\/]", trimmed):
            return True
        if re.match(r"^[-*]\s+(`[^`]+`|/|~/|\.\.?/|https?://)", trimmed):
            return True
        if re.match(r"^\s*[{[\]}],?\s*$", line):
            return True
        if re.match(r'^\s*"[^"]+"\s*:\s*("[^"]*"|\d+|true|false|null|[{[]),?\s*$', line):
            return True
        if re.match(r"^\s*[A-Z0-9_]+\s*=", line):
            return True
        if re.match(r"^\s*[-*]\s+[A-Za-z0-9_.\/~-]+\.(md|py|js|ts|tsx|json|yaml|yml|txt|log|diff|patch|html|css|sh)\b", line):
            return True
        return False

    def _chunks(self, text: str, max_chars: int) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in text.splitlines():
            line_len = len(line) + 1
            if current and current_len + line_len > max_chars:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len
        if current:
            chunks.append("\n".join(current))
        return chunks or [""]

    def _fallback_translate(self, text: str) -> str:
        replacements = [
            (r"Workflow classification: new_project\.", "工作流分类：新项目。"),
            (r"Use the full new-project workflow from planning through final delivery\.", "使用完整的新项目工作流，从规划一直到最终交付。"),
            (r"Establish project structure, implementation approach, validation strategy, and final handoff artifacts\.", "建立项目结构、实现方案、验证策略和最终移交产物。"),
            (r"Original user prompt:", "原始用户提示词："),
            (r"Create planning artifacts only\.", "只创建规划产物。"),
            (r"Analyze the request, existing artifacts, assumptions, risks, compatibility constraints, and an actionable task breakdown\.", "分析请求、现有产物、假设、风险、兼容性约束，并给出可执行的任务拆解。"),
            (r"Do not modify source files\.", "不要修改源码文件。"),
            (r"Your delivery\.md status must be 'success' if you produced a complete plan, even if you identify high risks\.", "如果你产出了完整计划，即使识别出高风险，delivery.md 的状态也必须是 'success'。"),
            (r"Specialization: Pragmatic Planner\.", "专长：务实规划者。"),
            (r"Preference: MVP-first, implementation-oriented, complexity-minimizing\.", "偏好：MVP 优先、面向实现、最小化复杂度。"),
            (r"Focus:", "关注点："),
            (r"Role Responsibility", "角色职责"),
            (r"Role Specialization", "角色专长"),
            (r"User Request", "用户请求"),
            (r"Implementation", "实现"),
            (r"approach", "方案"),
            (r"complete", "完整"),
            (r"success", "成功"),
        ]
        out = text
        for pattern, value in replacements:
            out = re.sub(pattern, value, out, flags=re.IGNORECASE)
        return out
