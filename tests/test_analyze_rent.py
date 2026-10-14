import unittest

from pathlib import Path

from scripts.analyze_rent import build_html, build_map_svg, summarize_annual_medians, summarize_missing, summarize_monthly


class AnalysisTests(unittest.TestCase):
    def test_summarizes_annual_medians_by_district_and_use(self) -> None:
        rows = [
            {"CGG_NM": "강남구", "BLDG_USG": "아파트", "CTRT_DAY": "20240101", "RENT_SE": "전세", "GRFE": "10000", "RENT_AREA": "100"},
            {"CGG_NM": "강남구", "BLDG_USG": "아파트", "CTRT_DAY": "20241231", "RENT_SE": "전세", "GRFE": "50000", "RENT_AREA": "100"},
            {"CGG_NM": "강남구", "BLDG_USG": "아파트", "CTRT_DAY": "20250101", "RENT_SE": "전세", "GRFE": "30000", "RENT_AREA": "100"},
            {"CGG_NM": "서초구", "BLDG_USG": "아파트", "CTRT_DAY": "20240101", "RENT_SE": "전세", "GRFE": "90000", "RENT_AREA": "100"},
            {"CGG_NM": "강남구", "BLDG_USG": "아파트", "CTRT_DAY": "20240101", "RENT_SE": "월세", "GRFE": "1000", "RTFE": "0", "RENT_AREA": "100"},
        ]

        result = summarize_annual_medians(rows)

        self.assertEqual(result, [
            {"year": 2024, "district": "강남구", "building_use": "아파트", "count": 3, "minimum": 10.0, "median": 100.0, "maximum": 500.0},
            {"year": 2024, "district": "서초구", "building_use": "아파트", "count": 1, "minimum": 900.0, "median": 900.0, "maximum": 900.0},
            {"year": 2025, "district": "강남구", "building_use": "아파트", "count": 1, "minimum": 300.0, "median": 300.0, "maximum": 300.0},
        ])

    def test_converts_monthly_rent_to_jeonse_equivalent(self) -> None:
        result = summarize_annual_medians([
            {"CGG_NM": "강남구", "BLDG_USG": "아파트", "CTRT_DAY": "20240101", "RENT_SE": "월세", "GRFE": "1000", "RTFE": "100", "RENT_AREA": "50"},
        ])

        self.assertEqual(result, [{
            "year": 2024, "district": "강남구", "building_use": "아파트", "count": 1,
            "minimum": 500.0, "median": 500.0, "maximum": 500.0,
        }])

    def test_html_has_three_charts_and_explanatory_cards(self) -> None:
        missing = {
            "rows": 0, "MNO": {"missing": 0, "rate": 0},
            "SNO": {"missing": 0, "rate": 0}, "both_missing": 0, "reasons": [],
        }
        boundaries = __import__("json").loads(Path("data/seoul_district_boundaries_2025.json").read_text(encoding="utf-8"))
        page = build_html([], [], missing, Path("sample.csv.gz"), build_map_svg(boundaries))

        self.assertIn('id="chart"', page)
        self.assertIn('id="barChart"', page)
        self.assertIn('id="districtTrend"', page)
        self.assertIn('id="sortYear"', page)
        self.assertIn('id="trendUsage"', page)
        self.assertEqual(page.count('class="chart-note"'), 3)
        self.assertIn("monthCanvas.addEventListener('mousemove'", page)
        self.assertIn("monthCanvas.addEventListener('mouseleave'", page)
        self.assertIn("drawDistrictTrend", page)
        self.assertIn("만원/㎡", page)
        self.assertIn("minimum", page)
        self.assertIn("maximum", page)
        self.assertIn("district-colors", page)
        self.assertIn("ctx.lineWidth=1;ctx.beginPath();monthRows", page)
        self.assertIn("bwrColor", page)
        self.assertIn("yearColor", page)
        self.assertIn("sortYear.value", page)
        self.assertIn("bwrColor", page)
        self.assertIn('class="year-legend"', page)
        self.assertIn('id="districtMap"', page)
        self.assertIn('id="mapLegend"', page)
        self.assertIn("drawDistrictMap", page)
        self.assertIn("const bwrColor=", page)

    def test_builds_svg_for_a_district_boundary(self) -> None:
        svg = build_map_svg({
            "features": [{
                "type": "Feature",
                "properties": {"SIG_KOR_NM": "강남구", "SIG_CD": "11680"},
                "geometry": {"type": "Polygon", "coordinates": [[[127, 37], [127.1, 37], [127.1, 37.1], [127, 37.1], [127, 37]]]},
            }],
        })

        self.assertIn('<svg', svg)
        self.assertIn('data-district="강남구"', svg)
        self.assertIn('id="gu-11680"', svg)
        self.assertIn('class="district-label"', svg)

    def test_map_has_all_seoul_districts(self) -> None:
        boundaries = __import__("json").loads(Path("data/seoul_district_boundaries_2025.json").read_text(encoding="utf-8"))
        svg = build_map_svg(boundaries)
        self.assertEqual(svg.count('class="district-shape"'), 25)
        self.assertIn("서울 열린데이터광장", svg)

    def test_summarizes_price_per_area_and_ignores_invalid_rows(self) -> None:
        rows = [
            {"CGG_NM": "강남구", "BLDG_USG": "아파트", "CTRT_DAY": "20250101", "RENT_SE": "전세", "GRFE": "10000", "RENT_AREA": "50"},
            {"CGG_NM": "강남구", "BLDG_USG": "아파트", "CTRT_DAY": "20250120", "RENT_SE": "전세", "GRFE": "20000", "RENT_AREA": "50"},
            {"CGG_NM": "강남구", "BLDG_USG": "아파트", "CTRT_DAY": "20250125", "RENT_SE": "월세", "GRFE": "1000", "RTFE": "100", "RENT_AREA": "50"},
            {"CGG_NM": "강남구", "BLDG_USG": "아파트", "CTRT_DAY": "20250121", "RENT_SE": "월세", "GRFE": "90000", "RENT_AREA": "50"},
            {"CGG_NM": "강남구", "BLDG_USG": "아파트", "CTRT_DAY": "20250122", "RENT_SE": "전세", "GRFE": "10000", "RENT_AREA": "0"},
        ]

        result = summarize_monthly(rows)

        self.assertEqual(result, [{
            "district": "강남구", "building_use": "아파트", "month": "2025-01",
            "count": 3, "minimum": 200.0, "median": 400.0, "maximum": 500.0,
        }])

    def test_reports_missing_rates_from_eligible_contracts(self) -> None:
        rows = [
            {"MNO": "", "SNO": "1", "CTRT_DAY": "20250101", "RENT_SE": "전세", "GRFE": "100", "RENT_AREA": "30"},
            {"MNO": "2", "SNO": "", "CTRT_DAY": "20250102", "RENT_SE": "전세", "GRFE": "100", "RENT_AREA": "30"},
        ]

        result = summarize_missing(rows)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["MNO"], {"missing": 1, "rate": 0.5})
        self.assertEqual(result["SNO"], {"missing": 1, "rate": 0.5})
        self.assertEqual(result["both_missing"], 0)


if __name__ == "__main__":
    unittest.main()
