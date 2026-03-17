"""Fuzzy string matching utilities for OCR-tolerant name comparison."""

from __future__ import annotations


def edit_distance(s1: str, s2: str) -> int:
    """Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return edit_distance(s2, s1)
    if not s2:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for c1 in s1:
        curr = [prev[0] + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(
                prev[j + 1] + 1,
                curr[j] + 1,
                prev[j] + (c1 != c2),
            ))
        prev = curr
    return prev[-1]


def fuzzy_threshold(name: str) -> int:
    """Max edit distance for OCR fuzzy matching, scaled by name length."""
    n = len(name)
    if n <= 5:
        return 1
    if n <= 15:
        return 2
    return 3


def names_match(saved: str, detected: str) -> bool:
    """Check if two names are close enough to be the same.

    Accounts for OCR truncation (prefix matching) and minor OCR noise
    (edit distance).
    """
    if not saved or not detected:
        return False
    s, d = saved.lower(), detected.lower()
    if s == d:
        return True
    if s.startswith(d) or d.startswith(s):
        return True
    return edit_distance(s, d) <= fuzzy_threshold(saved)
