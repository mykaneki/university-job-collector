from __future__ import annotations

import base64
import json
import tempfile
import unittest
import zlib
from pathlib import Path

from Crypto.Cipher import AES

from collectors.cup import _list_records as parse_cup_list
from collectors.ruc import _matches as ruc_matches
from collectors.tsinghua import _parse_detail, _parse_list
from collectors.uibe import IV, KEY, _decrypt as decrypt_uibe
from common.filters import parse_filter_args
from common.html import extract_application_methods
from common.output import SIMPLE_KEYS, normalize_simple
from common.time import parse_cli_datetime


class CoreTests(unittest.TestCase):
    def test_end_date_means_end_of_day(self) -> None:
        parsed = parse_cli_datetime("2026-09-08", is_end=True)
        self.assertEqual(parsed.strftime("%H:%M:%S"), "23:59:59")

    def test_duplicate_filter_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能重复"):
            parse_filter_args(["company=A", "company=B"])

    def test_email_extraction_does_not_swallow_chinese_context(self) -> None:
        result = extract_application_methods("可直接投递简历至zhaopin@example.com")
        self.assertEqual(result, "邮箱：zhaopin@example.com")

    def test_malformed_mailto_keeps_only_valid_email(self) -> None:
        html = '<a href="mailto:请将简历发送至：hr@example.com">投递简历</a>'
        self.assertEqual(extract_application_methods(html), "邮箱：hr@example.com")

    def test_percent_encoded_mailto_context_keeps_only_real_email(self) -> None:
        html = (
            '<a href="mailto:%E8%AF%B7%E5%B0%86%E7%AE%80%E5%8E%86%E5%8F%91%E9%80%81%E8%87%B3%EF%BC%9A%20'
            'recruiting@example.com">请将简历发送至 recruiting@example.com</a>'
        )
        self.assertEqual(extract_application_methods(html), "邮箱：recruiting@example.com")

    def test_percent_encoded_url_fragment_is_not_an_email(self) -> None:
        result = extract_application_methods(
            "简历投递：https://jobs.example.com/%E8%AF%B7%E5%8F%91%E9%80%81%20fake@example.com"
        )
        self.assertNotIn("邮箱：", result)
        self.assertIn("网申地址：https://jobs.example.com/", result)

    def test_url_extraction_stops_before_chinese_context(self) -> None:
        result = extract_application_methods("请到https://jobs.example.com/apply进行简历投递")
        self.assertEqual(result, "网申地址：https://jobs.example.com/apply")

    def test_simple_output_is_strict_deduplicated_and_sorted(self) -> None:
        items = [
            {"公司": "A", "岗位": "old", "岗位上新时间": "2026-09-07", "原始链接": "https://x/1", "extra": 1},
            {"公司": "B", "岗位": "new", "岗位上新时间": "2026-09-08 12:00:00", "原始链接": "https://x/2"},
            {"公司": "C", "岗位": "duplicate", "岗位上新时间": "2026-09-09", "原始链接": "https://x/1"},
        ]
        result = normalize_simple(items)
        self.assertEqual([item["岗位"] for item in result], ["new", "old"])
        self.assertEqual(tuple(result[0]), SIMPLE_KEYS)

    def test_tsinghua_list_parser_preserves_ahref_and_company(self) -> None:
        html = b"""
        <b id="totalPg">9</b><ul id="todayList"><li class="clearfix">
        <span>2026-09-08</span><a ahref="/show?zpxxid=12" href="javascript:void(0)"
        style="color:#ff0000;" fbfw="\xe5\x86\x85">Role\xe2\x80\x94\xe2\x80\x94\xe2\x80\x94\xe2\x80\x94Company</a></li></ul>
        """
        records, pages = _parse_list(html)
        self.assertEqual(pages, 9)
        self.assertEqual(records[0]["title"], "Role")
        self.assertEqual(records[0]["company"], "Company")
        self.assertEqual(records[0]["source_id"], "12")
        self.assertTrue(records[0]["pinned"])

    def test_tsinghua_detail_extracts_only_position_description(self) -> None:
        html = """
        <div class="content teacher">
          <p>公司名称： 测试公司</p>
          <h2 class="company-headline">职位描述</h2>
          <p>完整正文<br>简历投递：job@example.com</p>
          <p>投递通道</p><p><a href="https://jobs.example.com">招聘官网</a></p>
          <h2 class="company-headline">单位简介</h2><p>不应进入 JD</p>
        </div>
        """.encode()
        company, jd, methods, accessible = _parse_detail(html)
        self.assertTrue(accessible)
        self.assertEqual(company, "测试公司")
        self.assertIn("完整正文", jd)
        self.assertNotIn("不应进入", jd)
        self.assertIn("job@example.com", methods)
        self.assertIn("https://jobs.example.com", methods)

    def test_uibe_aes_response_decryption(self) -> None:
        expected = {"msg": "Y", "data": [{"title": "测试招聘"}]}
        plaintext = json.dumps(expected, ensure_ascii=False).encode()
        padded = plaintext + b"\x00" * (-len(plaintext) % AES.block_size)
        encrypted = AES.new(KEY, AES.MODE_CBC, IV).encrypt(padded)
        self.assertEqual(decrypt_uibe(base64.encodebytes(encrypted)), expected)

    def test_cup_embedded_list_decoding(self) -> None:
        inner = """
        <ul class="infoList"><li><span class="status-ding">顶</span>
        <a href="/campus/view/id/42">测试公告</a></li>
        <li>2026-09-08 12:34:56</li></ul>
        """
        decoded_offset = 18
        decoded = "x" * decoded_offset + inner
        inflated_offset = 77
        inflated = "x" * inflated_offset + base64.b64encode(decoded.encode()).decode()
        encoded = base64.b64encode(zlib.compress(inflated.encode())).decode()
        html = f'<script>Base64.decode(unzip("{encoded}").substr({inflated_offset})).substr({decoded_offset})</script>'
        records = parse_cup_list(html)
        self.assertEqual(records[0]["title"], "测试公告")
        self.assertEqual(records[0]["published"], "2026-09-08 12:34:56")
        self.assertTrue(records[0]["is_top"])

    def test_ruc_local_filters_use_explicit_fields(self) -> None:
        record = {"positionName": "AI 工程师", "employerName": "测试国企", "workLocation": "北京市", "educationName": "硕士,博士"}
        self.assertTrue(ruc_matches(record, {"company": "国企", "position": "AI", "location": "北京", "education": "硕士"}))
        self.assertFalse(ruc_matches(record, {"industry": "金融"}))

    def test_nankai_list_parser_uses_explicit_fields(self) -> None:
        from collectors.nankai import _list_records

        html = """
        <div class="newslist zhuanlan"><div class="zl1"><div class="content"><ul><li>
        <div class="date"><span class="day">08</span><span class="year">2026.09</span></div>
        <div class="title1"><a href="/correcruit/content/id/42.html">测试岗位</a></div>
        <div class="company"><a href="/company/index/id/1.html">【测试公司】</a>北京市 / 硕士研究生</div>
        </li></ul></div></div></div>
        """
        record = _list_records(html)[0]
        self.assertEqual(record["published"], "2026-09-08")
        self.assertEqual(record["company"], "测试公司")
        self.assertEqual(record["title"], "测试岗位")

    def test_tju_homepage_parser_excludes_other_tabs(self) -> None:
        from collectors.tju import _list_records

        html = """
        <ul class="ullistcenter list3"><li><a href="/correcruit/content/id/7.html">
        <p class="date">09.08</p><div class="zpname"><p>公开岗位</p><span>公开公司</span></div></a></li></ul>
        <ul class="ullistcenter list4"><li><a href="/correcruit/content/id/8.html">
        <p class="date">09.08</p><div class="zpname"><p>实习岗位</p><span>实习公司</span></div></a></li></ul>
        """
        reference = parse_cli_datetime("2026-09-08 12:00:00")
        records = _list_records(html, reference)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "公开岗位")
        self.assertEqual(records[0]["published"], "2026-09-08")

    def test_sjtu_homepage_parser_uses_only_recruitment_section(self) -> None:
        from collectors.sjtu import _list_records

        html = """
        <div class="inviteTypeCon"><div class="inviteTypeConListLeft"
        onclick='windowOpen("\\/career\\/zpxx\\/view\\/zpxx\\/42")'>
        <div class="sxhrq">2026</div><div class="sxhsj">09-08</div>
        <div class="inviteAddress"><div class="inviteUnit">测试招聘</div>
        <div class="inviteUnit">测试公司</div></div></div></div>
        <div class="inviteTypeCon"><div class="inviteTypeConListLeft"
        onclick='windowOpen("/career/zpxx/view/zpxx/43")'>
        <div class="sxhrq">2026</div><div class="sxhsj">09-08</div>
        <div class="inviteAddress"><div class="inviteUnit">实习信息</div></div></div></div>
        """
        records = _list_records(html)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source_id"], "42")
        self.assertEqual(records[0]["company"], "测试公司")

    def test_ecnu_homepage_parser_uses_active_grid(self) -> None:
        from collectors.ecnu import _list_records

        html = """
        <div class="jobs-grid jobs-grid--active"><article class="job-card">
        <div class="job-card__body" data-url="/career/zwxx/view/abc"></div>
        <h3 class="job-card__title">测试岗位</h3><p class="job-card__company">测试公司</p>
        <span class="job-card__date">2026-09-08</span></article></div>
        <div class="jobs-grid"><article class="job-card"><div class="job-card__body"
        data-url="/career/zwxx/view/ignored"></div><h3 class="job-card__title">忽略</h3>
        <span class="job-card__date">2026-09-08</span></article></div>
        """
        records = _list_records(html)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source_id"], "abc")
        self.assertEqual(records[0]["title"], "测试岗位")

    def test_nju_filters_decode_explicit_company_metadata(self) -> None:
        from collectors.nju import _config_maps, _matches

        mappings = _config_maps({
            "company_industry": {"value": [{"code": "35", "name": "金融业"}]},
            "company_property": {"value": [{"code": "31", "name": "国有企业"}]},
        })
        record = {
            "theme": "银行校园招聘",
            "intro": "招聘正文",
            "company": {
                "name": "测试银行",
                "industry": {"value": "35"},
                "property": {"value": "31"},
            },
            "positions": [{
                "position": {"name": "科技岗", "education": "硕士", "fullTime": True},
                "region": {"province": "江苏省", "city": "南京市"},
            }],
        }
        self.assertTrue(_matches(record, {
            "company": "银行", "position": "科技", "location": "南京",
            "company_nature": "国有企业", "industry": "金融", "education": "硕士",
            "employment_type": "全职",
        }, mappings))


if __name__ == "__main__":
    unittest.main()
