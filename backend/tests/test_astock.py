"""a-stock-data 集成核心契约测试（零真实网络）。

覆盖 ``app.astock``（研报/两融/财联社电报 移植端点）与 ``app.services.astock_service``
（按日缓存 / 错误信封）的契约：代码归一化、请求参数、分页与空结果语义、
字段映射、签名 URL、北京时间换算、缓存命中与失败降级。

所有上游请求经 mock 注入假 ``httpx.Response`` / 假 fetch 函数。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from unittest import mock

import httpx

import app.astock.common as astock_common
import app.services.astock_service as astock_service
from app.astock import cls_telegraph, margin_trading, norm_ticker
from app.astock import eastmoney_reports, download_pdf
from app.astock.tickers import em_secid, get_prefix


def _json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


class TickerContractTest(unittest.TestCase):
    def test_norm_variants(self):
        self.assertEqual(norm_ticker("688017"), "688017")
        self.assertEqual(norm_ticker("SH688017"), "688017")
        self.assertEqual(norm_ticker("688017.SH"), "688017")
        self.assertEqual(norm_ticker("bj920982"), "920982")

    def test_norm_rejects_ambiguous_and_invalid(self):
        for bad in ("6005190", "茅台", "SH000001.SZ", ""):
            with self.assertRaises(ValueError):
                norm_ticker(bad)
        with self.assertRaises(ValueError):
            norm_ticker("SZ600519")               # 600519 是沪市
        with self.assertRaises(ValueError):
            norm_ticker("SH000001", stock_only=True)   # 上证指数

    def test_prefix_and_secid(self):
        self.assertEqual(get_prefix("510300"), "sh")   # 沪 ETF 不误判深市
        self.assertEqual(get_prefix("000001"), "sz")
        self.assertEqual(em_secid("600519"), "1.600519")
        self.assertEqual(em_secid("920982"), "0.920982")


class EastmoneyDatacenterContractTest(unittest.TestCase):
    def test_returns_rows_and_params(self):
        rows = [{"DATE": "2026-08-01 00:00:00", "RZYE": 100}]
        with mock.patch.object(
            astock_common,
            "em_get",
            return_value=_json_response({"result": {"data": rows}}),
        ) as em_get:
            out = astock_common.eastmoney_datacenter("RPTA_WEB_RZRQ_GGMX", page_size=30)
        self.assertEqual(out, rows)
        params = em_get.call_args.kwargs["params"]
        self.assertEqual(params["reportName"], "RPTA_WEB_RZRQ_GGMX")
        self.assertEqual(params["pageSize"], "30")
        self.assertEqual(params["source"], "WEB")

    def test_empty_envelope_returns_empty_list(self):
        for envelope in ({}, {"result": None}, {"result": {"data": None}}):
            with mock.patch.object(
                astock_common, "em_get", return_value=_json_response(envelope)
            ):
                self.assertEqual(astock_common.eastmoney_datacenter("X"), [])

    def test_403_not_retried(self):
        astock_common.set_em_min_interval(0)
        try:
            with mock.patch.object(astock_common, "get_client") as get_client:
                get_client.return_value.get.side_effect = [
                    _json_response({}, status=403)
                ]
                resp = astock_common.em_get("https://eastmoney.com/x", retries=3)
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(get_client.return_value.get.call_count, 1)
        finally:
            astock_common.set_em_min_interval(1.0)


class ReportsContractTest(unittest.TestCase):
    def _record(self, page: int, i: int) -> dict:
        return {"title": f"研报-{page}-{i}", "publishDate": f"2026-08-{i:02d} 00:00:00",
                "orgSName": "某券商", "infoCode": f"AP{i}{page}",
                "predictThisYearEps": 3.5, "emRatingName": "买入"}

    def test_code_normalized_and_referer_sent(self):
        with mock.patch.object(
            astock_common,
            "em_get",
            return_value=_json_response({"data": [self._record(1, 1)], "TotalPage": 1}),
        ) as em_get:
            out = eastmoney_reports("600519.SH", max_pages=1)
        self.assertEqual(len(out), 1)
        self.assertEqual(em_get.call_args.kwargs["params"]["code"], "600519")
        self.assertEqual(
            em_get.call_args.kwargs["headers"]["Referer"],
            "https://data.eastmoney.com/",
        )

    def test_multi_page_merge_and_empty_stop(self):
        pages = [
            {"data": [self._record(1, 1)], "TotalPage": 3},
            {"data": None},                          # 空页 → 停
        ]
        with mock.patch.object(
            astock_common, "em_get",
            side_effect=[_json_response(p) for p in pages],
        ) as em_get:
            out = eastmoney_reports("600519", max_pages=5)
        self.assertEqual(len(out), 1)
        self.assertEqual(em_get.call_count, 2)

    def test_legacy_bj_code_raises_instead_of_empty(self):
        with mock.patch.object(
            astock_common, "em_get", return_value=_json_response({"data": None})
        ):
            with self.assertRaises(ValueError):
                eastmoney_reports("832982")

    def test_invalid_or_index_code_raises(self):
        with self.assertRaises(ValueError):
            eastmoney_reports("茅台")
        with self.assertRaises(ValueError):
            eastmoney_reports("SH000001")

    def test_download_pdf_writes_and_reuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = {"infoCode": "AP1", "publishDate": "2026-01-01",
                   "orgSName": "某机构", "title": "标题/含非法符"}
            with mock.patch.object(
                astock_common,
                "em_get",
                return_value=httpx.Response(200, content=b"P" * 2048),
            ) as em_get:
                path = download_pdf(rec, target_dir=tmp)
            self.assertIsNotNone(path)
            with mock.patch.object(astock_common, "em_get") as em_get2:
                self.assertEqual(download_pdf(rec, target_dir=tmp), path)
            em_get2.assert_not_called()   # 已存在 → 不再请求


class MarginContractTest(unittest.TestCase):
    def test_normalization_and_mapping(self):
        rows = [{"DATE": "2026-08-01 00:00:00.000", "RZYE": 100, "RZMRE": 20,
                 "RZCHE": 15, "RQYE": 30, "RQMCL": 4, "RQCHL": 3, "RZRQYE": 130}]
        captured = {}

        def fake_em_get(url, params=None, headers=None, timeout=15, **kwargs):
            captured["params"] = params
            return _json_response({"result": {"data": rows}})

        with mock.patch.object(astock_common, "em_get", side_effect=fake_em_get):
            out = margin_trading("600519.SH")
        self.assertIn('SCODE="600519"', captured["params"]["filter"])
        self.assertNotIn(".SH", captured["params"]["filter"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["date"], "2026-08-01")
        self.assertEqual(out[0]["rzye"], 100)
        self.assertEqual(out[0]["rqye"], 30)
        self.assertEqual(out[0]["rzrqye"], 130)

    def test_missing_fields_default_zero_and_empty(self):
        def fake_em_get(url, params=None, headers=None, timeout=15, **kwargs):
            return _json_response({"result": {"data": [{"DATE": "2026-08-01"}]}})

        with mock.patch.object(astock_common, "em_get", side_effect=fake_em_get):
            out = margin_trading("000001")
        self.assertEqual(out[0]["rzye"], 0)
        self.assertEqual(out[0]["rzrqye"], 0)


class TelegraphContractTest(unittest.TestCase):
    def test_signed_url_order(self):
        captured = {}

        def fake_http_get(url, params=None, headers=None, timeout=15, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            return _json_response({"data": {"roll_data": []}})

        with mock.patch.object(astock_common, "http_get", side_effect=fake_http_get):
            out = cls_telegraph(page_size=50)
        self.assertEqual(out, [])
        params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
                  "last_time": "", "refresh_type": "1", "rn": "50"}
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
        self.assertIn(f"sign={sign}", captured["url"])
        keys = [kv.split("=")[0] for kv in captured["url"].split("?", 1)[1].split("&")]
        self.assertEqual(keys[-1], "sign")
        self.assertEqual(captured["headers"].get("Referer"), "https://www.cls.cn/")

    def test_beijing_wallclock_and_brief_fallback(self):
        import datetime as _dt
        ts = int(_dt.datetime(2026, 8, 1, 9, 30, 0, tzinfo=_dt.timezone.utc).timestamp())
        payload = {"data": {"roll_data": [
            {"ctime": ts, "title": "A", "content": "c", "brief": "b"},
            {"ctime": ts, "title": "", "content": "", "brief": "只有 brief"},
        ]}}
        with mock.patch.object(
            astock_common, "http_get", return_value=_json_response(payload)
        ):
            out = cls_telegraph(page_size=2)
        self.assertEqual(out[0]["time"], "2026-08-01 17:30:00")   # UTC 09:30 → 北京 17:30
        self.assertEqual(out[1]["title"], "只有 brief")


class AstockServiceTest(unittest.TestCase):
    """按日 JSON 缓存 / state=error 降级 / 电报 TTL。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from pathlib import Path
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        astock_service._telegraph_cache.update(ts=0.0, payload=None)

    def test_reports_cached_after_first_fetch(self):
        items = [{"title": "x", "publishDate": "2026-08-01"}]
        with mock.patch.object(astock_service, "_fetch_reports", return_value=items) as fetch:
            r1 = astock_service.get_reports(self.data_dir, "600519", max_pages=2)
            r2 = astock_service.get_reports(self.data_dir, "600519", max_pages=2)
        self.assertEqual(r1["state"], "ok")
        self.assertEqual(r1["count"], 1)
        self.assertEqual(r2, r1)
        fetch.assert_called_once()          # 第二次走缓存，不再打上游

    def test_reports_bad_code_raises_value_error(self):
        with self.assertRaises(ValueError):
            astock_service.get_reports(self.data_dir, "茅台")

    def test_margin_upstream_failure_returns_error_envelope(self):
        with mock.patch.object(
            astock_service, "_fetch_margin", side_effect=RuntimeError("boom")
        ):
            out = astock_service.get_margin(self.data_dir, "600519")
        self.assertEqual(out["state"], "error")
        self.assertIn("boom", out["message"])
        self.assertEqual(out["items"], [])

    def test_telegraph_ttl_reuses_memory_cache(self):
        items = [{"title": "t", "time": "2026-08-01 09:30:00"}]
        with mock.patch.object(astock_service, "_fetch_telegraph", return_value=items) as fetch:
            r1 = astock_service.get_telegraph(limit=10)
            r2 = astock_service.get_telegraph(limit=10)
        self.assertEqual(r1["state"], "ok")
        self.assertIs(r2, r1)
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
