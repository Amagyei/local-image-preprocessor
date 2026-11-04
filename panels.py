"""Turn one request into the separate photos the model should draw.

The prompts were written for a model that draws a whole grid at once. A 1536×2048
grid costs this Mac about 2 minutes a step, so each panel is drawn on its own and
the panels are placed edge to edge afterwards. That is what the laptop's slicer
expects, so nothing changes on its side.
"""

import re
from dataclasses import dataclass


class PromptError(ValueError):
	pass


PLACES = {
	(2, 2): ("top-left", "top-right", "bottom-left", "bottom-right"),
	(1, 2): ("left", "right"),
}
SINGLE = "One single photograph, not a grid or collage."

LAYOUT = re.compile(r"Layout: a single image made of exactly (\d+) rows? × (\d+) columns?[^\n]*")
GRID_FORMAT = re.compile(r"A \d+x\d+ grid composite[^\n]*")
SHOT_LINE = re.compile(
	r"^- (Top-left|Top-right|Bottom-left|Bottom-right|Left|Right) frame \(Shot \d+\): (.*)$", re.M
)
LABELLED = re.compile(
	r"(?:(?<=: )|(?<=; ))(top-left|top-right|bottom-left|bottom-right|left|right), (.+?)(?=; |\.(?: |\n|$))"
)
COUNTED_PHOTOS = re.compile(r"(?i)\b(?:two|four)\s+([\w -]*?)photos(?:\s+side by side)?")
IN_ALL = re.compile(r"\s+in (?:all four|both)\b")


@dataclass
class Panel:
	number: int
	prompt: str
	box: tuple  # left, top, right, bottom in the finished image
	draw_size: tuple  # width, height the model draws


def split_prompt(prompt):
	"""Return (rows, cols, one prompt per panel in reading order)."""
	layout = LAYOUT.search(prompt)
	if not layout:
		return 1, 1, [prompt]

	rows, cols = int(layout.group(1)), int(layout.group(2))
	if rows * cols == 1:
		return 1, 1, [prompt]
	places = PLACES.get((rows, cols))
	if not places:
		raise PromptError(f"This service can't draw a {rows}×{cols} grid yet.")

	text = _tidy(prompt[: layout.start()] + prompt[layout.end() :])
	if SHOT_LINE.search(text):
		return rows, cols, _from_shot_lines(text, places)
	return rows, cols, _from_labels(text, places)


def plan(prompt, width, height, max_long_side):
	rows, cols, texts = split_prompt(prompt)
	panels = []
	for row in range(rows):
		for col in range(cols):
			left, right = round(width * col / cols), round(width * (col + 1) / cols)
			top, bottom = round(height * row / rows), round(height * (row + 1) / rows)
			panels.append(
				Panel(
					number=len(panels) + 1,
					prompt=texts[len(panels)],
					box=(left, top, right, bottom),
					draw_size=draw_size(right - left, bottom - top, max_long_side),
				)
			)
	return panels


def draw_size(width, height, max_long_side):
	"""The model wants multiples of 32, and 16 GB can't afford more than the long side."""
	scale = min(1, max_long_side / max(width, height))
	return _multiple_of_32(width * scale), _multiple_of_32(height * scale)


def _multiple_of_32(n):
	return max(32, round(n / 32) * 32)


def _from_shot_lines(text, places):
	found = [m.group(1).lower() for m in SHOT_LINE.finditer(text)]
	if sorted(found) != sorted(places):
		raise PromptError(f"Expected shots for {', '.join(places)} but found {', '.join(found)}.")

	panels = []
	for place in places:
		kept = SHOT_LINE.sub(lambda m: m.group(2) if m.group(1).lower() == place else "", text)
		kept = GRID_FORMAT.sub(f"A fashion photograph of one model. {SINGLE}", kept)
		panels.append(_tidy(kept))
	return panels


def _from_labels(text, places):
	matches = list(LABELLED.finditer(text))
	found = {m.group(1): m.group(2) for m in matches}
	if sorted(found) != sorted(places):
		raise PromptError(
			f"Couldn't find a shot for each of {', '.join(places)} in this grid prompt. "
			"Write them as 'top-left, ...; top-right, ...'."
		)

	first, last = matches[0], matches[-1]
	start = text.rfind("\n\n", 0, first.start()) + 2 if "\n\n" in text[: first.start()] else 0
	before, intro = text[:start], COUNTED_PHOTOS.sub(r"one \1photo", text[start : first.start()]).rstrip()
	after = IN_ALL.sub("", text[last.end() :])
	return [_tidy(f"{before}{intro} {found[place]}{after}\n\n{SINGLE}") for place in places]


def _tidy(text):
	text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
	return re.sub(r"\n{2,}(?=- )|(?<=\])\n{2,}", "\n", text).strip()
