"""Device profiles.

The generic MTP/PTP core copies every file below each DCIM folder of each
storage (the DCF layout used by cameras and phones). A profile adds
model-specific rules: which files belong together (a video and its proxy
and thumbnail are copied, verified and deleted as one group) and how
chapters of one recording are recognised.
"""
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .camera.base import DeviceInfo, RemoteFile


@dataclass
class FileGroup:
    """Files copied and deleted together. `primary` decides the name."""
    primary: RemoteFile
    companions: List[RemoteFile] = field(default_factory=list)

    @property
    def files(self):
        return [self.primary] + self.companions

    @property
    def size(self):
        return sum(f.size for f in self.files)


class GenericProfile:
    id = "generic"
    name = "Generic MTP/PTP"

    def matches(self, info: DeviceInfo) -> bool:
        return True

    def media_folders(self, camera) -> List[str]:
        """All folders whose files are footage: every folder below a DCIM folder."""
        result = []
        for store in camera.list_folders("/"):
            root = "/" + store
            # A listing error propagates so the run retries and, failing that,
            # reports an error: a storage that cannot be read must never make
            # the camera look empty (it would be released as if offloaded).
            top = camera.list_folders(root)
            for name in top:
                if name.upper() == "DCIM":
                    dcim = root + "/" + name
                    result.append(dcim)
                    result.extend(self._walk(camera, dcim))
        return result

    def _walk(self, camera, folder, depth=0):
        if depth > 4:
            return []
        out = []
        for sub in camera.list_folders(folder):
            path = folder + "/" + sub
            out.append(path)
            out.extend(self._walk(camera, path, depth + 1))
        return out

    def stem(self, name: str) -> str:
        return os.path.splitext(name)[0].upper()

    def group(self, files: List[RemoteFile]) -> List[FileGroup]:
        """Group files of one folder by stem; the largest file is primary."""
        by_key = {}
        order = []
        for f in files:
            key = (f.folder, self.stem(f.name))
            if key not in by_key:
                by_key[key] = []
                order.append(key)
            by_key[key].append(f)
        groups = []
        for key in order:
            members = sorted(by_key[key], key=lambda f: (-f.size, f.name))
            groups.append(FileGroup(members[0], members[1:]))
        return groups

    def local_name(self, remote: RemoteFile, primary: RemoteFile, suffix: str) -> str:
        """Name on the stick. `suffix` (e.g. "_1") resolves collisions and is
        applied to every file in the group so companions still match."""
        stem, ext = os.path.splitext(remote.name)
        return f"{stem}{suffix}{ext}"

    def recording_key(self, name: str) -> str:
        return self.stem(name)


# GoPro HERO5 and later: GH010123.MP4 (chapter 01 of recording 0123),
# GX for HEVC on later models, GL010123.LRV proxy, GH010123.THM thumbnail.
_GOPRO_NEW = re.compile(r"^G([HXL])(\d\d)(\d{4})\.(\w+)$", re.I)
# HERO4 style: GOPR0123.MP4 first chapter, GP010123.MP4 later chapters.
_GOPRO_OLD = re.compile(r"^G(OPR|P\d\d)(\d{4})\.(\w+)$", re.I)
# Photos: GOPR0123.JPG, bursts G0010123.JPG, RAW .GPR with the same stem.


class GoProProfile(GenericProfile):
    id = "gopro"
    name = "GoPro"

    VENDOR_ID = "2672"

    def matches(self, info):
        return (info.vendor_id.lower() == self.VENDOR_ID
                or "gopro" in info.manufacturer.lower())

    def stem(self, name):
        m = _GOPRO_NEW.match(name)
        if m:
            kind = m.group(1).upper()
            # The proxy GL is grouped with its video GH/GX.
            return f"G*{m.group(2)}{m.group(3)}".upper() if kind in "HXL" else name.upper()
        return os.path.splitext(name)[0].upper()

    def group(self, files):
        groups = super().group(files)
        # Make sure a video, not its proxy, is the primary file.
        for g in groups:
            members = g.files
            videos = [f for f in members if f.name.upper().endswith((".MP4", ".MOV"))]
            if videos:
                primary = max(videos, key=lambda f: f.size)
                g.primary = primary
                g.companions = [f for f in members if f is not primary]
        return groups

    def recording_key(self, name):
        m = _GOPRO_NEW.match(name)
        if m:
            return m.group(3)
        m = _GOPRO_OLD.match(name)
        if m:
            return m.group(2)
        return self.stem(name)

    def chapter(self, name) -> Optional[int]:
        m = _GOPRO_NEW.match(name)
        if m:
            return int(m.group(2))
        m = _GOPRO_OLD.match(name)
        if m:
            return 0 if m.group(1).upper() == "OPR" else int(m.group(1)[1:])
        return None


PROFILES = [GoProProfile(), GenericProfile()]


def profile_for(info: DeviceInfo):
    for p in PROFILES:
        if p.matches(info):
            return p
    return PROFILES[-1]


def preview_names(video_name: str):
    """Possible proxy and thumbnail names for a video on the stick.

    Returns (thumbnail_candidates, proxy_candidates). Works for files with a
    collision suffix too: GH010123_1.MP4 -> GH010123_1.THM, GL010123_1.LRV.
    """
    stem, _ = os.path.splitext(video_name)
    thumbs = [stem + ".THM", stem + ".thm"]
    proxies = [stem + ".LRV", stem + ".lrv"]
    if len(stem) >= 2 and stem[0].upper() == "G" and stem[1].upper() in "HX" and _GOPRO_NEW.match(stem[:8] + ".MP4"):
        proxies.insert(0, "GL" + stem[2:] + ".LRV")
    return thumbs, proxies
