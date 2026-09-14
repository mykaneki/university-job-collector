from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collectors import chd, cqu, nwpu, scu, swpu, xidian, xjtu


class WestBatchParserTests(unittest.TestCase):
    def test_xjtu_school_detail_url_contract(self) -> None:
        source_id = "abc123"
        self.assertEqual(
            f"{xjtu.BASE_URL}/f/recruitmentinfo/show?recruitmentId={source_id}",
            "https://job.xjtu.edu.cn/f/recruitmentinfo/show?recruitmentId=abc123",
        )
    def test_scu_list(self):
        html = '<div id="mycontent"><li><span class="list1_time">2026-09-08</span><a href="/index/index/employjobdetail.html?data=x">工程师【某公司】</a></li></div>'
        self.assertEqual(scu._list_records(html)[0]["company"], "某公司")

    def test_scu_detail_table_header_and_values(self):
        html = '''<div id="mycontent"><table><tr><td>职位名称</td><td>类型</td><td>性质</td><td>最低学历要求</td></tr><tr><td>工程师</td><td>技术</td><td>全职</td><td>硕士</td></tr><tr><td>发布时间</td><td colspan="3">2026-09-08 12:00:00</td></tr></table><table><tr><td>单位名称</td><td>某公司</td></tr><tr><td>单位性质</td><td>国有企业</td><td>单位行业</td><td>制造业</td></tr></table></div>'''
        detail = scu._detail(html)
        self.assertEqual((detail["类型"], detail["性质"], detail["最低学历要求"]), ("技术", "全职", "硕士"))

    def test_scu_company_filter_is_not_applied_before_detail(self):
        record = {"title": "工程师", "company": ""}
        self.assertTrue(scu._list_match(record, {"company": "详情页公司"}))

    def test_swpu_rendered_list_and_detail(self):
        html = '<ul id="data_html"><li class="item"><a class="item-link" href="/detail/news?id=1" title="招聘标题">标题</a><span class="item-time">2026-09-08</span></li></ul>'
        self.assertEqual(swpu._list_records(html)[0]["url"], "https://jyzx.swpu.edu.cn/detail/news?id=1")
        detail = '<div id="data_details"><h1 class="dh-tit">招聘标题</h1><p class="dh-info"><span class="time">2026年9月8日</span></p><div class="details-content">正文</div></div>'
        self.assertEqual(swpu._detail(detail)[:2], ("招聘标题", "2026-09-08"))

    def test_swpu_keyword_has_deterministic_local_verification(self):
        row = {"title": "人工智能工程师"}
        self.assertTrue(swpu._list_match(row, {"keyword": "人工智能"}))
        self.assertFalse(swpu._list_match(row, {"keyword": "量子"}))

    def test_nwpu_complete_rendered_dom(self):
        html = '''<div id="listPlace"><div class="infoItem"><div class="left"><a class="tit" title="招聘标题" href="/f/recruitmentinfo/show?recruitmentId=1">招聘标题</a><span class="time">2026-09-08</span></div><div class="mid"><a class="eName" title="某公司">某公司</a><p class="eNature">国有企业 | 1000以上</p></div></div></div>'''
        record = nwpu._list_records(html)[0]
        self.assertEqual((record["company"], record["company_nature"]), ("某公司", "国有企业"))

    def test_nwpu_native_mapping_and_detail(self):
        filters = '<label><input name="corporationNature" value="31">国有企业</label>'
        self.assertEqual(nwpu._native_value(filters, "corporationNature", "国有企业"), "31")
        detail = '<div class="positionDetail"><div class="name getCompany">标题</div></div><div class="midInfo"><div class="l_con">发布企业：某公司 日期：2026-09-08</div></div><div class="positionDetailMain"><div class="positionDetailLeft">JD</div></div>'
        parsed = nwpu._detail(detail)
        self.assertEqual((parsed["company"], parsed["published"]), ("某公司", "2026-09-08"))

    def test_nwpu_does_not_claim_unstructured_detail_filters(self):
        self.assertEqual(nwpu.SUPPORTED_FILTERS["location"], "unsupported")
        self.assertEqual(nwpu.SUPPORTED_FILTERS["education"], "unsupported")

    def test_nwpu_pagination_requires_explicit_terminal_state(self):
        middle = '<div class="pageWrap"><li class="active">2</li><a onclick="page(3,15,\'\')">下一页</a></div>'
        last = '<div class="pageWrap"><div class="dataNum">共 <span>30</span> 条记录</div><li class="active">2</li><a>下一页</a></div>'
        empty = '<div class="pageWrap"><div class="dataNum">共 <span>0</span> 条记录</div></div>'
        self.assertTrue(nwpu._pagination_has_next(middle, 2))
        self.assertFalse(nwpu._pagination_has_next(last, 2))
        self.assertEqual(nwpu._reported_total(empty), 0)

    def test_nwpu_pagination_error_is_persisted_and_raised(self):
        class Page:
            url = nwpu.ENTRY_URL

            def content(self):
                return '<div id="listPlace">上一页仍在</div>'

        with TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "翻到第 2 页失败"):
                nwpu._raise_pagination_error(Page(), raw_dir, 2, TimeoutError("xhr timeout"))
            self.assertTrue((raw_dir / "list_page_002.failed.html").exists())
            self.assertTrue((raw_dir / "list_page_002.meta.json").exists())

    def test_detail_error_capture_saves_rendered_dom_and_metadata(self):
        class Response:
            status = 503

        class Page:
            url = "https://example.edu/error"

            def content(self):
                return "<html><body>challenge</body></html>"

        for module in (scu, swpu, cqu, nwpu):
            with self.subTest(module=module.__name__), TemporaryDirectory() as directory:
                path = Path(directory) / "detail.failed.html"
                self.assertTrue(module._save_failed_render(Page(), path, "https://example.edu/detail", Response()))
                self.assertTrue(path.exists())
                self.assertTrue(path.with_name("detail.failed.meta.json").exists())

    def test_embedded_24365_record(self):
        decoded = '<ul><li><a href="/campus/view/id/1">标题</a></li><li>2026-09-08 10:00:00</li></ul>'
        original = cqu._blocks
        try:
            cqu._blocks = lambda _: decoded
            self.assertEqual(cqu._list("x")[0]["published"], "2026-09-08 10:00:00")
        finally:
            cqu._blocks = original

    def test_chd_detail(self):
        html = '<div class="info-title">招聘</div><div class="info-ct"><span>某公司</span><span>2026-09-08 12:00:00</span></div><div class="info-cont">JD</div>'
        self.assertEqual(chd._detail(html)[:3], ("招聘", "某公司", "2026-09-08 12:00:00"))


if __name__ == "__main__":
    unittest.main()
