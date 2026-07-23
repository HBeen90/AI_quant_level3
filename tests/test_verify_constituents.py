from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from analysis import verify_constituents as verifier
from analysis import theme_relevance


class VerifyConstituentsTests(unittest.TestCase):
    def _write(self, rows: list[dict]) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        tmp.close()
        path = Path(tmp.name)
        pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    @staticmethod
    def _valid_rows() -> list[dict]:
        return [
            {
                "코드": "005930",
                "종목명": "A",
                "bucket": "anchor",
                "weight": 0.4,
                "ff_market_cap": 4,
            },
            {
                "코드": "042700",
                "종목명": "B",
                "bucket": "core",
                "weight": 0.6,
                "ff_market_cap": 6,
            },
        ]

    def test_missing_code_is_contract_failure(self):
        rows = self._valid_rows()
        for row in rows:
            row.pop("코드")
        with self.assertRaisesRegex(ValueError, "필수 컬럼 누락"):
            verifier._load_input(self._write(rows))

    def test_nonfinite_weight_and_market_cap_are_rejected(self):
        rows = self._valid_rows()
        rows[0]["weight"] = "NaN"
        with self.assertRaisesRegex(ValueError, "weight 결측·비유한값"):
            verifier._load_input(self._write(rows))

        rows = self._valid_rows()
        rows[0]["ff_market_cap"] = "inf"
        with self.assertRaisesRegex(ValueError, "ff_market_cap 결측·비유한값"):
            verifier._load_input(self._write(rows))

    def test_verify_issues_make_the_run_fail(self):
        path = self._write(self._valid_rows())
        with (
            patch.object(verifier.weighting, "allocate", return_value=np.array([0.4, 0.6])),
            patch.object(verifier.weighting, "verify", return_value=["상한 위반"]),
        ):
            self.assertFalse(verifier.verify_csv(path))

    def test_published_rounding_tolerance_is_accepted(self):
        rows = self._valid_rows()
        rows[0]["weight"] = 0.40004
        rows[1]["weight"] = 0.59996
        path = self._write(rows)
        with (
            patch.object(verifier.weighting, "allocate", return_value=np.array([0.4, 0.6])),
            patch.object(verifier.weighting, "verify", return_value=[]),
        ):
            self.assertTrue(verifier.verify_csv(path))


class ThemeRelevanceTests(unittest.TestCase):
    @staticmethod
    def _constituents() -> pd.DataFrame:
        return pd.DataFrame([
            {"코드": "005930", "종목명": "삼성전자", "bucket": "anchor",
             "weight": 0.2157, "exposure": np.nan, "mem_ratio": np.nan},
            {"코드": "000660", "종목명": "SK하이닉스", "bucket": "anchor",
             "weight": 0.1843, "exposure": np.nan, "mem_ratio": np.nan},
            {"코드": "042700", "종목명": "한미반도체", "bucket": "core",
             "weight": 0.18, "exposure": 0.60, "mem_ratio": np.nan},
            {"코드": "089030", "종목명": "테크윙", "bucket": "core",
             "weight": 0.18, "exposure": 0.30, "mem_ratio": np.nan},
            {"코드": "003160", "종목명": "디아이", "bucket": "core",
             "weight": 0.1281, "exposure": 0.35, "mem_ratio": np.nan},
            {"코드": "348210", "종목명": "넥스틴", "bucket": "core",
             "weight": 0.1058, "exposure": 0.50, "mem_ratio": np.nan},
            {"코드": "112290", "종목명": "와이씨켐", "bucket": "satellite",
             "weight": 0.0061, "exposure": np.nan, "mem_ratio": 0.75},
        ])

    def test_published_composition_score_and_floor(self):
        data = self._constituents()
        self.assertAlmostEqual(theme_relevance.relevance_score(data), 0.4405166667)
        self.assertAlmostEqual(theme_relevance.current_weight_floor(data), 0.2740666667)

    def test_single_name_stress_identifies_hanmi_trigger(self):
        stress = theme_relevance.single_name_stress(self._constituents())
        scores = stress.set_index("종목명")["스트레스 점수"]
        self.assertAlmostEqual(scores["한미반도체"], 0.3415166667)
        self.assertAlmostEqual(scores["테크윙"], 0.4315166667)

    def test_missing_group_score_fails_closed(self):
        data = self._constituents()
        data.loc[data["종목명"].eq("와이씨켐"), "mem_ratio"] = np.nan
        with self.assertRaisesRegex(ValueError, "결측 없이 0~1"):
            theme_relevance.relevance_score(data)


if __name__ == "__main__":
    unittest.main()
