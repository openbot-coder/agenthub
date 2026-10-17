"""Media support: workspace sentinel scan for agent-produced files.

The ACP protocol only streams text updates — there is no "agent produced a
file" notification. Outbound media therefore works as a sentinel diff: the
session layer snapshots the agent workspace before a turn and scans it after;
new files become "media" events that channels deliver (wecom: 3-step upload;
other channels: text fallback with the path).
"""
from __future__ import annotations

import logging
import mimetypes
import os

logger = logging.getLogger("agenthub.media")

# hard caps: never upload/download anything bigger; keep the scan cheap
MEDIA_MAX_BYTES = 20 * 1024 * 1024
SCAN_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "media_in"}
SCAN_EXCLUDE_FILES = {".opencode.json", "opencode.json", "AGENTS.md"}
MAX_FILES_PER_TURN = 10

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_AUDIO_EXTS = {".mp3", ".wav", ".amr", ".ogg", ".m4a", ".flac"}


def guess_kind(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "file"


def scan_new_files(cwd: str, seen: dict[str, float]) -> list[dict]:
    """Diff cwd against the `seen` snapshot {abspath: size}.

    Returns descriptors for new files and mutates `seen`. Hidden entries and
    SKIP_DIRS are never reported; oversized files are skipped (logged).
    """
    found: list[dict] = []
    for root, dirs, names in os.walk(cwd):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SCAN_SKIP_DIRS]
        for n in names:
            if n.startswith(".") or n in SCAN_EXCLUDE_FILES:
                continue
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = os.path.abspath(p)
            if key in seen or st.st_size == 0:
                continue
            seen[key] = st.st_size
            if st.st_size > MEDIA_MAX_BYTES:
                logger.info(f"[media] skip oversize file: {n} ({st.st_size}B)")
                continue
            found.append({
                "path": p,
                "filename": n,
                "kind": guess_kind(n),
                "mime": mimetypes.guess_type(n)[0] or "application/octet-stream",
                "size": st.st_size,
            })
    return found[:MAX_FILES_PER_TURN]
