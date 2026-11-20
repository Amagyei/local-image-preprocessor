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
import re
import shutil
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

import panels as panels_module
from engine import GenerationError, SdCli, _free_percent
from observe import Observer

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"  # The laptop comes in over an SSH tunnel. Never listen wider than this.
MAX_BODY = 64 * 1024 * 1024
MAX_SIDE = 4096
MAX_BATCH_JOBS = 50
BATCH_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

log = logging.getLogger("local_image_server")


class BadRequest(Exception):
	pass


class Job:
	def __init__(self, key, prompt, width, height, photo, seed=None, mask=None, strength=None, keep_masked=True,
			auto_canvas=False, hem=None):
		self.key = key
		self.auto_canvas = auto_canvas  # map the garment here and build the mask (canvas_map.py)
		self.hem = hem  # where the hem is, as a fraction of the photo's height; below it goes
		self.mask = mask  # canvas mode: white = draw here, black = keep the photo's pixels
		self.strength = strength
		self.keep_masked = keep_masked  # paste the photo's own pixels back where the mask is black
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
	return job_from(data)


def job_from(data):
	"""Check one job's fields (already decoded) and return a Job. Raises BadRequest."""
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

	auto, hem = _auto_fields(data)
	mask, strength, keep_masked = _canvas_fields(data, photo, auto)

	fingerprint = [prompt, width, height] + ([seed] if seed is not None else [])
	if mask is not None:  # only canvas jobs carry these, so earlier keys stay the same
		fingerprint.append({"mask": hashlib.sha256(mask).hexdigest(), "strength": strength, "keep": keep_masked})
	elif auto:
		fingerprint.append({"canvas": "auto", "hem": hem, "strength": strength, "keep": keep_masked})
	key = hashlib.sha256(json.dumps(fingerprint).encode() + hashlib.sha256(photo).digest()).hexdigest()
	return Job(key, prompt, width, height, photo, seed, mask, strength, keep_masked, auto, hem)


def _auto_fields(data):
	"""{"canvas": "auto", "hem": 0-1}: this Mac maps the garment and makes the mask itself."""
	canvas = data.get("canvas")
	if canvas is None:
		if "hem" in data:
			raise BadRequest("hem only applies with \"canvas\": \"auto\".")
		return False, None
	if canvas != "auto":
		raise BadRequest('canvas must be "auto" (or send your own mask instead).')
	if "mask" in data:
		raise BadRequest('Send a mask or "canvas": "auto", not both.')
	hem = data.get("hem")
	if hem is not None and (not isinstance(hem, (int, float)) or isinstance(hem, bool) or not 0 < hem <= 1):
		raise BadRequest("hem is where the hem is, as a fraction of the photo's height: above 0, at most 1.")
	return True, (float(hem) if hem is not None else None)


def _canvas_fields(data, photo, auto=False):
	"""Optional canvas mode: {"mask": base64 PNG, "strength": 0-1, "keep_masked_pixels": bool}.

	The mask is the size of the photo: white where the model draws, black where the photo stays.
	Returns (mask bytes or None, strength, keep_masked)."""
	raw = data.get("mask")
	if raw is None and auto:
		return None, *_canvas_options(data)
	if raw is None:
		for field in ("strength", "keep_masked_pixels"):
			if field in data:
				raise BadRequest(f"{field} only applies with a mask.")
		return None, None, True
	if not isinstance(raw, str):
		raise BadRequest("The mask must be a base64 image.")
	try:
		mask = base64.b64decode(raw, validate=True)
	except (binascii.Error, ValueError):
		raise BadRequest("The mask wasn't valid base64.")
	try:
		with Image.open(io.BytesIO(mask)) as m, Image.open(io.BytesIO(photo)) as p:
			m_size, p_size = m.size, ImageOps.exif_transpose(p).size
			extrema = m.convert("L").getextrema()
	except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
		raise BadRequest("The mask couldn't be opened as an image.")
	if m_size != p_size:
		raise BadRequest(f"The mask is {m_size[0]}x{m_size[1]} but the photo is {p_size[0]}x{p_size[1]}; they must match.")
	if extrema[1] < 128:
		raise BadRequest("The mask has no white area, so there is nothing to draw.")
	return mask, *_canvas_options(data)


