"""Offline contract/compiler checks; the real browser regression runs separately in Linux CI."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from content_domains import editorial_contract, editorial_markup
from scripts.check_editorial_layout import CASES, REPORTED_ENGLISH, fixture_plan


class EditorialLayoutTests(unittest.TestCase):
    def test_reported_and_widest_fixtures_remain_accepted(self):
        self.assertEqual(58, len(REPORTED_ENGLISH))
        self.assertEqual(58, len(CASES['wide-58'][0]))
        for name, english in CASES.items():
            with self.subTest(name=name):
                self.assertEqual(fixture_plan(english),
                    editorial_contract.validate_plan(fixture_plan(english), 5))

    def test_short_accent_and_mixed_pair_regressions_are_not_rejected(self):
        plan = fixture_plan(CASES['accent-short'])
        self.assertEqual(['内容'], plan['keywords'])
        self.assertEqual('Small', plan['captions'][0]['en'])
        self.assertEqual('内容有价值', plan['captions'][0]['text'])
        mixed = fixture_plan(CASES['mixed-three-pairs'])
        self.assertEqual(6, len(mixed['captions']))
        self.assertEqual(mixed, editorial_contract.validate_plan(mixed, 5))
        html = editorial_markup.make_html({**plan, 'duration': 5})
        self.assertIn('height:108px;font-size:54px;line-height:84px', html)
        self.assertNotIn('data-layout-allow-overlap', html)

    def test_fixed_fonts_are_ready_before_timeline_registration(self):
        html = editorial_markup.make_html({**fixture_plan(CASES['reported-both']), 'duration': 5})
        self.assertLess(html.index('document.fonts.load'), html.index('const tl='))
        self.assertLess(html.index('probe.remove()'), html.index('window.__timelines["main"]=tl'))
        self.assertIn('overflowWrap', html)
        self.assertIn('size>=20', html)
        self.assertNotIn('text-overflow:ellipsis', html)
        self.assertNotIn('data-layout-allow-overflow class="english"', html)

    def test_probe_preserves_text_escaping_and_does_not_execute_captions(self):
        html = editorial_markup.make_html({**fixture_plan(CASES['escaped-text']), 'duration': 5})
        self.assertIn('&lt;b&gt;literal&lt;/b&gt;', html)
        self.assertNotIn('<b>literal</b>', html)
        self.assertIn('english.cloneNode(true)', html)
        self.assertIn("probe.removeAttribute('id')", html)


if __name__ == '__main__':
    unittest.main()
