"""Run with: .venv/bin/python -m unittest discover tests"""

import base64
import http.client
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import panels  # noqa: E402
import server  # noqa: E402

LAYOUT_2X2 = (
	"Layout: a single image made of exactly 2 rows × 2 columns = 4 equal panels, edge to edge, "
	"with no gaps, borders, frames, captions or dividing lines between them. "
	"Keep each panel's subject fully inside its own panel."
)
LAYOUT_1X2 = LAYOUT_2X2.replace("2 rows × 2 columns = 4", "1 row × 2 columns = 2")
HOUSE = "Photorealistic high-end editorial fashion photography.\nNo text, logos or borders."

STORY = "\n\n".join((
	"A 2x2 grid composite fashion photoshoot featuring 4 distinct sequential shots of the same model in a professional studio setting.",
	"[MODEL]\nDeep ebony skin, short natural afro.",
	"[WARDROBE]\nShe is wearing the garment in the attached photo.",
	"[DIRECTORIAL NARRATIVE ARC: Hesitation to Authority]\n"
	"- Top-left frame (Shot 1): Seated, hands clasped.\n"
	"- Top-right frame (Shot 2): One hand on hip.\n"
	"- Bottom-left frame (Shot 3): Feet apart, neutral gaze.\n"
	"- Bottom-right frame (Shot 4): Hands on hips, commanding smile.",
	"[STYLE & RENDERING]\nConsistent lighting.",
	LAYOUT_2X2,
	HOUSE,
))
LOOKBOOK = "\n\n".join((
	"Show one Ghanaian woman model wearing the garment, as four photos: "
	"top-left, full-body front view standing tall; top-right, full-body three-quarter view mid-stride; "
	"bottom-left, full-body back view; bottom-right, waist-up shot with one hand at the waist showing the fabric. "
	"Same model, hair, makeup, simple gold jewellery and backdrop in all four. "
	"In the full-body shots keep the whole figure in frame, head to shoes.",
	LAYOUT_2X2,
	HOUSE,
))
FRONT_BACK = "\n\n".join((
	"Show one Ghanaian woman model wearing the garment, as two photos side by side: "
	"left, full-body front view; right, full-body back view in the same pose. "
	"Same model and backdrop in both, whole figure in frame, head to shoes.",
	LAYOUT_1X2,
	HOUSE,
))
FABRIC = "\n\n".join((
	"Four macro close-up photos of the garment itself: top-left, the fabric print and texture filling the frame; "
	"top-right, the waistband or neckline; bottom-left, the hem; bottom-right, a seam, trim or finishing detail. "
	"Shallow depth of field, crisp focus on the fabric.",
	LAYOUT_2X2,
	HOUSE,
))


def garment_photo():
	buffer = io.BytesIO()
	Image.new("RGB", (1536, 2048), (10, 120, 60)).save(buffer, "JPEG")
	return base64.b64encode(buffer.getvalue()).decode("ascii")


