"""Load and reuse Illustrator-exported SVG patterns without altering them.

The module treats the ``<pattern>`` element in each source SVG as the pattern
asset.  The rectangle filled with ``url(#...)`` is used only to discover the
pattern and its optional opacity attributes; it is not part of the repeating
tile.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Iterator, Mapping
import xml.etree.ElementTree as ET


SVG_NAMESPACE = "http://www.w3.org/2000/svg"
POINTS_PER_INCH = 72.0
MM_PER_INCH = 25.4

_FILL_REFERENCE_RE = re.compile(r"^\s*url\(\s*#([^\s)]+)\s*\)\s*$")
_INVALID_ID_CHARACTER_RE = re.compile(r"[^A-Za-z0-9_.-]+")
# The directory also holds dedicated map-furniture SVGs such as ``N.svg``.
# Only numbered Illustrator texture exports are repeating pattern assets.
_PATTERN_FILENAME_RE = re.compile(r"^\d{2}_.+\.svg$", re.IGNORECASE)


def mm_to_pt(mm: float) -> float:
    """Convert millimetres to SVG points (72 pt = 25.4 mm)."""

    return float(mm) * POINTS_PER_INCH / MM_PER_INCH


def pt_to_mm(pt: float) -> float:
    """Convert SVG points to millimetres (72 pt = 25.4 mm)."""

    return float(pt) * MM_PER_INCH / POINTS_PER_INCH


@dataclass(frozen=True)
class PatternAsset:
    """An SVG pattern and the source metadata needed to reuse it exactly.

    ``pattern_xml`` is a deep copy of the complete source ``<pattern>``
    element.  Its attributes retain their original string values, including
    the complete ``patternTransform``.  ``width`` and ``height`` are exposed as
    point values for calculations while the unmodified values remain in
    ``pattern_xml.attrib``.
    """

    name: str
    source_file: Path
    original_pattern_id: str
    width: float
    height: float
    view_box: str | None
    pattern_units: str | None
    pattern_transform: str | None
    pattern_xml: ET.Element
    fill_opacity: str | None = None
    opacity: str | None = None

    @property
    def original_id(self) -> str:
        """Alias for ``original_pattern_id``."""

        return self.original_pattern_id

    @property
    def viewBox(self) -> str | None:  # noqa: N802 - matches the SVG attribute
        """The exact source ``viewBox`` string."""

        return self.view_box

    @property
    def patternUnits(self) -> str | None:  # noqa: N802 - SVG terminology
        """The exact source ``patternUnits`` value."""

        return self.pattern_units

    @property
    def patternTransform(self) -> str | None:  # noqa: N802 - SVG terminology
        """The exact, complete source ``patternTransform`` value."""

        return self.pattern_transform


class PatternLibrary:
    """A collection of reusable patterns loaded from a directory of SVGs."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser().resolve()
        if not self.directory.is_dir():
            raise NotADirectoryError(f"Pattern directory does not exist: {self.directory}")

        assets: dict[str, PatternAsset] = {}
        for source_file in sorted(self.directory.glob("*.svg")):
            if not _PATTERN_FILENAME_RE.match(source_file.name):
                continue
            asset = self._load_asset(source_file)
            if asset.name in assets:
                raise ValueError(f"Duplicate pattern name: {asset.name!r}")
            assets[asset.name] = asset

        self._assets = assets

    @property
    def assets(self) -> Mapping[str, PatternAsset]:
        """A read-only mapping of filename stems to pattern assets."""

        return MappingProxyType(self._assets)

    @property
    def names(self) -> tuple[str, ...]:
        """Registered pattern names in deterministic filename order."""

        return tuple(self._assets)

    def __len__(self) -> int:
        return len(self._assets)

    def __iter__(self) -> Iterator[str]:
        return iter(self._assets)

    def __contains__(self, name: object) -> bool:
        return name in self._assets

    def get(self, name: str) -> PatternAsset:
        """Return a pattern by filename stem.

        Raises:
            KeyError: if ``name`` is not registered.
        """

        try:
            return self._assets[name]
        except KeyError as exc:
            available = ", ".join(self._assets) or "<none>"
            raise KeyError(f"Unknown pattern {name!r}; available: {available}") from exc

    def get_pattern(self, name: str) -> PatternAsset:
        """Descriptive alias for :meth:`get`."""

        return self.get(name)

    def copy_to_defs(
        self,
        name: str,
        defs: ET.Element,
        *,
        new_id: str | None = None,
    ) -> str:
        """Deep-copy a pattern into ``defs`` and return ``url(#new_id)``.

        The copied root pattern receives a collision-free ID.  Every other
        attribute and all descendant geometry are retained exactly.  If
        ``new_id`` is supplied and already exists in ``defs``, a numeric suffix
        is added rather than replacing the existing definition.
        """

        if _local_name(defs.tag) != "defs":
            raise ValueError("defs must be an SVG <defs> element")

        asset = self.get(name)
        requested_id = new_id or f"pattern_{name}"
        safe_id = _safe_svg_id(requested_id)
        unique_id = _unique_id(safe_id, defs)

        pattern_copy = deepcopy(asset.pattern_xml)
        pattern_copy.set("id", unique_id)
        defs.append(pattern_copy)
        return f"url(#{unique_id})"

    def copy_pattern_to_defs(
        self,
        name: str,
        defs: ET.Element,
        *,
        new_id: str | None = None,
    ) -> str:
        """Descriptive alias for :meth:`copy_to_defs`."""

        return self.copy_to_defs(name, defs, new_id=new_id)

    @staticmethod
    def _load_asset(source_file: Path) -> PatternAsset:
        try:
            root = ET.parse(source_file).getroot()
        except ET.ParseError as exc:
            raise ValueError(f"Invalid SVG XML in {source_file}") from exc

        patterns = [element for element in root.iter() if _local_name(element.tag) == "pattern"]
        if len(patterns) != 1:
            raise ValueError(
                f"Expected exactly one <pattern> in {source_file}, found {len(patterns)}"
            )

        pattern = patterns[0]
        pattern_id = pattern.get("id")
        if not pattern_id:
            raise ValueError(f"Pattern in {source_file} has no id")

        carrier = _find_carrier(root, pattern_id)
        if carrier is None:
            raise ValueError(
                f"No carrier element referencing url(#{pattern_id}) in {source_file}"
            )

        width = _required_float_attribute(pattern, "width", source_file)
        height = _required_float_attribute(pattern, "height", source_file)

        return PatternAsset(
            name=source_file.stem,
            source_file=source_file.resolve(),
            original_pattern_id=pattern_id,
            width=width,
            height=height,
            view_box=pattern.get("viewBox"),
            pattern_units=pattern.get("patternUnits"),
            pattern_transform=pattern.get("patternTransform"),
            pattern_xml=deepcopy(pattern),
            fill_opacity=_presentation_attribute(carrier, "fill-opacity"),
            opacity=_presentation_attribute(carrier, "opacity"),
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_carrier(root: ET.Element, pattern_id: str) -> ET.Element | None:
    for element in root.iter():
        match = _FILL_REFERENCE_RE.match(element.get("fill", ""))
        if match and match.group(1) == pattern_id:
            return element
    return None


def _presentation_attribute(element: ET.Element, name: str) -> str | None:
    direct_value = element.get(name)
    if direct_value is not None:
        return direct_value

    # Illustrator normally writes presentation attributes directly, but a
    # style declaration is equivalent and should be retained when encountered.
    for declaration in element.get("style", "").split(";"):
        property_name, separator, value = declaration.partition(":")
        if separator and property_name.strip() == name:
            return value.strip()
    return None


def _required_float_attribute(
    element: ET.Element,
    attribute: str,
    source_file: Path,
) -> float:
    raw_value = element.get(attribute)
    if raw_value is None:
        raise ValueError(f"Pattern in {source_file} has no {attribute}")
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"Pattern {attribute} in {source_file} is not a point value: {raw_value!r}"
        ) from exc


def _safe_svg_id(value: str) -> str:
    cleaned = _INVALID_ID_CHARACTER_RE.sub("_", value.strip())
    if not cleaned:
        cleaned = "pattern"
    if not re.match(r"[A-Za-z_]", cleaned):
        cleaned = f"pattern_{cleaned}"
    return cleaned


def _unique_id(base_id: str, defs: ET.Element) -> str:
    existing_ids = {
        element_id
        for element in defs.iter()
        if (element_id := element.get("id")) is not None
    }
    if base_id not in existing_ids:
        return base_id

    suffix = 2
    while f"{base_id}_{suffix}" in existing_ids:
        suffix += 1
    return f"{base_id}_{suffix}"


__all__ = ["PatternAsset", "PatternLibrary", "mm_to_pt", "pt_to_mm"]
