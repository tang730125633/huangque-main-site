import concurrent.futures
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import audio, core, director_agent, upstream_guard, video


def payload(**overrides):
    value = {
        "prompt": "帮我生成一份分镜脚本",
        "session_id": "director_session_123",
        "page_revision": "a1b2c3d4",
        "page_context": {
            "page": "script", "path": "/workbench/script.html", "mode": "write",
            "topic": "东鹏特饮", "selling_points": "买三送一", "style": "口播",
            "duration": "30s", "platform": "抖音", "has_script": False,
            "scene_count": 0, "has_breakdown": False, "breakdown_scene_count": 0,
            "breakdown_url": "", "breakdown_tool": "scenes",
            "has_reverse_prompt": False, "active_job_status": "idle",
        },
        "history": [], "source_page": "script", "provider": "openai_responses",
        "quoted_cost": 0,
    }
    value.update(overrides)
    return value


def issued_script_value(db, offer_id, expected_cost=3, now=2_000_000_000):
    cli_input = {
        "request_id": offer_id,
        "topic": "东鹏特饮", "selling_points": "买三送一",
        "style": "口播", "duration": "30s", "platform": "抖音",
    }
    issued = director_agent._issue_production_offer(
        db, "alice", {
            "offer_id": offer_id, "kind": "script",
            "expected_cost": expected_cost, "requires_confirmation": True,
            "input": cli_input,
            "summary": {
                "topic": "东鹏特饮", "style": "口播",
                "duration": "30s", "platform": "抖音",
            },
        }, "a1b2c3d4", now=now,
    )
    return {
        "offer_id": offer_id,
        "expected_cost": expected_cost,
        "input": cli_input,
        "plan_digest": issued["plan_digest"],
        "quote_token": issued["quote_token"],
    }