def _canvas_options(data):
	strength = data.get("strength", 1.0)
	if not isinstance(strength, (int, float)) or isinstance(strength, bool) or not 0 < strength <= 1:
		raise BadRequest("The strength must be a number above 0 and at most 1.")
	keep = data.get("keep_masked_pixels", True)
	if not isinstance(keep, bool):
		raise BadRequest("keep_masked_pixels must be true or false.")
	return float(strength), keep


def parse_batch(body):
	"""Check a batch and return (batch id, jobs). Raises BadRequest.

	The body is {"batch_id": optional, "init_images": [shared photo], "jobs": [{...}, ...]}. Each job
	is what POST /jobs takes; a job's own init_images replaces the shared photo. Every job is checked
	before any is queued, so a bad one refuses the whole batch."""
	try:
		data = json.loads(body)
	except ValueError:
		raise BadRequest("The request wasn't valid JSON.")
	if not isinstance(data, dict):
		raise BadRequest("The request wasn't a JSON object.")
	items = data.get("jobs")
	if not isinstance(items, list) or not items:
		raise BadRequest("Send a list of jobs.")
	if len(items) > MAX_BATCH_JOBS:
		raise BadRequest(f"A batch holds at most {MAX_BATCH_JOBS} jobs.")
	batch_id = data.get("batch_id")
	if batch_id is None:
		batch_id = uuid.uuid4().hex[:16]
	elif not isinstance(batch_id, str) or not BATCH_ID.match(batch_id):
		raise BadRequest("The batch_id must be 1 to 64 letters, digits, - or _.")
	shared = data.get("init_images")

	jobs, seen = [], set()
	for number, item in enumerate(items, start=1):
		if not isinstance(item, dict):
			raise BadRequest(f"Job {number} wasn't a JSON object.")
		merged = {"batch_size": 1, **({"init_images": shared} if shared is not None else {}), **item}
		try:
			job = job_from(merged)
		except BadRequest as e:
			raise BadRequest(f"Job {number}: {e}")
		if job.key not in seen:  # the same drawing twice is one drawing
			seen.add(job.key)
			jobs.append(job)
	return batch_id, jobs


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
		# A batch only remembers which jobs belong together; the jobs hold everything else.
		self.batches_root = config.get("batches_dir") or os.path.join(os.path.dirname(self.jobs_root), "batches")
		self.batches = {}
		self._load_batches()

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
			if job.mask is not None:
				with open(os.path.join(job.folder, "mask.bin"), "wb") as f:
					f.write(job.mask)
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
			"canvas": "auto" if job.auto_canvas else job.mask is not None, "hem": job.hem,
			"strength": job.strength, "keep_masked": job.keep_masked,
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
				photo = mask = None
				if os.path.exists(os.path.join(folder, "photo.bin")):
					with open(os.path.join(folder, "photo.bin"), "rb") as f:
						photo = f.read()
				if os.path.exists(os.path.join(folder, "mask.bin")):
					with open(os.path.join(folder, "mask.bin"), "rb") as f:
						mask = f.read()
			except (OSError, ValueError):
				shutil.rmtree(folder, ignore_errors=True)
				continue
			auto = meta.get("canvas") == "auto"
			if meta.get("canvas") is True and mask is None and meta.get("state") != "done":
				photo = None  # a canvas job without its mask can't be drawn as asked
			job = Job(
				key, meta["prompt"], meta["width"], meta["height"], photo, meta.get("requested_seed"),
				mask, meta.get("strength"), meta.get("keep_masked", True), auto, meta.get("hem"),
			)
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

	# --- Batches: many jobs queued, watched and collected together ------------------------

	def submit_batch(self, batch_id, jobs):
		"""Queue every job as a draft and remember they belong together. Sending the same batch again
		queues nothing twice; jobs that are new are added to it."""
		for number, job in enumerate(jobs, start=1):
			try:
				panels_module.plan(job.prompt, job.width, job.height, self.config["max_long_side"])
			except panels_module.PromptError as e:
				raise panels_module.PromptError(f"Job {number}: {e}")
		keys = [self.submit_durable(job).key for job in jobs]
		with self.lock:
			batch = self.batches.get(batch_id) or {"id": batch_id, "created": time.time(), "job_ids": []}
			batch["job_ids"] += [key for key in keys if key not in batch["job_ids"]]
			self.batches[batch_id] = batch
			self._write_batch(batch)
		self.observer.record("batch_queued", batch=batch_id, jobs=len(keys), waiting=self.queue.qsize())
		return batch_id

	def batch_state(self, batch_id):
		batch = self.batches.get(batch_id)
		if not batch:
			return None
		counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "missing": 0}
		jobs = []
		for key in list(batch["job_ids"]):
			state = self.durable_state(key) or {"job_id": key, "status": "missing", "error": "The Mac no longer has this job."}
			counts[state["status"]] += 1
			jobs.append(state)
		left = counts["queued"] + counts["running"]
		if left:
			status = "queued" if counts["queued"] == len(jobs) else "running"
		elif counts["failed"]:
			status = "failed"  # nothing is left to draw, and some didn't come out
		elif counts["done"]:
			status = "done"
		else:
			status = "empty"
		finished = [j["finished"] for j in jobs if j.get("finished")]
		return {
			"batch_id": batch_id,
			"status": status,
			"total": len(jobs),
			"counts": counts,
			"created": batch["created"],
			"finished": max(finished) if status in ("done", "failed") and finished else None,
			"jobs": jobs,
		}

	def batch_list(self):
		out = []
		for batch_id in list(self.batches):
			state = self.batch_state(batch_id)
			if state:
				out.append({k: state[k] for k in ("batch_id", "status", "total", "counts", "created", "finished")})
		return sorted(out, key=lambda b: b["created"])

	def batch_results(self, batch_id):
		"""The pictures that are ready, by job id, so a batch can be collected in part."""
		batch = self.batches.get(batch_id)
		if not batch:
			return None
		images, pending, failed = {}, 0, []
		for key in list(batch["job_ids"]):
			image = self.durable_result(key)
			state = self.durable_state(key)
			if image:
				images[key] = image
			elif state and state["status"] == "failed":
				failed.append(key)
			elif state:
				pending += 1
		return {"batch_id": batch_id, "order": list(batch["job_ids"]), "images": images, "pending": pending, "failed": failed}

	def batch_forget(self, batch_id):
		"""Done with the whole batch: forget every job in it, photos and results too."""
		with self.lock:
			batch = self.batches.pop(batch_id, None)
		if not batch:
			return None
		removed = sum(1 for key in batch["job_ids"] if self.durable_forget(key))
		try:
			os.remove(os.path.join(self.batches_root, f"{batch_id}.json"))
		except OSError:
			pass
		self.observer.record("batch_removed", batch=batch_id, jobs=removed)
		return removed

	def _write_batch(self, batch):
		try:
			os.makedirs(self.batches_root, exist_ok=True)
			path = os.path.join(self.batches_root, f"{batch['id']}.json")
			with open(path + ".tmp", "w") as f:
				json.dump(batch, f)
			os.replace(path + ".tmp", path)
		except OSError:
			log.exception("batch=%s couldn't be written", batch["id"])

	def _load_batches(self):
		if not os.path.isdir(self.batches_root):
			return
		for name in os.listdir(self.batches_root):
			if not name.endswith(".json"):
				continue
			try:
				with open(os.path.join(self.batches_root, name)) as f:
					batch = json.load(f)
				if BATCH_ID.match(batch["id"]) and isinstance(batch["job_ids"], list):
					self.batches[batch["id"]] = batch
			except (OSError, ValueError, KeyError, TypeError):
				continue

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
			"features": ["jobs", "batches", "canvas"],
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
					for name in ("photo.bin", "mask.bin"):
						if os.path.exists(os.path.join(job.folder, name)):
							os.remove(os.path.join(job.folder, name))
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
					job.photo = job.mask = None
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
			canvas_source = None
			if job.mask is not None:
				canvas_source = _open_canvas(job.photo, job.mask)
			elif job.auto_canvas:
				canvas_source = self._auto_canvas(job)

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
				canvas_args = {}
				if canvas_source:
					canvas_args = _write_canvas(canvas_source, panel.draw_size, folder, panel.number)
					canvas_args["strength"] = job.strength
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
						**canvas_args,
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
					drawn = drawn.convert("RGB")
					if canvas_source and job.keep_masked:
						drawn = _keep_masked(drawn, canvas_source)
					fitted = ImageOps.fit(drawn, (right - left, bottom - top), Image.Resampling.LANCZOS)
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

	def _auto_canvas(self, job):
		"""Map the garment and build the canvas and mask. Kept with a draft as map.png and canvas.png."""
		try:
			import canvas_map
			canvas, mask, garment = canvas_map.build(job.photo, job.width, job.height, job.hem)
		except ImportError:
			raise GenerationError('"canvas": "auto" needs the extras: .venv/bin/pip install -r requirements-canvas.txt')
		except Exception as e:  # noqa: BLE001 — becomes the job's error
			raise GenerationError(f"Couldn't map the garment: {e}")
		if job.durable:
			garment.save(os.path.join(job.folder, "map.png"))
			canvas.save(os.path.join(job.folder, "canvas.png"))
		histogram = garment.convert("L").histogram()
		self.observer.record("mapped", job.key, garment_pct=round(100 * sum(histogram[128:]) / max(sum(histogram), 1), 1))
		return canvas.convert("RGB"), mask.convert("L")

	def durable_map(self, key, name):
		"""The garment map or canvas a canvas-auto draft was drawn from, or None."""
		job = self.jobs.get(key)
		if not job or not job.durable or name not in ("map", "canvas"):
			return None
		try:
			with open(os.path.join(job.folder, f"{name}.png"), "rb") as f:
				return "image/png", f.read()
		except OSError:
			return None

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
		for batch_id, batch in list(self.batches.items()):
			if not any(key in self.jobs for key in batch["job_ids"]):
				del self.batches[batch_id]
				try:
					os.remove(os.path.join(self.batches_root, f"{batch_id}.json"))
				except OSError:
					pass
		current = self.current.key if self.current else None
		for key, kept in list(self.unfinished.items()):
			if key != current and kept["at"] < limit:
				del self.unfinished[key]


