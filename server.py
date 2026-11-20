"""The image model host: answers the laptop's img2img call with the model in config.json.

Takes the call that akwaaba_theme/studio/local_engine.py makes, draws one request at a
time, and keeps each result in memory for a while so that a retried request gets it
back instead of running again. Photos only reach disk for as long as a run needs them.
"""

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import queue
import random
import shutil
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps, UnidentifiedImageError

import panels as panels_module
from engine import GenerationError, SdCli, _free_percent
from observe import Observer

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"  # The laptop comes in over an SSH tunnel. Never listen wider than this.
MAX_BODY = 64 * 1024 * 1024
MAX_SIDE = 4096

log = logging.getLogger("local_image_server")


class BadRequest(Exception):
	pass


class Job:
	def __init__(self, key, prompt, width, height, photo, seed=None):
		self.key = key
		self.seed = seed  # asked for by the caller, e.g. to redo a draw differently
		self.durable = False  # kept on disk until the caller collects it
		self.cancelled = False  # the caller doesn't want it any more
		self.folder = None
		self.created = time.time()
		self.finished_wall = None
		self.started_wall = None  # when the latest attempt began drawing
		self.panel_total = None
		self.panel_seconds = {}  # draw time of each photo, by number
		self.panel_prompts = {}  # what each photo was really drawn from, by number
		self.run_seconds = None
		self.prompt = prompt
		self.width = width
		self.height = height
		self.photo = photo
		self.done = threading.Event()
		self.image = None
		self.status = None
		self.error = None
		self.finished_at = None
		self.progress = None


def parse_request(body):
	"""Check the laptop's JSON and return a Job. Raises BadRequest."""
	try:
		data = json.loads(body)
	except ValueError:
		raise BadRequest("The request wasn't valid JSON.")
	if not isinstance(data, dict):
		raise BadRequest("The request wasn't a JSON object.")

	prompt = data.get("prompt")
	if not isinstance(prompt, str) or not prompt.strip():
		raise BadRequest("The request has no prompt.")
	images = data.get("init_images")
	if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], str):
		raise BadRequest("Send exactly one garment photo in init_images.")
	if data.get("batch_size", 1) != 1:
		raise BadRequest("This service only makes one image per request (batch_size 1).")

	width, height = data.get("width"), data.get("height")
	for side in (width, height):
		if not isinstance(side, int) or isinstance(side, bool) or not 64 <= side <= MAX_SIDE:
			raise BadRequest(f"Width and height must be whole numbers from 64 to {MAX_SIDE}.")

	try:
		photo = base64.b64decode(images[0], validate=True)
	except (binascii.Error, ValueError):
		raise BadRequest("The garment photo wasn't valid base64.")
	try:
		with Image.open(io.BytesIO(photo)) as image:
			image.verify()
	except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
		raise BadRequest("The garment photo couldn't be opened as an image.")

	seed = data.get("seed")
	if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2**31):
		raise BadRequest("The seed must be a whole number from 0 to 2147483647.")

	fingerprint = [prompt, width, height] + ([seed] if seed is not None else [])
	key = hashlib.sha256(json.dumps(fingerprint).encode() + hashlib.sha256(photo).digest()).hexdigest()
	return Job(key, prompt, width, height, photo, seed)


