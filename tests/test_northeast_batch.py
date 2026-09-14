import unittest

from collectors.hit import _query
from collectors.jlu import _detail, _list_records, _source_datetime


JLU_LIST = b"""
<ul><li><a href="/portal/jyzp/recruit/details?id=abc">[\xe7\xbd\xae\xe9\xa1\xb6] Old</a><span>\xe5\x88\x9b\xe5\xbb\xba\xe6\x97\xb6\xe9\x97\xb4\xef\xbc\x9a2025-9-1</span></li>
<li><a href="/portal/jyzp/recruit/details?id=def">New Job</a><span>\xe5\x88\x9b\xe5\xbb\xba\xe6\x97\xb6\xe9\x97\xb4\xef\xbc\x9aSep 8, 2026</span></li></ul>
"""

JLU_DETAIL = b"""
<h1 class="m-detail-container__title">New Job</h1>
<div class="m-detail-container__info">\xe5\x8f\x91\xe5\xb8\x83\xe6\x97\xb6\xe9\x97\xb4\xef\xbc\x9aSep 8, 2026 7:47:26 PM \xe7\x82\xb9\xe5\x87\xbb\xe9\x87\x8f\xef\xbc\x9a1</div>
<table><tr><td>\xe5\x8d\x95\xe4\xbd\x8d\xe5\x90\x8d\xe7\xa7\xb0</td><td>Example Co</td></tr></table>
<div class="m-detail-content__container">\xe7\xbd\x91\xe7\x94\xb3\xe5\x9c\xb0\xe5\x9d\x80\xef\xbc\x9a<a href="https://jobs.example.com">apply</a></div>
"""


class NortheastBatchTests(unittest.TestCase):
    def test_jlu_parses_both_locales(self):
        rows = _list_records(JLU_LIST)
        self.assertEqual([row["published"] for row in rows], ["2025-9-1", "Sep 8, 2026"])
        self.assertEqual(_source_datetime("2026-9-8").day, 8)
        self.assertEqual(_source_datetime("Sep 8, 2026 7:47:26 PM").hour, 19)

    def test_jlu_detail(self):
        detail = _detail(JLU_DETAIL)
        self.assertEqual(detail["company"], "Example Co")
        self.assertIn("https://jobs.example.com", detail["application"])

    def test_hit_native_query(self):
        query = _query(3, {"company": "中国移动", "position": "AI"})
        self.assertEqual(query["dwmc"], "中国移动")
        self.assertEqual(query["zpxxmc"], "AI")
        self.assertEqual(query["skip"], 40)

    def test_hit_rejects_conflicting_title_filters(self):
        with self.assertRaises(ValueError):
            _query(1, {"position": "AI", "keyword": "算法"})


if __name__ == "__main__":
    unittest.main()
