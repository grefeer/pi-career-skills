"""Bounded OCR helper used by the WeChat image evidence path.

This is the package-local equivalent of the source project's ``ocr_image.py``.
It deliberately has no filesystem state beyond the requested output directory;
the caller already enforces the public-image and size limits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _ocr_pytesseract(path: Path) -> tuple[str, float | None]:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "", None
    try:
        text = pytesseract.image_to_string(Image.open(path), lang="chi_sim+eng")
    except Exception:  # noqa: BLE001 - optional backend boundary
        return "", None
    return text.strip(), None


def _ocr_paddle(path: Path) -> tuple[str, float | None]:
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        return "", None
    try:
        engine = PaddleOCR(lang="ch", use_doc_orientation_classify=False)
        result: Any = engine.predict(str(path))
        lines: list[str] = []
        scores: list[float] = []
        for item in result or []:
            data = getattr(item, "json", None)
            payload = data() if callable(data) else data
            if not isinstance(payload, dict):
                continue
            res = payload.get("res") if isinstance(payload.get("res"), dict) else payload
            texts = res.get("rec_texts", []) if isinstance(res, dict) else []
            values = res.get("rec_scores", []) if isinstance(res, dict) else []
            lines.extend(str(value) for value in texts if str(value).strip())
            scores.extend(float(value) for value in values if isinstance(value, (int, float)))
        return "\n".join(lines).strip(), (sum(scores) / len(scores) if scores else None)
    except Exception:  # noqa: BLE001 - optional backend boundary
        return "", None


def run(path: Path, engine: str) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "failed", "code": "image_not_found", "full_text": ""}
    requested = engine.lower()
    backends = ["paddleocr", "tesseract"] if requested == "auto" else [requested]
    warnings: list[str] = []
    for backend in backends:
        if backend == "paddleocr":
            text, confidence = _ocr_paddle(path)
        elif backend == "tesseract":
            text, confidence = _ocr_pytesseract(path)
        elif backend == "vision":
            warnings.append("vision OCR requires the model-facing vision channel")
            continue
        else:
            return {"status": "failed", "code": "unsupported_engine", "full_text": ""}
        if text:
            return {
                "status": "ok",
                "full_text": text,
                "confidence": confidence,
                "engine": backend,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "warnings": warnings,
            }
    return {
        "status": "failed",
        "code": "ocr_backend_unavailable",
        "full_text": "",
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract text from one bounded image")
    parser.add_argument("image_path")
    parser.add_argument("--engine", choices=("auto", "paddleocr", "tesseract", "vision"), default="auto")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    result = run(Path(args.image_path), args.engine)
    if args.out and result.get("full_text"):
        output = Path(args.out)
        output.mkdir(parents=True, exist_ok=True)
        (output / f"{result['content_hash']}.txt").write_text(
            str(result["full_text"]), encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
