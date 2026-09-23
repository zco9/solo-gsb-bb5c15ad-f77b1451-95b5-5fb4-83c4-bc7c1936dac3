# -*- coding: utf-8 -*-
"""POST /verify-fetch-batch 的行为测试。

覆盖：
- 成功路径：206 + Content-Range + 强 ETag + 字节数全部一致时，返回按块编号
  升序排列、可直接放入 /assemble.contents 的数据与确认的 ETag；
- 与 /plan、/assemble 的端到端串联（验证产物可直接重组）；
- 状态码非 206、Content-Range 格式/闭区间/对象总长不符、Base64 非法、
  响应体字节数不符、缺块、重复块、多余块等 422；
- 弱 ETag（W/）、非法 ETag 语法、块间 ETag 版本不一致、expected_etag
  自身非法或与确认 ETag 不一致均拒绝；
- 空规划（blocks 为空）成功返回 etag=null；
- 任何 422 错误响应均不回显响应体（Base64）。

直接运行：python -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import unittest

from app.main import app

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover - 测试依赖缺失时给出明确提示
    TestClient = None  # type: ignore[assignment]


OBJECT_LENGTH = 100
STRONG_ETAG = '"v1-20260923"'
OTHER_ETAG = '"v2-other"'


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class VerifyFetchBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        plan_resp = self.client.post(
            "/plan",
            json={
                "object_length": OBJECT_LENGTH,
                # 三个相邻区间各自不超过上限；相邻合并后超 40 故仍拆为独立块：
                # [0,39]、[40,79]、[80,99]。
                "ranges": [
                    {"type": "interval", "start": 0, "end": 39},
                    {"type": "interval", "start": 40, "end": 79},
                    {"type": "interval", "start": 80, "end": 99},
                ],
                "max_gap": 0,
                "fetch_limit": 40,
            },
        )
        self.assertEqual(plan_resp.status_code, 200, plan_resp.text)
        self.plan = plan_resp.json()
        # 100 字节按 40 上限拆为 [0,39]、[40,79]、[80,99]。
        self.assertEqual(
            [(b["start"], b["end"]) for b in self.plan["blocks"]],
            [(0, 39), (40, 79), (80, 99)],
        )
        self.blocks = self.plan["blocks"]
        # 确定性的对象字节：data[i] = i mod 256。
        self.data = bytes(range(OBJECT_LENGTH))

    def block_bytes(self, block: dict) -> bytes:
        return self.data[block["start"] : block["end"] + 1]

    def make_response(
        self,
        block: dict,
        *,
        status_code: int = 206,
        content_range: str | None = None,
        etag: str = STRONG_ETAG,
        body: bytes | None = None,
    ) -> dict:
        start, end = block["start"], block["end"]
        return {
            "block_index": block["index"],
            "status_code": status_code,
            "content_range": content_range
            if content_range is not None
            else f"bytes {start}-{end}/{OBJECT_LENGTH}",
            "etag": etag,
            "body_b64": b64(body if body is not None else self.block_bytes(block)),
        }

    def make_request(self, responses: list[dict] | None = None, **over) -> dict:
        req = {
            "object_length": OBJECT_LENGTH,
            "blocks": self.blocks,
            "responses": (
                [self.make_response(b) for b in self.blocks]
                if responses is None
                else responses
            ),
        }
        req.update(over)
        return req

    def verify(self, req: dict):
        return self.client.post("/verify-fetch-batch", json=req)

    # ------------------------------ 成功路径 ------------------------------

    def test_success_returns_contents_sorted_and_etag(self) -> None:
        # responses 故意乱序提交，响应必须按 block_index 升序。
        responses = [self.make_response(b) for b in self.blocks]
        responses.reverse()
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["object_length"], OBJECT_LENGTH)
        self.assertEqual(body["etag"], STRONG_ETAG)
        self.assertEqual([c["block_index"] for c in body["contents"]], [0, 1, 2])
        for content, block in zip(body["contents"], self.blocks):
            self.assertEqual(content["data_b64"], b64(self.block_bytes(block)))

    def test_success_with_matching_expected_etag(self) -> None:
        # expected_etag 与各块 ETag 一致（允许首尾空白）。
        resp = self.verify(self.make_request(expected_etag=f"  {STRONG_ETAG}\t"))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["etag"], STRONG_ETAG)

    def test_etag_with_ows_is_normalized(self) -> None:
        # 各块 ETag 头值带不同首尾空白，去空白后为同一强标签即通过。
        responses = [
            self.make_response(b, etag=(" " * (i + 1)) + STRONG_ETAG + "\t")
            for i, b in enumerate(self.blocks)
        ]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["etag"], STRONG_ETAG)

    def test_empty_blocks_and_responses(self) -> None:
        # 全部命中缓存、没有回源块时，空列表合法；无 ETag 可确认，返回 null。
        resp = self.verify(
            {"object_length": OBJECT_LENGTH, "blocks": [], "responses": []}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"object_length": OBJECT_LENGTH, "etag": None, "contents": []})

    def test_end_to_end_verified_contents_feed_assemble(self) -> None:
        verified = self.verify(self.make_request()).json()
        assemble_req = {
            "object_length": OBJECT_LENGTH,
            "blocks": self.plan["blocks"],
            "mappings": self.plan["mappings"],
            "contents": verified["contents"],
        }
        resp = self.client.post("/assemble", json=assemble_req)
        self.assertEqual(resp.status_code, 200, resp.text)
        ranges = sorted(resp.json()["ranges"], key=lambda r: r["index"])
        self.assertEqual([r["index"] for r in ranges], [0, 1, 2])
        payload = b"".join(base64.b64decode(r["data_b64"]) for r in ranges)
        self.assertEqual(payload, self.data)

    def test_opaque_etag_with_non_ascii_is_accepted(self) -> None:
        etag = '"ver-\xe9\x80\x9a"'  # RFC 7232 允许 0x80-0xFF 的 etagc
        responses = [self.make_response(b, etag=etag) for b in self.blocks]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["etag"], etag)

    # --------------------------- 422：状态码 ---------------------------

    def test_422_status_200_rejected(self) -> None:
        responses = [
            self.make_response(self.blocks[0]),
            self.make_response(self.blocks[1], status_code=200),
            self.make_response(self.blocks[2]),
        ]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422, resp.text)
        err = resp.json()
        self.assertEqual(err["error"], "unexpected_status")
        self.assertEqual(err["detail"][0]["block_index"], 1)
        self.assertEqual(err["detail"][0]["loc"], "responses[1].status_code")

    def test_422_status_416_rejected(self) -> None:
        responses = [
            self.make_response(b, status_code=416) for b in self.blocks
        ]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "unexpected_status")

    # ------------------------- 422：Content-Range -------------------------

    def test_422_content_range_wrong_interval(self) -> None:
        block = self.blocks[1]
        bad = self.make_response(
            block, content_range=f"bytes 40-78/{OBJECT_LENGTH}"
        )  # 规划为 [40,79]
        responses = [self.make_response(self.blocks[0]), bad, self.make_response(self.blocks[2])]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "content_range_mismatch")
        self.assertEqual(resp.json()["detail"][0]["block_index"], 1)

    def test_422_content_range_wrong_total(self) -> None:
        block = self.blocks[0]
        bad = self.make_response(block, content_range="bytes 0-39/101")
        responses = [bad, self.make_response(self.blocks[1]), self.make_response(self.blocks[2])]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "object_length_mismatch")

    def test_422_content_range_malformed(self) -> None:
        for value in (
            "bytes 0-39/*",
            "bytes */100",
            "bytes=0-39/100",
            "bytes 0-39",
            "0-39/100",
            "bytes 39-0/100",  # 与规划不符（格式合法但闭区间不匹配）
        ):
            bad = self.make_response(self.blocks[0], content_range=value)
            responses = [bad, self.make_response(self.blocks[1]), self.make_response(self.blocks[2])]
            resp = self.verify(self.make_request(responses))
            self.assertEqual(resp.status_code, 422, (value, resp.text))
            # 格式语法错误 -> invalid_content_range；区间反转 -> content_range_mismatch
            self.assertIn(
                resp.json()["error"],
                ("invalid_content_range", "content_range_mismatch"),
                value,
            )

    # ----------------------------- 422：响应体 -----------------------------

    def test_422_body_length_mismatch(self) -> None:
        bad = self.make_response(self.blocks[2], body=b"short")  # 5 字节 vs 20
        responses = [
            self.make_response(self.blocks[0]),
            self.make_response(self.blocks[1]),
            bad,
        ]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "block_length_mismatch")
        self.assertEqual(resp.json()["detail"][0]["block_index"], 2)
        # 错误响应不得回显响应体。
        self.assertNotIn(b64(b"short"), resp.text)

    def test_422_invalid_base64(self) -> None:
        bad = {
            "block_index": 0,
            "status_code": 206,
            "content_range": "bytes 0-39/100",
            "etag": STRONG_ETAG,
            "body_b64": "not!valid!base64",
        }
        responses = [bad, self.make_response(self.blocks[1]), self.make_response(self.blocks[2])]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_base64")
        self.assertNotIn("not!valid!base64", resp.text)

    # --------------------------- 422：块覆盖核对 ---------------------------

    def test_422_missing_block_response(self) -> None:
        responses = [
            self.make_response(self.blocks[0]),
            self.make_response(self.blocks[2]),
        ]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422)
        err = resp.json()
        self.assertEqual(err["error"], "missing_block_response")
        self.assertEqual(err["detail"][0]["block_index"], 1)
        self.assertIn("1", err["detail"][0]["message"])

    def test_422_duplicate_block_response(self) -> None:
        responses = [
            self.make_response(self.blocks[0]),
            self.make_response(self.blocks[1]),
            self.make_response(self.blocks[1]),
            self.make_response(self.blocks[2]),
        ]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "duplicate_block_response")

    def test_422_unexpected_block_response(self) -> None:
        extra = dict(self.make_response(self.blocks[0]))
        extra["block_index"] = 9
        extra["status_code"] = 206
        extra["content_range"] = "bytes 0-39/100"
        responses = [
            self.make_response(self.blocks[0]),
            self.make_response(self.blocks[1]),
            self.make_response(self.blocks[2]),
            extra,
        ]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "unexpected_block_response")
        self.assertEqual(resp.json()["detail"][0]["block_index"], 9)

    def test_422_blocks_out_of_object_bounds(self) -> None:
        blocks = [dict(b) for b in self.blocks]
        blocks[2]["end"] = 100  # 超出 object_length=100
        resp = self.verify(self.make_request(blocks=blocks))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "block_out_of_bounds")

    def test_422_response_for_wrong_plan_with_missing_all(self) -> None:
        resp = self.verify(
            {"object_length": OBJECT_LENGTH, "blocks": self.blocks, "responses": []}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "missing_block_response")

    # ------------------------------ 422：ETag ------------------------------

    def test_422_weak_etag_rejected(self) -> None:
        for weak in ('W/"v1"', 'w/"v1"', ' W/"v1" '):
            bad = self.make_response(self.blocks[0], etag=weak)
            responses = [
                bad,
                self.make_response(self.blocks[1]),
                self.make_response(self.blocks[2]),
            ]
            resp = self.verify(self.make_request(responses))
            self.assertEqual(resp.status_code, 422, (weak, resp.text))
            self.assertEqual(resp.json()["error"], "weak_or_invalid_etag")
            self.assertEqual(resp.json()["detail"][0]["block_index"], 0)

    def test_422_malformed_etag_rejected(self) -> None:
        for bad_etag in ('v1"', '"v1', 'v1', '""extra', '"a"b"'):
            bad = self.make_response(self.blocks[0], etag=bad_etag)
            responses = [
                bad,
                self.make_response(self.blocks[1]),
                self.make_response(self.blocks[2]),
            ]
            resp = self.verify(self.make_request(responses))
            self.assertEqual(resp.status_code, 422, (bad_etag, resp.text))
            self.assertEqual(resp.json()["error"], "weak_or_invalid_etag")

    def test_422_empty_opaque_etag_is_strong(self) -> None:
        # 空 opaque-tag（""）是合法强验证器，不应当作非法语法拒绝。
        responses = [self.make_response(b, etag='""') for b in self.blocks]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["etag"], '""')

    def test_422_differing_etag_versions(self) -> None:
        bad = self.make_response(self.blocks[2], etag=OTHER_ETAG)
        responses = [
            self.make_response(self.blocks[0]),
            self.make_response(self.blocks[1]),
            bad,
        ]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "etag_mismatch")
        self.assertEqual(resp.json()["detail"][0]["block_index"], 2)

    def test_422_expected_etag_mismatch(self) -> None:
        resp = self.verify(self.make_request(expected_etag=OTHER_ETAG))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "expected_etag_mismatch")
        self.assertEqual(resp.json()["detail"][0]["loc"], "expected_etag")
        # 期望与实际 ETag 都不应回显到错误消息里。
        self.assertNotIn(OTHER_ETAG, resp.text)
        self.assertNotIn(STRONG_ETAG, resp.text)

    def test_422_expected_etag_weak(self) -> None:
        resp = self.verify(self.make_request(expected_etag='W/"v1"'))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_expected_etag")

    def test_422_expected_etag_malformed(self) -> None:
        resp = self.verify(self.make_request(expected_etag="not-an-etag"))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_expected_etag")

    def test_expected_etag_skipped_when_blocks_empty(self) -> None:
        # 没有任何块时无法确认 ETag；即便提供 expected_etag 也跳过比较，
        # 返回 200 且 etag=null。
        resp = self.verify(
            {
                "object_length": OBJECT_LENGTH,
                "blocks": [],
                "responses": [],
                "expected_etag": STRONG_ETAG,
            }
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["etag"])

    # --------------------------- 422：字段校验 ---------------------------

    def test_422_missing_required_fields(self) -> None:
        resp = self.client.post("/verify-fetch-batch", json={})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_422_status_code_wrong_type(self) -> None:
        bad = self.make_response(self.blocks[0])
        bad["status_code"] = "206"
        responses = [bad, self.make_response(self.blocks[1]), self.make_response(self.blocks[2])]
        resp = self.verify(self.make_request(responses))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_422_negative_block_index(self) -> None:
        bad = self.make_response(self.blocks[0])
        bad["block_index"] = -1
        resp = self.verify(self.make_request(responses=[bad]))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_block")


if __name__ == "__main__":
    unittest.main()
