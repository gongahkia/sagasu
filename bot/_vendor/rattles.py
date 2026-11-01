"""Python subset port of vyfor/rattles: deterministic frames and two presets.

Upstream: https://github.com/vyfor/rattles
Revision: 6eabf671ee86ddd773f458b9c25ef9d3f00ae53b
Adapted from src/timed.rs and src/presets/{ascii,braille}/mod.rs.
Copyright (c) 2026 vyfor. MIT license: licenses/rattles-MIT.txt.
Preset frames also originate in sindresorhus/cli-spinners, MIT licensed;
see licenses/cli-spinners-MIT.txt. Terminal rendering belongs to Sagasu.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rattle:
    frames: tuple[str, ...]
    interval_ms: int

    def frame_at(self, elapsed_ms: float) -> str:
        return self.frames[
            int(max(0, elapsed_ms) // self.interval_ms) % len(self.frames)
        ]


DOTS = Rattle(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"), 80)
LINE = Rattle(("/", "-", "\\", "|", "\\", "-"), 80)
PRESETS = {"dots": DOTS, "line": LINE}
