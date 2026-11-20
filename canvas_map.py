"""Map the garment in a photo and build the canvas a canvas-mode drawing starts from.

Used when a request asks for "canvas": "auto": the laptop sends only the photo, and this
Mac finds the garment, puts the shot on a taller frame so there is room for a head and
feet, and makes the mask (white = draw, black = keep the garment).

Photos are of a garment on a dress form. The form's top knob, neck and pole are taken out
by shape: thin parts go, and rows above the shoulders that are much narrower than the
garment are the neck. The stand's base can't be told from a hem reliably, so the laptop may
send `hem` (where the hem is, as a fraction of the photo's height) and everything below goes.

Needs the extras in requirements-canvas.txt; nothing here is imported until a job asks.
"""

import io

import numpy as np
from PIL import Image, ImageOps

PHOTO_SCALE = 0.75  # the shot fills this much of the frame
TOP_MARGIN = 0.176  # room above the neckline for a head
GROW_INTO_GARMENT = 3  # px the drawn area reaches into the garment edge, so the seam blends
_session = None


class MapError(Exception):
	pass


def garment_map(photo):
	"""Bool array: True where the garment is. `photo` is an upright RGB PIL image."""
	import cv2
	from rembg import new_session, remove

	global _session
	if _session is None:
		_session = new_session("isnet-general-use")
	small = photo.copy()
	small.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
	found = np.asarray(remove(small, session=_session, only_mask=True)) > 128

	g = cv2.morphologyEx(found.astype(np.uint8), cv2.MORPH_OPEN, np.ones((1, 31), np.uint8))  # pole, knob
	g = _biggest(g)
	if not g.any():
		raise MapError("Couldn't find a garment in the photo.")
	widths = g.sum(1)
	rows = np.where(widths > 0)[0]
	top, bottom = rows[0], rows[-1]
	for y in range(top, top + (bottom - top) // 4):  # the form's neck above the shoulders
		if widths[y] < 0.18 * widths.max():
			g[y] = 0
		else:
			break
	g = _fill_holes(_biggest(g))
	return cv2.resize(g.astype(np.uint8), photo.size, interpolation=cv2.INTER_NEAREST) > 0


def build(photo_bytes, width, height, hem=None):
	"""Return (canvas, mask, map) PIL images at width x height.

	canvas: the photo on a taller frame; mask: L, 255 = draw; map: L, 255 = garment."""
	import cv2

	with Image.open(io.BytesIO(photo_bytes)) as image:
		photo = ImageOps.exif_transpose(image).convert("RGB")
	garment = garment_map(photo)
	if hem is not None:
		garment[int(round(hem * photo.size[1])) + 1:] = False
		garment = _biggest(garment.astype(np.uint8)) > 0
	if not garment.any():
		raise MapError("Nothing is left of the garment above the hem.")

	box_w, box_h = width * PHOTO_SCALE, height * PHOTO_SCALE
	scale = min(box_w / photo.size[0], box_h / photo.size[1])
	w, h = max(1, round(photo.size[0] * scale)), max(1, round(photo.size[1] * scale))
	x0, y0 = (width - w) // 2, min(round(height * TOP_MARGIN), height - h)

	shot = np.asarray(photo.resize((w, h), Image.Resampling.LANCZOS))
	canvas = cv2.copyMakeBorder(shot, y0, height - h - y0, x0, width - w - x0, cv2.BORDER_REPLICATE)
	keep = np.zeros((height, width), np.uint8)
	keep[y0:y0 + h, x0:x0 + w] = cv2.resize(garment.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
	draw = cv2.dilate(1 - keep, np.ones((2 * GROW_INTO_GARMENT + 1,) * 2, np.uint8))
	return Image.fromarray(canvas), Image.fromarray(draw * 255), Image.fromarray(keep * 255)


def _biggest(mask):
	import cv2

	n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
	if n <= 1:
		return np.zeros_like(mask, dtype=np.uint8)
	return (labels == 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))).astype(np.uint8)


def _fill_holes(mask):
	import cv2

	h, w = mask.shape
	flood = np.zeros((h + 2, w + 2), np.uint8)
	outside = mask.astype(np.uint8).copy()
	cv2.floodFill(outside, flood, (0, 0), 1)
	return (mask.astype(bool) | (outside == 0)).astype(np.uint8)
