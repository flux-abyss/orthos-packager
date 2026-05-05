"""Deterministic upstream metadata probing."""

import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

_IGNORE_DIRS = frozenset({
    ".git", ".orthos", "debian", "debian.*", "debian.repo-backup",
    "build", "_build", "subprojects/packagecache",
    "target", "node_modules", "vendor", ".cargo"
})


def _is_ignored_path(path: Path) -> bool:
    """Return True if the path or any of its parents are in the ignore list."""
    for part in path.parts:
        if part in _IGNORE_DIRS or part.startswith("obj-") or part.startswith("build-") or part.startswith("debian."):
            return True
    return False


def _clean_text(text: str) -> str:
    """Normalize whitespace and strip."""
    return " ".join(text.split())


def _is_usable_source_url(url: str) -> bool:
    """Return True if URL is not a known non-project URL."""
    lower = url.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        return False
    reject = [
        "sh.rustup.rs", "/api/", "/rss/", ".xml", "/feed", ".json",
        "docs.", "/doc/", ".png", ".jpg", ".svg", ".gif",
        "badge", "shield", "issue", "bug", "pull", ".tar.", ".zip", "releases/download"
    ]
    return not any(x in lower for x in reject)


def _looks_like_repo_url(url: str, repo_name: str) -> bool:
    if not _is_usable_source_url(url):
        return False
    return repo_name.lower() in url.lower()


def _first_readme_paragraph(repo: Path) -> str:
    """Extract the first meaningful paragraph from a README file."""
    for name in ("README.md", "README", "README.rst", "README.txt"):
        p = repo / name
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
            
        paragraph_lines = []
        for line in content.splitlines():
            s = line.strip()
            # Stop if we hit a horizontal rule or heading after starting a paragraph
            if paragraph_lines and (s == "" or s.startswith(("#", "---", "==="))):
                text = _clean_text(" ".join(paragraph_lines))
                if len(text) >= 40:
                    return text
                paragraph_lines = []
                continue
                
            # Skip non-meaningful lines when searching for a paragraph
            if not s:
                continue
            if s.startswith(("#", "---", "===", "!", "[", "<", "<!--", "* ", "- ", "+ ")):
                continue
            if re.match(r'^!?\[.*\]\(.*\)$', s):  # badge/link
                continue
            if "bug" in s.lower() and "report" in s.lower() and len(s) < 60:
                continue
            
            paragraph_lines.append(line.strip())
            
        if paragraph_lines:
            text = _clean_text(" ".join(paragraph_lines))
            if len(text) >= 40:
                return text
                
    return ""


def _read_authors_contact(repo: Path) -> str:
    """Find the first 'Name <email>' in AUTHORS."""
    for name in ("AUTHORS", "AUTHORS.md", "AUTHORS.txt"):
        p = repo / name
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                m = re.search(r'([^\<]+)\s+<([^>]+@[^>]+)>', s)
                if m:
                    name_part = m.group(1).strip()
                    email_part = m.group(2).strip()
                    if name_part and email_part:
                        return f"{name_part} <{email_part}>"
        except UnicodeDecodeError:
            pass
    return ""


def _read_git_author_contact(repo: Path) -> str:
    """Find the dominant human author from git history."""
    if not (repo / ".git").is_dir() and not (repo / ".git").is_file():
        return ""

    try:
        # Run git log and extract authors
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%an <%ae>"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""

    lines = result.stdout.strip().splitlines()
    if not lines:
        return ""

    # Filter out bots and noreply emails
    valid_authors = []
    bot_names = {"bot", "github-actions", "dependabot", "renovate", "snyk"}
    
    for line in lines:
        line = line.strip()
        m = re.match(r'^(.*?)\s*<([^>]+)>$', line)
        if not m:
            continue
            
        name = m.group(1).strip()
        email = m.group(2).strip()
        
        # Reject empty names or empty emails
        if not name or not email:
            continue
            
        lower_line = line.lower()
        # Ignore common bot and noreply strings
        if "noreply" in lower_line or any(b in lower_line for b in bot_names):
            continue
            
        valid_authors.append(f"{name} <{email}>")

    if not valid_authors:
        return ""

    # Return the most common
    counter = Counter(valid_authors)
    return counter.most_common(1)[0][0]


