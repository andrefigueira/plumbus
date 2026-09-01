# Plumbus

Local-first image-to-3D. Drop a reference image, get a textured GLB, without a
subscription.

Everyone has one in their home.

## Why

Meshy, Tripo and Rodin are polished wrappers around a published pipeline. The
weights for the hard part are open. Plumbus runs that pipeline on your own
machine and keeps a per-call hosted fallback for the few assets where a local
run is not good enough.

| | Local | Hosted |
|---|---|---|
| Cost | electricity | ~$0.02 to $0.16 per generation |
| Time | 2 to 9 minutes | 20 to 60 seconds |
| Ceiling | very close, see below | slightly higher on image alignment |
| Offline | yes | no |

The honest gap: Meshy 7 leads on single-view geometry alignment on its own
published benchmark, and Hunyuan3D 3.0 has no open release yet. For static
props exported as GLB you will not reliably pick the hosted output blind.

## Install

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
cp .env.example .env
.venv/bin/python -m plumbus doctor
```

`doctor` tells you which engines are usable and what each one is missing.

### Engines

Plumbus ships three adapters. You need at least one.

**TRELLIS 2** (recommended). MIT weights, 4B DiT, up to 1536 cubed with 4K textures.

```sh
plumbus install trellis
```

Needs a HuggingFace login and accepted terms on two gated repos (DINOv3 and
RMBG-2.0), then roughly 15GB of weights. Only you can click those through.

> RMBG-2.0 is CC BY-NC 4.0. It is only the background remover. Swap it for
> BiRefNet or rembg if the output is going anywhere commercial.

**Hunyuan3D 2.1.** Open weights with production PBR texture synthesis, up to 8K.
Stronger texturing, heavier run.

```sh
plumbus install hunyuan
```

**fal.ai.** The hosted escape hatch. Put a `FAL_KEY` in `.env` and it appears in
the engine list. Billed per call, no monthly commitment.

### Reference images (GPT Image 2)

Put an `OPENAI_API_KEY` in `.env` and the Reference panel wakes up. Describe a
subject, get a reference drawn for it, and it drops straight into the source
slots.

Two things make it worth having rather than pasting from ChatGPT:

**Transparent backgrounds.** `gpt-image-2` supports `background: transparent`,
so the reference arrives pre-cut. That removes the background-removal stage
downstream, which is also the stage carrying TRELLIS's non-commercial RMBG-2.0
dependency.

**Multi-view that holds identity.** Pick two to four views. The first is drawn
from your prompt; the rest go through `/v1/images/edits` with that first image
attached, so the model is matching a subject it has already drawn instead of
re-reading a description. Feed the set to a multi-view 3D backend and the back
of the mesh becomes observed rather than guessed.

Your text is wrapped in a preset that strips what becomes geometry error: depth
of field, cast shadows, rim light, cropping, props, ground planes. Open *See the
prompt actually sent* to read it, or tick **Send my prompt verbatim** to bypass
it entirely.

## Use

```sh
plumbus serve          # http://127.0.0.1:8420
```

Drop an image on the left, pick an engine, hit Generate. Jobs run one at a time
because two concurrent MPS runs is how you get a swap storm instead of two
meshes. The viewer turntables the result, captures its own thumbnail, and
**Export to repo** drops the asset into `../models/<name>/` with a README in the
parent repo's house style.

The web UI has three real quality profiles rather than cosmetic labels:

- **Preview** uses the low-resolution geometry path for silhouette checks.
- **Detailed** is the default: 1024 geometry, 2K textures, and a substantially
  higher face budget.
- **Hero** selects cascade/max-resolution geometry and keeps a dense source GLB.

For people or creatures, choose **Character** before generating references. It
builds a consistent front/profile/back set in a neutral A-pose, with hands,
face, joints, anatomy and fine material detail explicitly constrained. The
animation-foundation inspector reports skeletons, skin bindings, blend shapes
and topology instead of assuming that a visually detailed mesh is animatable.

Important: current image-to-3D engines return dense static meshes. Character +
Hero gives a much better source for retopology and rigging, and Plumbus avoids
static hole-filling that would damage deformation topology, but it does not
pretend to produce a MetaHuman facial rig automatically. A production character
still needs semantic quad retopology, skin weights and facial blend shapes in a
DCC or dedicated character pipeline.

Headless:

```sh
plumbus gen ref.png --set pipeline_type=1024 --set texture_size=2048
plumbus doctor
```

### Agents (MCP)

Plumbus speaks MCP, so a Claude session can make assets directly:

```sh
plumbus serve          # the MCP server drives this over HTTP
claude mcp add -s user plumbus /path/to/plumbus/.venv/bin/python -- -m plumbus mcp
```

The repo also ships a project-scope `.mcp.json`, so opening it in Claude Code
offers the server automatically. Tools: `text_to_asset` goes from a description
to queued job(s) in one call (a character gets its head job queued too),
`draw_reference` / `create_asset` split the same flow, `wait_for_job`,
`job_status`, `list_assets`, `cancel_job`, `export_asset` (the game-ready
finisher) and `engine_status`. Reference drawing spends real money, and the
tool descriptions say so; builds run through the same single-slot worker as
the UI, so the browser shows whatever an agent is doing.

## Getting good meshes

Text-to-3D is the weakest path in every engine, open or hosted. Image first:

0. Use the Reference panel, or bring your own image.
1. A clean three-quarter view, single subject, plain background, even lighting.
2. Fix the *image* before you fix the mesh. The mesh only reproduces what is in
   the source, so a hidden prop stays hidden and a cropped foot stays cropped.
3. `pipeline_type` is the single biggest quality lever on TRELLIS. Start at 512
   to check composition, re-run at 1024 or 1024_cascade once it's right.
4. Keep the seed to reproduce a result you liked.

For web delivery leave **Build a web copy** on. It runs the GLB through
gltf-transform with Draco and WebP, which typically cuts 60 to 80% of the bytes.

## Adding an engine

Subclass `Backend` in `plumbus/backends/`, implement `available()`,
`param_schema()` and `generate()`, and add it to `REGISTRY`. The web UI renders
its entire settings form from `param_schema()`, so there is no frontend work.

## Layout

```
plumbus/
├── plumbus/
│   ├── server.py       FastAPI, static, export
│   ├── worker.py       single-slot job runner
│   ├── db.py           SQLite job store
│   ├── postprocess.py  mesh stats, web optimisation
│   ├── imagegen/       reference images (GPT Image 2) + prompt presets
│   └── backends/       trellis, hunyuan, fal
└── web/                UI, three.js viewer
```

## Licence

Plumbus is MIT. The engines it drives carry their own terms, and TRELLIS 2's
default background remover is non-commercial. Check them before you ship.
