# Group shape template

Colour-free garment maps of one item group, cropped and scaled to one 256 px frame (height fixed, centred on the garment), then averaged:

- `mean`: how often each pixel is garment across the group
- `core`: garment in at least 80% of members; a draft missing it lost part of the garment
- `envelope`: garment in at least 20%; a draft outside it added something the group never has
- width profile at 20 heights, with its spread across the group

`compare()` gives IoU against the group shape, the % outside the envelope, the % of core missing and the worst width z-score, with the height where it happens.

First run: eli-mix-long, 6 members (IMG_3896 left out, its top is cut off). Members score IoU 0.87–0.91 against a leave-one-out template; the cut-off photo scores 0.83 with 12% outside the envelope, so the bad one stands out.
