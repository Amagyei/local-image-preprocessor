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

from PIL import Image, ImageOps, UnidentifiedImageError

import panels as panels_module
from engine import GenerationError, SdCli

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"  # The laptop comes in over an SSH tunnel. Never listen wider than this.
MAX_BODY = 64 * 1024 * 1024
MAX_SIDE = 4096

log = logging.getLogger("local_image_server")


class BadRequest(Exception):
	pass


class Job:
	def __init__(self, key, prompt, width, height, photo):
		self.key = key
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

	key = hashlib.sha256(
		json.dumps([prompt, width, height]).encode() + hashlib.sha256(photo).digest()
	).hexdigest()
	return Job(key, prompt, width, height, photo)


class Service:
	def __init__(self, config):
		self.config = config
		self.engine = SdCli(config)
		self.queue = queue.Queue()
		self.lock = threading.Lock()
		self.jobs = {}
		self.unfinished = {}  # photos already drawn for requests that failed part way
		self.current = None
		self.temp_root = config["temp_dir"]
		# Anything here was left by a crash, and it's the customer's photos.
		shutil.rmtree(self.temp_root, ignore_errors=True)

	def start(self):
		threading.Thread(target=self._work, daemon=True).start()
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
			return job

	def health(self):
		current = self.current
		missing = self.engine.missing_files()
		return {
			"model": self.config["model_name"],
			"ready": not missing,
			"missing_files": [os.path.basename(path) for path in missing],
			# The model only sits in memory during a run, see engine.py.
			"loaded": bool(current),
			"busy": bool(current),
			"queue_length": self.queue.qsize(),
			"progress": current.progress if current else None,
			"results_kept": sum(1 for job in list(self.jobs.values()) if job.done.is_set()),
		}

	def stop(self):
		self.engine.stop()
		shutil.rmtree(self.temp_root, ignore_errors=True)

	def _work(self):
		while True:
			job = self.queue.get()
			self.current = job
			try:
				job.image = self._run(job)
				job.status = 200
			except GenerationError as e:
				self._fail(job, 500, str(e))
			except panels_module.PromptError as e:
				self._fail(job, 400, str(e))
			except Exception as e:  # noqa: BLE001 — one bad run mustn't stop the queue
				log.exception("job=%s unexpected failure", job.key[:12])
				self._fail(job, 500, f"Unexpected error: {e.__class__.__name__}")
			finally:
				job.photo = None
				job.finished_at = time.monotonic()
				self.current = None
				job.done.set()

	def _fail(self, job, status, message):
		log.error("job=%s failed: %s", job.key[:12], message)
		job.status, job.error = status, message
		# Failures aren't kept: the next try should really try again.
		with self.lock:
			if self.jobs.get(job.key) is job:
				del self.jobs[job.key]

	def _run(self, job):
		c = self.config
		kept = self.unfinished.get(job.key) or {
			"seed": c["seed"] if c["seed"] >= 0 else random.randrange(2**31),
			"panels": {},
			"at": time.monotonic(),
		}
		self.unfinished[job.key] = kept
		seed = kept["seed"]
		plan = panels_module.plan(job.prompt, job.width, job.height, c["max_long_side"])
		os.makedirs(self.temp_root, exist_ok=True)
		folder = os.path.join(self.temp_root, job.key[:12])
		os.makedirs(folder, exist_ok=True)
		started = time.monotonic()
		try:
			reference = os.path.join(folder, "reference.png")
			_save_reference(job.photo, reference, c["reference_long_side"])

			canvas = Image.new("RGB", (job.width, job.height))
			for panel in plan:
				job.progress = f"photo {panel.number} of {len(plan)}"
				left, top, right, bottom = panel.box
				if panel.number in kept["panels"]:
					log.info("job=%s photo=%s/%s kept from the last try", job.key[:12], panel.number, len(plan))
					canvas.paste(Image.open(io.BytesIO(kept["panels"][panel.number])), (left, top))
					continue
				output = os.path.join(folder, f"panel-{panel.number}.png")
				try:
					seconds = self.engine.generate(
						prompt=f"{panel.prompt}\n\n{c['garment_instruction']}".strip(),
						reference=reference,
						width=panel.draw_size[0],
						height=panel.draw_size[1],
						seed=seed,
						output=output,
					)
				except GenerationError as e:
					raise GenerationError(f"Photo {panel.number} of {len(plan)}: {e}")
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

			self.unfinished.pop(job.key, None)
			buffer = io.BytesIO()
			canvas.save(buffer, "PNG")
			log.info("job=%s finished seconds=%.1f", job.key[:12], time.monotonic() - started)
			return base64.b64encode(buffer.getvalue()).decode("ascii")
		finally:
			shutil.rmtree(folder, ignore_errors=True)

	def _prune_forever(self):
		while True:
			time.sleep(30)
			with self.lock:
				self._prune()

	def _prune(self):
		"""Call with the lock held."""
		limit = time.monotonic() - self.config["result_ttl_seconds"]
		for key, job in list(self.jobs.items()):
			if job.done.is_set() and job.finished_at is not None and job.finished_at < limit:
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
			if self.path == "/health":
				self._send(200, service.health())
			else:
				self._send(404, {"error": "Not found."})

		def do_POST(self):
			if self.path != "/sdapi/v1/img2img":
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

			try:
				job = service.submit(parse_request(self.rfile.read(length)))
			except (BadRequest, panels_module.PromptError) as e:
				self._send(400, {"error": str(e)})
				return

			# The run belongs to the worker, so it finishes even if this connection drops.
			job.done.wait()
			if job.image:
				self._send(200, {"images": [job.image]})
			else:
				self._send(job.status or 500, {"error": job.error or "The run didn't produce an image."})

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
			log.info("%s %s", self.address_string(), format % args)

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
