"""Canvas test: keep the real garment, let FLUX.2 Klein draw only the person and
the scene around it, then score how much the garment moved.

	.venv/bin/python experiments/canvas_test/run.py            # from the repo root
	.venv/bin/python experiments/canvas_test/run.py --dry-run  # print the commands only

Reads sd-cli and weights from config.json. Inputs in inputs/ (not in git):
	canvas.png         photo shrunk onto a taller frame, so there is room for a head and feet
	mask.png           white = draw here, black = keep (the garment)
	garment_keep.png   the garment map itself, used for paste-back and scoring
	reference.png      the original photo, sent with -r so the model sees the garment
Outputs in outputs/: raw and final photos, overlays, results.json and sheet.jpg.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

import cv2
import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import score  # noqa: E402

PROMPT = (
	"Full-length photorealistic fashion catalogue photo of a Ghanaian woman wearing this exact dress. "
	"Her head, face, neck, arms, lower legs and feet are visible. Her arms come out of the short sleeves "
	"and hang relaxed at her sides. Bare feet or simple flat sandals. The dress is unchanged: same print, "
	"same V-neck, same tassels, same knee length. Plain warm studio backdrop, soft daylight, natural skin."
)
VARIANTS = [
	{"name": "ref-seed42", "ref": True, "seed": 42},
	{"name": "ref-seed7", "ref": True, "seed": 7},
	{"name": "noref-seed42", "ref": False, "seed": 42},
]


def load_config():
	with open(os.environ.get("LOCAL_IMAGE_SERVER_CONFIG") or os.path.join(ROOT, "config.json")) as f:
		c = json.load(f)
	fix = lambda p: os.path.join(ROOT, os.path.expanduser(p)) if not os.path.isabs(os.path.expanduser(p)) else os.path.expanduser(p)
	c["sd_cli"] = fix(c["sd_cli"])
	c["weights"] = {k: fix(v) for k, v in c["weights"].items()}
	return c


def server_busy(port):
	try:
		with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
			h = json.load(r)
		return h.get("busy") or h.get("queue_length")
	except OSError:
		return False  # not running: fine


def command(c, v, inputs, out, size):
	cmd = [c["sd_cli"], *(x for kv in c["weights"].items() for x in kv),
		"-i", os.path.join(inputs, "canvas.png"),
		"--mask", os.path.join(inputs, "mask.png"),
		"--strength", "1.0",
		"-p", PROMPT,
		"--cfg-scale", str(c["cfg_scale"]), "--sampling-method", c["sampling_method"], "--steps", str(c["steps"]),
		"-W", str(size[0]), "-H", str(size[1]), "--seed", str(v["seed"]), "-o", out, *c["extra_args"]]
	if v["ref"]:
		cmd[cmd.index("-p"):cmd.index("-p")] = ["-r", os.path.join(inputs, "reference.png")]
	return cmd


def paste_back(canvas, raw, keep):
	"""Put the real garment pixels back, feathered over the blend ring."""
	alpha = cv2.GaussianBlur(cv2.erode(keep.astype(np.uint8) * 255, np.ones((3, 3), np.uint8), iterations=3), (0, 0), 2) / 255.0
	return (canvas * alpha[..., None] + raw * (1 - alpha[..., None])).astype(np.uint8)


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--dry-run", action="store_true")
	ap.add_argument("--force", action="store_true", help="run even if the image server is busy")
	args = ap.parse_args()

	c = load_config()
	inputs, outputs = os.path.join(HERE, "inputs"), os.path.join(HERE, "outputs")
	os.makedirs(outputs, exist_ok=True)
	canvas = np.asarray(Image.open(os.path.join(inputs, "canvas.png")).convert("RGB"))
	keep = np.asarray(Image.open(os.path.join(inputs, "garment_keep.png")).convert("L")) > 128
	size = (canvas.shape[1], canvas.shape[0])

	if not args.dry_run and not args.force and server_busy(c.get("port", 7860)):
		sys.exit("The image server is drawing right now. Wait for its queue to empty (16 GB can't hold two runs), or pass --force.")

	results, tiles = [], []
	for v in VARIANTS:
		raw_path = os.path.join(outputs, f"{v['name']}-raw.png")
		cmd = command(c, v, inputs, raw_path, size)
		if args.dry_run:
			print(" ".join(f'"{x}"' if " " in x else x for x in cmd), "\n")
			continue
		print(f"drawing {v['name']} ...", flush=True)
		t = time.monotonic()
		p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
		secs = round(time.monotonic() - t, 1)
		if p.returncode or not os.path.exists(raw_path):
			print(f"  failed after {secs}s:\n{p.stderr[-1500:]}")
			results.append({**v, "seconds": secs, "error": p.stderr[-500:]})
			continue
		raw = np.asarray(Image.open(raw_path).convert("RGB").resize(size))
		s, added = score.score(canvas, raw, keep)
		final = paste_back(canvas, raw, keep)
		Image.fromarray(final).save(os.path.join(outputs, f"{v['name']}-final.png"))
		ov = score.overlay(raw, keep, added)
		Image.fromarray(ov).save(os.path.join(outputs, f"{v['name']}-check.png"))
		print(f"  {secs}s  {s}")
		results.append({**v, "seconds": secs, **s})
		tiles.append((v["name"], s["verdict"], final, ov))

	if args.dry_run:
		return
	with open(os.path.join(outputs, "results.json"), "w") as f:
		json.dump({"prompt": PROMPT, "results": results}, f, indent=2)
	if tiles:
		h, w = canvas.shape[:2]
		sheet = Image.new("RGB", (w * 2, (h + 40) * len(tiles)), "white")
		for i, (name, verdict, final, ov) in enumerate(tiles):
			y = i * (h + 40)
			sheet.paste(Image.fromarray(final), (0, y + 40)); sheet.paste(Image.fromarray(ov), (w, y + 40))
			ImageDraw.Draw(sheet).text((10, y + 12), f"{name}: {verdict}   (left: final, right: raw with map in green, added fabric in red)", fill="black")
		sheet.thumbnail((1600, 6000))
		sheet.save(os.path.join(outputs, "sheet.jpg"), quality=88)
	print("\nDone. Open outputs/sheet.jpg and send me outputs/results.json.")


if __name__ == "__main__":
	main()
