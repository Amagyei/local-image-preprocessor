"""Join split .safetensors files into one, without loading them. Usage: join_safetensors.py out in1 in2 ..."""
import json
import os
import shutil
import struct
import sys


def main(out, inputs):
	header, offset, sources = {}, 0, []
	for path in inputs:
		with open(path, "rb") as f:
			size = struct.unpack("<Q", f.read(8))[0]
			part = json.loads(f.read(size))
		part.pop("__metadata__", None)
		start = 8 + size
		for name, info in part.items():
			begin, end = info["data_offsets"]
			header[name] = {**info, "data_offsets": [begin + offset, end + offset]}
		length = os.path.getsize(path) - start
		sources.append((path, start))
		offset += length

	encoded = json.dumps(header, separators=(",", ":")).encode()
	encoded += b" " * (-len(encoded) % 8)
	tmp = out + ".part"
	with open(tmp, "wb") as f:
		f.write(struct.pack("<Q", len(encoded)))
		f.write(encoded)
		for path, start in sources:
			with open(path, "rb") as source:
				source.seek(start)
				shutil.copyfileobj(source, f, 16 * 1024 * 1024)
	os.replace(tmp, out)
	print(f"{len(header)} tensors, {os.path.getsize(out) / 1e9:.2f} GB -> {out}")


main(sys.argv[1], sys.argv[2:])
