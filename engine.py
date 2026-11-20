"""Run one generation with stable-diffusion.cpp's sd-cli.

A new process per photo costs some loading time against several minutes of drawing. In
return its memory is handed back between jobs, and running out of memory kills that
process rather than this service. Run with --mmap and without --offload-to-cpu: on a Mac
the CPU and GPU share memory, so offloading only holds the weights twice (measured peak
19.8 GB with it, 6.4 GB without).
"""

import logging
import os
import re
import subprocess
import threading
import time

ERROR_LINE = re.compile(r"^\[E\]\s*(.*?)(?:\s+---\s.*)?$", re.M)
OUT_OF_MEMORY = re.compile(
	r"(?i)out of memory|insufficient memory|failed to allocate|alloc.*failed|failed during weight preparation"
)

log = logging.getLogger("local_image_server")


class GenerationError(Exception):
	pass


class OutOfMemory(GenerationError):
	pass


class SdCli:
	def __init__(self, config):
		self.config = config
		self._process = None
		self._stopping = False
		self._cancelled = False
		self._lock = threading.Lock()

	def missing_files(self):
		paths = [self.config["sd_cli"], *self.config["weights"].values()]
		return [path for path in paths if not os.path.exists(path)]

	def generate(self, *, prompt, reference, width, height, seed, output, is_cancelled=lambda: False, on_event=None):
		"""Write a PNG to `output` and return the seconds it took. Raises GenerationError.

		`on_event(kind, **fields)` hears about memory waits and out-of-memory retries."""
		note = on_event or (lambda kind, **fields: None)
		c = self.config
		command = [
			c["sd_cli"],
			*(part for flag_and_path in c["weights"].items() for part in flag_and_path),
			"-r", reference,
			"-p", prompt,
			"--cfg-scale", str(c["cfg_scale"]),
			"--sampling-method", c["sampling_method"],
			"--steps", str(c["steps"]),
			"-W", str(width),
			"-H", str(height),
			"--seed", str(seed),
			"-o", output,
			*c["extra_args"],
		]
		started = time.monotonic()
		retries = c["memory_retries"]
		for attempt in range(retries + 1):
			waited = self._wait_for_memory(is_cancelled)
			if waited >= 1:
				note("memory_wait", seconds=round(waited, 1), attempt=attempt + 1)
			if is_cancelled():
				raise GenerationError("Cancelled.")
			try:
				self._run(command, output)
				break
			except OutOfMemory:
				# On 16 GB this comes and goes: the same request usually works next time.
				if attempt == retries or self._stopping:
					raise
				log.warning("ran out of memory, trying this photo again (%s of %s)", attempt + 1, retries)
				note("oom_retry", attempt=attempt + 1, of=retries)
		return time.monotonic() - started

	def stop(self):
		with self._lock:
			self._stopping = True
			if self._process:
				self._process.kill()

	def cancel(self):
		"""Stop the photo being drawn now. It doesn't count as running out of memory."""
		with self._lock:
			if self._process:
				self._cancelled = True
				self._process.kill()

	def _wait_for_memory(self, is_cancelled):
		"""The last run's memory takes a while to come back, and starting early is what fails.
		Returns the seconds spent waiting."""
		began = time.monotonic()
		deadline = began + self.config["memory_settle_seconds"]
		while time.monotonic() < deadline and not self._stopping and not is_cancelled():
			if (_free_percent() or 100) >= self.config["memory_settle_free_percent"]:
				break
			time.sleep(5)
		return time.monotonic() - began

	def _run(self, command, output):
		timeout = self.config["generation_timeout_seconds"]
		try:
			process = subprocess.Popen(
				command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace"
			)
		except OSError as e:
			raise GenerationError(f"Couldn't start the image model: {e.strerror}")

		with self._lock:
			self._process = process
		try:
			_, output_text = process.communicate(timeout=timeout)
		except subprocess.TimeoutExpired:
			process.kill()
			process.communicate()
			raise GenerationError(f"Gave up after {timeout}s on one photo.")
		finally:
			with self._lock:
				self._process = None
				cancelled, self._cancelled = self._cancelled, False

		if cancelled:
			raise GenerationError("Cancelled.")
		if process.returncode != 0 or not os.path.exists(output):
			raise _failure(process.returncode, output_text)


def _free_percent():
	try:
		text = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=20).stdout
	except (OSError, subprocess.TimeoutExpired):
		return None
	match = re.search(r"free percentage: (\d+)", text)
	return int(match.group(1)) if match else None


def _failure(returncode, output_text):
	errors = ERROR_LINE.findall(output_text or "")
	for line in errors:
		log.error("sd-cli: %s", line)
	if returncode in (-9, 137) or OUT_OF_MEMORY.search(output_text or ""):
		return OutOfMemory("The Mac ran out of memory. Close other apps and try again.")
	if errors:
		# The last line is only ever a generic "generate failed". The first is the cause.
		return GenerationError(errors[0][:150])
	return GenerationError(f"The image model stopped without an image (exit code {returncode}).")
