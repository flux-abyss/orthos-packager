"""Target repository profiles for Orthos chroot creation and dependency resolution."""

from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class TargetRepoProfile:
    name: str
    apt_source_line: str | None = None
    keyring_host_path: Path | None = None
    keyring_chroot_path: Path | None = None
    origin_markers: tuple[str, ...] = ()


_PROFILES = {
    "debian": TargetRepoProfile(
        name="debian",
        apt_source_line=None,
        keyring_host_path=None,
        keyring_chroot_path=None,
        origin_markers=(),
    ),
    "bodhi": TargetRepoProfile(
        name="bodhi",
        apt_source_line=(
            "deb [signed-by=/usr/share/keyrings/bodhi-archive-keyring.gpg]"
            " http://packages.bodhilinux.com/bodhi/ lila b8debbie"
        ),
        keyring_host_path=Path("/usr/share/keyrings/bodhi-archive-keyring.gpg"),
        keyring_chroot_path=Path("/usr/share/keyrings/bodhi-archive-keyring.gpg"),
        origin_markers=("bodhilinux.com",),
    ),
}

def get_target_repo_profile(name: str) -> TargetRepoProfile:
    """Return the TargetRepoProfile for the given name.
    
    Raises ValueError if the profile is not known.
    """
    if name not in _PROFILES:
        raise ValueError(f"Unknown target repo profile: {name}")
    return _PROFILES[name]
