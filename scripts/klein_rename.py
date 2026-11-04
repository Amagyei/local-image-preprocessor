"""Rewrite FLUX.2 Klein's diffusers transformer with Black Forest Labs' tensor names, which sd-cli recognises.

Weights keep their values. q, k and v are joined, and scale/shift swapped, which is plain
byte work because each is a matrix stored row by row. Usage: klein_rename.py in out
"""
import json
import os
import re
import struct
import sys

SIMPLE = [
	(r"^x_embedder\.", "img_in."),
	(r"^context_embedder\.", "txt_in."),
	(r"^time_guidance_embed\.timestep_embedder\.linear_1\.", "time_in.in_layer."),
	(r"^time_guidance_embed\.timestep_embedder\.linear_2\.", "time_in.out_layer."),
	(r"^(double_stream_modulation_img|double_stream_modulation_txt|single_stream_modulation)\.linear\.", r"\1.lin."),
	(r"^proj_out\.", "final_layer.linear."),
	(r"^transformer_blocks\.(\d+)\.attn\.norm_q\.weight$", r"double_blocks.\1.img_attn.norm.query_norm.scale"),
	(r"^transformer_blocks\.(\d+)\.attn\.norm_k\.weight$", r"double_blocks.\1.img_attn.norm.key_norm.scale"),
	(r"^transformer_blocks\.(\d+)\.attn\.norm_added_q\.weight$", r"double_blocks.\1.txt_attn.norm.query_norm.scale"),
	(r"^transformer_blocks\.(\d+)\.attn\.norm_added_k\.weight$", r"double_blocks.\1.txt_attn.norm.key_norm.scale"),
	(r"^transformer_blocks\.(\d+)\.attn\.to_out\.0\.", r"double_blocks.\1.img_attn.proj."),
	(r"^transformer_blocks\.(\d+)\.attn\.to_add_out\.", r"double_blocks.\1.txt_attn.proj."),
	(r"^transformer_blocks\.(\d+)\.ff\.linear_in\.", r"double_blocks.\1.img_mlp.0."),
	(r"^transformer_blocks\.(\d+)\.ff\.linear_out\.", r"double_blocks.\1.img_mlp.2."),
	(r"^transformer_blocks\.(\d+)\.ff_context\.linear_in\.", r"double_blocks.\1.txt_mlp.0."),
	(r"^transformer_blocks\.(\d+)\.ff_context\.linear_out\.", r"double_blocks.\1.txt_mlp.2."),
	(r"^single_transformer_blocks\.(\d+)\.attn\.to_qkv_mlp_proj\.", r"single_blocks.\1.linear1."),
	(r"^single_transformer_blocks\.(\d+)\.attn\.to_out\.", r"single_blocks.\1.linear2."),
	(r"^single_transformer_blocks\.(\d+)\.attn\.norm_q\.weight$", r"single_blocks.\1.norm.query_norm.scale"),
	(r"^single_transformer_blocks\.(\d+)\.attn\.norm_k\.weight$", r"single_blocks.\1.norm.key_norm.scale"),
]


def main(source, out):
	f = open(source, "rb")
	size = struct.unpack("<Q", f.read(8))[0]
	tensors = json.loads(f.read(size))
	tensors.pop("__metadata__", None)
	base = 8 + size

	def read(name, start=0, length=None):
		begin, end = tensors[name]["data_offsets"]
		f.seek(base + begin + start)
		return f.read((end - begin - start) if length is None else length)

	# new name -> (dtype, shape, list of (old name, start, length))
	plan, used = {}, set()

	def add(new, old_names, axis0=None):
		first = tensors[old_names[0]]
		shape = list(first["shape"])
		if axis0:
			shape[0] = axis0
		pieces = []
		for old in old_names:
			begin, end = tensors[old]["data_offsets"]
			pieces.append((old, 0, end - begin))
			used.add(old)
		plan[new] = (first["dtype"], shape, pieces)

	for n in sorted({int(m.group(1)) for k in tensors if (m := re.match(r"transformer_blocks\.(\d+)\.", k))}):
		p = f"transformer_blocks.{n}.attn."
		for names, target in (
			((f"{p}to_q.weight", f"{p}to_k.weight", f"{p}to_v.weight"), "img_attn"),
			((f"{p}add_q_proj.weight", f"{p}add_k_proj.weight", f"{p}add_v_proj.weight"), "txt_attn"),
		):
			add(f"double_blocks.{n}.{target}.qkv.weight", list(names), tensors[names[0]]["shape"][0] * 3)

	# Diffusers stores (scale, shift); the original stores (shift, scale).
	name = "norm_out.linear.weight"
	begin, end = tensors[name]["data_offsets"]
	half = (end - begin) // 2
	plan["final_layer.adaLN_modulation.1.weight"] = (
		tensors[name]["dtype"], tensors[name]["shape"], [(name, half, half), (name, 0, half)]
	)
	used.add(name)

	for old in tensors:
		if old in used:
			continue
		for pattern, replacement in SIMPLE:
			new, count = re.subn(pattern, replacement, old)
			if count:
				add(new, [old])
				break
		else:
			raise SystemExit(f"No rule for {old}")

	header, offset = {}, 0
	for new, (dtype, shape, pieces) in plan.items():
		length = sum(piece[2] for piece in pieces)
		header[new] = {"dtype": dtype, "shape": shape, "data_offsets": [offset, offset + length]}
		offset += length
	encoded = json.dumps(header, separators=(",", ":")).encode()
	encoded += b" " * (-len(encoded) % 8)

	tmp = out + ".part"
	with open(tmp, "wb") as w:
		w.write(struct.pack("<Q", len(encoded)))
		w.write(encoded)
		for new, (_, _, pieces) in plan.items():
			for old, start, length in pieces:
				w.write(read(old, start, length))
	os.replace(tmp, out)
	print(f"{len(tensors)} tensors in, {len(header)} out, {os.path.getsize(out) / 1e9:.2f} GB -> {out}")


main(sys.argv[1], sys.argv[2])
