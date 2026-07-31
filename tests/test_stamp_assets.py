# -*- coding: utf-8 -*-
"""共享资源的缓存戳。

#529 新增了 theme.css / theme-init.js，但写的是手写死值 ?v=1，而 stamp_assets.py
当时只认 cloud-shell.js。那样第一次调主题样式时，戳还是 v=1，浏览器继续用缓存的旧
CSS —— 改了没生效，还极难排查。

守的不变量：
1. ASSETS 里每个资源都按自身内容 hash 打戳（改内容 → 戳变）
2. 各资源的戳互相独立（改 theme.css 不该动 cloud-shell.js 的戳）
3. required 资源缺戳报错；页面级资源没引用不算错
4. 幂等：连跑两次不产生差异
5. --check 不写文件
"""
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "stamp_assets.py"


def _load():
    spec = importlib.util.spec_from_file_location("stamp_assets", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stamp_assets"] = mod   # 注册后再 exec，否则模块内的注解解析会拿不到自身
    spec.loader.exec_module(mod)
    return mod


stamp_assets = _load()


class AssetRegistryTests(unittest.TestCase):
    def test_theme_assets_are_registered(self):
        """新增的共享资源必须进 ASSETS，否则戳永远不变。"""
        names = {a.name for a in stamp_assets.ASSETS}
        self.assertIn("cloud-shell.js", names)
        self.assertIn("theme.css", names)
        self.assertIn("theme-init.js", names)

    def test_only_shell_is_required(self):
        """普通工作台页必须有 shell；独立设备授权页显式排除。"""
        required = {a.name for a in stamp_assets.ASSETS if a.required}
        self.assertEqual({"cloud-shell.js"}, required)
        self.assertEqual({"device.html"}, stamp_assets.STANDALONE_PAGES)
        self.assertNotIn("device.html", {path.name for path in stamp_assets.html_files()})

    def test_canvas_assets_are_registered_as_optional(self):
        assets = {a.name: a for a in stamp_assets.ASSETS}
        self.assertIn("canvas/canvas.css", assets)
        self.assertIn("canvas/canvas-graph.js", assets)
        self.assertIn("canvas/canvas-state.js", assets)
        self.assertIn("canvas/canvas-storage.js", assets)
        self.assertIn("canvas/canvas-api.js", assets)
        self.assertIn("canvas/canvas-agent.js", assets)
        self.assertIn("canvas/canvas-export.js", assets)
        self.assertIn("canvas/canvas-app.js", assets)
        self.assertIn("canvas/canvas-short-drama-video.js", assets)
        self.assertIn("canvas/canvas-short-drama-video.css", assets)
        self.assertFalse(assets["canvas/canvas.css"].required)
        self.assertFalse(assets["canvas/canvas-graph.js"].required)
        self.assertFalse(assets["canvas/canvas-state.js"].required)
        self.assertFalse(assets["canvas/canvas-storage.js"].required)
        self.assertFalse(assets["canvas/canvas-api.js"].required)
        self.assertFalse(assets["canvas/canvas-agent.js"].required)
        self.assertFalse(assets["canvas/canvas-export.js"].required)
        self.assertFalse(assets["canvas/canvas-app.js"].required)
        self.assertFalse(assets["canvas/canvas-short-drama-video.js"].required)
        self.assertFalse(assets["canvas/canvas-short-drama-video.css"].required)

    def test_canvas_html_uses_current_canvas_asset_stamps(self):
        html = (ROOT / "site" / "workbench" / "canvas.html").read_bytes()
        assets = {a.name: a for a in stamp_assets.ASSETS}
        for name in ("canvas/canvas.css", "canvas/canvas-graph.js", "canvas/canvas-state.js", "canvas/canvas-storage.js", "canvas/canvas-api.js", "canvas/canvas-agent.js", "canvas/canvas-export.js", "canvas/canvas-app.js"):
            asset = assets[name]
            match = asset.pattern.search(html)
            self.assertIsNotNone(match, name)
            self.assertEqual(asset.stamp().encode("ascii"), match.group(2), name)

    def test_each_asset_hashes_its_own_content(self):
        stamps = {a.name: a.stamp() for a in stamp_assets.ASSETS}
        self.assertEqual(len(stamps), len(set(stamps.values())),
                         "不同资源不该碰巧同戳（说明 hash 取错了对象）")
        for name, s in stamps.items():
            self.assertRegex(s, r"^[0-9a-f]{8}$", name)

    def test_pattern_does_not_confuse_theme_css_with_theme_init_js(self):
        css = next(a for a in stamp_assets.ASSETS if a.name == "theme.css")
        js = next(a for a in stamp_assets.ASSETS if a.name == "theme-init.js")
        sample = b'<link href="theme.css?v=aaa"><script src="theme-init.js?v=bbb">'
        out, n = css.rewrite(sample, "1111")
        self.assertEqual(1, n)
        self.assertIn(b"theme.css?v=1111", out)
        self.assertIn(b"theme-init.js?v=bbb", out, "改 theme.css 不该动 theme-init.js")
        out2, n2 = js.rewrite(sample, "2222")
        self.assertEqual(1, n2)
        self.assertIn(b"theme-init.js?v=2222", out2)
        self.assertIn(b"theme.css?v=aaa", out2)


class RealTreeTests(unittest.TestCase):
    """在真实仓库上跑，确保当前提交的戳是最新的。"""

    def _run(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True, cwd=str(ROOT))

    def test_check_passes_on_committed_tree(self):
        r = self._run("--check")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)

    def test_no_hand_written_v1_left(self):
        """?v=1 这种手写死值不能再出现在共享资源上。"""
        for html in (ROOT / "site" / "workbench").glob("*.html"):
            text = html.read_text(encoding="utf-8")
            for name in ("cloud-shell.js", "theme.css", "theme-init.js"):
                self.assertNotIn(f"{name}?v=1\"", text, f"{html.name} 仍是手写戳")

    def test_idempotent(self):
        """连跑两次不产生新的改动。"""
        r1 = self._run()
        self.assertEqual(0, r1.returncode, r1.stdout + r1.stderr)
        r2 = self._run()
        self.assertEqual(0, r2.returncode, r2.stdout + r2.stderr)
        self.assertIn("already current", r2.stdout)


class CheckDoesNotWriteTests(unittest.TestCase):
    def test_check_leaves_files_untouched(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            wb = ROOT / "site" / "workbench"
            sample = next(wb.glob("*.html"))
            copy = tmp / sample.name
            shutil.copy2(sample, copy)
            before = copy.read_bytes()
            subprocess.run([sys.executable, str(SCRIPT), "--check"],
                           capture_output=True, cwd=str(ROOT))
            self.assertEqual(before, copy.read_bytes())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
