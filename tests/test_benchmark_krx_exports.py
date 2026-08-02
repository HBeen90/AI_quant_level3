# -*- coding: utf-8 -*-
"""Regression tests for the archived KRX benchmark cache builder."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analysis.build_benchmark_cache_from_krx_exports import build_cache  # noqa: E402


def _write_segment(path, rows):
    frame = pd.DataFrame(rows, columns=["일자", "종가", "대비", "등락률"])
    frame.to_csv(path, index=False, encoding="cp949", lineterminator="\r\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_stitches_validated_segments(tmp_path):
    f1, f2 = tmp_path / "a.csv", tmp_path / "b.csv"
    h1 = _write_segment(f1, [
        ["2020/06/16", 101.0, 1.0, 1.0],
        ["2020/06/15", 100.0, 0.0, 0.0],
    ])
    h2 = _write_segment(f2, [
        ["2020/06/17", 99.0, -2.0, -1.98],
        ["2020/06/16", 101.0, 1.0, 1.0],
    ])
    manifest = {
        "index_code": "5044", "return_type": "PR",
        "coverage_start": "2020-06-15", "coverage_end": "2020-06-17",
        "observations": 3,
        "files": [
            {"file": f1.name, "sha256": h1, "rows": 2,
             "start": "2020-06-15", "end": "2020-06-16"},
            {"file": f2.name, "sha256": h2, "rows": 2,
             "start": "2020-06-16", "end": "2020-06-17"},
        ],
    }
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "cache.csv"
    got = build_cache(str(mp), str(out))
    assert list(got["level"]) == [100.0, 101.0, 99.0]
    assert out.read_bytes().startswith(b"date,level\n")
    assert b"\r\n" not in out.read_bytes()


def test_rejects_changed_raw_export(tmp_path):
    raw = tmp_path / "a.csv"
    digest = _write_segment(raw, [["2020/06/15", 100.0, 0.0, 0.0]])
    manifest = {
        "index_code": "5044", "return_type": "PR",
        "coverage_start": "2020-06-15", "coverage_end": "2020-06-15",
        "observations": 1,
        "files": [{"file": raw.name, "sha256": digest, "rows": 1,
                   "start": "2020-06-15", "end": "2020-06-15"}],
    }
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(manifest), encoding="utf-8")
    raw.write_bytes(raw.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        build_cache(str(mp), str(tmp_path / "cache.csv"))
