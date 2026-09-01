# Plumbus GPU worker

Your engines, your container, your GPUs. One serverless endpoint serving both
TRELLIS 2 and Hunyuan3D 2.1 on CUDA, which is where the PBR texture pipeline
actually works.

## Why this exists

Hunyuan's painter depends on `custom_rasterizer`, a `CUDAExtension` compiling
`rasterizer_gpu.cu`. There is no CPU or MPS path, so texturing cannot run on
Apple Silicon at all. On a rented NVIDIA card it builds and works. This image
builds it at `docker build` time and fails the build if it did not, so you find
out on your machine rather than after a customer waited nine minutes for a grey
mesh.

## Pick a card

| | VRAM | Fits |
|---|---|---|
| L40S | 48GB | **Recommended.** Both engines, textured, with headroom |
| A100 40GB | 40GB | Works, tighter on Hunyuan shape+texture (29GB) |
| A10 / 4090 | 24GB | TRELLIS only. Too tight for textured Hunyuan |

`TORCH_CUDA_ARCH_LIST` in the Dockerfile is set to `8.6;8.9;9.0+PTX`, covering
A10/A40/3090, L40S/4090 and H100. Trim it to just your card to cut build time.

## Build

No GPU is needed to build, but you do need a Linux x86 builder. Building on an
Apple Silicon Mac produces an arm64 image that will not run on a rented card, so
either build in CI or pass an explicit platform.

```sh
cd deploy/runpod
docker build --platform linux/amd64 -t YOURUSER/plumbus-worker:0.1 .
docker push YOURUSER/plumbus-worker:0.1
```

Expect 20 to 40 minutes on a first build. Most of it is compiling the two CUDA
extensions.

## Deploy

1. RunPod console → **Storage** → create a **Network Volume**, 60GB, in the
   region you will run in. Weights land here so a cold worker downloads once for
   the life of the endpoint rather than once per request. At $0.07/GB/month that
   is about $4/month.
2. **Serverless** → New Endpoint.
   - Container image: the tag you pushed
   - GPU: L40S 48GB
   - Attach the network volume (it mounts at `/runpod-volume`)
   - Active workers: **0**, Max workers: start at 2
   - Idle timeout: 60s to begin with, see the note on cold starts below
   - Container disk: 20GB
3. Add `HF_TOKEN` as an endpoint environment variable if you use gated weights.
4. Copy the **Endpoint ID**.

## Point Plumbus at it

In `.env`:

```sh
RUNPOD_API_KEY=...
RUNPOD_ENDPOINT_ID=...
PLUMBUS_RUNPOD_USD_PER_HOUR=1.75
```

Restart, and **Your GPU workers** appears in the engine list with a real
per-job cost attributed from worker seconds.

## The cold start trap

RunPod bills the **full per-second rate during initialisation**, before any work
happens. Loading 15GB of weights on every cold start is billed time producing
nothing, and it is the single biggest way to lose money here.

Mitigations, in order of value:

1. Network volume so weights are read locally, not refetched
2. Raise idle timeout so consecutive jobs reuse a warm worker
3. One active worker once you have steady traffic, which removes cold starts
   entirely and costs about $1,260/month at L40S Flex rates

Watch your billed seconds against your job seconds. If billed is much higher,
you are paying for boot, not generation.

## Contract

Input:

```json
{
  "input": {
    "engine": "trellis",
    "image": "<base64 png>",
    "params": { "pipeline_type": "1024", "texture_size": "1024", "seed": 42 }
  }
}
```

Output:

```json
{ "glb_b64": "...", "stats": { "vertices": 0, "faces": 0, "pbr": true, "seconds": 0 }, "log": [] }
```

Meshes over 18MB are rejected rather than truncated, since RunPod's JSON
response has a ceiling. If you hit it, lower `target_polycount` or
`texture_size`, or move to S3 pre-signed uploads.

## Not yet verified

The Dockerfile has not been built. It was written on a Mac with no Docker and no
CUDA, so treat the first build as the real test. The parts most likely to need a
nudge:

- `microsoft/TRELLIS` is the TRELLIS**.1** paper repo. The macOS port tracks
  TRELLIS**.2**. Confirm which release you want before you ship, it is a real
  quality difference.
- TRELLIS upstream expects its own `setup.sh` environment. The image installs
  from `requirements.txt` instead, which may not be sufficient.
- `compile_mesh_painter.sh` assumes a standard CUDA toolkit layout.

The build-time import assertions at the end of the Dockerfile are there so these
fail loudly during build rather than silently at runtime.
