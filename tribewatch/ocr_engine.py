"""OCR engine — WinRT (winocr) primary, Tesseract fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1)


def _preprocess(img: Image.Image, upscale: int = 2) -> Image.Image:
    """Preprocess a tribe log screenshot for optimal OCR accuracy.

    The tribe log has colored text (green, red, white) on a dark background.
    A standard luminance grayscale would dim red text, so we use max(R,G,B)
    per pixel to ensure all text colors become bright, then upscale.
    """
    import numpy as np

    arr = np.array(img)

    # Max of RGB channels — preserves brightness of all text colors equally
    gray = arr.max(axis=2)

    # Contrast stretch: remap 80–180 range to 0–255, clamp edges
    low, high = 80, 180
    gray = np.clip((gray.astype(np.float32) - low) / (high - low) * 255, 0, 255).astype(np.uint8)

    img = Image.fromarray(gray, mode="L")

    if upscale > 1:
        new_size = (img.width * upscale, img.height * upscale)
        img = img.resize(new_size, Image.LANCZOS)

    return img


def preprocess_tribe_window(
    img: Image.Image, upscale: int = 4, binary_threshold: int = 0,
) -> Image.Image:
    """Preprocess a tribe window screenshot for optimal OCR accuracy.

    The tribe window has smaller text than the tribe log and a semi-transparent
    overlay that lets the game world bleed through.  Tested approaches:

      - Binary thresholding: clipped thin strokes, garbled headers — rejected
        for WinRT, but **works well for Tesseract** which handles hard edges
        better and benefits from the clean black/white separation.
      - Contrast stretch 2x (same as tribe log): `k` consistently misread as
        `h` by WinRT OCR due to thin diagonal strokes at low resolution.
      - **Contrast stretch + 4x upscale + sharpen + unsharp mask**: correctly
        reads `k` and other fine strokes with WinRT.

    Args:
        img: Raw RGB screenshot of the tribe window.
        upscale: Upscale factor (default 4x for small text).
        binary_threshold: When > 0, apply a hard binary threshold after
            sharpen: pixels >= threshold become 255 (white), below become 0
            (black).  This kills game-world bleed-through that confuses
            Tesseract while preserving the bright white text.  A value
            around 180 works well.  Set to 0 (default) for WinRT which
            prefers the grayscale output.

    Steps:
      1. max(R,G,B) to preserve all text colors
      2. Contrast stretch 80–180 → 0–255
      3. Upscale 4x with Lanczos (gives OCR enough pixels for thin strokes)
      4. Sharpen + unsharp mask to enhance character edges
      5. (Tesseract only) Binary threshold to remove background noise
    """
    import numpy as np

    from PIL import ImageFilter

    arr = np.array(img)

    # Max of RGB channels — preserves brightness of all text colors equally
    gray = arr.max(axis=2)

    # Contrast stretch: remap 80–180 range to 0–255, clamp edges
    low, high = 80, 180
    gray = np.clip(
        (gray.astype(np.float32) - low) / (high - low) * 255, 0, 255
    ).astype(np.uint8)

    img_out = Image.fromarray(gray, mode="L")

    if upscale > 1:
        new_size = (img_out.width * upscale, img_out.height * upscale)
        img_out = img_out.resize(new_size, Image.LANCZOS)

    # Sharpen + unsharp mask: makes thin strokes (like the diagonals in 'k')
    # distinct enough for WinRT OCR to read correctly.
    img_out = img_out.filter(ImageFilter.SHARPEN)
    img_out = img_out.filter(
        ImageFilter.UnsharpMask(radius=2, percent=500, threshold=0)
    )

    # Binary threshold: suppress game-world bleed-through for Tesseract.
    # Text is bright white after contrast stretch; background noise is dim gray.
    if binary_threshold > 0:
        arr_out = np.array(img_out)
        arr_out = np.where(arr_out >= binary_threshold, 255, 0).astype(np.uint8)
        img_out = Image.fromarray(arr_out, mode="L")

    return img_out


def detect_log_header(
    img: Image.Image,
    threshold: int = 200,
    min_pixels: int = 80,
    max_mean: float = 150.0,
) -> bool:
    """Check if the 'LOG' header text is visible in the capture image.

    Crops the top-left region and checks two conditions:
      1. The crop has a dark background (mean brightness < max_mean) — the
         tribe log overlay is dark teal/gray, while the game world is bright.
      2. There are enough bright pixels (>= min_pixels) for the "LOG" text.

    Both conditions must be true to avoid false positives from bright game
    scenes where scattered bright pixels easily exceed min_pixels alone.
    """
    import numpy as np

    w, h = img.size
    # Crop top ~10% height, left ~25% width — where "LOG" appears
    crop = img.crop((0, 0, max(int(w * 0.25), 1), max(int(h * 0.10), 1)))

    arr = np.array(crop)
    if arr.ndim == 3:
        gray = arr.max(axis=2)  # same max-RGB as _preprocess
    else:
        gray = arr

    # The tribe log overlay has a dark background (mean ~100-120) with bright
    # "LOG" text.  Game world scenes are much brighter (mean ~175+).
    if float(gray.mean()) > max_mean:
        return False

    return int((gray >= threshold).sum()) >= min_pixels


def _ocr_winrt_sync(img: Image.Image) -> str:
    """Run WinRT OCR synchronously (called inside executor thread)."""
    from winocr import recognize_pil_sync

    result = recognize_pil_sync(img)
    return result["text"]


def _ocr_tesseract_sync(img: Image.Image, tesseract_path: str | None = None) -> str:
    """Run Tesseract OCR synchronously."""
    import pytesseract

    if tesseract_path:
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
    # Tesseract benefits from grayscale + invert for light-on-dark text
    processed = img.convert("L")
    return pytesseract.image_to_string(processed, config="--psm 6 --oem 3")


# EasyOCR reader singleton — expensive to initialize (loads neural network),
# so we create it once on first use and reuse it.
_easyocr_reader = None
_easyocr_lock = __import__("threading").Lock()


def _get_easyocr_reader():
    """Lazily initialize and return the EasyOCR reader singleton."""
    global _easyocr_reader
    if _easyocr_reader is None:
        with _easyocr_lock:
            if _easyocr_reader is None:
                import warnings

                import easyocr

                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message=".*pin_memory.*")
                    _easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
                log.info("EasyOCR reader initialized (CPU mode)")
    return _easyocr_reader


def _ocr_easyocr_sync(img: Image.Image) -> str:
    """Run EasyOCR synchronously, merging detections into lines.

    EasyOCR returns each text region as a separate (bbox, text, confidence)
    tuple.  We group detections that share similar y-coordinates into lines,
    sort left-to-right within each line, and join with spaces.
    """
    import warnings

    import numpy as np

    reader = _get_easyocr_reader()
    arr = np.array(img.convert("L"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*pin_memory.*")
        results = reader.readtext(arr)

    if not results:
        return ""

    # Group by row: detections whose vertical midpoints are within a
    # threshold are considered the same line.
    lines: list[list[tuple[float, str]]] = []  # list of [(x, text), ...]
    for bbox, text, _conf in results:
        # bbox is [[x1,y1],[x2,y2],[x3,y3],[x4,y4]] — take vertical midpoint
        y_mid = (bbox[0][1] + bbox[2][1]) / 2
        x_left = bbox[0][0]
        row_height = abs(bbox[2][1] - bbox[0][1])
        merge_threshold = max(row_height * 0.5, 10)

        # Find an existing line with a close y-midpoint
        merged = False
        for line in lines:
            # Compare against average y of the line's first detection
            existing_y = line[0][2] if len(line[0]) > 2 else y_mid
            if abs(y_mid - existing_y) < merge_threshold:
                line.append((x_left, text, y_mid))
                merged = True
                break

        if not merged:
            lines.append([(x_left, text, y_mid)])

    # Sort lines top-to-bottom, items left-to-right within each line
    lines.sort(key=lambda line: min(item[2] for item in line))
    output_lines = []
    for line in lines:
        line.sort(key=lambda item: item[0])
        output_lines.append(" ".join(item[1] for item in line))

    return "\n".join(output_lines)


# RapidOCR singleton — uses PaddleOCR models via ONNX runtime (no paddlepaddle needed).
_rapidocr_engine = None
_rapidocr_lock = __import__("threading").Lock()


def _get_rapidocr_engine():
    """Lazily initialize and return the RapidOCR engine singleton."""
    global _rapidocr_engine
    if _rapidocr_engine is None:
        with _rapidocr_lock:
            if _rapidocr_engine is None:
                from rapidocr_onnxruntime import RapidOCR

                _rapidocr_engine = RapidOCR()
                log.info("RapidOCR (PaddleOCR ONNX) engine initialized")
    return _rapidocr_engine


def _ocr_paddleocr_sync(img: Image.Image) -> str:
    """Run RapidOCR (PaddleOCR models via ONNX) synchronously, returning line-merged text."""
    import numpy as np

    engine = _get_rapidocr_engine()
    arr = np.array(img.convert("L"))

    result = engine(arr)

    # result is (detections, elapsed) — detections is list of [bbox, text, conf] or None
    detections = result[0] if result else None
    if not detections:
        return ""

    # Group by row using polygon y-midpoints (same logic as easyocr)
    rows: list[list[tuple[float, str, float]]] = []
    for bbox, text, _conf in detections:
        # bbox is [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
        y_mid = (float(bbox[0][1]) + float(bbox[2][1])) / 2
        x_left = float(bbox[0][0])
        row_height = abs(float(bbox[2][1]) - float(bbox[0][1]))
        merge_threshold = max(row_height * 0.5, 10)

        merged = False
        for row in rows:
            existing_y = row[0][2]
            if abs(y_mid - existing_y) < merge_threshold:
                row.append((x_left, text, y_mid))
                merged = True
                break
        if not merged:
            rows.append([(x_left, text, y_mid)])

    rows.sort(key=lambda r: min(item[2] for item in r))
    lines: list[str] = []
    for row in rows:
        row.sort(key=lambda item: item[0])
        lines.append(" ".join(item[1] for item in row))

    return "\n".join(lines)


_MIN_OCR_LENGTH = 30
_MAX_RETRIES = 2


async def recognize(
    img: Image.Image,
    engine: str = "winrt",
    upscale: int = 2,
    tesseract_path: str | None = None,
    retries: int = _MAX_RETRIES,
    preprocess: bool = True,
) -> str:
    """Run OCR on a PIL image, returning raw text.

    If the result is suspiciously short (< 30 chars), retries up to `retries`
    times since WinRT OCR occasionally returns garbage on valid images.
    Set retries=0 for regions where empty text is expected (e.g. parasaur).
    Set preprocess=False for game-world captures where the contrast stretch
    would amplify background noise (e.g. parasaur notification area).

    Uses run_in_executor to avoid blocking the event loop — winocr's
    recognize_pil_sync internally calls asyncio.run() which crashes
    inside a running loop.
    """
    processed = _preprocess(img, upscale) if preprocess else img

    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()

        if engine == "winrt":
            text = await loop.run_in_executor(_executor, _ocr_winrt_sync, processed)
        elif engine == "tesseract":
            text = await loop.run_in_executor(
                _executor, _ocr_tesseract_sync, processed, tesseract_path
            )
        elif engine == "easyocr":
            text = await loop.run_in_executor(_executor, _ocr_easyocr_sync, processed)
        elif engine == "paddleocr":
            text = await loop.run_in_executor(_executor, _ocr_paddleocr_sync, processed)
        else:
            raise ValueError(f"Unknown OCR engine: {engine!r}")

        elapsed = (time.perf_counter() - t0) * 1000

        if len(text.strip()) >= _MIN_OCR_LENGTH or attempt == retries:
            if attempt > 0 and len(text.strip()) >= _MIN_OCR_LENGTH:
                log.info("OCR retry #%d succeeded (%d chars)", attempt, len(text))
            log.debug("OCR (%s) completed in %.1f ms, %d chars", engine, elapsed, len(text))
            return text

        log.debug(
            "OCR returned only %d chars (attempt %d/%d), retrying...",
            len(text.strip()), attempt + 1, retries + 1,
        )

    return text
