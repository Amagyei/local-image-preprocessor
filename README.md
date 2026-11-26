# Local image server

Runs the shop's image model on a separate Mac and answers requests from the laptop. For each image the laptop sends one garment photo and one prompt, waits, and saves the result. This folder has nothing to do with Frappe, bench or the live site.

The service listens on 127.0.0.1 only. The laptop reaches it through an SSH tunnel.

## API

`POST /sdapi/v1/img2img`

```json
{"prompt": "...", "init_images": ["<base64 JPEG>"], "width": 1536, "height": 2048, "batch_size": 1}
```

A successful call returns 200 with `{"images": ["<base64 PNG>"]}`. Bad requests get 400, and failed runs get 500. Both come with `{"error": "..."}`.

`GET /health` returns `model`, `ready`, `missing_files`, `loaded`, `busy`, `queue_length`, `progress` and `results_kept`. `loaded` and `busy` always match, because the model is only in memory during a run.

### Looking at what it is doing

| Call | Shows |
|---|---|
| `GET /jobs?status=running` | The job being drawn now. `queued`, `done` and `failed` work too; no filter lists everything. |
| `GET /metrics` | The health strip as JSON: jobs finished and failed, success rate, seconds per photo (median, 95th percentile, slowest), queue wait, memory retries and waits, last failure and the usual failure causes, free memory, disk used by `runtime/jobs`, and `now` (the same as `/health`). |
| `GET /events?limit=100&job=<id>` | The latest events, newest first, optionally for one job. |
| `GET /jobs/{id}/detail` | Also has `events` (that job's timeline) and `panel_prompts` (the exact text each photo was drawn from, with `garment_instruction` added). |

Every event (queued, started, photo drawn, memory wait, memory retry, finished, failed, cancelled, resumed, removed, expired, startup) is one line in `runtime/events.jsonl` (`events_file` in `config.json`), the last 2000 kept, so the figures survive a restart. Events hold ids, sizes, timings and error text, never photos or prompts. `/health` also has `worker_alive` and `running_seconds`.

### Status page

`GET /ui` (also `/`) is a read-only page showing whether the service is ready, what is drawing, what is waiting, drafts to collect, failures and uptime, refreshed every 3 seconds. It reads `/health` and `/jobs`, and shows a thumbnail of each finished picture (`/jobs/{id}/thumb`, full size at `/jobs/{id}/image`). It never shows the garment photo and never touches the queue. A health strip (from `/metrics`) and a list of recent events sit above and below the table, and opening a job shows its prompt, the text each photo was drawn from, and its timeline. Through the tunnel: `http://localhost:7860/ui`.

### Drafts: jobs that outlive the connection

The laptop doesn't have to stay connected while a drawing runs.

| Call | Does |
|---|---|
| `POST /jobs` | Same body as img2img, plus an optional `seed`. Saves the job to disk and answers `202` at once with `{"job_id", "status"}`. The same request again returns the same job; a failed one is queued again and carries on from its finished photos. |
| `GET /jobs/{id}` | `status` is `queued`, `running`, `done` or `failed`, with `progress` and `error`. |
| `GET /jobs/{id}/result` | `{"images": ["<base64 PNG>"]}` once done, `409` before that. Can be fetched again. |
| `DELETE /jobs/{id}` | The laptop is done with this job: deletes its folder. A queued job won't be drawn, and a running one stops at once. |
| `GET /jobs` | Every job on disk. |

Jobs live in `runtime/jobs/<id>/` (`jobs_dir` in `config.json`). After a crash, restart or reboot the service picks up where it left off: finished drafts wait to be collected, queued and half-drawn jobs run again, keeping the photos already drawn. A draft nobody collects is deleted after `draft_retention_days` (30).

### Batches: many drafts, handled together

A batch is a group of drafts the laptop can send in one call, watch as one thing and collect when it likes. The Mac keeps drawing whether or not the laptop is connected; every job is saved to disk like any other draft.

| Call | Does |
|---|---|
| `POST /batches` | Queues every job and answers `202` at once with the batch's state. |
| `GET /batches` | Every batch: `status`, `total`, `counts`, `created`, `finished`. |
| `GET /batches/{id}` | The same, plus each job's state (`job_id`, `status`, `progress`, `error`). |
| `GET /batches/{id}/results` | `{"images": {job_id: base64 PNG}, "order", "pending", "failed"}` for the pictures that are ready, so a batch can be collected in part. |
| `DELETE /batches/{id}` | Forgets the whole batch: stops what is running and deletes every job's photo and result. Answers `{"removed": n}`. |

```json
{"batch_id": "optional-name", "init_images": ["<base64 JPEG shared by every job>"],
 "jobs": [{"prompt": "...", "width": 768, "height": 1024, "seed": 1}, {"prompt": "...", "width": 768, "height": 1024, "init_images": ["<this job's own photo>"]}]}
```

- The `202` answer is the batch's state plus `job_ids`: one id per job you sent, in the order you sent them (a drawing sent twice repeats its id), so you can tell which is which.
- Each job takes what `POST /jobs` takes. A shared `init_images` is sent once, and a job's own `init_images` replaces it.
- Every job is checked before any is queued, so one bad job (the error says which, "Job 2: ...") refuses the whole batch and nothing is drawn.
- A batch holds at most 50 jobs. `batch_id` is 1 to 64 letters, digits, `-` or `_`; leave it out and the Mac makes one.
- Sending the same batch again queues nothing twice, and a job that failed carries on from its finished photos. The same drawing twice in a batch is one drawing. Jobs sent under an existing `batch_id` are added to it.
- `status` is `queued` (nothing started), `running` (something is queued or drawing), `done` (every job drawn), `failed` (nothing left to draw and some didn't come out) or `empty` (every job has been removed).
- Jobs still run one at a time, in the order they were sent. Batches are remembered across a restart, and one disappears once every job in it has been collected or has expired.
- Events say `batch_queued` and `batch_removed` with the batch id and a count, never prompts.

The jobs of a batch are ordinary drafts, so `/jobs`, `/jobs/{id}/result` and `DELETE /jobs/{id}` still work on them.

### Reaching it from the laptop

The laptop opens a tunnel to this Mac, and Studio's "Local Image Tool URL" is `http://127.0.0.1:7860` on the laptop. The tunnel only needs to be up when the laptop sends or collects; the Mac carries on without it.

```bash
ssh -N -L 7860:127.0.0.1:7860 user@this-mac
```

On this Mac, turn on Remote Login (System Settings, General, Sharing) and put the laptop's public key in `~/.ssh/authorized_keys`, limited to this one port:

```
restrict,port-forwarding,permitopen="127.0.0.1:7860" ssh-ed25519 AAAA... laptop
```

## How it behaves

- It runs one request at a time and queues the rest.
- A grid prompt is split, and each panel is drawn on its own, then placed edge to edge. A 16 GB Mac can't draw the whole grid at once. See `panels.py`.
- If the connection drops, the run still finishes. The result stays in memory for 30 minutes, and sending the same request again returns it.
- If a request fails part way, finished panels stay in memory for 30 minutes, so a retry only draws the rest.
- Plain `/sdapi/v1/img2img` calls only put photos on disk in `runtime/tmp` during a run; that folder is cleared after each run and at startup.
- Jobs sent to `/jobs` are the exception: the garment photo stays in `runtime/jobs/<id>/` until the drawing finishes, and the result until the laptop collects it. Keep that folder private. Logs hold job ids, sizes and timings, never photos or prompts.

## Model

FLUX.2 Klein 4B from [black-forest-labs/FLUX.2-klein-4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B), Apache-2.0: 4 steps, cfg 1, euler, 1024 px long side per panel.

Qwen-Image-2.1 was also tested. Its licence is non-commercial, so nothing it generates can go on the shop without a commercial licence.

Weights are never committed. These three files are expected in `~/models/flux2-klein/`:

| File | Size | How it's made |
|---|---|---|
| `flux2-klein-4b.safetensors` | 7.8 GB | `scripts/klein_rename.py` on `transformer/diffusion_pytorch_model.safetensors`. sd-cli doesn't recognise the diffusers tensor names. |
| `qwen3-4b-text-encoder.safetensors` | 8.0 GB | `scripts/join_safetensors.py` on the two `text_encoder/model-*.safetensors` files |
| `flux2-vae.safetensors` | 168 MB | Copy of `vae/diffusion_pytorch_model.safetensors` |

## Runtime

stable-diffusion.cpp `sd-cli` 290148d (MIT), unzipped to `runtime/sd-master-290148d/`:

```
https://github.com/leejet/stable-diffusion.cpp/releases/download/master-1039-290148d/sd-master-290148d-bin-Darwin-macOS-26.6.2-arm64.zip
sha256 9e85e31dfa4daae0259c66ac41fab8a5441688b77ee2a761845c3cb88c16f4a4
```

Run it with `--mmap` and without `--offload-to-cpu`. On a Mac the CPU and GPU share memory, so offloading holds the weights twice: the peak measured 19.8 GB with it and 6.4 GB without (Qwen run).

## Running it

```bash
./start.sh                  # in the foreground; sets up .venv the first time
./install_launch_agent.sh   # at login, restarted on a crash, Mac kept awake
```

Logs go to `~/Library/Logs/local-image-server.log`. `LOCAL_IMAGE_SERVER_CONFIG` points at a different config file.

Tests use a stand-in for sd-cli, so they need no weights:

```bash
.venv/bin/python -m unittest discover tests
```

## Prompts

`garment_instruction` in `config.json` adds the same line to every prompt, so it stays empty: not every garment is full length. Klein tends to shorten long garments, so Studio's form has a Garment length field that adds it to the prompt.