class Service:
	def __init__(self, config):
		self.config = config
		self.engine = SdCli(config)
		self.queue = queue.Queue()
		self.lock = threading.Lock()
		self.jobs = {}
		self.unfinished = {}  # photos already drawn for requests that failed part way
		self.current = None
		self.current_since = None
		self.temp_root = config["temp_dir"]
		self.started = time.time()
		self.observer = Observer(config.get("events_file") or os.path.join(os.path.dirname(self.temp_root), "events.jsonl"))
		self.observer.record("startup", model=config["model_name"])
		self.worker = None
		self.current_since = None
		self._memory = (0, None)  # when the free memory was last read, and what it was
		# Anything here was left by a crash, and it's the customer's photos.
		shutil.rmtree(self.temp_root, ignore_errors=True)
		# Drafts are the exception: they stay until the laptop collects them (or they age out).
		self.jobs_root = config.get("jobs_dir") or os.path.join(os.path.dirname(self.temp_root), "jobs")
		self._load_durable()

	def start(self):
		self.worker = threading.Thread(target=self._work, daemon=True)
		self.worker.start()
		threading.Thread(target=self._prune_forever, daemon=True).start()

	def submit(self, job):
		"""Return the job to wait on: a run of this same request if there is one."""
		panels_module.plan(job.prompt, job.width, job.height, self.config["max_long_side"])
		with self.lock:
			self._prune()
			existing = self.jobs.get(job.key)
			if existing:
				log.info("job=%s same request again, reusing its run", job.key[:12])
				return existing
			self.jobs[job.key] = job
			self.queue.put(job)
			log.info("job=%s queued size=%sx%s waiting=%s", job.key[:12], job.width, job.height, self.queue.qsize())
			self.observer.record("queued", job.key, size=f"{job.width}x{job.height}", waiting=self.queue.qsize() - 1, durable=False)
			return job

	# --- Jobs that outlive the connection, a crash and a restart -------------------------

	def submit_durable(self, job):
		"""Queue a job on disk and return it. The same request again returns the same job;
		one that failed earlier is queued again and carries on from its finished photos."""
		panels_module.plan(job.prompt, job.width, job.height, self.config["max_long_side"])
		with self.lock:
			self._prune()
			existing = self.jobs.get(job.key)
			if existing and existing.durable:
				if existing.done.is_set() and existing.status != 200:
					existing.status, existing.error = None, None
					existing.done.clear()
					self._write_meta(existing, "queued")
					self.queue.put(existing)
					self.observer.record("requeued", existing.key, waiting=self.queue.qsize() - 1)
				return existing
			if existing:  # a plain request is already running or kept: leave it be
				return existing
			job.durable = True
			job.folder = os.path.join(self.jobs_root, job.key)
			os.makedirs(job.folder, exist_ok=True)
			with open(os.path.join(job.folder, "photo.bin"), "wb") as f:
				f.write(job.photo)
			self._write_meta(job, "queued")
			self.jobs[job.key] = job
			self.queue.put(job)
			log.info("job=%s draft queued waiting=%s", job.key[:12], self.queue.qsize())
			self.observer.record("queued", job.key, size=f"{job.width}x{job.height}", waiting=self.queue.qsize() - 1, durable=True)
			return job

	def durable_state(self, key):
		job = self.jobs.get(key)
		if not job or not job.durable:
			return None
		if not job.done.is_set():
			state = "running" if job is self.current else "queued"
		else:
			state = "done" if job.status == 200 else "failed"
		return {
			"job_id": job.key,
			"status": state,
			"progress": job.progress if job is self.current else None,
			"error": job.error,
			"created": job.created,
			"started": job.started_wall,
			"finished": job.finished_wall,
			"run_seconds": job.run_seconds,
			"panels": job.panel_total,
		}

	def durable_detail(self, key):
		"""Everything the status page shows when a job is opened, including the prompt."""
		state = self.durable_state(key)
		if not state:
			return None
		job = self.jobs[key]
		try:
			with open(os.path.join(job.folder, "meta.json")) as f:
				meta = json.load(f)
		except (OSError, ValueError):
			meta = {}
		state.update({
			"prompt": job.prompt, "width": job.width, "height": job.height,
			"seed": meta.get("seed"), "steps": self.config["steps"],
			"panel_prompts": {str(n): t for n, t in sorted(job.panel_prompts.items())},
			"events": self.observer.timeline(key),
			"panel_seconds": {str(n): t for n, t in sorted(job.panel_seconds.items())},
			"panel_files": sorted(
				int(name[6:-4]) for name in os.listdir(job.folder)
				if name.startswith("panel-") and name.endswith(".png")
			) if job.folder and os.path.isdir(job.folder) else [],
			"has_result": os.path.exists(os.path.join(job.folder, "result.png")),
		})
		return state

	def durable_panel(self, key, number):
		job = self.jobs.get(key)
		if not job or not job.durable or not job.done.is_set() or job.status != 200:
			return None
		try:
			with open(os.path.join(job.folder, f"panel-{int(number)}.png"), "rb") as f:
				return "image/png", f.read()
		except (OSError, ValueError):
			return None

	def durable_list(self):
		return [self.durable_state(key) for key, job in list(self.jobs.items()) if job.durable]

	def durable_result(self, key):
		job = self.jobs.get(key)
		if not job or not job.durable or not job.done.is_set() or job.status != 200:
			return None
		return self.image_of(job)

	def durable_forget(self, key):
		"""The laptop is done with this job, drawn or not: forget it, photo and all."""
		with self.lock:
			job = self.jobs.get(key)
			if not job or not job.durable:
				return False
			del self.jobs[key]
			job.cancelled = True
			running = job is self.current
		if running:
			self.engine.cancel()  # the worker removes its folder when it lets go
		else:
			self.unfinished.pop(key, None)
			shutil.rmtree(job.folder, ignore_errors=True)
		log.info("job=%s removed", key[:12])
		self.observer.record("removed", key, running=running)
		return True

	def durable_picture(self, key, thumb=False):
		"""(content type, bytes) of a finished draft for the status page, or None."""
		job = self.jobs.get(key)
		if not job or not job.durable or not job.done.is_set() or job.status != 200:
			return None
		try:
			with open(os.path.join(job.folder, "result.png"), "rb") as f:
				data = f.read()
			if not thumb:
				return "image/png", data
			with Image.open(io.BytesIO(data)) as image:
				image.thumbnail((320, 320))
				out = io.BytesIO()
				image.convert("RGB").save(out, "JPEG", quality=80)
			return "image/jpeg", out.getvalue()
		except OSError:
			return None

	def image_of(self, job):
		if job.image:
			return job.image
		if job.durable and job.status == 200:
			try:
				with open(os.path.join(job.folder, "result.png"), "rb") as f:
					return base64.b64encode(f.read()).decode("ascii")
			except OSError:
				return None
		return None

	def _write_meta(self, job, state, seed=None):
		path = os.path.join(job.folder, "meta.json")
		meta = {}
		try:
			with open(path) as f:
				meta = json.load(f)
		except (OSError, ValueError):
			pass
		meta.update({
			"prompt": job.prompt, "width": job.width, "height": job.height, "requested_seed": job.seed,
			"created": job.created, "state": state, "error": job.error, "finished": job.finished_wall,
			"started": job.started_wall, "panels": job.panel_total, "run_seconds": job.run_seconds,
			"panel_seconds": {str(n): t for n, t in job.panel_seconds.items()},
			"panel_prompts": {str(n): t for n, t in job.panel_prompts.items()},
		})
		if seed is not None:
			meta["seed"] = seed
		with open(path + ".tmp", "w") as f:
			json.dump(meta, f)
		os.replace(path + ".tmp", path)

	def _save_result(self, job, image_b64):
		path = os.path.join(job.folder, "result.png")
		with open(path + ".tmp", "wb") as f:
			f.write(base64.b64decode(image_b64))
		os.replace(path + ".tmp", path)

	def _load_durable(self):
		"""Pick up drafts left by the last run: finished ones wait to be collected, the rest run again."""
		if not os.path.isdir(self.jobs_root):
			return
		found = []
		for key in os.listdir(self.jobs_root):
			folder = os.path.join(self.jobs_root, key)
			try:
				with open(os.path.join(folder, "meta.json")) as f:
					meta = json.load(f)
				photo = None
				if os.path.exists(os.path.join(folder, "photo.bin")):
					with open(os.path.join(folder, "photo.bin"), "rb") as f:
						photo = f.read()
			except (OSError, ValueError):
				shutil.rmtree(folder, ignore_errors=True)
				continue
			job = Job(key, meta["prompt"], meta["width"], meta["height"], photo, meta.get("requested_seed"))
			job.durable, job.folder = True, folder
			job.created, job.finished_wall = meta.get("created", time.time()), meta.get("finished")
			job.started_wall, job.panel_total = meta.get("started"), meta.get("panels")
			job.run_seconds = meta.get("run_seconds")
			job.panel_seconds = {int(n): t for n, t in (meta.get("panel_seconds") or {}).items()}
			job.panel_prompts = {int(n): t for n, t in (meta.get("panel_prompts") or {}).items()}
			if meta.get("state") == "done" and os.path.exists(os.path.join(folder, "result.png")):
				job.status, job.finished_at = 200, time.monotonic()
				job.done.set()
			elif meta.get("state") == "failed" or photo is None:
				job.status, job.error, job.finished_at = 500, meta.get("error") or "Failed before the restart.", time.monotonic()
				job.done.set()
			else:
				found.append(job)
			self.jobs[key] = job
		for job in sorted(found, key=lambda j: j.created):
			self.queue.put(job)
			log.info("job=%s draft resumed after restart", job.key[:12])
			self.observer.record("resumed", job.key)

	def health(self):
		current = self.current
		missing = self.engine.missing_files()
		return {
			"model": self.config["model_name"],
			"uptime_seconds": round(time.time() - self.started),
			"ready": not missing,
			"missing_files": [os.path.basename(path) for path in missing],
			# The model only sits in memory during a run, see engine.py.
			"loaded": bool(current),
			"busy": bool(current),
			"queue_length": self.queue.qsize(),
			"progress": current.progress if current else None,
			"worker_alive": self.worker.is_alive() if self.worker else None,
			"running_seconds": round(time.monotonic() - self.current_since) if current and self.current_since else None,
			"results_kept": sum(1 for job in list(self.jobs.values()) if job.done.is_set()),
			"drafts_waiting": sum(1 for job in list(self.jobs.values()) if job.durable and job.done.is_set() and job.status == 200),
		}

	def metrics(self):
		"""The health strip: history from the event log, plus what is true right now."""
		data = self.observer.stats()
		size, count = 0, 0
		for folder, _, names in os.walk(self.jobs_root):
			if folder != self.jobs_root:
				count += 1
			for name in names:
				try:
					size += os.path.getsize(os.path.join(folder, name))
				except OSError:
					pass
		read_at, free = self._memory
		if time.monotonic() - read_at > 15:
			free = _free_percent()
			self._memory = (time.monotonic(), free)
		data["now"] = self.health()
		data["disk"] = {"jobs": count, "jobs_bytes": size}
		data["memory_free_percent"] = free
		return data

	def stop(self):
		self.engine.stop()
		shutil.rmtree(self.temp_root, ignore_errors=True)

	def _work(self):
		while True:
			job = self.queue.get()
			with self.lock:
				if job.cancelled:
					self.observer.record("cancelled", job.key, stage="queued")
					continue
				self.current = job
				self.current_since = time.monotonic()
			try:
				image = self._run(job)
				job.finished_wall = time.time()
				job.run_seconds = round(sum(job.panel_seconds.values()), 1)
				if job.durable:
					self._save_result(job, image)
					self._write_meta(job, "done")
					# The garment photo isn't needed now. The drawn photos stay, so a grid can be
					# looked at one photo at a time until it's collected.
					if os.path.exists(os.path.join(job.folder, "photo.bin")):
						os.remove(os.path.join(job.folder, "photo.bin"))
				else:
					job.image = image
				job.status = 200
				self.observer.record("finished", job.key, seconds=job.run_seconds, panels=job.panel_total)
			except GenerationError as e:
				self._fail(job, 500, str(e))
			except panels_module.PromptError as e:
				self._fail(job, 400, str(e))
			except Exception as e:  # noqa: BLE001 — one bad run mustn't stop the queue
				log.exception("job=%s unexpected failure", job.key[:12])
				self._fail(job, 500, f"Unexpected error: {e.__class__.__name__}")
			finally:
				if not job.durable:
					job.photo = None
				job.finished_at = time.monotonic()
				with self.lock:
					self.current = None
					forgotten = job.cancelled and job.key not in self.jobs
				if forgotten:
					self.unfinished.pop(job.key, None)
					shutil.rmtree(job.folder, ignore_errors=True)
				job.done.set()

	def _fail(self, job, status, message):
		log.error("job=%s failed: %s", job.key[:12], message)
		if job.cancelled:
			self.observer.record("cancelled", job.key, stage="drawing")
		else:
			self.observer.record("failed", job.key, error=message[:200], status=status, panel=job.progress)
		job.status, job.error = status, message
		if job.durable:
			# Kept, with its finished photos, so the laptop sees the failure and a retry carries on.
			job.finished_wall = time.time()
			try:
				self._write_meta(job, "failed")
			except Exception:  # noqa: BLE001 — a full disk, say: the queue must carry on regardless
				log.exception("job=%s couldn't record its failure", job.key[:12])
			return
		# Failures aren't kept: the next try should really try again.
		with self.lock:
			if self.jobs.get(job.key) is job:
				del self.jobs[job.key]

	def _run(self, job):
		c = self.config
		kept = self.unfinished.get(job.key) or self._saved_progress(job) or {
			"seed": job.seed if job.seed is not None else (c["seed"] if c["seed"] >= 0 else random.randrange(2**31)),
			"panels": {},
			"at": time.monotonic(),
		}
		self.unfinished[job.key] = kept
		if job.durable:
			self._write_meta(job, "running", seed=kept["seed"])
		seed = kept["seed"]
		plan = panels_module.plan(job.prompt, job.width, job.height, c["max_long_side"])
		os.makedirs(self.temp_root, exist_ok=True)
		folder = os.path.join(self.temp_root, job.key[:12])
		os.makedirs(folder, exist_ok=True)
		started = time.monotonic()
		job.started_wall = time.time()
		job.panel_total = len(plan)
		self.observer.record("started", job.key, waited=round(job.started_wall - job.created, 1), panels=len(plan), kept=len(kept["panels"]), seed=seed)
		try:
			reference = os.path.join(folder, "reference.png")
			_save_reference(job.photo, reference, c["reference_long_side"])

			canvas = Image.new("RGB", (job.width, job.height))
			for panel in plan:
				if job.cancelled:
					raise GenerationError("Cancelled.")
				job.progress = f"photo {panel.number} of {len(plan)}"
				left, top, right, bottom = panel.box
				if panel.number in kept["panels"]:
					log.info("job=%s photo=%s/%s kept from the last try", job.key[:12], panel.number, len(plan))
					self.observer.record("panel_kept", job.key, panel=panel.number)
					canvas.paste(Image.open(io.BytesIO(kept["panels"][panel.number])), (left, top))
					continue
				output = os.path.join(folder, f"panel-{panel.number}.png")
				effective = f"{panel.prompt}\n\n{c['garment_instruction']}".strip()
				job.panel_prompts[panel.number] = effective
				try:
					seconds = self.engine.generate(
						prompt=effective,
						reference=reference,
						width=panel.draw_size[0],
						height=panel.draw_size[1],
						seed=seed,
						output=output,
						is_cancelled=lambda: job.cancelled,
						on_event=lambda kind, **fields: self.observer.record(kind, job.key, panel=panel.number, **fields),
					)
				except GenerationError as e:
					raise GenerationError(f"Photo {panel.number} of {len(plan)}: {e}")
				job.panel_seconds[panel.number] = round(seconds, 1)
				self.observer.record(
					"panel_done", job.key, panel=panel.number, seconds=round(seconds, 1), size=f"{panel.draw_size[0]}x{panel.draw_size[1]}",
					words=len(effective.split()), steps=c["steps"], cfg=c["cfg_scale"],
				)
				log.info(
					"job=%s photo=%s/%s draw=%sx%s seed=%s steps=%s cfg=%s words=%s seconds=%.1f",
					job.key[:12], panel.number, len(plan), *panel.draw_size,
					seed, c["steps"], c["cfg_scale"], len(panel.prompt.split()), seconds,
				)
				with Image.open(output) as drawn:
					fitted = ImageOps.fit(drawn.convert("RGB"), (right - left, bottom - top), Image.Resampling.LANCZOS)
				canvas.paste(fitted, (left, top))
				os.remove(output)
				buffer = io.BytesIO()
				fitted.save(buffer, "PNG")
				kept["panels"][panel.number] = buffer.getvalue()
				kept["at"] = time.monotonic()
				if job.durable:
					with open(os.path.join(job.folder, f"panel-{panel.number}.png"), "wb") as f:
						f.write(kept["panels"][panel.number])
					self._write_meta(job, "running")

			self.unfinished.pop(job.key, None)
			buffer = io.BytesIO()
			canvas.save(buffer, "PNG")
			log.info("job=%s finished seconds=%.1f", job.key[:12], time.monotonic() - started)
			return base64.b64encode(buffer.getvalue()).decode("ascii")
		finally:
			shutil.rmtree(folder, ignore_errors=True)

	def _saved_progress(self, job):
		"""Seed and finished photos a draft saved before a restart."""
		if not job.durable:
			return None
		try:
			with open(os.path.join(job.folder, "meta.json")) as f:
				seed = json.load(f).get("seed")
		except (OSError, ValueError):
			return None
		if seed is None:
			return None
		panels = {}
		for name in os.listdir(job.folder):
			if name.startswith("panel-") and name.endswith(".png"):
				with open(os.path.join(job.folder, name), "rb") as f:
					panels[int(name[6:-4])] = f.read()
		return {"seed": seed, "panels": panels, "at": time.monotonic()}

	def _prune_forever(self):
		while True:
			time.sleep(30)
			with self.lock:
				self._prune()

	def _prune(self):
		"""Call with the lock held."""
		limit = time.monotonic() - self.config["result_ttl_seconds"]
		keep_for = self.config.get("draft_retention_days", 30) * 86400
		for key, job in list(self.jobs.items()):
			if job.durable:
				# Drafts wait for the laptop, however long that takes, up to the retention limit.
				if job.done.is_set() and job.finished_wall and time.time() - job.finished_wall > keep_for:
					del self.jobs[key]
					shutil.rmtree(job.folder, ignore_errors=True)
					log.info("job=%s draft expired uncollected", key[:12])
					self.observer.record("expired", key)
			elif job.done.is_set() and job.finished_at is not None and job.finished_at < limit:
				del self.jobs[key]
		current = self.current.key if self.current else None
		for key, kept in list(self.unfinished.items()):
			if key != current and kept["at"] < limit:
				del self.unfinished[key]


