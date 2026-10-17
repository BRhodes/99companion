"""Datagrams in, tracker events out: per-direction reassembly, message decoding, dispatch."""

import logging
import time
from collections import Counter

from .framing import Reassembler
from .messages import decode

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, tracker):
        self.tracker = tracker
        self.reassemblers = {"c2s": Reassembler(), "s2c": Reassembler()}
        self.stats = Counter()

    def reset(self):
        for r in self.reassemblers.values():
            r.reset()

    def feed(self, direction, data, now=None):
        self.tracker.last_packet = time.time() if now is None else now
        for group, blob in self.reassemblers[direction].feed(data, now):
            self.stats["messages"] += 1
            for ev in decode(direction, group, blob):
                self.stats[ev["type"]] += 1
                if ev["type"] == "decode_error":
                    log.debug("decode error: %s", ev)
                    continue
                self.tracker.handle(ev, now)
