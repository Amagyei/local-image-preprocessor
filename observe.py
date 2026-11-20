"""What the service did, kept as a short history that survives restarts.

One line per thing that happened (a job queued, a photo drawn, a memory retry, a failure),
appended to runtime/events.jsonl and held in memory for the status page. Everything on the
status page's health strip comes from these lines, so there is nothing else to keep in step.

Events never hold photos or prompts: only ids, sizes, timings and the error text. Recording
must never break a draw, so every disk problem is swallowed here.
"""

import collections
import json
import os
import re
import threading
import time


class Observer:
	def __init__(self, path=None, keep=2000):
		self.path = path
		self.keep = keep
		self.lock = threading.Lock()
		self.events = collections.deque(maxlen=keep)
		self._appended = 0
		self._load()

	def _load(self):
		if not self.path:
			return
		try:
			with open(self.path) as f:
				for line in f:
					try:
						event = json.loads(line)
					except ValueError:
						continue  # half a line from a crash
					if isinstance(event, dict):
						self.events.append(event)
		except OSError:
			pass

	def record(self, kind, job=None, **fields):
		event = {"t": round(time.time(), 2), "kind": kind}
		if job:
			event["job"] = job[:12]
		event.update({name: value for name, value in fields.items() if value is not None})
		with self.lock:
			self.events.append(event)
			if self.path:
				try:
					os.makedirs(os.path.dirname(self.path), exist_ok=True)
					self._appended += 1
					if self._appended >= self.keep:
						# Trim the file back to what's in memory, so it never grows without end.
						self._appended = 0
						with open(self.path + ".tmp", "w") as f:
							f.writelines(json.dumps(e) + "\n" for e in self.events)
						os.replace(self.path + ".tmp", self.path)
					else:
						with open(self.path, "a") as f:
							f.write(json.dumps(event) + "\n")
				except (OSError, TypeError, ValueError):
					pass
		return event

	def recent(self, limit=100, job=None):
		"""Newest first. `job` is a job id or its first 12 characters."""
		with self.lock:
			events = list(self.events)
		if job:
			events = [e for e in events if e.get("job") == job[:12]]
		return events[::-1][: max(1, min(limit, self.keep))]

	def timeline(self, job):
		"""Oldest first, for one job."""
		return self.recent(self.keep, job)[::-1]

	def stats(self, now=None):
		now = time.time() if now is None else now
		with self.lock:
			events = list(self.events)
		day_ago = now - 86400
		of = lambda kind: [e for e in events if e["kind"] == kind]
		finished, failed, cancelled = of("finished"), of("failed"), of("cancelled")
		done = len(finished) + len(failed)

		reasons = {}
		for e in failed:
			error = re.sub(r"^Photo \d+ of \d+: ", "", e.get("error", "Unknown"))  # same cause, whichever photo
			reason = reasons.setdefault(error, {"error": error, "count": 0, "last": 0})
			reason["count"] += 1
			reason["last"] = max(reason["last"], e["t"])

		return {
			"window": {"events": len(events), "since": events[0]["t"] if events else None},
			"jobs": {
				"finished": len(finished),
				"failed": len(failed),
				"cancelled": len(cancelled),
				"success_rate": round(len(finished) / done, 3) if done else None,
				"finished_24h": sum(1 for e in finished if e["t"] >= day_ago),
				"failed_24h": sum(1 for e in failed if e["t"] >= day_ago),
			},
			"photo_seconds": _spread([e["seconds"] for e in of("panel_done") if "seconds" in e]),
			"job_seconds": _spread([e["seconds"] for e in finished if "seconds" in e]),
			"queue_wait_seconds": _spread([e["waited"] for e in of("started") if "waited" in e]),
			"memory": {
				"retries": len(of("oom_retry")),
				"waits": len(of("memory_wait")),
				"wait_seconds": round(sum(e.get("seconds", 0) for e in of("memory_wait")), 1),
			},
			"restarts": len(of("startup")),
			"last_success": finished[-1]["t"] if finished else None,
			"last_failure": {"t": failed[-1]["t"], "job": failed[-1].get("job"), "error": failed[-1].get("error")} if failed else None,
			"failure_reasons": sorted(reasons.values(), key=lambda r: -r["count"])[:5],
		}


def _spread(values):
	"""Count, median, 95th percentile and slowest of some timings."""
	if not values:
		return {"count": 0, "p50": None, "p95": None, "max": None}
	ordered = sorted(values)
	rank = lambda p: ordered[min(len(ordered) - 1, max(0, -(-len(ordered) * p // 100) - 1))]
	return {"count": len(ordered), "p50": round(rank(50), 1), "p95": round(rank(95), 1), "max": round(ordered[-1], 1)}