# Regex patterns that identify a copyright notice line.
# Ordered from most specific to least.
_COPYRIGHT_PATTERNS = [
    # SPDX style: SPDX-FileCopyrightText: 2020 Some Person
    re.compile(r'SPDX-FileCopyrightText:\s*(.+)', re.IGNORECASE),
    # Copyright (C) ... or Copyright © ...
    re.compile(r'[Cc]opyright\s*(?:\([Cc]\)|©)\s*(.+)'),
    # Plain: Copyright 2020 ...
    re.compile(r'[Cc]opyright\s+(\d.+)'),
]

# Leading comment markers to strip before pattern matching.
_COMMENT_STRIP = re.compile(r'^[\s*/\\#]+')


def _read_source_header_copyright(repo: Path) -> str:
    """Recursively scan source directories for the most common real copyright notice."""
    source_roots = ["src", "app", "lib", "include", "tools"]
    extensions = {".vala", ".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".py", ".js", ".ts", ".go", ".java", ".cs"}
    
    found_notices = []
    
    for root_name in source_roots:
        root_dir = repo / root_name
        if not root_dir.is_dir():
            continue
            
        for p in root_dir.rglob("*"):
            if not p.is_file() or p.suffix not in extensions:
                continue
            if _is_ignored_path(p.relative_to(repo)):
                continue
                
            try:
                # Read only the first 50 lines to avoid massive memory usage
                with open(p, "r", encoding="utf-8") as f:
                    for _ in range(50):
                        raw_line = f.readline()
                        if not raw_line:
                            break
                        line = _COMMENT_STRIP.sub("", raw_line).strip()
                        if not line:
                            continue
                        for pat in _COPYRIGHT_PATTERNS:
                            m = pat.search(line)
                            if m:
                                holder = " ".join(m.group(1).split())
                                if holder:
                                    holder_lower = holder.lower()
                                    if "free software foundation" in holder_lower and ("fsf.org" in holder_lower or "1989" in holder or "2007" in holder):
                                        continue
                                    if "<year>" in holder_lower or "<name of author>" in holder_lower or "<program>" in holder_lower:
                                        continue
                                    if "one line to give the program's name" in holder_lower:
                                        continue
                                    found_notices.append(holder)
                                    break
                        else:
                            continue
                        break
            except (UnicodeDecodeError, OSError):
                continue
                
    if not found_notices:
        return ""
        
    counter = Counter(found_notices)
    return counter.most_common(1)[0][0]


def _is_rejected_holder(holder: str) -> bool:
    """Return True if the holder string is GPL boilerplate / placeholder."""
    h = holder.lower()
    if "free software foundation" in h and ("fsf.org" in h or "1989" in holder or "2007" in holder):
        return True
    if "<year>" in h or "<name of author>" in h or "<program>" in h:
        return True
    if "one line to give the program's name" in h:
        return True
    return False


def _read_upstream_copyright(repo: Path) -> str:
    """Return the best credible copyright notice found in upstream files.

    Priority order:
      A. Top-level project-level legal/metadata files:
           AUTHORS, COPYRIGHT, COPYING, LICENSE, LICENSE.md, NOTICE,
           README.md, README
         These are checked first because they often contain authoritative
         project-level aggregate strings like
         "2000-2025 Carsten Haitzler and various contributors (see AUTHORS)".
      B. Recursive source-header scan (src/, app/, lib/, include/, tools/).
         Used when no project-level aggregate is found above.

    Returns the normalized copyright holder/year text, or empty string.
    """
    def _extract_from_file(path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return ""
        for raw_line in text.splitlines():
            line = _COMMENT_STRIP.sub("", raw_line).strip()
            if not line:
                continue
            for pat in _COPYRIGHT_PATTERNS:
                m = pat.search(line)
                if m:
                    holder = " ".join(m.group(1).split())
                    if holder and not _is_rejected_holder(holder):
                        return holder
        return ""

    # A. Project-level files, in preference order.
    # AUTHORS is the strongest signal for the project's own copyright aggregate.
    project_level = [
        "AUTHORS", "COPYRIGHT", "COPYING", "LICENSE", "LICENSE.md",
        "NOTICE", "README.md", "README",
    ]
    for name in project_level:
        p = repo / name
        if not p.is_file():
            continue
        result = _extract_from_file(p)
        if result:
            return result

    # B. Recursive source-header scan (Paperboy-style projects with no AUTHORS/COPYRIGHT).
    return _read_source_header_copyright(repo)


# Signals the start of a BSD redistribution clause.
_BSD_START = re.compile(
    r'^Redistribution and use in source and binary forms', re.IGNORECASE
)
# Signals the end of the BSD disclaimer.
_BSD_END = re.compile(
    r'EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE\.?\s*$', re.IGNORECASE
)
# A line that looks like a copyright notice (used to skip header lines).
_COPYRIGHT_LINE = re.compile(
    r'Copyright\s|SPDX-FileCopyrightText:', re.IGNORECASE
)


def _read_upstream_license_text(repo: Path) -> str:
    """Return the upstream license body found in legal files.

    Searches COPYING, COPYRIGHT, LICENSE, LICENSE.md, LICENSE.txt, NOTICE
    in that order.  Returns the first credible license body found, normalized
    for whitespace only.  Returns "" when nothing is found.
    """
    legal_files = [
        "COPYING", "COPYRIGHT", "LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE",
    ]

    def _extract_bsd(lines: list[str]) -> str:
        """Extract BSD-style clause from start marker to end disclaimer."""
        in_block = False
        block: list[str] = []
        for line in lines:
            if not in_block:
                if _BSD_START.match(line.strip()):
                    in_block = True
                    block.append(line.rstrip())
            else:
                block.append(line.rstrip())
                if _BSD_END.search(line):
                    break
        if block:
            return "\n".join(block).strip()
        return ""

    def _extract_body(text: str) -> str:
        """Extract the license body, skipping leading copyright-notice lines."""
        lines = text.splitlines()

        # Try BSD extraction first.
        bsd = _extract_bsd(lines)
        if bsd:
            return bsd

        # Otherwise skip leading copyright/blank lines and return the rest.
        body_start = 0
        for i, line in enumerate(lines):
            stripped = _COMMENT_STRIP.sub("", line).strip()
            # Skip copyright notice lines and blank lines at the top.
            if not stripped or _COPYRIGHT_LINE.search(stripped):
                body_start = i + 1
                continue
            # First non-copyright, non-blank line — body starts here.
            break

        body_lines = lines[body_start:]
        # Must be substantial enough to be a real license body.
        body = "\n".join(line.rstrip() for line in body_lines).strip()
        if len(body) >= 80:
            return body
        return ""

    for name in legal_files:
        p = repo / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        result = _extract_body(text)
        if result:
            return result

    return ""


# ---------------------------------------------------------------------------
# Ordered list of (pattern, canonical_name) pairs for license-name detection.
# Patterns are matched case-insensitively against each line of the scanned file.
# The first match wins.  Order matters: longer/more-specific phrases go first.
# ---------------------------------------------------------------------------
_LICENSE_NAME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Apache
    (re.compile(r'Apache(?:\s+|-)License[,\s]+2\.0|Apache-2\.0', re.IGNORECASE), "Apache-2.0"),
    # BSD 2-Clause
    (re.compile(r'BSD[- ]2[- ]Clause', re.IGNORECASE), "BSD-2-Clause"),
    # BSD 3-Clause
    (re.compile(r'BSD[- ]3[- ]Clause', re.IGNORECASE), "BSD-3-Clause"),
    # LGPL versioned variants (most specific first)
    (re.compile(r'LGPL-2\.1-or-later|LGPL\s+version\s+2\.1\s+or\s+later', re.IGNORECASE), "LGPL-2.1-or-later"),
    (re.compile(r'LGPL-2\.1-only', re.IGNORECASE), "LGPL-2.1-only"),
    (re.compile(r'LGPL-2\.1(?![.-])', re.IGNORECASE), "LGPL-2.1"),
    (re.compile(r'LGPL-3\.0-or-later|LGPL\s+version\s+3\s+or\s+later', re.IGNORECASE), "LGPL-3.0-or-later"),
    (re.compile(r'LGPL-3\.0-only', re.IGNORECASE), "LGPL-3.0-only"),
    (re.compile(r'LGPL-3\.0(?![.-])', re.IGNORECASE), "LGPL-3.0"),
    # GPL versioned variants (most specific first)
    (re.compile(r'GPL-2\.0-or-later|GPL\s+version\s+2\s+or\s+later', re.IGNORECASE), "GPL-2.0-or-later"),
    (re.compile(r'GPL-2\.0-only', re.IGNORECASE), "GPL-2.0-only"),
    (re.compile(r'GPL-2\.0(?![.-])', re.IGNORECASE), "GPL-2.0"),
    (re.compile(r'GPL-3\.0-or-later|GPL\s+version\s+3\s+or\s+later', re.IGNORECASE), "GPL-3.0-or-later"),
    (re.compile(r'GPL-3\.0-only', re.IGNORECASE), "GPL-3.0-only"),
    (re.compile(r'GPL-3\.0(?![.-])', re.IGNORECASE), "GPL-3.0"),
    # ISC
    (re.compile(r'ISC\s+licen[sc]ed|licensed\s+under\s+the\s+ISC|ISC\s+Licen[sc]e', re.IGNORECASE), "ISC"),
    # MIT
    (re.compile(r'MIT\s+licen[sc]ed|licensed\s+under\s+the\s+MIT|MIT\s+Licen[sc]e', re.IGNORECASE), "MIT"),
]

# Top-level doc/legal files to search for explicit license name phrases.
_LICENSE_NAME_FILES = [
    "README", "README.md", "README.rst",
    "NEWS", "CHANGELOG", "CHANGELOG.md",
    "COPYING", "LICENSE", "LICENSE.md", "LICENSE.txt",
]


def _read_upstream_license_name(repo: Path) -> str:
    """Detect an explicit license name from top-level upstream doc/legal files.

    Searches only the accepted file list (no recursion).  Returns a canonical
    SPDX-ish string (e.g. 'ISC', 'MIT', 'GPL-2.0-or-later') on the first
    match, or '' when nothing is recognised.
    """
    for filename in _LICENSE_NAME_FILES:
        p = repo / filename
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line in text.splitlines():
            for pattern, canonical in _LICENSE_NAME_PATTERNS:
                if pattern.search(line):
                    return canonical
    return ""


def _read_meson_metadata(repo: Path) -> dict[str, str]:
    """Extract project(), license, and description from meson.build."""
    res = {}
    p = repo / "meson.build"
    if not p.is_file():
        return res
    try:
        content = p.read_text(encoding="utf-8")
        m_proj = re.search(r"project\s*\(\s*['\"]([^'\"]+)['\"]", content)
        if m_proj:
            res["upstream_name"] = m_proj.group(1)
            
        m_lic = re.search(r"license\s*:\s*(?:\[\s*)?['\"]([^'\"]+)['\"]", content)
        if m_lic:
            res["license"] = m_lic.group(1)
            
        m_desc = re.search(r"description\s*:\s*['\"]([^'\"]+)['\"]", content)
        if m_desc:
            res["description_short"] = m_desc.group(1)
    except UnicodeDecodeError:
        pass
    return res


def _read_appstream_metadata(repo: Path) -> dict[str, str]:
    """Find <summary> and <description><p> in metainfo.xml."""
    res = {}
    for p in repo.rglob("*.metainfo.xml"):
        if _is_ignored_path(p.relative_to(repo)):
            continue
        try:
            tree = ET.parse(p)
            root = tree.getroot()
            summary = root.findtext(".//summary")
            if summary:
                res["description_short"] = _clean_text(summary)
            desc_p = root.findtext(".//description/p")
            if desc_p:
                res["description_long"] = _clean_text(desc_p)
            homepage = root.findtext(".//url[@type='homepage']")
            if homepage:
                res["source_url"] = homepage.strip()
            if res:
                return res
        except (ET.ParseError, OSError):
            continue
            
    for p in repo.rglob("*.appdata.xml"):
        if _is_ignored_path(p.relative_to(repo)):
            continue
        try:
            tree = ET.parse(p)
            root = tree.getroot()
            summary = root.findtext(".//summary")
            if summary:
                res["description_short"] = _clean_text(summary)
            desc_p = root.findtext(".//description/p")
            if desc_p:
                res["description_long"] = _clean_text(desc_p)
            homepage = root.findtext(".//url[@type='homepage']")
            if homepage:
                res["source_url"] = homepage.strip()
            if res:
                return res
        except (ET.ParseError, OSError):
            continue
    return res


def _read_desktop_metadata(repo: Path) -> dict[str, str]:
    """Extract GenericName or Comment from an application desktop file."""
    res = {}
    valid_patterns = [
        "data/desktop/*.desktop",
        "data/applications/*.desktop",
        "*/data/desktop/*.desktop",
        "*/data/applications/*.desktop",
        "usr/share/applications/*.desktop",
    ]
    desktop_files = []
    for pat in valid_patterns:
        desktop_files.extend(repo.glob(pat))
        
    for p in desktop_files:
        if _is_ignored_path(p.relative_to(repo)):
            continue
        try:
            content = p.read_text(encoding="utf-8")
            if "Type=Application" not in content:
                continue
            
            comment = ""
            generic_name = ""
            for line in content.splitlines():
                if line.startswith("Comment="):
                    comment = line.split("=", 1)[1].strip()
                elif line.startswith("GenericName="):
                    generic_name = line.split("=", 1)[1].strip()
                    
            if generic_name:
                res["description_short"] = generic_name
                res["_src"] = "generic_name"
            elif comment:
                res["description_short"] = comment
                res["_src"] = "comment"
                
            if res:
                return res
        except UnicodeDecodeError:
            continue
            
    return res


def _read_readme_metadata(repo: Path) -> dict[str, str]:
    """Extract URL, H1 project name, and description from README."""
    res = {}

    desc = _first_readme_paragraph(repo)
    if desc:
        res["description_long"] = desc

    for name in ("README.md", "README", "README.rst", "README.txt"):
        p = repo / name
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8")

            # Extract H1 project name (first "# Title" line, ignore badges/images)
            if "upstream_name" not in res:
                for line in content.splitlines():
                    s = line.strip()
                    if re.match(r'^#{2,}', s):
                        # deeper heading — stop looking
                        break
                    m = re.match(r'^#\s+(.+)$', s)
                    if m:
                        title = m.group(1).strip()
                        # Ignore lines that are purely badge/image markdown
                        if re.match(r'^!?\[.*\]\(.*\)$', title):
                            continue
                        res["upstream_name"] = _clean_text(title)
                        break

            # Extract best project URL
            if "source_url" not in res:
                urls = re.findall(r'https?://[^\s<>"\')\]]+', content)
                repo_name = repo.name
                for url in urls:
                    url = url.rstrip(".,")
                    if _looks_like_repo_url(url, repo_name):
                        res["source_url"] = url
                        break

            if "upstream_name" in res and "source_url" in res:
                break
        except UnicodeDecodeError:
            continue

    return res


def _read_git_origin_url(repo: Path) -> str:
    """Return a normalized HTTPS URL for the git remote origin, or ''.

    Reads repo/.git/config directly without invoking git.  Only the
    [remote "origin"] section is inspected.  Local and ambiguous remotes
    are rejected; only recognised remote forms are returned.

    Normalization rules:
      https://host/path/repo.git  ->  https://host/path/repo
      http://host/path/repo.git   ->  http://host/path/repo
      git@host:path/repo.git      ->  https://host/path/repo
      ssh://git@host/path/repo.git -> https://host/path/repo
      Other https/http URLs        ->  unchanged (trailing .git stripped)

    Rejected (returns ''):
      file://...  /abs/path  ../rel  ./rel  bare host:path (no git@)
    """
    git_config = repo / ".git" / "config"
    if not git_config.is_file():
        return ""

    try:
        text = git_config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""

    # Walk lines looking for the [remote "origin"] section, then its url =.
    in_origin = False
    raw_url = ""
    for line in text.splitlines():
        stripped = line.strip()
        # Detect section headers.
        if stripped.startswith("["):
            # Are we entering [remote "origin"]?
            in_origin = stripped.lower() in ('[remote "origin"]', "[remote 'origin']")
            continue
        if in_origin and stripped.lower().startswith("url"):
            # "url = <value>" or "url=<value>"
            if "=" in stripped:
                raw_url = stripped.split("=", 1)[1].strip()
                break

    if not raw_url:
        return ""

    # --- Rejection rules ---
    # Local paths and file:// are never useful as a Source: field.
    lower = raw_url.lower()
    if (
        lower.startswith("file://")
        or lower.startswith("/")
        or lower.startswith("./")
        or lower.startswith("../")
    ):
        return ""

    # --- Normalization ---

    # SCP-style: git@host:path/repo.git
    _scp = re.match(r'^git@([^:]+):(.+)$', raw_url)
    if _scp:
        host = _scp.group(1)
        path = _scp.group(2).rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"https://{host}/{path}"

    # SSH with git@ user: ssh://git@host/path
    _ssh = re.match(r'^ssh://git@([^/]+)/(.+)$', raw_url, re.IGNORECASE)
    if _ssh:
        host = _ssh.group(1)
        path = _ssh.group(2).rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"https://{host}/{path}"

    # SSH without git@ user: reject (ambiguous).
    if lower.startswith("ssh://"):
        return ""

    # https:// or http://
    if lower.startswith("https://") or lower.startswith("http://"):
        url = raw_url.rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        return url

    # Anything else (bare host:path without git@, unknown schemes): reject.
    return ""


def probe_upstream_metadata(repo: Path) -> dict[str, Any]:
    """Collect upstream-provided metadata."""
    res: dict[str, Any] = {
        "upstream_name": "",
        "description_short": "",
        "description_long": "",
        "upstream_contact": "",
        "upstream_copyright": "",
        "upstream_license_text": "",
        "source_url": "",
        "license": "",
        "metadata_sources": {},
    }
    
    # 1. Git remote origin
    git_url = _read_git_origin_url(repo)
    if git_url:
        res["source_url"] = git_url
        res["metadata_sources"]["source_url"] = "git.origin"
    
    appstream = _read_appstream_metadata(repo)
    if "description_short" in appstream:
        res["description_short"] = appstream["description_short"]
        res["metadata_sources"]["description_short"] = "appstream.summary"
    if "description_long" in appstream:
        res["description_long"] = appstream["description_long"]
        res["metadata_sources"]["description_long"] = "appstream.description"
    if "source_url" in appstream and not res["source_url"]:
        if _is_usable_source_url(appstream["source_url"]):
            res["source_url"] = appstream["source_url"]
            res["metadata_sources"]["source_url"] = "appstream.homepage"

    meson = _read_meson_metadata(repo)
    if "upstream_name" in meson and not res["upstream_name"]:
        res["upstream_name"] = meson["upstream_name"]
        res["metadata_sources"]["upstream_name"] = "meson.project"
    if "description_short" in meson and not res["description_short"]:
        res["description_short"] = meson["description_short"]
        res["metadata_sources"]["description_short"] = "meson.description"
    if "license" in meson and not res["license"]:
        res["license"] = meson["license"]
        res["metadata_sources"]["license"] = "meson.license"

    readme = _read_readme_metadata(repo)
    if "description_long" in readme and not res["description_long"]:
        res["description_long"] = readme["description_long"]
        res["metadata_sources"]["description_long"] = "readme.first_paragraph"
    if "upstream_name" in readme and not res["upstream_name"]:
        res["upstream_name"] = readme["upstream_name"]
        res["metadata_sources"]["upstream_name"] = "readme.h1"
    if "source_url" in readme and not res["source_url"]:
        res["source_url"] = readme["source_url"]
        res["metadata_sources"]["source_url"] = "readme.url"

    if not res["source_url"]:
        res["source_url"] = "FIXME"
        res["metadata_sources"]["source_url"] = "fallback"

    desktop = _read_desktop_metadata(repo)
    if "description_short" in desktop and not res["description_short"]:
        res["description_short"] = desktop["description_short"]
        src = "desktop.generic_name" if desktop.get("_src") != "comment" else "desktop.comment"
        res["metadata_sources"]["description_short"] = src

    contact = _read_authors_contact(repo)
    if contact:
        res["upstream_contact"] = contact
        res["metadata_sources"]["upstream_contact"] = "authors.first_email"
    else:
        git_contact = _read_git_author_contact(repo)
        if git_contact:
            res["upstream_contact"] = git_contact
            res["metadata_sources"]["upstream_contact"] = "git.author"

    upstream_copyright = _read_upstream_copyright(repo)
    if upstream_copyright:
        res["upstream_copyright"] = upstream_copyright
        res["metadata_sources"]["upstream_copyright"] = "copyright.notice"

    upstream_license_text = _read_upstream_license_text(repo)
    if upstream_license_text:
        res["upstream_license_text"] = upstream_license_text
        res["metadata_sources"]["upstream_license_text"] = "license.text"

    # Fallback: scan top-level doc/legal files for an explicit license name
    # phrase only if Meson and AppStream did not provide one.
    if not res["license"]:
        discovered_name = _read_upstream_license_name(repo)
        if discovered_name:
            res["license"] = discovered_name
            res["metadata_sources"]["license"] = "license.name"

    # Fallback: guess GPL version from license text if license name wasn't detected
    if not res["license"] and res["upstream_license_text"]:
        text = res["upstream_license_text"]
        if "GNU GENERAL PUBLIC LICENSE" in text and "Version 3" in text:
            if "or later" in text.lower() or "any later version" in text.lower():
                res["license"] = "GPL-3.0-or-later"
            else:
                res["license"] = "GPL-3.0-only"
            res["metadata_sources"]["license"] = "license.text.guess"

    return res
