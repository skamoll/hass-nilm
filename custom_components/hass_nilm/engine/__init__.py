"""Pure-Python Non-Intrusive Load Monitoring (NILM) engine with zero HA dependencies."""

from .detectors import (
    BaseApplianceDetector,
    CyclingDetector,
    MultiphaseDetector,
    StepDetector,
    build_detector,
    match_program_fingerprint,
)
from .engine import NilmLineEngine
from .filters import MedianFilter
from .models import (
    UNKNOWN_PROGRAM,
    ApplianceProfile,
    ApplianceStatus,
    Archetype,
    CompletedCycle,
    LineFrame,
    LineSample,
    PhaseSpec,
    ProgramFingerprint,
    SourceLineConfig,
    SubmeterReading,
)
from .residual import ResidualResult, ResidualTracker
from .scanner import CycleCluster, ScanReport, StepCluster, scan_power_series

__all__ = [
    "UNKNOWN_PROGRAM",
    "ApplianceProfile",
    "ApplianceStatus",
    "Archetype",
    "BaseApplianceDetector",
    "CompletedCycle",
    "CycleCluster",
    "CyclingDetector",
    "LineFrame",
    "LineSample",
    "MedianFilter",
    "MultiphaseDetector",
    "NilmLineEngine",
    "PhaseSpec",
    "ProgramFingerprint",
    "ResidualResult",
    "ResidualTracker",
    "ScanReport",
    "SourceLineConfig",
    "StepCluster",
    "StepDetector",
    "SubmeterReading",
    "build_detector",
    "match_program_fingerprint",
    "scan_power_series",
]
