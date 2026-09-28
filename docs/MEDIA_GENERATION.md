# Local media generation

HomeBrain's self MCP server exposes `homebrain.media_capabilities`,
`homebrain.media_generate`, and `homebrain.media_job_status`. The existing
discrete Intel Arc/ComfyUI runtime is required. Generation temporarily stops
the chat model to release GPU memory; the OpenClaw gateway stays available
for deterministic starting notices and attachment delivery.

## Models

| Model ID | Output | Fixed workflow |
|---|---|---|
| `krea2` (image default) | PNG, 1024×1024 | 8 steps |
| `qwen-image-2.1` | PNG, 1024×1024 | 25 steps, Euler/simple, CFG 1 |
| `minimax-h3` (video default) | H.264 MP4, 672×384 | 56 frames, 8 steps |

Weights must already be installed under `/home/homebrain/ComfyUI/models`.
Generation never downloads models. Missing weights disable that model.
`src/picture.py` lists the exact filenames and published sizes.

Qwen's fixed text-to-image workflow uses the official
[ComfyUI template](https://github.com/Comfy-Org/workflow_templates/blob/e7cd011d4ded3411c2f481200544f0be6fdc962e/templates/image_qwen_image_2_1_t2i.json),
without the optional prompt-rewriting model or reference images. Its weights
are from [Comfy-Org/Qwen-Image-2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1):

- `diffusion_models/qwen_image_2.1_int8_convrot.safetensors`
- `text_encoders/qwen3vl_8b_int8_convrot.safetensors`
- `vae/qwen_image_2.1_vae_bf16.safetensors`

The pinned ComfyUI revision in `scripts/picture.sh` includes `TextEncodeQwenImage21`.
Only a model ID and prompt are accepted; callers cannot supply a graph,
output path, callback URL, recipient, or shell command.

## Telegram setup

1. Link the BotFather token in **Messaging Channels**.
2. Message your new bot in Telegram. Enter the pairing code it sends in
   HomeBrain and click **Approve**. A bot token alone does not authorize users.
3. Select the paired DM in **Media delivery** and save it.

Only numeric DMs on OpenClaw's default Telegram account are supported for
automatic delivery. The recipient is fixed in dashboard settings; it is not
inferred from the last conversation. Dashboard generations stay in the gallery.
Agent requests are delivered to that configured recipient even if initiated
from another agent surface. This is an owner-appliance feature, not multi-user
conversation routing or isolation from a privileged agent.

## Job lifetime

`media_generate` requires a request ID (8–100 letters, digits, `_`, or `-`).
Reuse it only when retrying the same request. HTTP acceptance is `202`; the
MCP call returns immediately. Replays return the existing job, and a different
payload with the same request ID is rejected. The shared task lock and database
reservation prevent concurrent dashboard/MCP generation.

`homebrain-media.service` persists jobs and delivery receipts in
`/var/lib/homebrain/media/jobs.sqlite3` (private to root), sends a starting notice,
and starts the existing picture/video systemd service. If the starting notice
is not confirmed within ten minutes, the job expires without taking the GPU.
Generation, chat recovery, and message delivery have separate outcomes.
The worker also requires ten continuous idle seconds from llama-server's
`/slots` endpoint, so the agent can finish its response after the tool call.
Unavailable slot status or a busy model also delays startup until that deadline.

The media services attempt chat recovery on normal exit and in `ExecStopPost`.
The worker reconciles interrupted jobs after restarting; a job already submitted
to systemd is never automatically regenerated. A saved artifact with its matching
sidecar can be delivered even if the model fails to recover.

Attachments are validated against their job ID and temporarily copied into
OpenClaw's approved media directory. Files over 49 MiB stay in the dashboard.
Destination authorization is checked again at send time. Changing or disabling
the recipient blocks outstanding deliveries to the previous recipient.

Known transient rejections retry with bounded backoff. Timeouts, interrupted
sends, and missing receipts become `unknown`, because Telegram may already have
accepted the message. The dashboard's **Retry delivery** action warns about
possible duplicates. A confirmed delivery is never resent automatically.
Pending/sending artifacts cannot be removed from the gallery.

## Installation and diagnostics

`scripts/picture.sh install` installs the runtime units and enables the worker.
Provision/update also install the worker, which stays inactive when the picture
runtime is absent. To install changed units on an existing media box:

```sh
sudo cp config/homebrain-{media,picture,video}.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homebrain-media.service
sudo systemctl restart homebrain-manager.service
```

Check job/delivery status in **Media delivery**, or use `media_job_status` when
the user asks. Do not add agent polling or cron reminders. Logs:

```sh
journalctl -u homebrain-media -u homebrain-picture -u homebrain-video
```

Tests: `python -m pytest scripts/tests/test_media_jobs.py scripts/tests/test_picture.py scripts/tests/test_video.py scripts/tests/test_activation.py`.