def _save_reference(photo, path, long_side):
	with Image.open(io.BytesIO(photo)) as image:
		image = ImageOps.exif_transpose(image).convert("RGB")
	image.thumbnail((long_side, long_side), Image.Resampling.LANCZOS)
	image.save(path, "PNG")


def make_handler(service):
	class Handler(BaseHTTPRequestHandler):
		protocol_version = "HTTP/1.1"
		timeout = None

		def do_GET(self):
			url = urlparse(self.path)
			query = parse_qs(url.query)
			parts = url.path.strip("/").split("/")
			if self.path in ("/", "/ui"):
				self._send_page()
			elif self.path == "/health":
				self._send(200, service.health())
			elif url.path == "/metrics":
				self._send(200, service.metrics())
			elif url.path == "/events":
				try:
					limit = int((query.get("limit") or [100])[0])
				except ValueError:
					limit = 100
				self._send(200, {"events": service.observer.recent(limit, (query.get("job") or [None])[0])})
			elif parts == ["jobs"]:
				jobs = service.durable_list()
				wanted = (query.get("status") or [None])[0]  # e.g. /jobs?status=running
				self._send(200, {"jobs": [j for j in jobs if not wanted or j["status"] == wanted]})
			elif len(parts) == 2 and parts[0] == "jobs":
				state = service.durable_state(parts[1])
				self._send(200, state) if state else self._send(404, {"error": "No such job."})
			elif len(parts) == 3 and parts[0] == "jobs" and parts[2] == "detail":
				detail = service.durable_detail(parts[1])
				self._send(200, detail) if detail else self._send(404, {"error": "No such job."})
			elif len(parts) == 4 and parts[0] == "jobs" and parts[2] == "panel":
				picture = service.durable_panel(parts[1], parts[3])
				self._send_bytes(*picture) if picture else self._send(404, {"error": "No such photo."})
			elif len(parts) == 3 and parts[0] == "jobs" and parts[2] in ("image", "thumb"):
				picture = service.durable_picture(parts[1], thumb=parts[2] == "thumb")
				if picture:
					self._send_bytes(*picture)
				else:
					self._send(404, {"error": "No finished picture for that job."})
			elif len(parts) == 3 and parts[0] == "jobs" and parts[2] == "result":
				state = service.durable_state(parts[1])
				image = service.durable_result(parts[1])
				if image:
					self._send(200, {"images": [image]})
				elif state:
					self._send(409, {"error": state["error"] or f"The job is {state['status']}."})
				else:
					self._send(404, {"error": "No such job."})
			else:
				self._send(404, {"error": "Not found."})

		def do_DELETE(self):
			parts = self.path.strip("/").split("/")
			if len(parts) == 2 and parts[0] == "jobs":
				if service.durable_forget(parts[1]):
					self._send(200, {"removed": True})
				else:
					self._send(404, {"error": "No job with that id."})
			else:
				self._send(404, {"error": "Not found."})

		def do_POST(self):
			if self.path not in ("/sdapi/v1/img2img", "/jobs"):
				self._send(404, {"error": "Not found."})
				return
			try:
				length = int(self.headers.get("Content-Length") or 0)
			except ValueError:
				length = 0
			if not 0 < length <= MAX_BODY:
				self.close_connection = True
				self._send(413 if length else 400, {"error": "The request was empty or over 64 MB."})
				return

			queued = self.path == "/jobs"
			try:
				parsed = parse_request(self.rfile.read(length))
				job = service.submit_durable(parsed) if queued else service.submit(parsed)
			except (BadRequest, panels_module.PromptError) as e:
				self._send(400, {"error": str(e)})
				return
			if queued:
				# Answer at once: the work goes on whether or not the laptop stays connected.
				state = service.durable_state(job.key)
				if state:
					self._send(202, state)
				else:
					self._send(409, {"error": "That request is already running as a live call; try again shortly."})
				return

			# The run belongs to the worker, so it finishes even if this connection drops.
			job.done.wait()
			image = service.image_of(job)
			if image:
				self._send(200, {"images": [image]})
			else:
				self._send(job.status or 500, {"error": job.error or "The run didn't produce an image."})

		def _send_bytes(self, content_type, body):
			try:
				self.send_response(200)
				self.send_header("Content-Type", content_type)
				self.send_header("Content-Length", str(len(body)))
				self.send_header("Cache-Control", "private, max-age=3600")
				self.end_headers()
				self.wfile.write(body)
			except OSError:
				self.close_connection = True

		def _send_page(self):
			"""The status page: a read-only view of /health and /jobs, no photos or prompts."""
			try:
				with open(os.path.join(HERE, "status.html"), "rb") as f:
					body = f.read()
			except OSError:
				self._send(404, {"error": "Not found."})
				return
			try:
				self.send_response(200)
				self.send_header("Content-Type", "text/html; charset=utf-8")
				self.send_header("Content-Length", str(len(body)))
				self.end_headers()
				self.wfile.write(body)
			except OSError:
				self.close_connection = True

		def _send(self, status, payload):
			body = json.dumps(payload).encode()
			try:
				self.send_response(status)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(body)))
				self.end_headers()
				self.wfile.write(body)
			except OSError:
				self.close_connection = True
				log.info("the client left before the answer; a retry will get the kept result")

		def log_message(self, format, *args):
			line = format % args
			# The status page asks every few seconds; keep those out of the log.
			if line.startswith(("GET /health ", "GET /metrics ", "GET /events", "GET /jobs HTTP", "GET /jobs?", "GET / HTTP", "GET /ui ")) and line.endswith(" 200 -"):
				return
			if line.startswith("GET /jobs/") and line.endswith((" 200 -", " 304 -")) and ("/thumb " in line or "/image " in line or "/panel/" in line or "/detail " in line):
				return
			log.info("%s %s", self.address_string(), line)

	return Handler


def load_config(path):
	with open(path) as f:
		config = json.load(f)
	config["sd_cli"] = _resolve(config["sd_cli"])
	config["temp_dir"] = _resolve(config["temp_dir"])
	config["weights"] = {name: _resolve(value) for name, value in config["weights"].items()}
	return config


def _resolve(path):
	return os.path.join(HERE, os.path.expanduser(path))


def make_server(config):
	service = Service(config)
	server = ThreadingHTTPServer((HOST, config["port"]), make_handler(service))
	server.daemon_threads = True
	return server, service


def main():
	logging.basicConfig(
		level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s", force=True
	)
	config = load_config(os.environ.get("LOCAL_IMAGE_SERVER_CONFIG") or os.path.join(HERE, "config.json"))
	server, service = make_server(config)
	service.start()

	def stop(signum, frame):
		raise KeyboardInterrupt

	signal.signal(signal.SIGTERM, stop)
	log.info("listening on %s:%s model=%s", HOST, config["port"], config["model_name"])
	for path in service.engine.missing_files():
		log.warning("missing file: %s", path)
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		service.stop()


if __name__ == "__main__":
	main()
