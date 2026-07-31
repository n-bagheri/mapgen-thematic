"""Step 0 -- Output specification.

No AI involved: the medium, page size, and physical constants are a human
decision. This module materializes them as config/output_spec.json, which every
later step reads. Values here are the single source of truth for what is
physically feelable (minimum sizes, gaps, texture count) and for the output
scale.

Defaults follow common tactile-graphics guidance (BANA-style); confirm against
the guidelines that apply to your production setup before running real jobs.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

MEDIA = ("swell_paper", "embosser", "print_3d")
ORIENTATIONS = ("portrait", "landscape")
BRAILLE_STANDARDS = ("unified-english-grade1",)
MAX_AREA_TEXTURES = 5

DEFAULT_CONFIG_PATH = Path("config") / "output_spec.json"


@dataclass
class PhysicalConstants:
    """Minimum feelable dimensions, in millimetres at output scale."""

    braille_cell_width_mm: float = 6.2    # one cell incl. inter-cell spacing
    braille_cell_height_mm: float = 10.0  # incl. inter-line spacing
    min_texture_area_side_mm: float = 13.0  # smallest square that carries an identifiable texture
    min_element_gap_mm: float = 3.0       # closer raised elements merge under the fingertip
    min_line_width_mm: float = 1.0
    min_line_length_mm: float = 13.0
    # Fixed hard ceiling, not a target: maps with fewer classes use fewer
    # patterns. Later steps must never invent classes to fill unused capacity.
    max_area_textures: int = MAX_AREA_TEXTURES


@dataclass
class OutputSpec:
    medium: str = "swell_paper"
    page_width_mm: float = 210.0   # A4 portrait
    page_height_mm: float = 297.0
    orientation: str = "portrait"
    margin_mm: float = 15.0
    braille_standard: str = "unified-english-grade1"  # open question 4 in PIPELINE.md
    constants: PhysicalConstants = field(default_factory=PhysicalConstants)

    def validate(self) -> None:
        if self.medium not in MEDIA:
            raise ValueError(f"medium must be one of {MEDIA}, got {self.medium!r}")
        if self.orientation not in ORIENTATIONS:
            raise ValueError(f"orientation must be one of {ORIENTATIONS}, got {self.orientation!r}")
        if self.braille_standard not in BRAILLE_STANDARDS:
            raise ValueError(
                f"braille_standard must be one of {BRAILLE_STANDARDS}, "
                f"got {self.braille_standard!r}"
            )
        if self.page_width_mm <= 0 or self.page_height_mm <= 0:
            raise ValueError("page width and height must be positive")
        if self.margin_mm < 0:
            raise ValueError("margin_mm must be non-negative")
        if self.page_width_mm <= 2 * self.margin_mm or self.page_height_mm <= 2 * self.margin_mm:
            raise ValueError("margins leave no drawable area")
        c = self.constants
        for name in (
            "braille_cell_width_mm", "braille_cell_height_mm", "min_texture_area_side_mm",
            "min_element_gap_mm", "min_line_width_mm", "min_line_length_mm",
        ):
            if getattr(c, name) <= 0:
                raise ValueError(f"constants.{name} must be positive")
        if type(c.max_area_textures) is not int:
            raise ValueError("constants.max_area_textures must be a whole number")
        if c.max_area_textures != MAX_AREA_TEXTURES:
            raise ValueError(
                f"constants.max_area_textures must be exactly {MAX_AREA_TEXTURES}"
            )

    @property
    def drawable_width_mm(self) -> float:
        return self.page_width_mm - 2 * self.margin_mm

    @property
    def drawable_height_mm(self) -> float:
        return self.page_height_mm - 2 * self.margin_mm

    def texture_slots(self, water_present: bool) -> int:
        """Return thematic capacity; unused slots never create extra classes.

        Water always claims one of the five area-pattern slots for its wavy
        pattern. The returned value is a maximum, not a number of groups that
        later stages must manufacture.
        """
        return self.constants.max_area_textures - (1 if water_present else 0)

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> Path:
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "OutputSpec":
        data = json.loads(path.read_text(encoding="utf-8"))
        constants = PhysicalConstants(**data.pop("constants", {}))
        spec = cls(constants=constants, **data)
        spec.validate()
        return spec

    @classmethod
    def load_or_create(cls, path: Path = DEFAULT_CONFIG_PATH) -> "OutputSpec":
        if path.exists():
            return cls.load(path)
        spec = cls()
        spec.save(path)
        return spec