class SplitPrompts(unittest.TestCase):
	def test_single_photo_is_left_alone(self):
		self.assertEqual(panels.split_prompt("One editorial photo."), (1, 1, ["One editorial photo."]))

	def test_story_keeps_the_woman_and_one_shot_each(self):
		rows, cols, texts = panels.split_prompt(STORY)
		self.assertEqual((rows, cols, len(texts)), (2, 2, 4))
		self.assertIn("Seated, hands clasped.", texts[0])
		self.assertNotIn("One hand on hip", texts[0])
		self.assertIn("Hands on hips", texts[3])
		for text in texts:
			self.assertIn("Deep ebony skin", text)
			self.assertIn("She is wearing the garment", text)
			self.assertIn("Photorealistic high-end", text)
			self.assertNotIn("grid composite", text)
			self.assertNotIn("Layout:", text)
			self.assertNotIn(" frame (Shot", text)

	def test_lookbook(self):
		rows, cols, texts = panels.split_prompt(LOOKBOOK)
		self.assertEqual((rows, cols, len(texts)), (2, 2, 4))
		self.assertTrue(texts[2].startswith(
			"Show one Ghanaian woman model wearing the garment, as one photo: full-body back view."
		))
		self.assertIn("backdrop. In the full-body", texts[2])
		self.assertNotIn("top-right", texts[2])
		self.assertNotIn("in all four", texts[2])

	def test_front_and_back(self):
		rows, cols, texts = panels.split_prompt(FRONT_BACK)
		self.assertEqual((rows, cols, len(texts)), (1, 2, 2))
		self.assertIn("as one photo: full-body back view in the same pose.", texts[1])
		self.assertNotIn("side by side", texts[1])
		self.assertNotIn("in both", texts[1])

	def test_fabric(self):
		_, _, texts = panels.split_prompt(FABRIC)
		self.assertTrue(texts[2].startswith("one macro close-up photo of the garment itself: the hem."))
		self.assertIn("a seam, trim or finishing detail.", texts[3])

	def test_a_paragraph_the_laptop_adds_reaches_every_photo(self):
		line = "Garment length: floor-length maxi. Wherever the whole garment is in frame, its hem falls at exactly this length."
		for prompt in (STORY, LOOKBOOK, FRONT_BACK, FABRIC):
			_rows, _cols, texts = panels.split_prompt(f"{prompt}\n\n{line}")
			for text in texts:
				self.assertIn(line, text)

	def test_grid_it_cannot_read_is_refused(self):
		with self.assertRaises(panels.PromptError):
			panels.split_prompt("Four photos of a dress in various poses.\n\n" + LAYOUT_2X2)

	def test_panels_cover_the_image_exactly(self):
		plan = panels.plan(FRONT_BACK, 2048, 1344, 1024)
		self.assertEqual([p.box for p in plan], [(0, 0, 1024, 1344), (1024, 0, 2048, 1344)])
		for panel in plan:
			self.assertLessEqual(max(panel.draw_size), 1024)
			self.assertEqual([side % 32 for side in panel.draw_size], [0, 0])
		self.assertEqual(panels.plan(STORY, 1536, 2048, 1024)[3].box, (768, 1024, 1536, 2048))
		self.assertEqual(panels.plan(STORY, 1536, 2048, 1024)[0].draw_size, (768, 1024))


