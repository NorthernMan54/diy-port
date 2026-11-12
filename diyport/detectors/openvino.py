# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""Intel OpenVINO backend: Intel iGPU / Arc GPU, Core Ultra NPU, or its CPU plugin.

Loads the cpu backend's models (ONNX directly, or an OpenVINO IR .xml + .bin); the
layout and input size are read from the graph, and a dynamic input is pinned to
640x640. Preprocessing (uint8 NHWC -> float NCHW /255) is folded into the graph.
AIPORT_OV_DEVICE picks GPU, NPU or CPU (default auto, GPU -> NPU -> CPU), with a
CPU fallback when compilation fails; compiled blobs cache in AIPORT_OV_CACHE_DIR.
"""
import glob
import logging
import os

import numpy as np

from . import postprocess
from .base import Detector

log = logging.getLogger("aiport-detector")

_INTEL_VENDOR = "0x8086"
DEFAULT_CACHE_DIR = os.path.join(postprocess.MODELS_DIR, "openvino_cache")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _intel_accelerators(root="/"):
    """Intel GPU/NPU device nodes this process can see, e.g. ["GPU", "NPU"] -- cheap
    sysfs check, nothing loaded. Needs both the sysfs entry (vendor 0x8086) and the
    /dev node, so a container without /dev/dri passed through reports none."""
    found = []
    for sys_glob, dev_dir, kind in (("sys/class/drm/renderD*", "dev/dri", "GPU"),
                                    ("sys/class/accel/accel*", "dev/accel", "NPU")):
        dev_dir = os.path.join(root, dev_dir)
        for node in sorted(glob.glob(os.path.join(root, sys_glob))):
            name = os.path.basename(node)
            if (_read(os.path.join(node, "device", "vendor")) == _INTEL_VENDOR
                    and os.path.exists(os.path.join(dev_dir, name))):
                found.append(kind)
                break
    return found


def _ov_device(available, want=None):
    """OpenVINO device string for AIPORT_OV_DEVICE (or `want`) given Core's
    available_devices: "auto" takes the first GPU (GPU / GPU.0 ...), then NPU, then CPU;
    any other value is used verbatim, upper-cased (OpenVINO device names are)."""
    if want is None:
        want = os.environ.get("AIPORT_OV_DEVICE", "auto")
    want = want.strip()
    if want and want.lower() != "auto":
        return want.upper()
    for kind in ("GPU", "NPU"):
        for dev in available:
            if dev.split(".")[0] == kind:
                return dev
    return "CPU"


def _prepare_model(ov, model):
    """Pin a dynamic input to 640x640 and fold the uint8 NHWC -> float NCHW /255
    conversion into the graph; outputs come back float32 whatever the model's precision.
    Returns (model, input_size)."""
    if len(model.inputs) != 1:
        raise RuntimeError(f"expected a single-input detection model, got {len(model.inputs)} inputs")
    pshape = model.input(0).get_partial_shape()
    if pshape.rank.is_dynamic or pshape.rank.get_length() != 4:
        raise RuntimeError(f"expected an NCHW image input, got {pshape}")
    side = pshape[2].get_length() if pshape[2].is_static else 640
    if pshape.is_dynamic:
        model.reshape([1, 3, side, side])
    ppp = ov.preprocess.PrePostProcessor(model)
    ppp.input(0).tensor().set_element_type(ov.Type.u8).set_layout(ov.Layout("NHWC"))
    ppp.input(0).model().set_layout(ov.Layout("NCHW"))
    # f32 for the divide; PrePostProcessor converts to the model's own type (fp16 for the
    # Ultralytics release ONNX) after it.
    ppp.input(0).preprocess().convert_element_type(ov.Type.f32).scale(255.0)
    for i in range(len(model.outputs)):
        ppp.output(i).tensor().set_element_type(ov.Type.f32)
    return ppp.build(), side


class OpenVinoDetector(Detector):
    NAME = "openvino"
    DEFAULT_MODEL = os.path.join(postprocess.MODELS_DIR, "yolov5n.onnx")

    @classmethod
    def available(cls, model_path=None):
        try:
            import openvino  # noqa: F401
        except ImportError:
            return False, "openvino not installed"
        # Auto-pick only with an Intel accelerator; on a plain CPU the cpu backend is the
        # default (detector = openvino still runs on OpenVINO's CPU plugin).
        if not _intel_accelerators():
            return False, "no Intel GPU (/dev/dri) or NPU (/dev/accel) found"
        model = model_path or cls.DEFAULT_MODEL
        if not os.path.exists(model):
            return False, f"model missing: {model}"
        return True, ""

    def __init__(self, model_path=None):
        super().__init__(model_path)
        try:
            import openvino as ov
        except ImportError as exc:
            raise RuntimeError(
                "openvino is not installed -- pip install openvino, or see diyport/README.md "
                "for the Intel GPU runtime setup") from exc
        self._anchors = postprocess.load_anchors()
        core = ov.Core()
        cache = os.environ.get("AIPORT_OV_CACHE_DIR", DEFAULT_CACHE_DIR).strip()
        if cache:
            try:
                os.makedirs(cache, exist_ok=True)
                core.set_property({"CACHE_DIR": cache})
            except OSError as exc:
                log.warning("OpenVINO model cache %s unusable (%s); compiling uncached", cache, exc)
        model, self.input_size = _prepare_model(ov, core.read_model(self.model_path))
        device = _ov_device(core.available_devices)
        # One request at a time behind base.Detector's lock: optimize for latency.
        config = {"PERFORMANCE_HINT": "LATENCY"}
        try:
            compiled = core.compile_model(model, device, config)
        except Exception:
            if device == "CPU":
                raise
            # A missing GPU runtime (intel-opencl-icd) or NPU driver, or an op the device
            # can't compile; the CPU plugin always works.
            log.exception("OpenVINO could not compile %s for %s; falling back to CPU",
                          self.model_path, device)
            device = "CPU"
            compiled = core.compile_model(model, device, config)
        self.device = device
        self._outputs = list(compiled.outputs)
        self._request = compiled.create_infer_request()
        self._mode = postprocess.layout([list(o.get_shape()) for o in self._outputs],
                                        len(self._labels))
        try:
            name = core.get_property(device.split(":")[0].split(",")[0], "FULL_DEVICE_NAME")
        except Exception:
            name = device
        log.info("OpenVINO detector initialized (%s, mode=%s, device=%s [%s], input=%d)",
                 self.model_path, self._mode, device, name, self.input_size)

    def _infer(self, frame_rgb):
        result = self._request.infer({0: np.ascontiguousarray(frame_rgb[np.newaxis, ...])})
        outputs = [np.asarray(result[o], dtype=np.float32) for o in self._outputs]
        return postprocess.decode(self._mode, outputs, self._anchors, self.input_size)
