"""Keep the tracked entry tied to one immutable, media-only release."""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

class Frames(HTMLParser):
    def __init__(self):
        super().__init__()
        self.frames = []
    def handle_starttag(self, tag, attrs):
        if tag == 'iframe':
            self.frames.append(dict(attrs))

class DemoReleaseTests(unittest.TestCase):
    def test_entry_and_artifact_contract(self):
        manifest = json.loads((ROOT / 'deploy/zelong/ip12-demo-release.json').read_text())
        self.assertEqual(manifest['domain'], 'zelong.huangquechuanmei.com')
        self.assertRegex(manifest['release'], r'^\d{8}-[a-f0-9]{12}$')
        self.assertEqual(manifest['path'], '/ip12-demo/releases/' + manifest['release'] + '/')
        parser = Frames()
        parser.feed((ROOT / 'site/ip12-demo/index.html').read_text())
        self.assertEqual(len(parser.frames), 1)
        self.assertTrue(parser.frames[0]['title'])
        self.assertEqual(parser.frames[0]['src'], 'https://' + manifest['domain'] + manifest['path'] + 'showcase')
        names = [item['path'] for item in manifest['files']]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 28)
        self.assertIn('showcase.html', names)
        for item in manifest['files']:
            self.assertTrue(item['path'] == 'showcase.html' or re.fullmatch(r'media/[a-zA-Z0-9_-]+\.mp4', item['path']))
            self.assertRegex(item['sha256'], r'^[a-f0-9]{64}$')
            self.assertGreater(item['size'], 0)

if __name__ == '__main__':
    unittest.main()
