import os
import shutil
import tempfile
import unittest
from pathlib import Path

from otk import config, importer, oci, pack, reconcile, select
from otk.ledger import Ledger
from tests.fakes import FakeRegistry, make_image, make_index

LOW, HIGH = "low.example.internal", "high.example.internal"


class Lab(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="otk-test-"))
        self.reg = FakeRegistry()
        self.src = self.reg.layout(LOW)
        base = b"base-layer" * 100
        self.v1 = make_image(self.src, [base, b"app-1.0.0"])
        self.v2 = make_image(self.src, [base, b"app-1.1.0"])
        arm = make_image(self.src, [b"arm-base", b"app-1.1.0-arm"], arch="arm64")
        self.multi = make_index(self.src, [(self.v2, "amd64"), (arm, "arm64")])
        self.reg.seed(LOW, "proxy/app", "1.0.0", self.v1)
        self.reg.seed(LOW, "proxy/app", "1.1.0", self.multi)
        self.reg.seed(LOW, "proxy/app", "latest", self.multi)
        self.write_cfg(tags='["1.0.0", "1.1.0", "latest"]')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.reg.root, ignore_errors=True)

    def write_cfg(self, tags, extra=""):
        (self.dir / "otk.toml").write_text(f"""
[low]
registry = "{LOW}"
work_dir = "{self.dir}/low"
outbox = "{self.dir}/diode"
prune = true
[high]
registry = "{HIGH}"
work_dir = "{self.dir}/high"
inbox = "{self.dir}/diode"
max_delete_pct = 50
[[images]]
source = "proxy/app"
target = "mirror/app"
tags = {tags}
{extra}
""")
        self.cfg = config.load(self.dir / "otk.toml")

    def cross(self):
        return importer.run(self.cfg, self.reg, log=lambda *_: None)

    def high(self):
        return self.reg.tags.get((HIGH, "mirror/app"), {})

    def test_roundtrip_keeps_digests_and_tags(self):
        pack.build(self.cfg, self.reg, log=lambda *_: None)
        [r] = self.cross()
        self.assertEqual(r["incomplete"], [])
        self.assertEqual(self.high(), {"1.0.0": self.v1, "1.1.0": self.multi, "latest": self.multi})

    def test_second_pack_carries_only_new_blobs(self):
        pack.build(self.cfg, self.reg, log=lambda *_: None)
        self.cross()
        v3 = make_image(self.src, [b"base-layer" * 100, b"app-1.2.0"])
        self.reg.seed(LOW, "proxy/app", "1.2.0", v3)
        self.write_cfg(tags='["1.0.0", "1.1.0", "1.2.0", "latest"]')
        p = pack.build(self.cfg, self.reg, log=lambda *_: None)
        carried = importer._extract(p, self.dir / "x")["carried"]
        self.assertEqual(len(carried), 2)     # new manifest + new top layer; base layer and config already crossed
        self.cross()
        self.assertEqual(self.high()["1.2.0"], v3)

    def test_unchanged_state_writes_no_pack(self):
        pack.build(self.cfg, self.reg, log=lambda *_: None)
        self.assertIsNone(pack.build(self.cfg, self.reg, log=lambda *_: None))

    def test_lost_pack_is_reported_then_repaired_by_rewind(self):
        pack.build(self.cfg, self.reg, log=lambda *_: None)
        self.cross()
        v3 = make_image(self.src, [b"base-layer" * 100, b"app-1.2.0"])
        self.reg.seed(LOW, "proxy/app", "1.2.0", v3)
        self.write_cfg(tags='["1.0.0", "1.1.0", "1.2.0", "latest"]')
        lost = pack.build(self.cfg, self.reg, log=lambda *_: None)
        os.remove(lost)                                   # pack 2 never arrives
        os.remove(str(lost) + ".sha256")
        self.reg.seed(LOW, "proxy/app", "latest", v3)
        pack.build(self.cfg, self.reg, log=lambda *_: None)   # pack 3: retarget only, 1.2.0 blobs "already sent"
        [r] = self.cross()
        self.assertTrue(r["incomplete"])
        self.assertEqual(importer.Store(self.cfg.high.work_dir).gaps(), [2])
        lg = Ledger(self.cfg.low.work_dir)
        lg.rewind(2)
        lg.save()
        pack.build(self.cfg, self.reg, log=lambda *_: None)   # pack 4 re-carries what pack 2 held
        [r] = self.cross()
        self.assertEqual(r["incomplete"], [])
        self.assertEqual(self.high()["1.2.0"], v3)
        self.assertEqual(self.high()["latest"], v3)

    def test_retarget_and_delete_shared_digest(self):
        pack.build(self.cfg, self.reg, log=lambda *_: None)
        self.cross()
        self.write_cfg(tags='["1.1.0", "latest"]')         # drop 1.0.0; latest and 1.1.0 share a digest
        pack.build(self.cfg, self.reg, log=lambda *_: None)
        [r] = self.cross()
        self.assertEqual(r["deleted"], ["mirror/app:1.0.0"])
        self.assertEqual(self.high(), {"1.1.0": self.multi, "latest": self.multi})

    def test_mass_delete_is_refused(self):
        pack.build(self.cfg, self.reg, log=lambda *_: None)
        self.cross()
        self.write_cfg(tags='["1.0.0"]')                    # would delete 2 of 3 tags
        pack.build(self.cfg, self.reg, log=lambda *_: None)
        [r] = self.cross()
        self.assertEqual(r["refused"], ["mirror/app"])
        self.assertEqual(len(self.high()), 3)

    def test_prune_removes_only_unreachable_blobs(self):
        pack.build(self.cfg, self.reg, log=lambda *_: None)
        self.cross()
        store = Path(self.cfg.high.work_dir) / "store"
        only_v1 = [d for d in oci.graph(store, self.v1) if d not in oci.graph(store, self.multi)]
        self.write_cfg(tags='["1.1.0", "latest"]')
        pack.build(self.cfg, self.reg, log=lambda *_: None)
        self.cross()
        self.assertEqual(oci.missing(store, only_v1), only_v1)
        self.assertEqual(oci.missing(store, oci.graph(store, self.multi)), [])

    def test_corrupt_pack_is_rejected_and_parked(self):
        p = pack.build(self.cfg, self.reg, log=lambda *_: None)
        with open(p, "ab") as f:
            f.write(b"x")
        [r] = self.cross()
        self.assertIn("checksum mismatch", r["error"])
        self.assertTrue((Path(self.cfg.inbox) / "failed" / p.name).exists())
        self.assertEqual(self.high(), {})

    def test_multi_arch_graph_includes_every_platform(self):
        g = oci.graph(self.src, self.multi)
        self.assertEqual(g[0], self.multi)
        self.assertEqual(len(g), 1 + 2 * 4)                # index + 2 x (manifest, config, 2 layers)


class Pure(unittest.TestCase):
    def test_semver_newest(self):
        tags = ["1.2.0", "1.10.0", "v1.9.9", "latest", "2.0.0-rc1", "1.2.1", "2.0.0"]
        self.assertEqual(select.newest(tags, 3), ["2.0.0", "1.10.0", "v1.9.9"])
        self.assertEqual(select.newest(tags, 2, prefix="1.2."), ["1.2.1", "1.2.0"])

    def test_reconcile_plan(self):
        self.assertEqual(reconcile.plan({"a": "1", "b": "2"}, {"a": "1", "c": "3"}, 50), (["c"], ["b"], False))
        self.assertEqual(reconcile.plan({"a": "1", "b": "2", "c": "3"}, {"a": "1"}, 50), ([], [], True))

    def test_config_rejects_duplicate_targets(self):
        d = Path(tempfile.mkdtemp())
        (d / "c.toml").write_text('[low]\nregistry="x"\n[[images]]\nsource="a"\ntarget="t"\ntags=["1"]\n'
                                  '[[images]]\nsource="b"\ntarget="t"\ntags=["1"]\n')
        with self.assertRaises(config.ConfigError):
            config.load(d / "c.toml")


if __name__ == "__main__":
    unittest.main()
