"""Garment accuracy scores for one generated photo, using the same garment map
the photo was drawn from.

Two questions:
  drift   - did the garment itself change? Compared pixel by pixel against the
            canvas inside the garment map (raw model output, before paste-back).
  added   - did the model add garment where the map says there is none?
            Pixels near the garment, outside the map, that match the garment's
            own colours (a longer hem, a sleeve that wasn't there, a filled slit).

Only numpy and OpenCV: no weights, runs in a second on a laptop.
"""

import cv2
import numpy as np

EDGE_PX = 6        # border ring where the mask is allowed to blend
NEAR_PX = 90       # how far outside the garment to look for added fabric
DE_CHANGED = 10.0  # colour distance that counts as "changed" (CIE76, ~ clearly visible)
PALETTE = 8        # garment colours to match against


def _lab(rgb):
	return cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32) * np.array([100 / 255, 1, 1], np.float32) - np.array([0, 128, 128], np.float32)


def score(canvas_rgb, output_rgb, garment_keep):
	"""canvas_rgb, output_rgb: HxWx3 uint8. garment_keep: HxW bool (True = garment)."""
	keep = garment_keep.astype(np.uint8)
	k = np.ones((3, 3), np.uint8)
	inner = cv2.erode(keep, k, iterations=EDGE_PX) > 0
	ring = (keep > 0) & ~inner

	a, b = _lab(canvas_rgb), _lab(output_rgb)
	de = np.linalg.norm(a - b, axis=2)

	# Added fabric: palette of the garment, matched in a band just outside it.
	near = (cv2.dilate(keep, k, iterations=NEAR_PX) > 0) & ~(cv2.dilate(keep, k, iterations=EDGE_PX) > 0)
	px = a[inner].reshape(-1, 3)
	px = px[np.random.default_rng(0).choice(len(px), min(len(px), 20000), replace=False)]
	_, _, centres = cv2.kmeans(px, PALETTE, None, (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5), 3, cv2.KMEANS_PP_CENTERS)
	dist = np.min(np.linalg.norm(b[..., None, :] - centres[None, None], axis=3), axis=2)
	# Textured, like the print, rather than flat skin or a plain backdrop.
	gray = cv2.cvtColor(output_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
	mu = cv2.blur(gray, (9, 9))
	sd = np.sqrt(np.maximum(cv2.blur(gray * gray, (9, 9)) - mu * mu, 0))
	added = near & (dist < 8) & (sd > 6)
	added = cv2.morphologyEx(added.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)) > 0

	area = int(keep.sum())
	# Hem: lowest garment-coloured row vs. the map's lowest row.
	rows = np.where(added.any(axis=1))[0]
	hem_map = int(np.where(keep.any(axis=1))[0].max())
	hem_out = int(rows.max()) if len(rows) and rows.max() > hem_map else hem_map

	s = {
		"drift_mean_dE": round(float(de[inner].mean()), 2),
		"drift_changed_pct": round(100 * float((de[inner] > DE_CHANGED).mean()), 2),
		"edge_changed_pct": round(100 * float((de[ring] > DE_CHANGED).mean()), 2),
		"added_fabric_pct": round(100 * float(added.sum()) / max(area, 1), 2),
		"hem_extended_px": hem_out - hem_map,
	}
	s["verdict"] = verdict(s)
	return s, added


def verdict(s):
	problems = []
	if s["drift_changed_pct"] > 5:
		problems.append("garment changed")
	if s["added_fabric_pct"] > 2:
		problems.append("fabric added outside the map")
	if s["hem_extended_px"] > 15:
		problems.append("hem longer than the garment")
	return "ok" if not problems else "; ".join(problems)


def overlay(output_rgb, garment_keep, added):
	"""Garment map outline in green, added fabric in red."""
	img = output_rgb.copy()
	contours, _ = cv2.findContours(garment_keep.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
	cv2.drawContours(img, contours, -1, (0, 255, 0), 2)
	img[added] = (img[added] * 0.3 + np.array([255, 0, 0]) * 0.7).astype(np.uint8)
	return img
