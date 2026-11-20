# Canvas test

Can FLUX.2 Klein keep the real garment and draw only the person around it? And how far does the garment move?

## Run it on the image Mac

1. Copy `inputs/` (sent separately, never committed) into `experiments/canvas_test/inputs/`.
2. Make sure the image server's queue is empty: two runs at once won't fit in 16 GB.
3. From the repo root:

```bash
.venv/bin/python -m pip install -r experiments/canvas_test/requirements.txt
.venv/bin/python experiments/canvas_test/run.py
```

It draws three versions (with and without the photo as a reference, two seeds), then writes `outputs/sheet.jpg` and `outputs/results.json`.

## What the scores mean

| Score | Measures | Bad when |
|---|---|---|
| `drift_changed_pct` | Garment pixels the model changed, before paste-back | over 5% |
| `edge_changed_pct` | Change in the 6 px blend ring | Expected to be high: the mask lets this ring blend |
| `added_fabric_pct` | Garment-coloured, print-like pixels just outside the garment map (a longer hem, an extra sleeve, a filled slit) | over 2% |
| `hem_extended_px` | How far the garment colours reach below the map's hem | over 15 px |

The `-final.png` files have the real garment pixels pasted back, so the garment in them is the photo's own.
