from __future__ import annotations

import unittest
from urllib.parse import urlparse

from collectors import hrbeu, lzu, scut


class HrbeuLzuTests(unittest.TestCase):
    def test_hrbeu_uses_correct_frontpage_entry_and_filter_translation(self) -> None:
        self.assertIn("/frontpage/hrbeu/html/recruitmentinfoList.html", hrbeu.ENTRY_URL)
        options = {
            "corporationNature": [{"label": "国有企业", "value": "31"}],
            "industry": [{"label": "制造业", "value": "22"}],
        }
        payload = hrbeu._native_payload(
            {"position": "算法", "company_nature": "国有企业", "industry": "制造业"},
            options,
            3,
        )
        self.assertEqual(payload["pageNo"], 3)
        self.assertEqual(payload["title"], "算法")
        self.assertEqual(payload["corporationNature"], "31")
        self.assertEqual(payload["corporationinfo.industry"], "22")

    def test_hrbeu_simple_keeps_public_list_data_when_detail_is_restricted(self) -> None:
        item = hrbeu._simple_item({
            "corporationName": "某单位", "title": "2027 校园招聘",
            "startTime": "2026-09-08 12:00:00",
            "url": "/f/recruitmentinfo/show?recruitmentId=abc",
        })
        self.assertEqual(item["公司"], "某单位")
        self.assertEqual(item["JD"], "")
        self.assertEqual(item["投递方式"], "")
        self.assertIn("recruitmentId=abc", item["原始链接"])

    def test_hrbeu_restricted_requires_login_url_or_explicit_login_dom(self) -> None:
        self.assertTrue(hrbeu._is_login_page("https://job.hrbeu.edu.cn/a/login", "<html></html>"))
        self.assertTrue(hrbeu._is_login_page(
            "https://job.hrbeu.edu.cn/other",
            '<html><title>系统登录</title><div class="login"><a href="/a/login_student">登录</a></div></html>',
        ))
        self.assertFalse(hrbeu._is_login_page(
            "https://job.hrbeu.edu.cn/public/detail",
            '<html><title>招聘详情</title><div class="content">公开正文</div></html>',
        ))

    def test_lzu_list_parser_only_reads_primary_list(self) -> None:
        html = """
        <div class="lmain_item_con"><ul>
          <li><span>2026-09-08</span><a href="/html/74/article/2026/92032.html">招聘 A</a></li>
          <li><span>2026-09-07</span><a href="https://mp.weixin.qq.com/s/example">招聘 B</a></li>
        </ul></div>
        <div class="lastupdate"><ul><li><span>2026-09-08</span><a href="/noise.html">噪声</a></li></ul></div>
        """
        rows = lzu._parse_list(html)
        self.assertEqual([row["title"] for row in rows], ["招聘 A", "招聘 B"])
        self.assertEqual(rows[0]["url"], "https://job.lzu.edu.cn/html/74/article/2026/92032.html")

    def test_lzu_simple_contract_rejects_external_original_links(self) -> None:
        urls = [
            "https://job.lzu.edu.cn/html/74/article/2026/92032.html",
            "https://mp.weixin.qq.com/s/example",
        ]
        accepted = [url for url in urls if lzu._is_school_detail_url(url)]
        self.assertEqual(accepted, [urls[0]])
        self.assertTrue(all(urlparse(url).hostname == "job.lzu.edu.cn" for url in accepted))

    def test_lzu_detail_uses_explicit_content_container(self) -> None:
        html = '<div id="content"><p>正文</p></div><div class="lastupdate">站点噪声</div>'
        content = lzu._detail_content(html, "https://job.lzu.edu.cn/html/74/article/2026/1.html")
        self.assertIn("正文", content)
        self.assertNotIn("站点噪声", content)

    def test_scut_remains_explicitly_login_restricted(self) -> None:
        self.assertTrue(all(value == "unsupported" for value in scut.SUPPORTED_FILTERS.values()))
        self.assertEqual(scut.ENTRY_URL, "https://jyzx.scut.edu.cn/")


if __name__ == "__main__":
    unittest.main()
