# framework.detection — pHash and Gemini symbol detection
from framework.detection.phash import PhashDetector, PhashResult
from framework.detection.gemini_detect import GeminiDetector, GeminiDetectResult
from framework.detection.config import DetectionConfig, DetectionResult

__all__ = [
    "PhashDetector",
    "PhashResult",
    "GeminiDetector",
    "GeminiDetectResult",
    "DetectionConfig",
    "DetectionResult",
]
