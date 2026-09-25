"""Low side record of every blob already sent, keyed by the pack sequence that carried it.

A one-way link gives no receipt, so a blob counts as sent when its pack is written. Losing a pack
is repaired with rewind(seq): every blob first sent in that pack or later is forgotten and the next
pack carries it again."""

import json
import os
from pathlib import Path


class Ledger:
    def __init__(self, work_dir):
        self.path = Path(work_dir) / "ledger.json"
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.blobs = data.get("blobs", {})
        self.last_seq = data.get("last_seq", 0)

    def __contains__(self, digest):
        return digest in self.blobs

    def record(self, seq, digests):
        for d in digests:
            self.blobs.setdefault(d, seq)
        self.last_seq = max(self.last_seq, seq)

    def forget(self, digests):
        for d in digests:
            self.blobs.pop(d, None)

    def rewind(self, seq):
        gone = [d for d, s in self.blobs.items() if s >= seq]
        self.forget(gone)
        return len(gone)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(".ledger.json.tmp")
        tmp.write_text(json.dumps({"last_seq": self.last_seq, "blobs": self.blobs}, indent=0, sort_keys=True))
        os.replace(tmp, self.path)
