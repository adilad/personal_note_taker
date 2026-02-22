"""LLM client — single analyze_transcript() call per segment (Phase 6)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from recorder.config import settings
from recorder.llm.prompts import (
    ANALYSIS_PROMPT,
    CHUNK_SUMMARY_PROMPT,
    DAILY_SUMMARY_PROMPT,
    DIARIZED_SECTION,
    HOURLY_SUMMARY_PROMPT,
)
from recorder.metrics import llm_duration, llm_errors_total

logger = logging.getLogger(__name__)

_local_llm = None


def _get_local_llm():
    global _local_llm
    if _local_llm is None and not settings.use_litellm:
        import os

        model_path = os.path.expanduser(settings.llm_model_path)
        if os.path.exists(model_path):
            try:
                from llama_cpp import Llama  # type: ignore

                _local_llm = Llama(model_path=model_path, n_ctx=4096, n_threads=8)
                logger.info("llm.local_loaded", extra={"model_path": model_path})
            except ImportError:
                logger.warning("llm.llama_cpp_not_installed")
    return _local_llm


@dataclass
class AnalysisResult:
    summary: str = ""
    speakers: str = ""
    participants: list[str] = field(default_factory=list)
    category: str = ""
    action_items: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    sentiment: str = ""
    keywords: list[str] = field(default_factory=list)


def _call_litellm(prompt: str, max_tokens: int = 2000, json_mode: bool = False) -> str:
    """
    Make a single chat completion call to LiteLLM.
    Retries on 429/5xx with exponential backoff.
    """
    import requests  # type: ignore

    payload: dict[str, Any] = {
        "model": settings.litellm_model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": settings.litellm_temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    t0 = time.monotonic()
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{settings.litellm_base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.litellm_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
            if resp.status_code == 200:
                elapsed = time.monotonic() - t0
                llm_duration.labels(provider="litellm").observe(elapsed)  # type: ignore[attr-defined]
                return resp.json()["choices"][0]["message"]["content"].strip()
            elif resp.status_code in (429, 502, 503, 504):
                wait = 2**attempt
                logger.warning(
                    "llm.retry",
                    extra={"status": resp.status_code, "attempt": attempt + 1, "wait": wait},
                )
                time.sleep(wait)
            else:
                logger.error(
                    "llm.error",
                    extra={"status": resp.status_code, "body": resp.text[:200]},
                )
                llm_errors_total.labels(provider="litellm").inc()  # type: ignore[attr-defined]
                break
        except Exception as exc:
            llm_errors_total.labels(provider="litellm").inc()  # type: ignore[attr-defined]
            logger.warning("llm.request_failed", extra={"error": str(exc), "attempt": attempt + 1})
            if attempt < 2:
                time.sleep(2**attempt)
    return ""


def _call_local_llm(prompt: str, max_tokens: int = 512) -> str:
    llm = _get_local_llm()
    if not llm:
        return ""
    try:
        t0 = time.monotonic()
        out = llm(prompt, max_tokens=max_tokens, temperature=0.1, stop=["</s>"])
        llm_duration.labels(provider="local").observe(time.monotonic() - t0)  # type: ignore[attr-defined]
        return out["choices"][0]["text"].strip()
    except Exception as exc:
        llm_errors_total.labels(provider="local").inc()  # type: ignore[attr-defined]
        logger.error("llm.local_error", extra={"error": str(exc)})
        return ""


def _simple_summarize(text: str, max_sentences: int = 4) -> str:
    """Extractive summarizer — last-resort fallback."""
    sents = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
    if not sents:
        return ""
    scores = [(len(s), i, s) for i, s in enumerate(sents)]
    scores.sort(reverse=True)
    chosen: list[str] = []
    for _, _, s in scores:
        if len(chosen) >= max_sentences:
            break
        if any(s.lower()[:40] in c.lower() or c.lower()[:40] in s.lower() for c in chosen):
            continue
        chosen.append(s)
    return ". ".join(chosen) + ("." if chosen else "")


def analyze_transcript(transcript: str, diarized_text: str = "") -> AnalysisResult:
    """
    Single LLM call that returns all analysis for a segment.
    Falls back gracefully on JSON parse errors.
    """
    if not transcript.strip():
        return AnalysisResult()

    diarized_section = ""
    if diarized_text:
        diarized_section = DIARIZED_SECTION.format(diarized_text=diarized_text)

    prompt = ANALYSIS_PROMPT.format(transcript=transcript, diarized_section=diarized_section)

    raw = ""
    if settings.use_litellm:
        raw = _call_litellm(prompt, max_tokens=settings.model_max_tokens, json_mode=True)
    elif _get_local_llm():
        raw = _call_local_llm(prompt, max_tokens=1024)

    if not raw:
        return AnalysisResult(
            summary=_simple_summarize(transcript),
        )

    # Parse JSON — strip markdown fences if present
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("llm.json_parse_error", extra={"error": str(exc), "raw": raw[:200]})
        return AnalysisResult(summary=_simple_summarize(transcript))

    def _str(key: str) -> str:
        v = data.get(key, "")
        return v if isinstance(v, str) else ""

    def _list(key: str) -> list[str]:
        v = data.get(key, [])
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            return [x.strip() for x in v.split("\n") if x.strip()]
        return []

    return AnalysisResult(
        summary=_str("summary"),
        speakers=_str("speakers"),
        participants=_list("participants"),
        category=_str("category"),
        action_items=_list("action_items"),
        open_questions=_list("open_questions"),
        sentiment=_str("sentiment"),
        keywords=_list("keywords"),
    )


def summarize_daily(text: str) -> str:
    """Generate a daily summary, chunking if needed."""
    if not text.strip():
        return "(no transcripts today)"

    MAX_CHARS = 12000
    if len(text) > MAX_CHARS:
        import re

        chunks: list[str] = []
        current = ""
        for line in text.split("\n\n"):
            if len(current) + len(line) > MAX_CHARS:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = (current + "\n\n" + line) if current else line
        if current:
            chunks.append(current)

        parts: list[str] = []
        for i, chunk in enumerate(chunks):
            logger.info("llm.chunk_summary", extra={"chunk": i + 1, "total": len(chunks)})
            times = re.findall(r"\[(\d{1,2}:\d{2} [AP]M)\]", chunk)
            time_range = f"({times[0]} – {times[-1]})" if times else ""
            summary = _call_litellm(
                CHUNK_SUMMARY_PROMPT.format(text=chunk), max_tokens=400
            ) or _simple_summarize(chunk)
            parts.append(f"**{time_range}**\n{summary}" if time_range else summary)
        return "\n\n".join(parts)

    prompt = DAILY_SUMMARY_PROMPT.format(text=text)
    if settings.use_litellm:
        result = _call_litellm(prompt, max_tokens=600)
        if result:
            return result
    if _get_local_llm():
        result = _call_local_llm(prompt, max_tokens=1024)
        if result:
            return result
    return _simple_summarize(text)


def summarize_hourly(text: str) -> str:
    """Short hourly summary."""
    if not text.strip():
        return ""
    prompt = HOURLY_SUMMARY_PROMPT.format(text=text)
    if settings.use_litellm:
        result = _call_litellm(prompt, max_tokens=500)
        if result:
            return result
    if _get_local_llm():
        return _call_local_llm(prompt, max_tokens=512)
    return _simple_summarize(text)