def _open_canvas(photo, mask):
	"""The photo and its mask, upright, same size. White in the mask = draw, black = keep."""
	with Image.open(io.BytesIO(photo)) as image:
		image = ImageOps.exif_transpose(image).convert("RGB")
	with Image.open(io.BytesIO(mask)) as m:
		m = m.convert("L").point(lambda v: 255 if v >= 128 else 0)
	return image, m


def _write_canvas(source, size, folder, number):
	"""The canvas and mask at the size this panel is drawn, for sd-cli's -i and --mask."""
	image, mask = source
	init = os.path.join(folder, f"init-{number}.png")
	mask_path = os.path.join(folder, f"mask-{number}.png")
	ImageOps.fit(image, size, Image.Resampling.LANCZOS).save(init, "PNG")
	ImageOps.fit(mask, size, Image.Resampling.NEAREST).save(mask_path, "PNG")
	return {"init": init, "mask": mask_path}


KEEP_FEATHER = 2  # px of soft edge where kept and drawn pixels meet


def _keep_masked(drawn, source):
	"""Put the photo's own pixels back wherever the mask is black. The model works on a coarse
	grid, so a few garment pixels near the edge could otherwise shift."""
	image, mask = source
	image = ImageOps.fit(image, drawn.size, Image.Resampling.LANCZOS)
	keep = ImageOps.invert(ImageOps.fit(mask, drawn.size, Image.Resampling.NEAREST))
	keep = keep.filter(ImageFilter.MinFilter(2 * KEEP_FEATHER + 1)).filter(ImageFilter.GaussianBlur(KEEP_FEATHER))
	return Image.composite(image, drawn, keep)


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
			elif parts == ["batches"]:
				self._send(200, {"batches": service.batch_list()})
			elif len(parts) == 2 and parts[0] == "batches":
				state = service.batch_state(parts[1])
				self._send(200, state) if state else self._send(404, {"error": "No such batch."})
			elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "results":
				results = service.batch_results(parts[1])
				self._send(200, results) if results else self._send(404, {"error": "No such batch."})
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
			elif len(parts) == 3 and parts[0] == "jobs" and parts[2] in ("map", "canvas"):
				picture = service.durable_map(parts[1], parts[2])
				self._send_bytes(*picture) if picture else self._send(404, {"error": "No map for that job."})
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
			elif len(parts) == 2 and parts[0] == "batches":
				removed = service.batch_forget(parts[1])
				self._send(200, {"removed": removed}) if removed is not None else self._send(404, {"error": "No such batch."})
			else:
				self._send(404, {"error": "Not found."})

		def do_POST(self):
			if self.path not in ("/sdapi/v1/img2img", "/jobs", "/batches"):
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

			if self.path == "/batches":
				try:
					batch_id, jobs = parse_batch(self.rfile.read(length))
					service.submit_batch(batch_id, jobs)
				except (BadRequest, panels_module.PromptError) as e:
					self._send(400, {"error": str(e)})
					return
				# Answer at once, like /jobs: the drawing goes on whether or not the laptop stays connected.
				self._send(202, service.batch_state(batch_id))
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
