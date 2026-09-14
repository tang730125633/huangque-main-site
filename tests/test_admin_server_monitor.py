import pathlib
import unittest
from unittest import mock

from server import admin_api


class AdminServerMonitorTests(unittest.TestCase):
    def setUp(self):
        admin_api._KOMARI_CACHE.update({"at": 0.0, "value": None})

    def test_snapshot_normalizes_live_nodes_without_exposing_uuid(self):
        nodes = [
            {
                "uuid": "primary-secret-id",
                "name": "Novix 主生产网络",
                "region": "🇺🇸 洛杉矶",
                "public_remark": "NovixLink",
                "group": "生产网络",
                "os": "Ubuntu 22.04 LTS",
                "arch": "amd64",
                "cpu_cores": 2,
                "mem_total": 1000,
                "disk_total": 2000,
            },
            {
                "uuid": "backup-secret-id",
                "name": "搬瓦工生产备用网络",
                "region": "🇺🇸 洛杉矶",
                "public_remark": "BandwagonHost",
                "group": "生产网络",
                "os": "Debian 12",
            },
        ]
        samples = {
            "primary-secret-id": [{
                "updated_at": "1970-01-01T00:16:35.123456789Z",
                "cpu": {"usage": 12.34},
                "ram": {"used": 500, "total": 1000},
                "disk": {"used": 1000, "total": 2000},
                "network": {"up": 10, "down": 20, "totalUp": 30, "totalDown": 40},
                "uptime": 50,
            }],
            "backup-secret-id": [],
        }

        attempts = {"primary-secret-id": 0}

        def fetch(path):
            if path == "/api/nodes":
                return nodes
            node_id = path.rsplit("/", 1)[-1]
            if node_id == "primary-secret-id" and attempts[node_id] == 0:
                attempts[node_id] += 1
                raise TimeoutError("transient read timeout")
            return samples[node_id]

        with mock.patch.object(admin_api, "_komari_get", side_effect=fetch), mock.patch.object(
            admin_api, "_render_relay_nodes", return_value={}
        ):
            data = admin_api.server_monitor_snapshot(force=True, now=1000)

        self.assertEqual(data["summary"], {
            "total": 2, "online": 1, "warning": 0, "offline": 1,
            "render_running": 0, "gpu_active": 0,
            "render_telemetry_available": False,
        })
        self.assertEqual(data["items"][0]["role"], "primary_network")
        self.assertEqual(attempts["primary-secret-id"], 1)
        self.assertEqual(data["items"][0]["memory"], 50.0)
        self.assertEqual(data["items"][1]["role"], "backup_network")
        self.assertNotIn("uuid", data["items"][0])

    def test_snapshot_adds_render_gpu_without_exposing_node_key(self):
        nodes = [{
            "uuid": "gpu-secret-id", "name": "Tang GPU 渲染节点",
            "tags": "gpu,rtx3060,template-render", "region": "局域网",
        }]
        sample = [{
            "updated_at": "1970-01-01T00:16:35Z", "cpu": {"usage": 20},
            "ram": {}, "disk": {}, "network": {},
        }]
        gpu = {"utilization": 48, "encoder": 31, "memory_used": 1024,
               "memory_total": 4096, "temperature": 40, "power": 55,
               "name": "RTX 3060", "sample_age_seconds": 2}
        with mock.patch.object(
            admin_api, "_komari_get", side_effect=[nodes, sample]
        ), mock.patch.object(admin_api, "_render_relay_nodes", return_value={
            "tang": {"online": True, "running": 2, "gpu": gpu}
        }):
            data = admin_api.server_monitor_snapshot(force=True, now=1000)
        self.assertEqual(48, data["items"][0]["gpu"]["utilization"])
        self.assertEqual(2, data["items"][0]["render_running"])
        self.assertNotIn("tang", data["items"][0])

    def test_admin_page_wires_server_module_and_read_only_endpoint(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        page = (root / "site/admin/index.html").read_text(encoding="utf-8")
        api_source = pathlib.Path(admin_api.__file__).read_text(encoding="utf-8")
        nginx = (root / "deploy/zelong/komari-monitor.nginx.conf").read_text(encoding="utf-8")
        self.assertIn('data-module-tab="servers"', page)
        self.assertIn("/api/admin/server-monitor", page)
        self.assertIn('path == "/api/admin/server-monitor"', api_source)
        self.assertIn("allow 129.204.166.13;", nginx)
        self.assertIn("location ^~ /komari-agent/", nginx)


if __name__ == "__main__":
    unittest.main()
