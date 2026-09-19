"""渠道切换能力表：可切换的必须有真实适配器，不可切换的必须给得出原因。

这个测试守的是「不能只放开白名单」这条约束：
``channel_runtime`` 支持哪些协议是执行能力的唯一事实来源，
能力表里声明 switchable 的类型，其 adapters 必须全部真实存在于
``channel_manager.ADAPTERS``，否则就会出现「后台能切、任务执行时炸」。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

from content_domains import channel_capabilities as caps  # noqa: E402
from content_domains import channel_manager as manager     # noqa: E402
from content_domains import function_registry as registry  # noqa: E402


class ChannelCapabilityTests(unittest.TestCase):

    def test_switchable_kinds_declare_real_runtime_adapters(self):
        """声明可切换的类型，必须给出真正能执行它的适配器（且都已在运行时注册）。"""
        for kind in caps.switchable_kinds():
            with self.subTest(kind=kind):
                adapters = caps.adapters_for(kind)
                self.assertTrue(adapters, '%s 声明可切换却没有适配器' % kind)
                for adapter in adapters:
                    self.assertIn(adapter, manager.ADAPTERS,
                                  '%s 声明的适配器 %s 未在 channel_runtime 注册' % (kind, adapter))

    def test_every_declared_adapter_matches_the_kind(self):
        """适配器的 kind 必须与任务类型一致，避免把生图渠道配到视频功能上。"""
        for kind in caps.switchable_kinds():
            for adapter in caps.adapters_for(kind):
                with self.subTest(kind=kind, adapter=adapter):
                    self.assertEqual(kind, manager.ADAPTERS[adapter]['kind'])

    def test_non_switchable_kinds_have_a_readable_reason(self):
        """不可切换的类型必须给得出具体中文原因，后台才有话可说。"""
        for kind, item in caps.CAPABILITIES.items():
            if item['switchable']:
                continue
            with self.subTest(kind=kind):
                reason = item['reason']
                self.assertTrue(reason, '%s 不可切换但没写原因' % kind)
                self.assertGreaterEqual(len(reason), 12, '%s 的原因太短，管理员看不懂' % kind)
                self.assertFalse(item['adapters'], '%s 不可切换却声明了适配器' % kind)

    def test_unknown_kind_is_reported_as_not_applicable(self):
        self.assertFalse(caps.is_switchable(''))
        self.assertFalse(caps.is_switchable('不存在的类型'))
        self.assertTrue(caps.reason_for('不存在的类型'))

    def test_registry_operations_carry_reason_and_adapters(self):
        """registry 暴露给后台的字段：可切换带适配器、不可切换带原因。"""
        catalog = registry.operation_catalog()
        self.assertTrue(catalog)
        switchable = [op for op in catalog if op['channel_eligible']]
        blocked = [op for op in catalog if not op['channel_eligible']]
        self.assertTrue(switchable, '至少要有一个可切换功能（生图/视频）')
        self.assertTrue(blocked, '当前仍有未接入切换的功能，必须如实标记')
        for op in switchable:
            with self.subTest(op=op['operation_id']):
                self.assertEqual('', op['channel_reason'])
                self.assertTrue(op['channel_adapters'])
                self.assertTrue(op['channel_kind'])
        for op in blocked:
            with self.subTest(op=op['operation_id']):
                self.assertFalse(op['channel_kind'])
                self.assertTrue(op['channel_reason'], '%s 没有给不可切换原因' % op['operation_id'])

    def test_image_and_video_switch_are_still_enabled(self):
        """本次改动不能把已经能用的能力收回去。"""
        self.assertIn('image', caps.switchable_kinds())
        self.assertIn('xiaole_video', caps.switchable_kinds())
        self.assertTrue(registry.operation('image.banana.nb2.text')['channel_eligible'])
        self.assertTrue(registry.operation('video.grok.text')['channel_eligible'])

    def test_known_supplier_functions_are_blocked_with_specific_reasons(self):
        """还没接通执行器的，必须明确说明缺什么，而不是含糊的「不支持」。"""
        for operation_id, keyword in (
            ('video.digital_ip.text.single', 'HeyGen'),
            ('video.tryon.fast', 'WaveSpeed'),
            ('audio.tts.public', 'TTS'),
        ):
            with self.subTest(operation_id=operation_id):
                op = registry.operation(operation_id)
                self.assertIsNotNone(op, operation_id)
                self.assertFalse(op['channel_eligible'])
                self.assertIn(keyword, op['channel_reason'])

    def test_sora_is_switchable_by_reusing_the_official_client(self):
        """Sora 已接通：适配器复用原厂 video_openai（它已支持注入 api_key / api_base）。"""
        self.assertIn('sora_video', caps.switchable_kinds())
        self.assertEqual(('sora_video',), caps.adapters_for('sora_video'))
        op = registry.operation('video.sora.text')
        self.assertTrue(op['channel_eligible'])
        self.assertEqual('sora_video', op['channel_kind'])
        self.assertEqual('', op['channel_reason'])

    def test_sora_adapter_reuses_injectable_official_client(self):
        """Sora 适配器的执行必须落到可注入凭据的原厂客户端上，不能自建一套。"""
        import inspect
        from content_domains import video_openai
        from content_domains import channel_runtime
        signature = inspect.signature(video_openai.generate)
        self.assertIn('api_key', signature.parameters)
        self.assertIn('api_base', signature.parameters)
        self.assertIn('download_content', dir(video_openai))
        source = inspect.getsource(channel_runtime._generate_sora)
        self.assertIn('video_openai.generate', source)
        self.assertIn("cfg['secret']", source)
        self.assertIn("cfg['base_url']", source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
