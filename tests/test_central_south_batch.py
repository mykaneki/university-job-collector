import unittest

from collectors import csu, hust, sysu, xmu


class CentralSouthBatchTests(unittest.TestCase):
    def test_hust_list_parser(self) -> None:
        html = b'<ul class="zplist"><li><a href="/zpinfo1/123.htm">Example</a><span class="n2">[2026-09-08]</span></li></ul>'
        self.assertEqual(hust._records(html), [{"title": "Example", "date": "2026-09-08", "url": "https://job.hust.edu.cn/zpinfo1/123.htm"}])

    def test_csu_list_parser(self) -> None:
        html = '<div class="campus-index-front-list"><ul class="infoList"><li><a href="/campus/view/id/1">单位</a></li><li>企业招聘</li><li>2026-09-08 12:00:00</li></ul></div>'.encode()
        record = csu._records(html)[0]
        self.assertEqual(record["title"], "单位")
        self.assertEqual(record["date"], "2026-09-08 12:00:00")

    def test_packed_html_decoders(self) -> None:
        source = _pack("<ul><li>ok</li></ul>", 7, 9)
        self.assertEqual(sysu._unpack(source), "<ul><li>ok</li></ul>")
        self.assertEqual(xmu._unpack(source), "<ul><li>ok</li></ul>")
        self.assertEqual(csu._unpack(source), "<ul><li>ok</li></ul>")


def _pack(payload: str, cut1: int, cut2: int) -> str:
    import base64
    import zlib
    middle = "x" * cut2 + payload
    first = "y" * cut1 + base64.b64encode(middle.encode()).decode()
    packed = base64.b64encode(zlib.compress(first.encode())).decode()
    return f'Base64.decode(unzip("{packed}").substr({cut1})).substr({cut2})'