class DirectorAgentConfirmationTests(unittest.TestCase):
    def test_internal_director_copy_route_uses_durable_paid_attempt_and_replays(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,username TEXT NOT NULL,cost INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',payload TEXT NOT NULL,
                    result TEXT,error TEXT,refunded INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,owner TEXT
                )""")
                connection.commit()

            class Points:
                AuthPointsError = type("AuthPointsError", (Exception,), {})

                def __init__(self):
                    self.ledger = {}
                    self.deductions = []

                def cost_of(self, kind, _body):
                    self.assert_kind = kind
                    return 3

                def get_points_transaction(self, key):
                    return self.ledger.get(key)

                def deduct_points(self, username, amount, _reason, transaction_key=""):
                    if transaction_key not in self.ledger:
                        self.deductions.append((username, amount, transaction_key))
                        self.ledger[transaction_key] = {
                            "username": username, "delta": -int(amount),
                            "after_points": 97,
                        }
                    return 97

                def refund_points(self, username, amount, _reason, transaction_key=""):
                    self.ledger.setdefault(transaction_key, {
                        "username": username, "delta": int(amount),
                        "after_points": 100,
                    })
                    return 100

            points = Points()
            patches = [
                mock.patch.object(core, "jdb", db),
                mock.patch.dict(core.HANDLERS, {"copy": lambda _body: {}}),
                mock.patch.object(core, "AUTH_INTERNAL_TOKEN", "director-internal-test"),
                mock.patch.object(core, "verify", return_value={
                    "username": "alice", "must_change": False, "points": 100,
                }),
                mock.patch.object(core, "_must_change_password", return_value=False),
                mock.patch.object(core, "_domains", return_value=(audio, points, video)),
                mock.patch.object(core.feature_flags, "require_enabled"),
                mock.patch.object(core.miniprogram_security, "check_payload"),
                mock.patch.object(upstream_guard, "exhausted_reason", return_value=None),
                mock.patch.object(core, "_user_video_submit_limit", return_value=None),
                mock.patch.object(core, "_user_active_job_count", return_value=0),
                mock.patch.object(core, "is_shutting_down", return_value=False),
                mock.patch.object(core, "enqueue_job", return_value=True),
            ]
            for item in patches:
                item.start()
                self.addCleanup(item.stop)

            server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = "http://127.0.0.1:%d/api/gen/copy" % server.server_address[1]
            body = {
                "prompt": "energy drink; buy three get one free",
                "format": "script", "style": "种草", "dur": "30s",
                "platform": "抖音", "ctype": "分镜脚本",
                "source_page": "script",
            }

            def post():
                request = urllib.request.Request(
                    url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST", headers={
                        "Authorization": "Bearer alice",
                        "Content-Type": "application/json",
                        "Idempotency-Key": "director-production-route-0001",
                        "X-HQ-Expected-Cost": "3",
                        "X-HQ-Internal-Token": "director-internal-test",
                        "X-HQ-Submission-Class": "director-agent",
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())

            first_status, first = post()
            replay_status, replay = post()
            self.assertEqual((200, 200), (first_status, replay_status))
            self.assertEqual(first["job_id"], replay["job_id"])
            self.assertEqual(1, len(points.deductions))
            with closing(db()) as connection:
                job = connection.execute(
                    "SELECT kind,payload FROM jobs WHERE id=?", (first["job_id"],),
                ).fetchone()
                attempt = connection.execute(
                    "SELECT kind,state,job_id FROM matrix_template_submission_attempts "
                    "WHERE username='alice' AND endpoint='/api/gen/copy'",
                ).fetchone()
            self.assertEqual("copy", job["kind"])
            self.assertEqual("copy_model", json.loads(job["payload"])["provider"])
            self.assertEqual(
                ("copy", "linked", first["job_id"]),
                (attempt["kind"], attempt["state"], attempt["job_id"]),
            )

    def test_production_confirmation_accepts_canonical_signed_quote_token_shape(self):
        offer_id = "director-production-1234567890abcdef"
        token = "A" * 80 + "." + "b" * 64
        normalized = director_agent._normalize_production_request({
            "offer_id": offer_id,
            "input": {
                "request_id": offer_id, "topic": "东鹏特饮",
                "selling_points": "买三送一", "style": "种草",
                "duration": "30s", "platform": "抖音",
            },
            "expected_cost": 3,
            "plan_digest": "a" * 64,
            "quote_token": token,
        })
        self.assertEqual(normalized[0], offer_id)
        self.assertEqual(normalized[4], token)

    def test_ready_request_returns_direct_production_question(self):
        request = director_agent.validate_payload(payload())
        request.update(_username="alice", _job_id=42)
        raw = json.dumps({
            "content": "我已经把参数填好了。",
            "stage": "production",
            "actions": [],
            "warnings": [],
            "offer_production": True,
        }, ensure_ascii=False)
        with mock.patch.object(
            director_agent.director_cli, "production_is_available", return_value=True,
        ), mock.patch(
            "content_domains.points.cost_of", return_value=3,
        ):
            result = director_agent.normalize_model_result(raw, request)
        self.assertEqual(
            "生产信息已经准备好，预计扣除 3 点。是否开始生产？",
            result["content"],
        )
        self.assertEqual(3, result["production_offer"]["expected_cost"])
        self.assertTrue(result["production_offer"]["requires_confirmation"])
        self.assertEqual("东鹏特饮", result["production_offer"]["summary"]["topic"])

    def test_direct_production_request_cannot_degrade_into_page_instructions(self):
        request = director_agent.validate_payload(payload())
        request.update(_username="alice", _job_id=42)
        raw = json.dumps({
            "content": "请到编导页面点击生成按钮。",
            "stage": "understand",
            "actions": [],
            "warnings": [],
            "offer_production": False,
        }, ensure_ascii=False)
        with mock.patch.object(
            director_agent.director_cli, "production_is_available", return_value=True,
        ), mock.patch(
            "content_domains.points.cost_of", return_value=3,
        ):
            result = director_agent.normalize_model_result(raw, request)
        self.assertEqual(
            "生产信息已经准备好，预计扣除 3 点。是否开始生产？",
            result["content"],
        )
        self.assertEqual("script", result["production_offer"]["kind"])
        self.assertTrue(result["production_offer"]["requires_confirmation"])

    def test_script_production_intent_does_not_capture_guidance_or_negation(self):
        direct = director_agent.validate_payload(payload(
            prompt="可以帮我生成一份分镜脚本吗？",
        ))
        guidance = director_agent.validate_payload(payload(
            prompt="生成脚本后怎么做视频？",
        ))
        negated = director_agent.validate_payload(payload(
            prompt="先不要生成分镜脚本，只帮我看看参数",
        ))
        capability = director_agent.validate_payload(payload(
            prompt="你会生成什么？",
        ))
        capability_question = director_agent.validate_payload(payload(
            prompt="你能不能生成一份分镜脚本？",
        ))
        user_rule = director_agent.validate_payload(payload(
            prompt="不要告诉我怎么做，直接帮我生成一份分镜脚本",
        ))
        tutorial = director_agent.validate_payload(payload(
            prompt="帮我生成分镜脚本的步骤",
        ))
        embedded_capability = director_agent.validate_payload(payload(
            prompt="你能不能帮我生成一份分镜脚本？",
        ))
        meta_guidance = director_agent.validate_payload(payload(
            prompt="不用教我如何生成脚本，直接帮我生成一份分镜脚本",
        ))
        self.assertTrue(director_agent._explicit_script_production_request(direct))
        self.assertFalse(director_agent._explicit_script_production_request(guidance))
        self.assertFalse(director_agent._explicit_script_production_request(negated))
        self.assertFalse(director_agent._explicit_script_production_request(capability))
        self.assertFalse(
            director_agent._explicit_script_production_request(capability_question)
        )
        self.assertTrue(director_agent._explicit_script_production_request(user_rule))
        self.assertFalse(director_agent._explicit_script_production_request(tutorial))
        self.assertFalse(
            director_agent._explicit_script_production_request(embedded_capability)
        )
        self.assertTrue(
            director_agent._explicit_script_production_request(meta_guidance)
        )

    def test_server_intent_gate_overrides_model_in_both_directions(self):
        def normalize(prompt, model_offer, topic="东鹏特饮"):
            value = payload(prompt=prompt)
            value["page_context"] = dict(value["page_context"], topic=topic)
            request = director_agent.validate_payload(value)
            request.update(_username="alice", _job_id=42)
            raw = json.dumps({
                "content": "模型原始回答",
                "stage": "understand",
                "actions": [],
                "warnings": [],
                "offer_production": model_offer,
            }, ensure_ascii=False)
            with mock.patch.object(
                director_agent.director_cli, "production_is_available",
                return_value=True,
            ), mock.patch(
                "content_domains.points.cost_of", return_value=3,
            ):
                return director_agent.normalize_model_result(raw, request)

        advisory = (
            "你能不能帮我生成一份分镜脚本？",
            "怎么帮我生成一份分镜脚本？",
            "如何调用编导 CLI 生成分镜脚本？",
            "我还没有确认开始生产，只是问问流程。",
            "生成分镜脚本需要多少点？",
            "刚才生成脚本失败了。",
            "先不要生成分镜脚本。",
            "帮我生成分镜脚本，取消",
            "帮我生成分镜脚本，先不要",
            "帮我生成分镜脚本，算了",
            "我不想要分镜脚本",
            "分镜脚本先不用了",
            "暂时不做分镜脚本",
            "等我确认后再生成分镜脚本",
            "生成分镜脚本要多久？",
            "明天再生成分镜脚本",
            "晚点生成分镜脚本",
            "待会儿再生成分镜脚本",
            "过会儿再生成分镜脚本",
            "暂缓生成分镜脚本",
            "等会儿再生成分镜脚本",
            "晚些时候再生成分镜脚本",
            "过两天再生成分镜脚本",
            "下周再生成分镜脚本",
            "生成分镜脚本一般要几分钟",
            "分镜脚本生成需要几小时",
            "生成分镜脚本耗时吗",
            "生成分镜脚本什么时候能好",
            "生成分镜脚本什么时候可以完成",
            "生成分镜脚本要花多长时间",
            "分镜脚本多快能生成好",
            "还不是现在生成分镜脚本",
            "暂时先放一放，别生成分镜脚本",
            "以后有需要再生成分镜脚本",
            "先不急着生成分镜脚本",
            "生成视频但不生成脚本",
            "不生成脚本只生成视频",
            "先生成视频，不要生成分镜脚本",
            "只生成视频，分镜脚本先不要",
            "生成视频而不是脚本",
            "生成视频而非脚本",
            "我想生成视频，不是分镜脚本",
            "不是要生成脚本，是要生成视频",
            "只要视频，不要脚本",
            "做视频，不做脚本",
            "我不打算生成脚本",
            "暂时没计划做脚本",
            "目前不考虑生成脚本",
            "我不准备生成脚本",
            "还没决定要不要生成脚本",
            "我没说要生成脚本",
            "先别急，脚本过几天再做",
            "脚本晚一会儿再生成",
            "有空再生成脚本",
            "脚本几天能做完",
            "生成脚本预计要几天",
            "什么时候能把脚本做完",
            "先缓缓，脚本不急着做",
            "我压根不想生成脚本",
            "我没有打算做脚本",
            "先不考虑做脚本",
            "脚本暂时搁置",
            "脚本先放一放",
            "之后有需要再做脚本",
            "脚本大概要几天完成",
            "脚本预计多久能交付",
            "我只是想问生成脚本要多久",
            "先生成视频，脚本就不要了",
            "我要视频而非脚本",
            "视频可以做，脚本不做",
            "不要生成视频",
            "请生成视频",
            "我们聊聊脚本",
        )
        execute = (
            "不要生成视频，只帮我生成一份分镜脚本。",
            "不要生成视频只帮我生成一份分镜脚本。",
            "请生成 一份分镜脚本",
            "不要告诉我怎么做，直接帮我生成一份分镜脚本",
            "不讲方法直接帮我生成一份分镜脚本",
            "生成一份关于新能源汽车的分镜脚本",
            "生成关于新能源汽车的分镜脚本",
            "写一个关于 AI 营销的脚本",
            "围绕新能源汽车生成分镜脚本",
            "给东鹏特饮做一份分镜脚本",
            "按刚才方案开始生产",
            "直接做吧",
            "不用告诉我生成脚本的方法直接帮我生成一份分镜脚本",
            "不要讲方法直接生成分镜脚本",
            "按这个方案开始",
            "就这么做",
            "先生成分镜脚本，不要生成视频",
            "生成脚本，不生成视频",
            "不做视频只做分镜脚本",
            "照这个方案开始",
            "直接开始",
            "现在开始",
            "开始吧",
            "只生成分镜脚本",
            "分镜脚本直接做",
            "开始生成吧",
            "依照这个方案执行",
            "沿用上面的方案开始做",
            "按之前的方案做",
            "照刚才说的做吧",
            "生成分镜脚本但不要生成视频",
            "只生成分镜脚本，视频先不要",
            "视频不要，只做分镜脚本",
            "不需要你解释生成脚本的方法，直接帮我生成一份分镜脚本",
            "无需说明生成脚本步骤直接生成分镜脚本",
            "不想听流程直接生成分镜脚本",
            "别跟我讲操作方法直接做分镜脚本",
            "不用介绍流程直接生成一份分镜脚本",
            "做吧",
            "生成吧",
            "执行吧",
            "继续做吧",
            "继续生成",
            "按原方案执行",
            "照着这个方案开始",
            "那就开始",
            "只做脚本不做数字人",
            "就按上面的来",
            "根据这个方案开始",
            "照旧开始",
            "给我来一个脚本",
            "直接出脚本",
            "搞一份分镜脚本",
            "别分析了，直接给我脚本",
            "就这么来吧",
            "生成脚本而不是视频",
            "要脚本，不要视频",
            "视频先放一边，直接生成脚本",
            "不要成片，只出分镜",
            "开干吧",
            "开始干",
            "继续吧",
            "继续",
            "确认开始",
            "确认生产",
            "就按这个来吧",
            "用原方案继续",
            "按刚才说的继续",
            "直接来",
            "就它了，开始",
            "可以开始了",
        )
        for prompt in advisory:
            with self.subTest(prompt=prompt, model_offer=True):
                self.assertNotIn("production_offer", normalize(prompt, True))
        for prompt in execute:
            with self.subTest(prompt=prompt, model_offer=False):
                result = normalize(prompt, False)
                self.assertEqual("script", result["production_offer"]["kind"])
                self.assertTrue(result["production_offer"]["requires_confirmation"])
        for prompt in advisory:
            with self.subTest(prompt=prompt, topic="", model_offer=True):
                self.assertNotIn("production_offer", normalize(prompt, True, topic=""))
        for prompt in (
            "我们聊聊脚本",
            "脚本挺重要",
            "我对脚本有个想法",
            "先看看这个脚本",
        ):
            with self.subTest(prompt=prompt, expected_intent="unknown"):
                request = director_agent.validate_payload(payload(prompt=prompt))
                self.assertEqual(
                    director_agent.SCRIPT_PRODUCTION_UNKNOWN,
                    director_agent._script_production_intent(request),
                )
                self.assertNotIn("production_offer", normalize(prompt, True))
        for prompt in (
            "生成一份关于新能源汽车的分镜脚本",
            "生成关于新能源汽车的分镜脚本",
            "写一个关于 AI 营销的脚本",
            "围绕新能源汽车生成分镜脚本",
            "围绕“AI营销”生成一份分镜脚本",
            "围绕AI营销来生成一份分镜脚本",
            "围绕AI营销，生成一份分镜脚本",
            "以AI营销为题写一份分镜脚本",
            "给新能源汽车来一份分镜脚本",
            "做一份围绕AI营销的分镜脚本",
            "给东鹏特饮做一份分镜脚本",
        ):
            with self.subTest(prompt=prompt, topic="", model_offer=False):
                result = normalize(prompt, False, topic="")
                self.assertEqual("script", result["production_offer"]["kind"])

        cancelled_after_extraction = (
            "帮我生成一份关于 AI 的分镜脚本，但是先不要生成",
            "生成一份关于 AI 的分镜脚本，算了",
        )
        for prompt in cancelled_after_extraction:
            with self.subTest(prompt=prompt, topic="", model_offer=True):
                self.assertNotIn("production_offer", normalize(prompt, True, topic=""))

        invalid_topic_prompts = (
            "给我生成一份分镜脚本",
            "为我生成一份分镜脚本",
            "给咱们生成一份分镜脚本",
            "为自己生成一份分镜脚本",
            "给大家生成一份分镜脚本",
            "给您生成一份分镜脚本",
            "围绕上述主题生成一份分镜脚本",
            "以刚才的主题生成一份分镜脚本",
            "给本人生成一份分镜脚本",
            "给大伙生成一份分镜脚本",
            "围绕那个主题生成一份分镜脚本",
            "以这个方向生成一份分镜脚本",
            "围绕前文主题生成一份分镜脚本",
            "以刚才说的主题生成一份分镜脚本",
            "围绕上面的选题生成一份分镜脚本",
            "以之前的选题写一份分镜脚本",
            "按那个选题生成一份分镜脚本",
            "围绕上次的话题生成一份分镜脚本",
            "用前面说的主题写一份分镜脚本",
            "给咱俩生成一份分镜脚本",
            "以此为主题生成一份分镜脚本",
            "按之前说的方向做一份分镜脚本",
        )
        for prompt in invalid_topic_prompts:
            with self.subTest(prompt=prompt, invalid_topic=True):
                result = normalize(prompt, False, topic="")
                self.assertNotIn("production_offer", result)
                self.assertEqual("请告诉我这次要生成的分镜脚本主题。", result["content"])

        result = normalize("围绕AI营销生成一份分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("围绕AI营销，生成一份分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("以AI营销为题写一份分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("围绕“AI营销”生成一份分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("围绕AI营销来生成一份分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("给新能源汽车来一份分镜脚本", False, topic="")
        self.assertEqual("新能源汽车", result["production_offer"]["summary"]["topic"])
        result = normalize("做一份围绕AI营销的分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("以AI营销作为主题生成一份分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("围绕AI营销这个主题生成一份分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("主题是AI营销，生成一份分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("做个低空经济分镜脚本", False, topic="")
        self.assertEqual("低空经济", result["production_offer"]["summary"]["topic"])
        result = normalize("写个AI营销的分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("给我做一个AI营销主题的分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("关于AI营销和品牌增长的脚本", False, topic="")
        self.assertEqual(
            "AI营销和品牌增长", result["production_offer"]["summary"]["topic"]
        )
        result = normalize("主题定为AI营销，生成一份分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("选题是低空经济，直接生成分镜脚本", False, topic="")
        self.assertEqual("低空经济", result["production_offer"]["summary"]["topic"])
        result = normalize("写一份AI营销主题分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])
        result = normalize("来个低空经济的脚本", False, topic="")
        self.assertEqual("低空经济", result["production_offer"]["summary"]["topic"])
        result = normalize("按AI营销这个选题做分镜脚本", False, topic="")
        self.assertEqual("AI营销", result["production_offer"]["summary"]["topic"])

    def test_customer_topic_and_existing_page_topic_override_model_topic_action(self):
        def normalize(prompt, page_topic, model_topic):
            value = payload(prompt=prompt)
            value["page_context"] = dict(value["page_context"], topic=page_topic)
            request = director_agent.validate_payload(value)
            request.update(_username="alice", _job_id=42)
            raw = json.dumps({
                "content": "模型原始回答",
                "stage": "production",
                "actions": [{
                    "type": "fill_field", "field": "topic", "value": model_topic,
                    "label": "填入选题",
                }],
                "warnings": [],
                "offer_production": True,
            }, ensure_ascii=False)
            with mock.patch.object(
                director_agent.director_cli, "production_is_available",
                return_value=True,
            ), mock.patch(
                "content_domains.points.cost_of", return_value=3,
            ):
                return director_agent.normalize_model_result(raw, request)

        explicit = normalize("围绕AI营销生成一份分镜脚本", "", "房地产")
        self.assertEqual("AI营销", explicit["production_offer"]["summary"]["topic"])
        self.assertEqual("AI营销", explicit["plan"]["actions"][0]["value"])

        existing = normalize("直接生成分镜脚本", "东鹏特饮", "房地产")
        self.assertEqual("东鹏特饮", existing["production_offer"]["summary"]["topic"])
        self.assertFalse(any(
            action["type"] == "fill_field" and action["field"] == "topic"
            for action in existing["plan"]["actions"]
        ))

        for prompt in (
            "不做视频只做分镜脚本",
            "无需说明生成脚本步骤直接生成分镜脚本",
        ):
            with self.subTest(prompt=prompt, page_topic_wins=True):
                result = normalize(prompt, "东鹏特饮", "房地产")
                self.assertEqual(
                    "东鹏特饮", result["production_offer"]["summary"]["topic"]
                )

        missing = normalize("给我生成一份分镜脚本", "", "新能源汽车")
        self.assertNotIn("production_offer", missing)
        self.assertEqual("请告诉我这次要生成的分镜脚本主题。", missing["content"])

    def test_empty_page_advisory_vetoes_model_topic_action(self):
        value = payload(prompt="我还没有确认开始生产，只是问问流程。")
        value["page_context"] = dict(value["page_context"], topic="")
        request = director_agent.validate_payload(value)
        request.update(_username="alice", _job_id=42)
        raw = json.dumps({
            "content": "模型误判为生产。",
            "stage": "production",
            "actions": [{
                "type": "fill_field", "field": "topic", "value": "新能源汽车",
                "label": "填入选题",
            }],
            "warnings": [],
            "offer_production": True,
        }, ensure_ascii=False)
        with mock.patch.object(
            director_agent.director_cli, "production_is_available", return_value=True,
        ), mock.patch(
            "content_domains.points.cost_of", return_value=3,
        ):
            result = director_agent.normalize_model_result(raw, request)
        self.assertNotIn("production_offer", result)

    def test_direct_request_extracts_topic_when_model_omits_field_action(self):
        value = payload(prompt="帮我生成一份关于新能源汽车的分镜脚本")
        value["page_context"] = dict(value["page_context"], topic="")
        request = director_agent.validate_payload(value)
        request.update(_username="alice", _job_id=42)
        raw = json.dumps({
            "content": "请到页面填写选题。",
            "stage": "understand",
            "actions": [],
            "warnings": [],
            "offer_production": False,
        }, ensure_ascii=False)
        with mock.patch.object(
            director_agent.director_cli, "production_is_available", return_value=True,
        ), mock.patch(
            "content_domains.points.cost_of", return_value=3,
        ):
            result = director_agent.normalize_model_result(raw, request)
        self.assertEqual("新能源汽车", result["production_offer"]["summary"]["topic"])
        self.assertEqual("fill_field", result["plan"]["actions"][0]["type"])
        self.assertEqual("新能源汽车", result["plan"]["actions"][0]["value"])

    def test_chat_job_claim_is_atomic_and_replayable_without_schema_change(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT, username TEXT, cost INTEGER,
                    status TEXT DEFAULT 'pending', payload TEXT, result TEXT,
                    error TEXT, created_at INTEGER, updated_at INTEGER,
                    deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                    owner TEXT
                )""")
                connection.commit()

            request = director_agent.validate_payload(payload())

            def submit(_index):
                return director_agent.accept_chat_job(
                    db, "alice", request, "content", "/api/gen/director_agent",
                    "director-agent-idem-0001", points_left=99,
                    max_active_jobs=20, now=2_000_000_000,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(submit, range(8)))
            self.assertEqual(1, sum(state == "new" for state, _ in results))
            self.assertEqual({"new", "replay"}, {state for state, _ in results})
            self.assertEqual(1, len({item["job_id"] for _, item in results}))
            with closing(db()) as connection:
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(1) FROM jobs WHERE kind='director_agent'"
                ).fetchone()[0])
                columns = [row[1] for row in connection.execute(
                    "PRAGMA table_info(submission_idempotency)"
                )]
            self.assertEqual(
                ["username", "endpoint", "idem_key", "request_hash",
                 "response_json", "created_at", "updated_at"],
                columns,
            )

            changed = dict(request, prompt="换一个请求")
            state, response = director_agent.accept_chat_job(
                db, "alice", changed, "content", "/api/gen/director_agent",
                "director-agent-idem-0001", points_left=99,
                max_active_jobs=20, now=2_000_000_001,
            )
            self.assertEqual("conflict", state)
            self.assertIsNone(response)

    def test_http_chat_route_accepts_once_and_replays_same_job(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT, username TEXT, cost INTEGER,
                    status TEXT DEFAULT 'pending', payload TEXT, result TEXT,
                    error TEXT, created_at INTEGER, updated_at INTEGER,
                    deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                    owner TEXT
                )""")
                connection.commit()

            patches = [
                mock.patch.object(core, "jdb", db),
                mock.patch.object(core, "verify", lambda token: {
                    "username": "alice", "must_change": False, "points": 99,
                } if token else None),
                mock.patch.object(core, "_domains", return_value=(
                    SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                )),
                mock.patch.object(core.feature_flags, "require_enabled", lambda _key: None),
                mock.patch.object(core, "_director_agent_available", return_value=True),
                mock.patch.object(core.miniprogram_security, "check_payload", lambda _body: None),
                mock.patch.object(core, "enqueue_job", return_value=True),
            ]
            for item in patches:
                item.start()
                self.addCleanup(item.stop)

            server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = "http://127.0.0.1:%d/api/gen/director_agent" % server.server_address[1]

            def post(body):
                request = urllib.request.Request(
                    url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST", headers={
                        "Authorization": "Bearer alice",
                        "Content-Type": "application/json",
                        "Idempotency-Key": "director-agent-http-0001",
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())

            first_status, first = post(payload())
            replay_status, replay = post(payload())
            self.assertEqual((200, 200), (first_status, replay_status))
            self.assertEqual(first["job_id"], replay["job_id"])
            self.assertEqual(0, first["cost"])
            with closing(db()) as connection:
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(1) FROM jobs WHERE kind='director_agent'"
                ).fetchone()[0])

    def test_confirmed_script_production_quotes_submits_once_and_replays(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY, kind TEXT, username TEXT,
                    cost INTEGER, status TEXT
                )""")
                connection.execute(
                    "INSERT INTO jobs(id,kind,username,cost,status) VALUES(77,'copy','alice',3,'pending')"
                )
                connection.commit()

            offer_id = "director-production-1234567890abcdef"
            value = issued_script_value(db, offer_id)
            quote = {
                "quote_token": "q" * 24, "cost": 3,
                "points": 99, "expires_in": 60,
            }
            submitted = {"job_id": 77, "points_left": 96}
            with mock.patch.object(
                director_agent.director_cli, "quote_script", return_value=quote,
            ) as quote_call, mock.patch.object(
                director_agent.director_cli, "confirm_script", return_value=submitted,
            ) as confirm_call:
                first_status, first = director_agent.produce_script(
                    db, "alice", value, now=2_000_000_000,
                )
                replay_status, replay = director_agent.produce_script(
                    db, "alice", value, now=2_000_000_001,
                )
            self.assertEqual((200, 200), (first_status, replay_status))
            self.assertEqual(77, first["job_id"])
            self.assertFalse(first["recovered"])
            self.assertTrue(replay["recovered"])
            quote_call.assert_called_once()
            confirm_call.assert_called_once()

    def test_confirmed_script_production_requotes_changed_price_without_submit(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY, kind TEXT, username TEXT,
                    cost INTEGER, status TEXT
                )""")
                connection.commit()
            offer_id = "director-production-fedcba0987654321"
            value = issued_script_value(db, offer_id)
            with mock.patch.object(
                director_agent.director_cli, "quote_script", return_value={
                    "quote_token": "q" * 24, "cost": 4,
                    "points": 99, "expires_in": 60,
                },
            ), mock.patch.object(
                director_agent.director_cli, "confirm_script",
            ) as confirm_call:
                status, result = director_agent.produce_script(
                    db, "alice", value, now=2_000_000_000,
                )
            self.assertEqual(409, status)
            self.assertEqual("production_price_changed", result["code"])
            self.assertEqual(4, result["current_cost"])
            self.assertNotEqual(value["quote_token"], result["quote_token"])
            self.assertEqual(value["plan_digest"], result["plan_digest"])
            confirm_call.assert_not_called()

            refreshed = dict(
                value, expected_cost=4, quote_token=result["quote_token"],
            )
            with mock.patch.object(
                director_agent.director_cli, "confirm_script",
            ) as confirm_call, self.assertRaises(
                director_agent.DirectorOfferError,
            ) as raised:
                director_agent.produce_script(
                    db, "alice", refreshed,
                    now=result["expires_at"] + 1,
                )
            self.assertEqual("director_offer_expired", raised.exception.code)
            confirm_call.assert_not_called()

    def test_offer_claim_and_attempt_insert_roll_back_together_on_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY, kind TEXT, username TEXT,
                    cost INTEGER, status TEXT
                )""")
                connection.commit()
            issued_at = 2_000_000_000
            value = issued_script_value(
                db, "director-production-atomic1234567890", now=issued_at,
            )

            def crash_between_claim_and_insert():
                raise RuntimeError("simulated crash")

            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                director_agent.produce_script(
                    db, "alice", value, now=issued_at + 1,
                    before_attempt_insert=crash_between_claim_and_insert,
                )
            with closing(db()) as connection:
                offer = connection.execute(
                    "SELECT confirmed_at FROM director_agent_offers WHERE username='alice'"
                ).fetchone()
                production_table = connection.execute(
                    "SELECT COUNT(1) FROM sqlite_master "
                    "WHERE type='table' AND name='director_cli_productions'"
                ).fetchone()[0]
                attempts = (connection.execute(
                    "SELECT COUNT(1) FROM director_cli_productions"
                ).fetchone()[0] if production_table else 0)
            self.assertIsNone(offer["confirmed_at"])
            self.assertEqual(0, attempts)

            with mock.patch.object(
                director_agent.director_cli, "quote_script",
            ) as quote_call, self.assertRaisesRegex(
                director_agent.DirectorOfferError, "已过期",
            ) as raised:
                director_agent.produce_script(
                    db, "alice", value,
                    now=issued_at + director_agent.OFFER_TTL_SECONDS + 1,
                )
            self.assertEqual("director_offer_expired", raised.exception.code)
            quote_call.assert_not_called()

    def test_price_refresh_uses_old_token_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            value = issued_script_value(
                db, "director-production-refresh123456789",
            )
            first = director_agent._rotate_production_offer(
                db, "alice", value["offer_id"], value["plan_digest"],
                value["input"], 4, value["quote_token"], now=2_000_000_001,
            )
            self.assertNotEqual(value["quote_token"], first["quote_token"])
            with self.assertRaises(
                director_agent.DirectorOfferError,
            ) as raised:
                director_agent._rotate_production_offer(
                    db, "alice", value["offer_id"], value["plan_digest"],
                    value["input"], 5, value["quote_token"], now=2_000_000_002,
                )
            self.assertEqual("director_offer_refreshed", raised.exception.code)
            with closing(db()) as connection:
                row = connection.execute(
                    "SELECT expected_cost,token_hash FROM director_agent_offers"
                ).fetchone()
            self.assertEqual(4, row["expected_cost"])
            self.assertEqual(
                director_agent._token_hash(first["quote_token"]),
                row["token_hash"],
            )

    def test_forged_or_expired_offer_is_rejected_before_cli_quote(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY, kind TEXT, username TEXT,
                    cost INTEGER, status TEXT
                )""")
                connection.commit()
            offer_id = "director-production-forged1234567890"
            forged = {
                "offer_id": offer_id, "expected_cost": 3,
                "input": {
                    "request_id": offer_id, "topic": "绕过确认",
                    "selling_points": "", "style": "口播",
                    "duration": "30s", "platform": "抖音",
                },
                "plan_digest": "a" * 64,
                "quote_token": "forged_confirmation_token_1234",
            }
            with mock.patch.object(
                director_agent.director_cli, "quote_script",
            ) as quote_call, self.assertRaisesRegex(ValueError, "服务器签发"):
                director_agent.produce_script(
                    db, "alice", forged, now=2_000_000_000,
                )
            quote_call.assert_not_called()

            expired = issued_script_value(
                db, "director-production-expired123456789",
                now=2_000_000_000,
            )
            with mock.patch.object(
                director_agent.director_cli, "quote_script",
            ) as quote_call, self.assertRaisesRegex(ValueError, "已过期"):
                director_agent.produce_script(
                    db, "alice", expired,
                    now=2_000_000_000 + director_agent.OFFER_TTL_SECONDS + 1,
                )
            quote_call.assert_not_called()

    def test_director_agent_uses_dedicated_queue(self):
        self.assertIs(
            core._pick_job_queue("director_agent"),
            core._director_agent_job_queue,
        )
        self.assertIsNot(core._director_agent_job_queue, core._fast_job_queue)
        self.assertNotIn("private_domain_video", director_agent.NAV_TARGETS)

    def test_digital_human_guide_contract_is_required(self):
        context = {
            "page": "digital_human_oneclick",
            "path": "/workbench/digital-human-oneclick.html",
            "mode": "photo", "narration_mode": "text",
            "script_text": "测试口播", "script_length": 4,
            "has_portrait": False, "has_video_source": False,
            "has_voice_source": False, "has_drive_audio": False,
            "customer_material_count": 0, "consent_confirmed": False,
            "precision_template": "", "has_result": False,
            "active_job_status": "idle",
        }
        with self.assertRaisesRegex(ValueError, "引导契约无效"):
            director_agent._digital_human_page_context(context)
        context["guide_contract"] = director_agent.DIGITAL_HUMAN_GUIDE_CONTRACT
        self.assertEqual(
            director_agent.DIGITAL_HUMAN_GUIDE_CONTRACT,
            director_agent._digital_human_page_context(context)["guide_contract"],
        )

    def test_http_production_route_rejects_unissued_offer_before_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            database = pathlib.Path(temp) / "jobs.db"

            def db():
                connection = sqlite3.connect(str(database), timeout=10)
                connection.row_factory = sqlite3.Row
                return connection

            offer_id = "director-production-httpforged123456"
            body = {
                "offer_id": offer_id, "expected_cost": 3,
                "input": {
                    "request_id": offer_id, "topic": "绕过确认",
                    "selling_points": "", "style": "口播",
                    "duration": "30s", "platform": "抖音",
                },
                "plan_digest": "a" * 64,
                "quote_token": "forged_confirmation_token_1234",
            }
            patches = [
                mock.patch.object(core, "jdb", db),
                mock.patch.object(core, "verify", lambda token: {
                    "username": "alice", "must_change": False, "points": 99,
                } if token else None),
                mock.patch.object(core.feature_flags, "require_enabled", lambda _key: None),
                mock.patch.object(
                    director_agent.director_cli, "production_is_available",
                    return_value=True,
                ),
                mock.patch.object(director_agent.director_cli, "quote_script"),
            ]
            started = []
            for item in patches:
                started.append(item.start())
                self.addCleanup(item.stop)
            quote_call = started[-1]
            server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = "http://127.0.0.1:%d/api/gen/director_agent/produce" % server.server_address[1]
            request = urllib.request.Request(
                url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                method="POST", headers={
                    "Authorization": "Bearer alice",
                    "Content-Type": "application/json",
                    "Idempotency-Key": offer_id,
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(400, raised.exception.code)
            response = json.loads(raised.exception.read())
            self.assertIn("服务器签发", response["detail"])
            self.assertEqual("director_offer_invalid", response["code"])
            quote_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
