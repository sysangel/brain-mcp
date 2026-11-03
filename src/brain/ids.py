"""Stable document ids.

Episode ids stay the transcript-derived ``ep_`` values (machine-independent).
Memory ids in the postgres store are repo-relative v2 (``brain-mem-v2``).
Absolute-path v1 ids are recorded in ``memory_id_aliases`` so older handoffs
still resolve. Summaries, proposals, and handoffs get their own salts over
the repo-relative path — summary ids are stable here, not the ephemeral
sqlite ``sum_<rowid>``.
"""

from __future__ import annotations

import hashlib


def _sha16(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def repo_key(relpath: str) -> str:
    return relpath.replace("\\", "/").lstrip("./")


def memory_stable_id_v1(abspath: str) -> str:
    """Absolute-path id. Same salt as ``db.memory_stable_id``."""
    return "mem_" + _sha16("brain-mem-v1\x00" + str(abspath))


def memory_stable_id_v2(relpath: str) -> str:
    """Canonical memory id: repo-relative path."""
    return "mem_" + _sha16("brain-mem-v2\x00" + repo_key(relpath))


def summary_stable_id(relpath: str) -> str:
    return "sum_" + _sha16("brain-sum-v1\x00" + repo_key(relpath))


def proposal_stable_id(relpath: str) -> str:
    return "prop_" + _sha16("brain-prop-v1\x00" + repo_key(relpath))


def handoff_stable_id(relpath: str) -> str:
    return "hand_" + _sha16("brain-hand-v1\x00" + repo_key(relpath))
