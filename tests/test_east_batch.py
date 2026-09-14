from __future__ import annotations

import os
import signal
import tempfile
import threading
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit
from pathlib import Path
from unittest.mock import MagicMock, patch

from collectors import ouc
from collectors.ouc import _detail_text
from collectors.sdu import _rows
from collectors.ustc import (
    _is_pager_xhr,
    _list_signature,
    _rendered_meta,
    _rows as ustc_rows,
    _save_failure_snapshot,
    _save_observed_xhrs,
    _xhr_meta,
)
import common.browser as browser_common
from common.browser import ChromeCdpConfig, ChromeCdpSession, ChromeCleanupError
from discovery.inspect_browser import _request_log_payload, _safe_url


class EastBatchTests(unittest.TestCase):
    def test_chrome_session_enter_and_close_require_main_thread(self) -> None:
        session = ChromeCdpSession(ChromeCdpConfig(
            executable=Path("/tmp/test-chrome"), profile_dir=Path("/tmp/test-profile")
        ))
        errors: list[BaseException] = []

        def enter_in_worker() -> None:
            try:
                session.__enter__()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=enter_in_worker)
        worker.start()
        worker.join()
        self.assertEqual(len(errors), 1)
        self.assertIn("主线程", str(errors[0]))
        errors.clear()

        def close_in_worker() -> None:
            try:
                session.close()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=close_in_worker)
        worker.start()
        worker.join()
        self.assertEqual(len(errors), 1)
        self.assertIn("主线程", str(errors[0]))

    def test_chrome_profile_refuses_live_matching_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            session = ChromeCdpSession(ChromeCdpConfig(
                executable=Path("/tmp/test-chrome"), profile_dir=profile
            ))
            session._write_owner_state("running", 456789)
            command = f"/Applications/Google Chrome --user-data-dir={profile.resolve()} about:blank"
            with (
                patch.object(browser_common.os, "kill", return_value=None),
                patch.object(browser_common, "_pid_command", return_value=command),
            ):
                with self.assertRaisesRegex(RuntimeError, "只有确认该 PID 消失后"):
                    session._assert_previous_owner_stopped()

    def test_chrome_profile_allows_dead_owner_and_can_overwrite_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            session = ChromeCdpSession(ChromeCdpConfig(
                executable=Path("/tmp/test-chrome"), profile_dir=profile
            ))
            session._write_owner_state("running", 456789)
            with (
                patch.object(browser_common.os, "kill", side_effect=ProcessLookupError),
                patch.object(browser_common, "_pids_in_process_group", return_value=[]),
            ):
                session._assert_previous_owner_stopped()
            session._write_owner_state("running", 123)
            self.assertIn('"chrome_pid": 123', session._owner_path.read_text(encoding="utf-8"))

    def test_chrome_profile_refuses_residual_process_group_after_owner_pid_dies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            session = ChromeCdpSession(ChromeCdpConfig(
                executable=Path("/tmp/test-chrome"), profile_dir=profile
            ))
            session._write_owner_state("running", 456789)
            with (
                patch.object(browser_common.os, "kill", side_effect=ProcessLookupError),
                patch.object(browser_common, "_pids_in_process_group", return_value=[456790]),
            ):
                with self.assertRaisesRegex(RuntimeError, "PGID 456789.*残留进程"):
                    session._assert_previous_owner_stopped()

    def test_chrome_cdp_sessions_can_close_out_of_order(self) -> None:
        previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP)}
        first = ChromeCdpSession(ChromeCdpConfig(
            executable=Path("/tmp/test-chrome"), profile_dir=Path("/tmp/test-profile")
        ))
        second = ChromeCdpSession(ChromeCdpConfig(
            executable=Path("/tmp/test-chrome"), profile_dir=Path("/tmp/test-profile-2")
        ))
        browser_common._register_session(first)
        browser_common._register_session(second)
        try:
            for signum in previous:
                self.assertIs(signal.getsignal(signum), browser_common._handle_global_signal)
            first.close()
            for signum in previous:
                self.assertIs(signal.getsignal(signum), browser_common._handle_global_signal)
        finally:
            first.close()
            second.close()
        for signum, handler in previous.items():
            self.assertEqual(signal.getsignal(signum), handler)

    def test_chrome_cdp_repeated_signal_does_not_reenter_cleanup(self) -> None:
        with (
            patch.object(browser_common, "_HANDLING_SIGNAL", True),
            patch.object(browser_common, "_cleanup_active_sessions") as cleanup,
        ):
            browser_common._handle_global_signal(signal.SIGTERM, None)
        cleanup.assert_not_called()

    def test_chrome_permission_error_uses_direct_child_fallback(self) -> None:
        session = ChromeCdpSession(ChromeCdpConfig(
            executable=Path("/tmp/test-chrome"), profile_dir=Path("/tmp/test-profile")
        ))
        process = MagicMock()
        process.pid = 987654
        process.poll.return_value = 0
        session.process = process
        with (
            patch.object(browser_common.os, "killpg", side_effect=PermissionError("denied")),
            patch.object(browser_common, "_pids_in_process_group", return_value=[]),
        ):
            session._stop_process()
        process.terminate.assert_called_once()
        self.assertIsNone(session.process)

    def test_chrome_unconfirmed_cleanup_keeps_profile_lock(self) -> None:
        session = ChromeCdpSession(ChromeCdpConfig(
            executable=Path("/tmp/test-chrome"), profile_dir=Path("/tmp/test-profile")
        ))
        lock_file = MagicMock()
        session._lock_file = lock_file
        with patch.object(session, "_stop_process", side_effect=ChromeCleanupError("still running")):
            with self.assertRaises(ChromeCleanupError):
                session.close()
        lock_file.close.assert_not_called()
        self.assertIs(session._lock_file, lock_file)

    def test_chrome_cdp_config_uses_ujc_environment(self) -> None:
        values = {
            "UJC_CHROME_EXECUTABLE": "/tmp/test-chrome",
            "UJC_CHROME_PROFILE_DIR": "/tmp/test-ujc-profile",
            "UJC_CHROME_PROFILE_NAME": "Collector",
            "UJC_CHROME_DEBUG_HOST": "127.0.0.2",
            "UJC_CHROME_DEBUG_PORT": "0",
            "UJC_CHROME_START_TIMEOUT": "12.5",
            "UJC_CHROME_HEADLESS": "true",
        }
        with patch.dict(os.environ, values):
            config = ChromeCdpConfig.from_env()
        self.assertEqual(str(config.executable), "/tmp/test-chrome")
        self.assertEqual(str(config.profile_dir), "/tmp/test-ujc-profile")
        self.assertEqual(config.profile_name, "Collector")
        self.assertEqual(config.debug_host, "127.0.0.2")
        self.assertEqual(config.debug_port, 0)
        self.assertEqual(config.start_timeout, 12.5)
        self.assertTrue(config.headless)

    def test_chrome_cdp_refuses_non_loopback_debug_host(self) -> None:
        for host in ("0.0.0.0", "192.168.1.20", "example.com", "::1"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "只允许.*回环地址"):
                    ChromeCdpConfig(
                        executable=Path("/tmp/test-chrome"),
                        profile_dir=Path("/tmp/test-profile"),
                        debug_host=host,
                    )

    def test_chrome_cdp_accepts_supported_loopback_debug_hosts(self) -> None:
        for host, expected in (("localhost", "127.0.0.1"), ("127.0.0.1", "127.0.0.1"), ("127.0.0.2", "127.0.0.2")):
            with self.subTest(host=host):
                config = ChromeCdpConfig(
                    executable=Path("/tmp/test-chrome"),
                    profile_dir=Path("/tmp/test-profile"),
                    debug_host=host,
                )
                self.assertEqual(config.debug_host, expected)

    def test_chrome_cdp_rejects_fixed_port(self) -> None:
        for port in (-1, 1, 9333, 65535, 65536):
            with self.subTest(port=port):
                with self.assertRaisesRegex(ValueError, "DEBUG_PORT"):
                    ChromeCdpConfig(
                        executable=Path("/tmp/test-chrome"),
                        profile_dir=Path("/tmp/test-profile"),
                        debug_port=port,
                    )

    def test_chrome_cdp_ignores_stale_devtools_active_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            active_port = Path(tmpdir) / "DevToolsActivePort"
            active_port.write_text("9222\n/devtools/browser/old\n", encoding="utf-8")
            session = ChromeCdpSession(ChromeCdpConfig(
                executable=Path("/tmp/test-chrome"),
                profile_dir=Path(tmpdir),
                start_timeout=0.1,
            ))
            session.process = MagicMock()
            session.process.poll.return_value = None
            previous = session._active_port_signature(active_port)
            with (
                patch.object(browser_common.time, "monotonic", side_effect=[0.0, 0.0, 1.0]),
                patch.object(browser_common.time, "sleep"),
            ):
                with self.assertRaisesRegex(TimeoutError, "DevToolsActivePort"):
                    session._wait_for_debug_port(active_port, previous)
            self.assertIsNone(session.debug_port)
            self.assertIsNone(session._devtools_browser_path)

    def test_chrome_cdp_rejects_malformed_devtools_active_port(self) -> None:
        cases = (
            "not-a-port\n/devtools/browser/id\n",
            "70000\n/devtools/browser/id\n",
            "9222\n/not-a-browser-path\n",
        )
        for content in cases:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmpdir:
                active_port = Path(tmpdir) / "DevToolsActivePort"
                active_port.write_text(content, encoding="utf-8")
                session = ChromeCdpSession(ChromeCdpConfig(
                    executable=Path("/tmp/test-chrome"),
                    profile_dir=Path(tmpdir),
                    start_timeout=0.1,
                ))
                session.process = MagicMock()
                session.process.poll.return_value = None
                with (
                    patch.object(browser_common.time, "monotonic", side_effect=[0.0, 0.0, 1.0]),
                    patch.object(browser_common.time, "sleep"),
                ):
                    with self.assertRaisesRegex(TimeoutError, "DevToolsActivePort"):
                        session._wait_for_debug_port(active_port, None)
                self.assertIsNone(session.debug_port)
                self.assertIsNone(session._devtools_browser_path)

    def test_chrome_cdp_rejects_mismatched_websocket_browser_path(self) -> None:
        session = ChromeCdpSession(ChromeCdpConfig(
            executable=Path("/tmp/test-chrome"),
            profile_dir=Path("/tmp/test-profile"),
            start_timeout=0.1,
        ))
        session.process = MagicMock()
        session.process.poll.return_value = None
        session.debug_port = 9222
        session._devtools_browser_path = "/devtools/browser/expected"
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        with (
            patch.object(browser_common.urllib.request, "build_opener", return_value=opener),
            patch.object(
                browser_common.json,
                "load",
                return_value={
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/other"
                },
            ),
            patch.object(browser_common.time, "monotonic", side_effect=[0.0, 0.0, 1.0]),
            patch.object(browser_common.time, "sleep"),
        ):
            with self.assertRaisesRegex(TimeoutError, "CDP.*last_error"):
                session._wait_until_ready()

    def test_chrome_cdp_refuses_daily_chrome_profile(self) -> None:
        with patch.object(browser_common.Path, "home", return_value=Path("/Users/tester")):
            with self.assertRaisesRegex(ValueError, "拒绝使用日常 Chrome profile"):
                browser_common._assert_dedicated_profile_dir(
                    Path("/Users/tester/Library/Application Support/Google/Chrome/Default")
                )

    def test_chrome_cdp_refuses_profile_name_path_traversal(self) -> None:
        for name in ("", ".", "..", "../Default", "nested/Default", "nested\\Default", "/tmp/Default"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "PROFILE_NAME"):
                    ChromeCdpConfig(
                        executable=Path("/tmp/test-chrome"),
                        profile_dir=Path("/tmp/test-profile"),
                        profile_name=name,
                    )

    def test_chrome_cdp_refuses_profile_symlink_to_daily_chrome(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            daily = home / "Library/Application Support/Google/Chrome/Default"
            daily.mkdir(parents=True)
            dedicated = Path(tmpdir) / "dedicated"
            dedicated.mkdir()
            (dedicated / "Collector").symlink_to(daily, target_is_directory=True)
            with patch.object(browser_common.Path, "home", return_value=home):
                with self.assertRaisesRegex(ValueError, "拒绝使用日常 Chrome profile"):
                    browser_common._assert_dedicated_profile_dir(dedicated, "Collector")

    @unittest.skipIf(browser_common.pwd is None, "POSIX pwd 不可用")
    def test_chrome_cdp_refuses_real_home_profile_when_home_env_is_overridden(self) -> None:
        real_home = Path("/Users/real-user")
        account = SimpleNamespace(pw_dir=str(real_home))
        with (
            patch.object(browser_common.Path, "home", return_value=Path("/tmp/overridden-home")),
            patch.object(browser_common.pwd, "getpwuid", return_value=account),
        ):
            with self.assertRaisesRegex(ValueError, "拒绝使用日常 Chrome profile"):
                browser_common._assert_dedicated_profile_dir(
                    real_home / "Library/Application Support/Google/Chrome/Default",
                    "Default",
                )

    def test_chrome_cdp_accepts_dedicated_profile_outside_daily_root(self) -> None:
        with patch.object(browser_common.Path, "home", return_value=Path("/Users/tester")):
            browser_common._assert_dedicated_profile_dir(Path("/tmp/ujc-dedicated-profile"))

    def test_browser_probe_hides_post_data_by_default(self) -> None:
        request = MagicMock(
            method="POST",
            url="https://example.com/api",
            post_data='{"password":"secret"}',
        )
        payload = _request_log_payload(request, show_post_data=False)
        self.assertNotIn("post_data", payload)
        self.assertTrue(payload["has_post_data"])
        self.assertGreater(payload["post_data_bytes"], 0)

    def test_browser_probe_requires_explicit_flag_to_show_post_data(self) -> None:
        request = MagicMock(
            method="POST",
            url="https://example.com/api",
            post_data="page=2",
        )
        payload = _request_log_payload(request, show_post_data=True)
        self.assertEqual(payload["post_data"], "page=2")

    def test_browser_probe_redacts_sensitive_url_values(self) -> None:
        safe = _safe_url(
            "https://user:secret@example.com/api?page=2&access_token=abc&email=a%40b.test#bearer"
        )
        self.assertNotIn("user", safe)
        self.assertNotIn("secret", safe)
        self.assertNotIn("abc", safe)
        self.assertNotIn("a%40b.test", safe)
        self.assertNotIn("bearer", safe)
        self.assertIn("page=2", safe)
        self.assertIn("access_token=%3Credacted%3E", safe)

    def test_browser_probe_preserves_non_sensitive_exploration_values(self) -> None:
        safe = _safe_url(
            "https://example.com/api?keyword=AI&schoolCode=10710&industryCode=31&positionCode=A1"
        )
        self.assertIn("keyword=AI", safe)
        self.assertIn("schoolCode=10710", safe)
        self.assertIn("industryCode=31", safe)
        self.assertIn("positionCode=A1", safe)

    def test_browser_probe_redacts_cas_and_federated_login_values(self) -> None:
        safe = _safe_url(
            "https://example.com/callback?ticket=ST-1&jwt=abc&credential=cred"
            "&SAMLart=artifact&assertion=signed"
        )
        values = dict(parse_qsl(urlsplit(safe).query))
        self.assertEqual(set(values.values()), {"<redacted>"})

    def test_ustc_rows_use_rendered_dom_fields(self) -> None:
        html = """
        <div id="zpgg"><ul><li>
          <p class="pa"><a href="/Recruitment/info.aspx?itemid=123">测试招聘公告</a></p>
          <time>更新时间：<span>2026-09-08</span></time>
        </li></ul></div>
        """
        self.assertEqual(ustc_rows(html), [{
            "title": "测试招聘公告",
            "date": "2026-09-08",
            "url": "https://www.job.ustc.edu.cn/Recruitment/info.aspx?itemid=123",
        }])

    def test_ustc_list_signature_uses_every_row(self) -> None:
        first = [
            {"url": "https://example/1", "title": "相同首条", "date": "2026-09-08"},
            {"url": "https://example/2", "title": "第二条甲", "date": "2026-09-08"},
        ]
        second = [first[0], {"url": "https://example/3", "title": "第二条乙", "date": "2026-09-08"}]
        self.assertNotEqual(_list_signature(first), _list_signature(second))

    def test_ustc_xhr_metadata_preserves_request_and_response(self) -> None:
        class Request:
            method = "POST"
            post_data = "action=joblist2&pageindex=2&pagesize=10"
            resource_type = "xhr"

        class Response:
            url = "https://www.job.ustc.edu.cn/Ajax/jywapi.ashx"
            status = 200
            headers = {"content-type": "text/plain; charset=utf-8"}
            request = Request()

        metadata = _xhr_meta(Response(), 2)
        self.assertEqual(metadata["method"], "POST")
        self.assertEqual(metadata["status_code"], 200)
        self.assertEqual(metadata["content_type"], "text/plain; charset=utf-8")
        self.assertEqual(metadata["payload"], "action=joblist2&pageindex=2&pagesize=10")
        self.assertEqual(metadata["artifact_type"], "http_response_body")

    def test_ustc_rendered_metadata_preserves_non_200_status(self) -> None:
        class Request:
            method = "POST"
            post_data = "action=joblist2&pageindex=2&pagesize=10"

        class Response:
            url = "https://www.job.ustc.edu.cn/Ajax/jywapi.ashx"
            status = 403
            headers = {"content-type": "text/html; charset=utf-8"}
            request = Request()

        metadata = _rendered_meta("https://example/rendered-page", Response())
        self.assertEqual(metadata["url"], "https://example/rendered-page")
        self.assertEqual(metadata["method"], "BROWSER")
        self.assertNotIn("status_code", metadata)
        self.assertEqual(metadata["trigger_response"]["status_code"], 403)
        self.assertEqual(metadata["trigger_response"]["url"], Response.url)
        self.assertEqual(metadata["artifact_type"], "rendered_dom")

    def test_ustc_failure_snapshot_saves_observed_xhr_dom_and_error(self) -> None:
        class Request:
            method = "POST"
            post_data = "action=joblist2&pageindex=2&pagesize=10"

        class Response:
            url = "https://www.job.ustc.edu.cn/Ajax/jywapi.ashx"
            status = 503
            headers = {"content-type": "text/plain; charset=utf-8"}
            request = Request()

            def body(self) -> bytes:
                return b'{"r":1,"error":"busy"}'

        page = MagicMock()
        page.url = "https://www.job.ustc.edu.cn/Recruitment/list.aspx?pageindex=2"
        page.content.return_value = "<html><body>visible failure</body></html>"
        response = Response()
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            _save_failure_snapshot(
                raw_dir,
                page,
                "list_page_002",
                RuntimeError("xhr failed"),
                [response],
                set(),
                response,
                "https://www.job.ustc.edu.cn/Recruitment/list.aspx",
            )
            self.assertTrue((raw_dir / "list_api_page_002.json").is_file())
            self.assertTrue((raw_dir / "list_api_page_002.meta.json").is_file())
            self.assertIn("visible failure", (raw_dir / "list_page_002.html").read_text())
            self.assertTrue((raw_dir / "list_page_002_error.meta.json").is_file())

    def test_ustc_observed_xhrs_preserve_every_attempt_in_order(self) -> None:
        class Request:
            method = "POST"
            post_data = "action=joblist2&pageindex=2&pagesize=10"
            resource_type = "xhr"

        class Response:
            url = "https://www.job.ustc.edu.cn/Ajax/jywapi.ashx"
            headers = {"content-type": "application/json"}
            request = Request()

            def __init__(self, status: int, body: bytes) -> None:
                self.status = status
                self._body = body

            def body(self) -> bytes:
                return self._body

        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            saved: set[int] = set()
            responses = [Response(503, b'first'), Response(200, b'second')]
            _save_observed_xhrs(raw_dir, responses, saved)
            self.assertEqual((raw_dir / "list_api_page_002.json").read_bytes(), b"first")
            self.assertEqual(
                (raw_dir / "list_api_page_002_attempt_002.json").read_bytes(), b"second"
            )

    def test_ustc_goto_failure_snapshot_preserves_observed_document_body(self) -> None:
        class Request:
            method = "GET"
            post_data = None
            resource_type = "document"

        class Response:
            url = "https://www.job.ustc.edu.cn/Recruitment/list.aspx"
            status = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            request = Request()

            def body(self) -> bytes:
                return b"<html>original response</html>"

        page = MagicMock()
        page.url = Response.url
        page.content.return_value = "<html>rendered timeout state</html>"
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            response = Response()
            _save_failure_snapshot(
                raw_dir,
                page,
                "list_page_001",
                TimeoutError("DOMContentLoaded timeout"),
                [response],
                set(),
                None,
                Response.url,
                [response],
            )
            self.assertEqual(
                (raw_dir / "list_page_001_response.html").read_bytes(),
                b"<html>original response</html>",
            )
            self.assertIn("rendered timeout state", (raw_dir / "list_page_001.html").read_text())

    def test_ustc_pager_xhr_is_distinct_from_list_xhr(self) -> None:
        class Request:
            method = "POST"
            post_data = "action=getpage&pageindex=2&pagesize=10&total=696"

        class Response:
            url = "https://www.job.ustc.edu.cn/Ajax/jywapi.ashx"
            request = Request()

        self.assertTrue(_is_pager_xhr(Response(), 2))
        self.assertFalse(_is_pager_xhr(Response(), 3))

    def test_ouc_uses_real_jqgrid_pagination_parameters(self) -> None:
        params = ouc._list_params(2, {"company": "中国移动", "position": "AI"})
        self.assertEqual(params["queryModel.currentPage"], 2)
        self.assertEqual(params["queryModel.showCount"], ouc.PAGE_SIZE)
        self.assertEqual(params["queryModel.sortName"], "fbsj")
        self.assertEqual(params["queryModel.sortOrder"], "desc")
        self.assertEqual(params["dwmc"], "中国移动")
        self.assertEqual(params["zwmc"], "AI")
        self.assertNotIn("page", params)
        self.assertNotIn("rows", params)

    def test_sdu_rows_use_explicit_list_cells(self) -> None:
        html = """
        <div class="moreListR-long"><ul><li>
        <a onclick="viewZpxx('abc', 'jygl_zpxxck')">2026-09-08</a>
        <a onclick="viewZpxx('abc', 'jygl_zpxxck')">测试招聘</a>
        <a onclick="viewZpxx('abc', 'jygl_zpxxck')">国有企业</a>
        </li></ul></div>
        """
        self.assertEqual(_rows(html), [{"id": "abc", "date": "2026-09-08", "title": "测试招聘", "nature": "国有企业"}])

    def test_ouc_detail_text_drops_site_footer(self) -> None:
        html = """
        <body>导航<h1>招聘信息</h1><p>RECRUITMENT INFORMATIOM</p>
        <p>职位描述：测试正文</p><p>版权所有：测试大学</p></body>
        """
        result = _detail_text(html)
        self.assertIn("测试正文", result)
        self.assertNotIn("导航", result)
        self.assertNotIn("版权所有", result)


if __name__ == "__main__":
    unittest.main()
