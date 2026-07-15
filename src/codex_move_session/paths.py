from __future__ import annotations

import ntpath
import os
import posixpath
import re
from typing import Literal

PathFlavor = Literal["posix", "windows"]


def _strip_windows_extended_prefix(value: str) -> str:
    if value.casefold().startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


class PathMapper:
    """Replace one absolute directory root without touching similarly named paths."""

    def __init__(self, old: str, new: str, *, flavor: PathFlavor | None = None) -> None:
        self.flavor: PathFlavor = flavor or ("windows" if os.name == "nt" else "posix")
        if self.flavor == "windows":
            self.old = ntpath.normpath(_strip_windows_extended_prefix(old).replace("/", "\\"))
            self.new = ntpath.normpath(_strip_windows_extended_prefix(new).replace("/", "\\"))
            if not ntpath.isabs(self.old) or not ntpath.isabs(self.new):
                raise ValueError("old and new paths must be absolute")
            pattern = self._windows_pattern(self.old)
            self._pattern = re.compile(pattern + r"(?![A-Za-z0-9._~-])", re.IGNORECASE)
        else:
            self.old = posixpath.normpath(old)
            self.new = posixpath.normpath(new)
            if not posixpath.isabs(self.old) or not posixpath.isabs(self.new):
                raise ValueError("old and new paths must be absolute")
            self._pattern = re.compile(re.escape(self.old) + r"(?![A-Za-z0-9._~-])")

    @staticmethod
    def _windows_pattern(value: str) -> str:
        separator = r"[\\/]"
        if value.startswith("\\\\"):
            parts = [part for part in value[2:].split("\\") if part]
            prefix = r"(?:\\\\\?\\UNC[\\/]|\\\\)"
        else:
            parts = [part for part in value.split("\\") if part]
            prefix = r"(?:\\\\\?\\)?"
        return prefix + separator.join(re.escape(part) for part in parts)

    def _replacement(self, matched: str) -> str:
        if self.flavor == "posix":
            return self.new
        separator = "/" if "/" in matched and "\\" not in matched else "\\"
        replacement = self.new.replace("\\", separator)
        extended = matched.casefold().startswith("\\\\?\\")
        if extended:
            if replacement.startswith(separator * 2):
                replacement = f"{separator * 2}?{separator}UNC{separator}{replacement[2:]}"
            else:
                replacement = f"{separator * 2}?{separator}{replacement}"
        return replacement

    def replace_text(self, text: str) -> tuple[str, int]:
        return self._pattern.subn(lambda match: self._replacement(match.group(0)), text)

    def map_path(self, value: str) -> str | None:
        match = self._pattern.match(value)
        if match is None or match.start() != 0:
            return None
        suffix = value[match.end() :]
        separators = ("/", "\\") if self.flavor == "windows" else ("/",)
        if suffix and not suffix.startswith(separators):
            return None
        return self._replacement(match.group(0)) + suffix
