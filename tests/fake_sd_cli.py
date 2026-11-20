"""Stands in for sd-cli in the tests: draws a flat colour, or fails on request."""

import json
import os
import sys
import time

from PIL import Image


def main():
	args = sys.argv[1:]
	value = lambda flag: args[args.index(flag) + 1]

	with open(os.environ["FAKE_CALLS"], "a") as f:
		f.write(value("-p").replace("\n", " ") + "\n")

	time.sleep(float(os.environ.get("FAKE_SLEEP") or 0))
	fail = os.environ.get("FAKE_FAIL")
	if fail and os.path.exists(fail):
		print("[E] Metal buffer: out of memory --- fake.cpp:1", file=sys.stderr)
		sys.exit(1)

	fail_on = os.environ.get("FAKE_FAIL_ON")
	if fail and fail_on and os.path.exists(fail + "-on") and fail_on in value("-p"):
		print("[E] failed during weight preparation --- fake.cpp:4", file=sys.stderr)
		sys.exit(1)

	fail_once = os.environ.get("FAKE_FAIL_ONCE")
	if fail_once and os.path.exists(fail_once):
		os.remove(fail_once)
		print("[E] transformer_blocks.31 failed during weight preparation --- fake.cpp:2", file=sys.stderr)
		print("[E] generate failed --- fake.cpp:3", file=sys.stderr)
		sys.exit(1)

	if os.environ.get("FAKE_ARGS"):
		with open(os.environ["FAKE_ARGS"], "a") as f:
			f.write(json.dumps(args) + "\n")

	size = (int(value("-W")), int(value("-H")))
	drawn = Image.new("RGB", size, (200, 40, 40))
	if "-i" in args and "--mask" in args:
		# Canvas mode: a careless model that also smudges what it should keep.
		init = Image.open(value("-i")).convert("RGB").resize(size)
		drawn = Image.blend(init, drawn, 0.5)
	drawn.save(value("-o"))


main()
