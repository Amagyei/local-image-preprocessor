"""Group shape template: colour-free garment maps, cropped and scaled to one frame,
averaged into "what members of this item group look like".

normalise(mask)        -> tight crop, height scaled to H, centred on the garment's middle
profile(norm)          -> width at 20 evenly spaced heights, as a fraction of height
landmarks(norm)        -> shoulder, widest point (wings/sleeves), hem: positions as fractions
build(masks)           -> mean map, core (>=80% of members), envelope (>=20%), mean/std profile
compare(norm, tpl)     -> IoU with the group shape, area outside the envelope, profile z-scores
"""

import json

import cv2
import numpy as np

H = W = 256
ROWS = 20


def normalise(mask):
	ys, xs = np.where(mask)
	crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8)
	h, w = crop.shape
	s = H / h
	nw = max(1, round(w * s))
	crop = cv2.resize(crop * 255, (nw, H), interpolation=cv2.INTER_AREA) > 127
	out = np.zeros((H, max(W, nw)), bool)
	cx = int(np.round(np.where(crop)[1].mean()))  # centre on the garment's mass, not its bbox
	x0 = out.shape[1] // 2 - cx
	x0 = min(max(x0, 0), out.shape[1] - nw)
	out[:, x0:x0 + nw] = crop
	if out.shape[1] > W:  # wider than tall: trim evenly
		d = (out.shape[1] - W) // 2
		out = out[:, d:d + W]
	return out


def profile(norm):
	rows = np.linspace(0, H - 1, ROWS).astype(int)
	return norm[rows].sum(1) / H


def landmarks(norm):
	w = norm.sum(1)
	top_rows = norm[: H // 20]
	wide = int(np.argmax(w))
	xs = np.where(norm[wide])[0]
	return {
		"aspect_w_over_h": round(float(w.max() / H), 3),
		"shoulder_w": round(float(top_rows.sum(1).max() / H), 3),
		"widest_at": round(wide / H, 3),
		"widest_w": round(float(w.max() / H), 3),
		"widest_left_x": round(xs.min() / W, 3),
		"widest_right_x": round(xs.max() / W, 3),
		"hem_w": round(float(w[-3:].mean() / H), 3),
		"fill": round(float(norm.mean()), 3),
	}


def build(norms):
	stack = np.stack([n.astype(float) for n in norms])
	mean = stack.mean(0)
	profs = np.stack([profile(n) for n in norms])
	return {
		"members": len(norms),
		"mean": mean,
		"core": mean >= 0.8,
		"envelope": mean >= 0.2,
		"profile_mean": profs.mean(0),
		"profile_std": np.maximum(profs.std(0), 0.02),  # floor: tiny groups understate spread
	}


def compare(norm, tpl):
	shape = tpl["mean"] >= 0.5
	iou = (norm & shape).sum() / max((norm | shape).sum(), 1)
	outside = (norm & ~tpl["envelope"]).sum() / max(norm.sum(), 1)
	missing = (tpl["core"] & ~norm).sum() / max(tpl["core"].sum(), 1)
	z = (profile(norm) - tpl["profile_mean"]) / tpl["profile_std"]
	worst = int(np.argmax(np.abs(z)))
	return {
		"iou": round(float(iou), 3),
		"outside_envelope_pct": round(100 * float(outside), 1),
		"core_missing_pct": round(100 * float(missing), 1),
		"profile_max_z": round(float(np.abs(z).max()), 2),
		"profile_worst_height": round(worst / (ROWS - 1), 2),
	}


def save(tpl, path):
	np.savez_compressed(path, **{k: v for k, v in tpl.items() if isinstance(v, np.ndarray)}, members=tpl["members"])
