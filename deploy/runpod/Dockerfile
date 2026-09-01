# Plumbus GPU worker.
#
# A -devel base on purpose: Hunyuan's painter needs nvcc at build time to compile
# custom_rasterizer, and that extension is the entire reason texturing works here
# and cannot work on Apple Silicon.
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLUMBUS_ENGINE_ROOT=/opt/engines \
    PLUMBUS_VOLUME=/runpod-volume

# Build for the cards you will actually rent. No GPU is present while building,
# so this list has to be explicit or the extensions compile for nothing.
#   8.6 = A10/A40/3090   8.9 = L40S/4090   9.0 = H100
ENV TORCH_CUDA_ARCH_LIST="8.6;8.9;9.0+PTX" \
    FORCE_CUDA=1 \
    MAX_JOBS=4

RUN apt-get update && apt-get install -y --no-install-recommends \
        git git-lfs build-essential ninja-build cmake \
        libgl1 libglib2.0-0 libgomp1 libegl1 libxrender1 \
        curl ca-certificates \
    && apt-get clean

WORKDIR /opt/engines

# ---------------------------------------------------------------- TRELLIS
RUN git clone --depth 1 https://github.com/microsoft/TRELLIS.git trellis

# ---------------------------------------------------------------- Hunyuan3D
RUN git clone --depth 1 https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git hunyuan

RUN pip install --upgrade pip && \
    pip install \
        runpod \
        trimesh pymeshlab xatlas pygltflib \
        transformers accelerate diffusers safetensors huggingface_hub \
        einops omegaconf opencv-python-headless scikit-image \
        pytorch-lightning kornia timm imageio fast-simplification \
        onnxruntime-gpu \
        realesrgan basicsr facexlib gfpgan

# basicsr still imports torchvision.transforms.functional_tensor, removed in
# torchvision 0.20. Shim it rather than pinning torchvision back.
RUN python - <<'PY'
import pathlib, torchvision
p = pathlib.Path(torchvision.__file__).parent / "transforms" / "functional_tensor.py"
if not p.exists():
    p.write_text("from torchvision.transforms.functional import rgb_to_grayscale  # noqa: F401\n")
    print("shimmed", p)
PY

RUN pip install -r /opt/engines/hunyuan/requirements.txt || \
    echo "requirements.txt partially applied; explicit installs above cover the worker path"

# ---- the two CUDA extensions the PBR painter depends on
RUN cd /opt/engines/hunyuan/hy3dpaint/custom_rasterizer && pip install -e .
RUN cd /opt/engines/hunyuan/hy3dpaint/DifferentiableRenderer && bash compile_mesh_painter.sh

# ---- TRELLIS deps last, it is the fussier of the two
RUN cd /opt/engines/trellis && \
    (pip install -r requirements.txt || echo "see setup.sh for the full TRELLIS env")

COPY handler.py /opt/engines/handler.py
COPY hunyuan_runner.py /opt/engines/hunyuan_runner.py

# Fail the build rather than discover at 3am that texturing is silently off.
RUN python -c "import custom_rasterizer; print('custom_rasterizer OK')" && \
    python -c "import realesrgan; print('realesrgan OK')" && \
    python -c "import runpod, trimesh; print('worker deps OK')"

CMD ["python", "-u", "/opt/engines/handler.py"]