class Service(unittest.TestCase):
	def setUp(self):
		self.folder = tempfile.TemporaryDirectory()
		self.addCleanup(self.folder.cleanup)
		self.calls = os.path.join(self.folder.name, "calls")
		self.fail_flag = os.path.join(self.folder.name, "fail")
		open(self.calls, "w").close()

		cli = os.path.join(self.folder.name, "sd-cli")
		with open(cli, "w") as f:
			f.write(f"#!/bin/sh\nexec '{sys.executable}' '{os.path.join(HERE, 'fake_sd_cli.py')}' \"$@\"\n")
		os.chmod(cli, os.stat(cli).st_mode | stat.S_IEXEC)

		os.environ["FAKE_CALLS"] = self.calls
		os.environ["FAKE_FAIL"] = self.fail_flag
		os.environ["FAKE_FAIL_ONCE"] = self.fail_flag + "-once"
		os.environ["FAKE_SLEEP"] = "0"

		self.config = {
			"model_name": "test",
			"port": 0,
			"sd_cli": cli,
			"temp_dir": os.path.join(self.folder.name, "tmp"),
			"weights": {flag: cli for flag in ("--diffusion-model", "--vae", "--llm", "--llm_vision")},
			"memory_settle_seconds": 0,
			"memory_settle_free_percent": 40,
			"seed": 42,
			"steps": 2,
			"cfg_scale": 1.0,
			"sampling_method": "euler",
			"max_long_side": 1024,
			"reference_long_side": 1024,
			"garment_instruction": "",
			"extra_args": [],
			"memory_retries": 0,
			"generation_timeout_seconds": 30,
			"result_ttl_seconds": 1800,
		}
		self.server, self.service = server.make_server(self.config)
		self.service.start()
		threading.Thread(target=self.server.serve_forever, daemon=True).start()
		self.addCleanup(self.server.server_close)
		self.addCleanup(self.server.shutdown)
		self.port = self.server.server_address[1]

	def post(self, prompt="A photo.", width=768, height=1024, **extra):
		body = {"prompt": prompt, "init_images": [garment_photo()], "width": width, "height": height, "batch_size": 1}
		body.update(extra)
		connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
		connection.request("POST", "/sdapi/v1/img2img", json.dumps(body), {"Content-Type": "application/json"})
		response = connection.getresponse()
		data = json.loads(response.read())
		connection.close()
		return response.status, data

	def health(self):
		connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
		connection.request("GET", "/health")
		data = json.loads(connection.getresponse().read())
		connection.close()
		return data

	def call_count(self):
		with open(self.calls) as f:
			return len(f.readlines())

	def wait_until(self, check, seconds=10):
		end = time.monotonic() + seconds
		while time.monotonic() < end:
			if check():
				return True
			time.sleep(0.02)
		return False

	def test_listens_on_loopback_only(self):
		self.assertEqual(self.server.server_address[0], "127.0.0.1")

	def test_answer_is_the_shape_the_laptop_reads(self):
		status, data = self.post(width=768, height=1024)
		self.assertEqual(status, 200)
		image = Image.open(io.BytesIO(base64.b64decode(data["images"][0])))
		self.assertEqual(image.size, (768, 1024))

	def test_size_is_exact_even_when_the_model_draws_smaller(self):
		status, data = self.post(width=1536, height=2048)
		self.assertEqual(status, 200)
		self.assertEqual(Image.open(io.BytesIO(base64.b64decode(data["images"][0]))).size, (1536, 2048))
		self.assertEqual(self.call_count(), 1)

	def test_grid_draws_each_panel_and_fills_the_canvas(self):
		status, data = self.post(STORY, width=1536, height=2048)
		self.assertEqual(status, 200)
		self.assertEqual(self.call_count(), 4)
		image = Image.open(io.BytesIO(base64.b64decode(data["images"][0]))).convert("RGB")
		self.assertEqual(image.size, (1536, 2048))
		for point in ((0, 0), (767, 1023), (768, 1024), (1535, 2047)):
			self.assertEqual(image.getpixel(point), (200, 40, 40))

	def test_same_request_returns_the_kept_result(self):
		first = self.post()
		second = self.post()
		self.assertEqual(first, second)
		self.assertEqual(self.call_count(), 1)

	def test_a_different_prompt_runs_again(self):
		self.post("A photo.")
		self.post("Another photo.")
		self.assertEqual(self.call_count(), 2)

	def test_result_expires(self):
		self.config["result_ttl_seconds"] = 0
		self.post()
		time.sleep(0.01)
		self.post()
		self.assertEqual(self.call_count(), 2)

	def test_failure_is_clear_and_the_service_survives(self):
		open(self.fail_flag, "w").close()
		status, data = self.post()
		self.assertEqual(status, 500)
		self.assertIn("ran out of memory", data["error"])
		self.assertLess(len(json.dumps(data)), 200)

		os.remove(self.fail_flag)
		status, _ = self.post()
		self.assertEqual(status, 200)
		self.assertEqual(self.call_count(), 2)

	def test_memory_failure_is_tried_again(self):
		self.config["memory_retries"] = 1
		open(self.fail_flag + "-once", "w").close()
		status, _ = self.post()
		self.assertEqual(status, 200)
		self.assertEqual(self.call_count(), 2)

	def test_retry_after_a_failure_keeps_finished_photos(self):
		os.environ["FAKE_FAIL_ON"] = "One hand on hip"
		self.addCleanup(os.environ.pop, "FAKE_FAIL_ON", None)
		open(self.fail_flag + "-on", "w").close()
		status, _ = self.post(STORY, width=1536, height=2048)
		self.assertEqual(status, 500)
		self.assertEqual(self.call_count(), 2)

		os.remove(self.fail_flag + "-on")
		status, data = self.post(STORY, width=1536, height=2048)
		self.assertEqual(status, 200)
		self.assertEqual(self.call_count(), 5)
		image = Image.open(io.BytesIO(base64.b64decode(data["images"][0]))).convert("RGB")
		self.assertEqual(image.getpixel((10, 10)), (200, 40, 40))
		self.assertEqual(self.service.unfinished, {})

	def test_gives_up_when_retries_run_out(self):
		open(self.fail_flag + "-once", "w").close()
		status, data = self.post()
		self.assertEqual(status, 500)
		self.assertIn("ran out of memory", data["error"])
		self.assertEqual(self.call_count(), 1)

	def test_garment_instruction_reaches_every_photo(self):
		self.config["garment_instruction"] = "Keep the fabric unbroken."
		self.post(STORY, width=1536, height=2048)
		with open(self.calls) as f:
			lines = f.readlines()
		self.assertEqual(len(lines), 4)
		for line in lines:
			self.assertIn("Keep the fabric unbroken.", line)

	def test_runs_one_at_a_time_and_reports_the_queue(self):
		os.environ["FAKE_SLEEP"] = "0.4"
		results = []
		threads = [
			threading.Thread(target=lambda n=n: results.append(self.post(f"Photo {n}.")))
			for n in range(3)
		]
		for thread in threads:
			thread.start()
		self.assertTrue(self.wait_until(
			lambda: self.health()["queue_length"] == 2 and self.health()["progress"] == "photo 1 of 1"
		))
		self.assertTrue(self.health()["busy"])
		self.assertLessEqual(self.call_count(), 1)
		for thread in threads:
			thread.join()
		self.assertEqual([status for status, _ in results], [200, 200, 200])
		self.assertFalse(self.health()["busy"])

	def test_run_finishes_after_the_connection_drops(self):
		os.environ["FAKE_SLEEP"] = "0.3"
		body = json.dumps({
			"prompt": "A photo.", "init_images": [garment_photo()], "width": 768, "height": 1024, "batch_size": 1,
		})
		connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
		connection.request("POST", "/sdapi/v1/img2img", body)
		self.assertTrue(self.wait_until(lambda: self.health()["busy"]))
		connection.close()

		self.assertTrue(self.wait_until(lambda: self.health()["results_kept"] == 1))
		status, _ = self.post()
		self.assertEqual(status, 200)
		self.assertEqual(self.call_count(), 1)

	def test_keeps_no_photos_on_disk(self):
		self.post(STORY, width=1536, height=2048)
		leftovers = []
		for _, _, files in os.walk(self.config["temp_dir"]):
			leftovers += files
		self.assertEqual(leftovers, [])

	def test_bad_requests_get_a_clear_answer(self):
		cases = [
			({"prompt": ""}, "no prompt"),
			({"init_images": []}, "exactly one garment photo"),
			({"init_images": ["not base64!"]}, "valid base64"),
			({"init_images": [base64.b64encode(b"not an image").decode()]}, "opened as an image"),
			({"width": "768"}, "Width and height"),
			({"batch_size": 2}, "batch_size"),
		]
		for extra, expected in cases:
			status, data = self.post(**extra)
			self.assertEqual(status, 400, extra)
			self.assertIn(expected, data["error"])
		self.assertEqual(self.call_count(), 0)

	def test_grid_it_cannot_read_gets_a_clear_answer(self):
		status, data = self.post("Four poses.\n\n" + LAYOUT_2X2, width=1536, height=2048)
		self.assertEqual(status, 400)
		self.assertIn("Couldn't find a shot", data["error"])
		self.assertEqual(self.call_count(), 0)

	# --- Drafts: jobs that outlive the connection ---------------------------------------

	def request(self, method, path, body=None):
		connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
		connection.request(method, path, json.dumps(body) if body is not None else None)
		response = connection.getresponse()
		data = json.loads(response.read())
		connection.close()
		return response.status, data

	def submit_job(self, prompt="A photo.", **extra):
		body = {"prompt": prompt, "init_images": [garment_photo()], "width": 768, "height": 1024, "batch_size": 1}
		body.update(extra)
		return self.request("POST", "/jobs", body)

	def wait_for_status(self, job_id, status, seconds=10):
		return self.wait_until(lambda: self.request("GET", f"/jobs/{job_id}")[1]["status"] == status, seconds)

	def test_job_is_accepted_at_once_and_collected_later(self):
		os.environ["FAKE_SLEEP"] = "0.3"
		status, data = self.submit_job()
		self.assertEqual(status, 202)
		self.assertIn(data["status"], ("queued", "running"))
		self.assertEqual(self.request("GET", f"/jobs/{data['job_id']}/result")[0], 409)
		self.assertTrue(self.wait_for_status(data["job_id"], "done"))
		status, result = self.request("GET", f"/jobs/{data['job_id']}/result")
		self.assertEqual(status, 200)
		self.assertEqual(Image.open(io.BytesIO(base64.b64decode(result["images"][0]))).size, (768, 1024))
		self.assertEqual(self.health()["drafts_waiting"], 1)

	def test_the_draft_stays_until_collected_then_goes(self):
		_, data = self.submit_job()
		self.assertTrue(self.wait_for_status(data["job_id"], "done"))
		folder = os.path.join(self.service.jobs_root, data["job_id"])
		self.assertEqual(sorted(os.listdir(folder)), ["meta.json", "panel-1.png", "result.png"])  # photo dropped once drawn
		self.assertEqual(self.request("GET", f"/jobs/{data['job_id']}/result")[0], 200)
		self.assertEqual(self.request("GET", f"/jobs/{data['job_id']}/result")[0], 200)
		self.assertEqual(self.request("DELETE", f"/jobs/{data['job_id']}")[0], 200)
		self.assertFalse(os.path.exists(folder))
		self.assertEqual(self.request("GET", f"/jobs/{data['job_id']}")[0], 404)

	def test_same_job_again_is_not_drawn_twice(self):
		_, first = self.submit_job()
		_, second = self.submit_job()
		self.assertEqual(first["job_id"], second["job_id"])
		self.assertTrue(self.wait_for_status(first["job_id"], "done"))
		self.assertEqual(self.call_count(), 1)

	def test_a_new_seed_is_a_new_job(self):
		_, first = self.submit_job(seed=1)
		_, second = self.submit_job(seed=2)
		self.assertNotEqual(first["job_id"], second["job_id"])
		self.assertTrue(self.wait_for_status(second["job_id"], "done"))
		self.assertEqual(self.call_count(), 2)
		self.assertEqual(self.submit_job(seed=-1)[0], 400)

	def test_a_failed_job_is_kept_and_a_retry_carries_on(self):
		open(self.fail_flag, "w").close()
		_, data = self.submit_job()
		self.assertTrue(self.wait_for_status(data["job_id"], "failed"))
		self.assertTrue(self.request("GET", f"/jobs/{data['job_id']}")[1]["error"])
		os.remove(self.fail_flag)
		status, again = self.submit_job()
		self.assertEqual(again["job_id"], data["job_id"])
		self.assertTrue(self.wait_for_status(data["job_id"], "done"))

	def test_a_queued_job_can_be_cancelled(self):
		os.environ["FAKE_SLEEP"] = "0.5"
		_, first = self.submit_job("First photo.")
		_, second = self.submit_job("Second photo.")
		self.assertEqual(self.request("DELETE", f"/jobs/{second['job_id']}")[0], 200)
		self.assertEqual(self.request("GET", f"/jobs/{second['job_id']}")[0], 404)
		self.assertFalse(os.path.exists(os.path.join(self.service.jobs_root, second["job_id"])))
		self.assertTrue(self.wait_for_status(first["job_id"], "done"))
		self.assertEqual(self.call_count(), 1)

	def test_a_running_job_can_be_cancelled_and_the_queue_moves_on(self):
		self.config["memory_retries"] = 2  # a cancelled photo mustn't be mistaken for running out of memory
		os.environ["FAKE_SLEEP"] = "60"
		_, first = self.submit_job("First photo.")
		self.assertTrue(self.wait_until(lambda: self.call_count() == 1))
		time.sleep(0.2)
		os.environ["FAKE_SLEEP"] = "0"
		_, second = self.submit_job("Second photo.")
		self.assertEqual(self.request("DELETE", f"/jobs/{first['job_id']}")[0], 200)
		self.assertTrue(self.wait_for_status(second["job_id"], "done"))
		self.assertEqual(self.call_count(), 2)
		self.assertFalse(os.path.exists(os.path.join(self.service.jobs_root, first["job_id"])))

	def test_cancelling_an_unknown_job_says_so(self):
		self.assertEqual(self.request("DELETE", "/jobs/nothing")[0], 404)

	def test_a_failure_recording_a_failure_does_not_stop_the_queue(self):
		real = self.service._write_meta

		def full_disk(job, state, seed=None):
			if state in ("running", "failed"):
				raise OSError(28, "No space left on device")
			return real(job, state, seed)

		self.service._write_meta = full_disk
		_, first = self.submit_job("First photo.")
		self.assertTrue(self.wait_for_status(first["job_id"], "failed"))
		self.service._write_meta = real
		_, second = self.submit_job("Second photo.")
		self.assertTrue(self.wait_for_status(second["job_id"], "done"))

	def test_drafts_survive_a_restart_and_unfinished_jobs_resume(self):
		config = dict(self.config, temp_dir=os.path.join(self.folder.name, "tmp2"), jobs_dir=os.path.join(self.folder.name, "jobs2"))

		def draft(prompt):
			return server.parse_request(json.dumps({
				"prompt": prompt, "init_images": [garment_photo()], "width": 768, "height": 1024, "batch_size": 1,
			}))

		before = server.Service(config)
		before.start()
		done = before.submit_durable(draft("Finished photo."))
		self.assertTrue(self.wait_until(lambda: before.durable_state(done.key)["status"] == "done"))
		before.queue = __import__("queue").Queue()  # nobody reads this one: the power went out
		pending = [before.submit_durable(draft(f"Pending photo {n}.")) for n in range(2)]

		after = server.Service(config)
		after.start()
		self.assertEqual(after.durable_state(done.key)["status"], "done")
		self.assertTrue(after.durable_result(done.key))
		for job in pending:
			self.assertTrue(self.wait_until(lambda: after.durable_state(job.key)["status"] == "done"))
		self.assertEqual(self.call_count(), 3)

	def test_status_page_is_served_and_reads_only_public_data(self):
		connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
		connection.request("GET", "/ui")
		response = connection.getresponse()
		page = response.read().decode()
		connection.close()
		self.assertEqual(response.status, 200)
		self.assertIn("text/html", response.getheader("Content-Type"))
		self.assertIn('fetch("/health")', page)
		self.assertNotIn("/result", page)  # it never uses the collection endpoint
		self.assertGreaterEqual(self.health()["uptime_seconds"], 0)

	def test_status_page_does_not_hold_up_a_run(self):
		os.environ["FAKE_SLEEP"] = "0.3"
		_, data = self.submit_job()
		for _ in range(20):
			self.request("GET", "/jobs")
			self.health()
		self.assertTrue(self.wait_for_status(data["job_id"], "done"))

	def test_status_page_can_show_a_finished_picture_but_not_an_unfinished_one(self):
		os.environ["FAKE_SLEEP"] = "0.3"
		_, data = self.submit_job()
		job = data["job_id"]
		self.assertEqual(self.raw("GET", f"/jobs/{job}/thumb")[0], 404)  # still drawing
		self.assertTrue(self.wait_for_status(job, "done"))
		status, kind, thumb = self.raw("GET", f"/jobs/{job}/thumb")
		self.assertEqual((status, kind), (200, "image/jpeg"))
		self.assertLessEqual(max(Image.open(io.BytesIO(thumb)).size), 320)
		status, kind, full = self.raw("GET", f"/jobs/{job}/image")
		self.assertEqual((status, kind), (200, "image/png"))
		self.assertEqual(Image.open(io.BytesIO(full)).size, (768, 1024))
		self.assertEqual(self.raw("GET", "/jobs/nope/image")[0], 404)

	def raw(self, method, path):
		connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
		connection.request(method, path)
		response = connection.getresponse()
		body = response.read()
		connection.close()
		return response.status, response.getheader("Content-Type"), body

	def test_a_job_can_be_opened_to_see_every_photo_drawn_for_it(self):
		_, data = self.submit_job(STORY, width=1536, height=2048)
		job = data["job_id"]
		self.assertTrue(self.wait_for_status(job, "done"))
		status, detail = self.request("GET", f"/jobs/{job}/detail")
		self.assertEqual(status, 200)
		self.assertEqual(detail["panel_files"], [1, 2, 3, 4])
		self.assertEqual(detail["panels"], 4)
		self.assertEqual(sorted(detail["panel_seconds"]), ["1", "2", "3", "4"])
		self.assertEqual(detail["run_seconds"], round(sum(detail["panel_seconds"].values()), 1))
		self.assertEqual((detail["width"], detail["height"]), (1536, 2048))
		self.assertTrue(detail["prompt"].startswith("A 2x2 grid"))
		self.assertTrue(detail["started"] and detail["finished"] >= detail["started"])
		status, kind, body = self.raw("GET", f"/jobs/{job}/panel/3")
		self.assertEqual((status, kind), (200, "image/png"))
		self.assertEqual(Image.open(io.BytesIO(body)).size, (768, 1024))
		self.assertEqual(self.raw("GET", f"/jobs/{job}/panel/9")[0], 404)
		self.assertEqual(self.request("GET", "/jobs/nope/detail")[0], 404)

	def test_timings_survive_a_restart(self):
		_, data = self.submit_job()
		self.assertTrue(self.wait_for_status(data["job_id"], "done"))
		again = server.Service(dict(self.config, temp_dir=self.config["temp_dir"] + "-b"))
		state = again.durable_state(data["job_id"])
		self.assertEqual(state["status"], "done")
		self.assertIsNotNone(state["run_seconds"])
		self.assertEqual(state["panels"], 1)

	def test_health_names_missing_files(self):
		self.config["weights"]["--vae"] = "/nonexistent/vae.safetensors"
		state = self.health()
		self.assertFalse(state["ready"])
		self.assertEqual(state["missing_files"], ["vae.safetensors"])


	def test_metrics_and_timeline_describe_a_finished_grid(self):
		_, data = self.submit_job(STORY, width=1536, height=2048)
		job = data["job_id"]
		self.assertTrue(self.wait_for_status(job, "done"))
		_, metrics = self.request("GET", "/metrics")
		self.assertEqual(metrics["jobs"]["finished"], 1)
		self.assertEqual(metrics["jobs"]["success_rate"], 1.0)
		self.assertEqual(metrics["photo_seconds"]["count"], 4)
		self.assertTrue(metrics["now"]["worker_alive"])
		self.assertGreater(metrics["disk"]["jobs_bytes"], 0)
		self.assertEqual(metrics["restarts"], 1)

		_, detail = self.request("GET", f"/jobs/{job}/detail")
		kinds = [e["kind"] for e in detail["events"]]
		self.assertEqual(kinds, ["queued", "started"] + ["panel_done"] * 4 + ["finished"])
		self.assertEqual(sorted(detail["panel_prompts"]), ["1", "2", "3", "4"])
		self.assertIn("Seated, hands clasped", detail["panel_prompts"]["1"])
		self.assertNotIn("Seated, hands clasped", json.dumps(metrics))

	def test_failures_and_memory_retries_are_counted(self):
		self.config["memory_retries"] = 1
		open(self.fail_flag + "-once", "w").close()
		_, data = self.submit_job()
		self.assertTrue(self.wait_for_status(data["job_id"], "done"))
		open(self.fail_flag, "w").close()
		_, data = self.submit_job("Another photo.")
		self.assertTrue(self.wait_for_status(data["job_id"], "failed"))
		_, metrics = self.request("GET", "/metrics")
		self.assertEqual(metrics["memory"]["retries"], 2)
		self.assertEqual(metrics["jobs"]["failed"], 1)
		self.assertEqual(metrics["jobs"]["success_rate"], 0.5)
		self.assertIn("ran out of memory", metrics["last_failure"]["error"])
		self.assertEqual(metrics["failure_reasons"][0]["count"], 1)
		_, events = self.request("GET", f"/events?limit=3&job={data['job_id']}")
		self.assertEqual(events["events"][0]["kind"], "failed")

	def test_jobs_can_be_filtered_to_the_running_one(self):
		os.environ["FAKE_SLEEP"] = "0.5"
		_, first = self.submit_job("One.")
		_, second = self.submit_job("Two.")
		self.assertTrue(self.wait_until(lambda: self.request("GET", "/jobs?status=running")[1]["jobs"]))
		_, running = self.request("GET", "/jobs?status=running")
		self.assertEqual([j["job_id"] for j in running["jobs"]], [first["job_id"]])
		_, queued = self.request("GET", "/jobs?status=queued")
		self.assertEqual([j["job_id"] for j in queued["jobs"]], [second["job_id"]])

	def test_history_survives_a_restart_and_never_holds_prompts(self):
		_, data = self.submit_job("A secret prompt about a dress.")
		self.assertTrue(self.wait_for_status(data["job_id"], "done"))
		again = server.Service(dict(self.config, temp_dir=self.config["temp_dir"] + "-b"))
		self.assertEqual(again.observer.stats()["jobs"]["finished"], 1)
		self.assertEqual(again.observer.stats()["restarts"], 2)
		with open(again.observer.path) as f:
			self.assertNotIn("secret prompt", f.read())

	def test_a_broken_event_file_never_stops_recording(self):
		from observe import Observer
		blocked = os.path.join(self.folder.name, "file")
		open(blocked, "w").close()
		observer = Observer(os.path.join(blocked, "events.jsonl"))  # a folder can't be made inside a file
		observer.record("failed", "job", error="x")
		self.assertEqual(observer.stats()["jobs"]["failed"], 1)

	def test_event_file_is_trimmed(self):
		from observe import Observer
		path = os.path.join(self.folder.name, "ev", "events.jsonl")
		observer = Observer(path, keep=10)
		for n in range(25):
			observer.record("queued", f"job{n}")
		with open(path) as f:
			self.assertLess(len(f.readlines()), 25)  # trimmed every 10 lines, so it never reaches 2 x keep
		self.assertEqual(Observer(path, keep=10).recent(1)[0]["job"], "job24")

	def test_timing_spread(self):
		from observe import _spread
		self.assertEqual(_spread([]), {"count": 0, "p50": None, "p95": None, "max": None})
		self.assertEqual(_spread(list(range(1, 101))), {"count": 100, "p50": 50, "p95": 95, "max": 100})


if __name__ == "__main__":
	unittest.main()
