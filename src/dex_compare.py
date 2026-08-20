#!/usr/bin/env python3
"""
DEX Application Comparison Module

Downloads DEX Nexus artifacts (digital-playbook, digital-webapp-ces, etc.),
compares previous vs latest versions, and produces HTML reports.
"""

import difflib
import hashlib
import html
import os
import re
import shutil
import ssl
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional

from uxf_compare import UXFComparer

NEXUS_REPOSITORY_BASE = (
    "https://illin3273:28081/nexus/content/repositories"
)
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")


def parse_dex_nexus_version(version: str) -> tuple:
    """Return (release_train, full_version)."""
    version = (version or "").strip()
    if not version or not _VERSION_RE.match(version):
        raise ValueError(f"Invalid Nexus version '{version}'. Use e.g. 10.24.1.47 or 10.24.1.50")
    parts = version.split(".")
    if len(parts) < 3:
        raise ValueError(f"Version must have at least three segments: {version}")
    release_train = ".".join(parts[:3])
    return release_train, version


# Nexus artifact definitions per DEX product
DEX_NEXUS_PRODUCTS: Dict[str, Dict[str, Any]] = {
    "digital-playbook": {
        "label": "digital-playbook",
        "maven_name": "digital-playbook",
        "artifacts": [
            {"key": "jar", "suffix": ".jar", "kind": "JAR", "compare": "jar"},
            {"key": "tar", "suffix": ".tar", "kind": "TAR", "compare": "tar"},
        ],
    },
    "digital-webapp-ces": {
        "label": "digital-webapp-ces",
        "maven_name": "digital-webapp-ces",
        "artifacts": [
            {"key": "sources_jar", "suffix": "-sources.jar", "kind": "Sources JAR", "compare": "jar"},
            {"key": "pom", "suffix": ".pom", "kind": "POM", "compare": "text"},
            {"key": "war", "suffix": ".war", "kind": "WAR", "compare": "jar"},
        ],
    },
}


def build_dex_product_nexus_urls(version: str, product_id: str) -> Dict[str, str]:
    """Build Nexus download URLs for one DEX product version."""
    if product_id not in DEX_NEXUS_PRODUCTS:
        raise ValueError(f"Unknown DEX product: {product_id}")
    spec = DEX_NEXUS_PRODUCTS[product_id]
    release_train, full_version = parse_dex_nexus_version(version)
    repo = f"amdocs-dex-ad-rpm-releases-{release_train}"
    maven = spec["maven_name"]
    base = (
        f"{NEXUS_REPOSITORY_BASE}/{repo}/com/amdocs/digital/{maven}/"
        f"{full_version}/{maven}-{full_version}"
    )
    urls = {"release_train": release_train, "full_version": full_version}
    for art in spec["artifacts"]:
        urls[art["key"]] = f"{base}{art['suffix']}"
    return urls


def build_digital_playbook_nexus_urls(version: str) -> Dict[str, str]:
    """Build JAR and TAR Nexus URLs for a digital-playbook version."""
    return build_dex_product_nexus_urls(version, "digital-playbook")


def _insecure_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _download_url(url: str, dest: Path, log_callback=None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if log_callback:
        log_callback(f"Downloading {url}", "INFO")
    req = urllib.request.Request(url, headers={"User-Agent": "DEX-Compare/1.0"})
    errors = []
    for ctx in (ssl.create_default_context(), _insecure_ssl_context()):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=600) as resp:
                with open(dest, "wb") as out:
                    shutil.copyfileobj(resp, out)
            if log_callback:
                log_callback(f"Saved {dest.name} ({dest.stat().st_size:,} bytes)", "INFO")
            return
        except Exception as e:
            errors.append(e)
    if errors:
        raise errors[-1]
    raise RuntimeError(f"Failed to download {url}")


def download_dex_product_artifacts(
    version: str,
    product_id: str,
    dest_dir: Path,
    log_callback=None,
) -> Dict[str, Path]:
    """Download all configured Nexus artifacts for a product version."""
    if product_id not in DEX_NEXUS_PRODUCTS:
        raise ValueError(f"Unknown DEX product: {product_id}")
    spec = DEX_NEXUS_PRODUCTS[product_id]
    urls = build_dex_product_nexus_urls(version, product_id)
    full_version = urls["full_version"]
    maven = spec["maven_name"]
    paths = {}
    for art in spec["artifacts"]:
        url = urls[art["key"]]
        dest = dest_dir / f"{maven}-{full_version}{art['suffix']}"
        _download_url(url, dest, log_callback)
        paths[art["key"]] = dest
    return paths


def download_digital_playbook_artifacts(version: str, dest_dir: Path, log_callback=None) -> Dict[str, Path]:
    return download_dex_product_artifacts(version, "digital-playbook", dest_dir, log_callback)


def prepare_dex_nexus_products(
    product_versions: Dict[str, tuple],
    work_root: Optional[Path] = None,
    log_callback=None,
    progress_callback=None,
) -> Dict[str, Any]:
    """
    Download previous/latest artifacts for each product in product_versions.
    product_versions: {product_id: (previous_version, latest_version)}
    Returns metadata including per-product directories and URLs.
    """
    if not product_versions:
        raise ValueError("At least one DEX product version pair is required.")

    if work_root is None:
        work_root = Path(tempfile.mkdtemp(prefix="dex_nexus_"))
    else:
        work_root = Path(work_root)
        work_root.mkdir(parents=True, exist_ok=True)

    products_meta = []
    total_downloads = len(product_versions) * 2
    step = 0

    for product_id, (prev_v, latest_v) in product_versions.items():
        prev_v = (prev_v or "").strip()
        latest_v = (latest_v or "").strip()
        if not prev_v or not latest_v:
            raise ValueError(f"Previous and latest versions required for {product_id}.")
        if prev_v == latest_v:
            raise ValueError(f"Previous and latest versions must differ for {product_id}.")
        if product_id not in DEX_NEXUS_PRODUCTS:
            raise ValueError(f"Unknown DEX product: {product_id}")

        label = DEX_NEXUS_PRODUCTS[product_id]["label"]
        prev_urls = build_dex_product_nexus_urls(prev_v, product_id)
        latest_urls = build_dex_product_nexus_urls(latest_v, product_id)
        prev_dir = work_root / product_id / "previous"
        latest_dir = work_root / product_id / "latest"

        if log_callback:
            log_callback(f"=== {label}: previous {prev_v}, latest {latest_v} ===", "INFO")
            for art in DEX_NEXUS_PRODUCTS[product_id]["artifacts"]:
                key = art["key"]
                log_callback(f"  Previous {art['kind']}: {prev_urls.get(key, '')}", "INFO")
                log_callback(f"  Latest {art['kind']}: {latest_urls.get(key, '')}", "INFO")

        step += 1
        if progress_callback:
            progress_callback(f"Downloading previous {label}...", step, total_downloads)
        download_dex_product_artifacts(prev_v, product_id, prev_dir, log_callback)

        step += 1
        if progress_callback:
            progress_callback(f"Downloading latest {label}...", step, total_downloads)
        download_dex_product_artifacts(latest_v, product_id, latest_dir, log_callback)

        products_meta.append(
            {
                "product_id": product_id,
                "label": label,
                "previous_version": prev_v,
                "latest_version": latest_v,
                "previous_urls": prev_urls,
                "latest_urls": latest_urls,
                "previous_dir": str(prev_dir),
                "latest_dir": str(latest_dir),
                "artifact_specs": DEX_NEXUS_PRODUCTS[product_id]["artifacts"],
            }
        )

    return {
        "work_root": str(work_root),
        "products": products_meta,
        # Legacy single-product fields (first product) for older callers
        "previous_version": products_meta[0]["previous_version"] if products_meta else "",
        "latest_version": products_meta[0]["latest_version"] if products_meta else "",
        "previous_urls": products_meta[0]["previous_urls"] if products_meta else {},
        "latest_urls": products_meta[0]["latest_urls"] if products_meta else {},
    }


def prepare_dex_build_dirs_from_nexus(
    previous_version: str,
    latest_version: str,
    work_root: Optional[Path] = None,
    log_callback=None,
    progress_callback=None,
) -> tuple:
    """Download digital-playbook only (legacy). Returns (prev_dir, latest_dir, meta)."""
    meta = prepare_dex_nexus_products(
        {"digital-playbook": (previous_version, latest_version)},
        work_root=work_root,
        log_callback=log_callback,
        progress_callback=progress_callback,
    )
    prod = meta["products"][0]
    return prod["previous_dir"], prod["latest_dir"], meta


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class DEXComparer(UXFComparer):
    """DEX comparer — Nexus multi-product artifact comparison with HTML report."""

    def __init__(self, folder1: str, folder2: str):
        super().__init__(folder1, folder2)
        self.nexus_meta: Optional[Dict[str, Any]] = None
        self.jar_result: Optional[Dict[str, Any]] = None
        self.tar_result: Optional[Dict[str, Any]] = None
        self.product_comparisons: list = []
        self.nexus_compare_mode = False

    def _brand(self, text: str) -> str:
        if not text:
            return text
        return (
            text.replace("UXF BUILD COMPARISON REPORT", "DEX BUILD COMPARISON REPORT")
            .replace("END OF UXF COMPARISON REPORT", "END OF DEX COMPARISON REPORT")
            .replace("UXF Build Comparison Report", "DEX Build Comparison Report")
            .replace("Starting UXF comparison", "Starting DEX comparison")
            .replace("[UXF]", "[DEX]")
            .replace("UXF Build Comparison Report", "DEX Digital Playbook Comparison Report")
        )

    def _find_single_artifact(self, folder: str, ext: str) -> Path:
        suffix = ext if ext.startswith(".") else f".{ext}"
        return self._find_artifact_by_suffix(folder, suffix)

    def _find_artifact_by_suffix(self, folder: str, suffix: str) -> Path:
        matches = sorted(p for p in Path(folder).iterdir() if p.is_file() and p.name.endswith(suffix))
        if not matches:
            raise FileNotFoundError(f"No *{suffix} file found in {folder}")
        if len(matches) > 1:
            self._log(f"Multiple *{suffix} files in {folder}; using {matches[0].name}", "WARNING")
        return matches[0]

    def _compare_text_file(self, old_path: Path, new_path: Path, kind: str) -> Dict[str, Any]:
        """Compare a text artifact (e.g. POM) with line-level diff tables."""
        self._log(f"Comparing {kind}: {old_path.name} vs {new_path.name}", "INFO")
        b1 = old_path.read_bytes()
        b2 = new_path.read_bytes()
        old_size, new_size = len(b1), len(b2)
        old_hash = hashlib.sha256(b1).hexdigest()
        new_hash = hashlib.sha256(b2).hexdigest()
        identical = old_hash == new_hash
        member_diff = {"added": [], "removed": [], "modified": []}
        if not identical:
            content_rows = self._build_jar_member_content_diff_rows(b1, b2)
            member_diff["modified"] = [
                {
                    "name": old_path.name,
                    "old_size": old_size,
                    "new_size": new_size,
                    "old_crc": None,
                    "new_crc": None,
                    "size_delta": new_size - old_size,
                    "crc_changed": True,
                    "content_rows": content_rows,
                }
            ]
        return {
            "kind": kind,
            "old_name": old_path.name,
            "new_name": new_path.name,
            "old_path": str(old_path),
            "new_path": str(new_path),
            "old_size": old_size,
            "new_size": new_size,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "identical": identical,
            "changed": not identical,
            "member_diff": member_diff,
            "class_diffs": [],
        }

    def _compare_jar_files(self, old_path: Path, new_path: Path) -> Dict[str, Any]:
        self._log(f"Comparing JAR: {old_path.name} vs {new_path.name}", "INFO")
        old_size = old_path.stat().st_size
        new_size = new_path.stat().st_size
        old_hash = _file_sha256(old_path)
        new_hash = _file_sha256(new_path)
        identical = old_hash == new_hash

        with open(old_path, "rb") as f:
            jar_data1 = f.read()
        with open(new_path, "rb") as f:
            jar_data2 = f.read()

        member_diff = self._compare_jar_members(old_path.name, jar_data1, jar_data2)
        self._enrich_modified_jar_members(jar_data1, jar_data2, member_diff.get("modified", []))
        is_war = old_path.name.lower().endswith(".war") or new_path.name.lower().endswith(".war")
        jar_only_diff = self._filter_jar_member_diff(member_diff) if is_war else None
        class_diffs = []
        if not identical and self.decompiler_type and not is_war:
            class_diffs = self._compare_jar_class_files(old_path.name, jar_data1, jar_data2)

        jar_changed = (
            jar_only_diff["added"]
            or jar_only_diff["removed"]
            or jar_only_diff["modified"]
        ) if jar_only_diff else False

        changed = (
            not identical
            or member_diff["added"]
            or member_diff["removed"]
            or member_diff["modified"]
            or jar_changed
        )

        return {
            "kind": "JAR",
            "old_name": old_path.name,
            "new_name": new_path.name,
            "old_path": str(old_path),
            "new_path": str(new_path),
            "old_size": old_size,
            "new_size": new_size,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "identical": identical,
            "changed": changed,
            "member_diff": member_diff,
            "jar_only_diff": jar_only_diff,
            "is_war": is_war,
            "class_diffs": class_diffs,
            "old_url": (self.nexus_meta or {}).get("previous_urls", {}).get("jar", ""),
            "new_url": (self.nexus_meta or {}).get("latest_urls", {}).get("jar", ""),
        }

    def _enrich_modified_jar_members(self, jar_data1: bytes, jar_data2: bytes, modified: list) -> None:
        """Add CRC/size delta and line-level content diff rows for modified JAR members."""
        if not modified:
            return
        with zipfile.ZipFile(BytesIO(jar_data1), "r") as zf1, zipfile.ZipFile(BytesIO(jar_data2), "r") as zf2:
            for entry in modified:
                name = entry["name"]
                b1 = zf1.read(name)
                b2 = zf2.read(name)
                entry["size_delta"] = entry["new_size"] - entry["old_size"]
                entry["crc_changed"] = entry["old_crc"] != entry["new_crc"]
                entry["sha256_previous"] = hashlib.sha256(b1).hexdigest()
                entry["sha256_latest"] = hashlib.sha256(b2).hexdigest()
                entry["content_rows"] = self._build_jar_member_content_diff_rows(b1, b2)

    def _decode_jar_member_text(self, data: bytes) -> Optional[str]:
        for encoding in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return None

    def _build_jar_member_content_diff_rows(self, data1: bytes, data2: bytes) -> list:
        """Build tabular line-level diff rows for a modified JAR member."""
        if data1 == data2:
            return []

        text1 = self._decode_jar_member_text(data1)
        text2 = self._decode_jar_member_text(data2)
        if text1 is None or text2 is None:
            return [
                {
                    "change": "binary",
                    "line_prev": "",
                    "line_latest": "",
                    "previous": f"Binary / non-text ({len(data1):,} bytes, SHA-256 {hashlib.sha256(data1).hexdigest()[:12]}…)",
                    "latest": f"Binary / non-text ({len(data2):,} bytes, SHA-256 {hashlib.sha256(data2).hexdigest()[:12]}…)",
                }
            ]

        lines1 = text1.splitlines()
        lines2 = text2.splitlines()
        rows = []
        matcher = difflib.SequenceMatcher(None, lines1, lines2)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace":
                prev_block = lines1[i1:i2]
                latest_block = lines2[j1:j2]
                max_len = max(len(prev_block), len(latest_block))
                for idx in range(max_len):
                    rows.append(
                        {
                            "change": "modified",
                            "line_prev": str(i1 + idx + 1) if idx < len(prev_block) else "",
                            "line_latest": str(j1 + idx + 1) if idx < len(latest_block) else "",
                            "previous": prev_block[idx] if idx < len(prev_block) else "",
                            "latest": latest_block[idx] if idx < len(latest_block) else "",
                        }
                    )
            elif tag == "delete":
                for idx, line in enumerate(lines1[i1:i2]):
                    rows.append(
                        {
                            "change": "removed",
                            "line_prev": str(i1 + idx + 1),
                            "line_latest": "",
                            "previous": line,
                            "latest": "",
                        }
                    )
            elif tag == "insert":
                for idx, line in enumerate(lines2[j1:j2]):
                    rows.append(
                        {
                            "change": "added",
                            "line_prev": "",
                            "line_latest": str(j1 + idx + 1),
                            "previous": "",
                            "latest": line,
                        }
                    )
        if not rows:
            rows.append(
                {
                    "change": "modified",
                    "line_prev": "1",
                    "line_latest": "1",
                    "previous": "(content differs — same line count, different bytes)",
                    "latest": "(content differs — same line count, different bytes)",
                }
            )
        return rows[:200]

    def _tar_members(self, path: Path) -> Dict[str, int]:
        members = {}
        with tarfile.open(path, "r:*") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    members[m.name] = m.size
        return members

    @staticmethod
    def _is_yml_member(name: str) -> bool:
        lower = name.lower()
        return lower.endswith(".yml") or lower.endswith(".yaml")

    @staticmethod
    def _is_jar_member(name: str) -> bool:
        return name.lower().endswith(".jar")

    def _read_tar_file_bytes(self, tar_path: Path, member_name: str) -> bytes:
        with tarfile.open(tar_path, "r:*") as tf:
            member = tf.getmember(member_name)
            extracted = tf.extractfile(member)
            if extracted is None:
                return b""
            return extracted.read()

    def _compare_tar_yml_contents(self, old_path: Path, new_path: Path) -> Dict[str, Any]:
        """Compare .yml/.yaml files inside two TAR archives with tabular diff data."""
        self._log(f"Comparing YAML inside TAR: {old_path.name} vs {new_path.name}", "INFO")

        def yml_map(path: Path) -> Dict[str, int]:
            out = {}
            with tarfile.open(path, "r:*") as tf:
                for m in tf.getmembers():
                    if m.isfile() and self._is_yml_member(m.name):
                        out[m.name] = m.size
            return out

        yml1 = yml_map(old_path)
        yml2 = yml_map(new_path)
        set1, set2 = set(yml1.keys()), set(yml2.keys())
        removed = sorted(set1 - set2)
        added = sorted(set2 - set1)
        modified = []

        for name in sorted(set1 & set2):
            if yml1[name] != yml2[name]:
                modified.append(name)
                continue
            b1 = self._read_tar_file_bytes(old_path, name)
            b2 = self._read_tar_file_bytes(new_path, name)
            if hashlib.sha256(b1) != hashlib.sha256(b2):
                modified.append(name)

        modified_entries = []
        for name in modified:
            b1 = self._read_tar_file_bytes(old_path, name)
            b2 = self._read_tar_file_bytes(new_path, name)
            modified_entries.append(
                {
                    "name": name,
                    "old_size": len(b1),
                    "new_size": len(b2),
                    "old_crc": None,
                    "new_crc": None,
                    "size_delta": len(b2) - len(b1),
                    "crc_changed": True,
                    "content_rows": self._build_jar_member_content_diff_rows(b1, b2),
                }
            )

        has_changes = bool(added or removed or modified_entries)
        return {
            "added": added,
            "removed": removed,
            "modified": modified_entries,
            "total_previous": len(yml1),
            "total_latest": len(yml2),
            "has_changes": has_changes,
        }

    @staticmethod
    def _filter_jar_member_diff(member_diff: Dict[str, Any]) -> Dict[str, Any]:
        """Keep only .jar entries from a ZIP/WAR member diff."""
        if not member_diff:
            return {"added": [], "removed": [], "modified": []}
        return {
            "added": [n for n in member_diff.get("added", []) if DEXComparer._is_jar_member(n)],
            "removed": [n for n in member_diff.get("removed", []) if DEXComparer._is_jar_member(n)],
            "modified": [
                m
                for m in member_diff.get("modified", [])
                if DEXComparer._is_jar_member(m.get("name", ""))
            ],
        }

    def _compare_tar_files(self, old_path: Path, new_path: Path) -> Dict[str, Any]:
        self._log(f"Comparing TAR: {old_path.name} vs {new_path.name}", "INFO")
        old_size = old_path.stat().st_size
        new_size = new_path.stat().st_size
        old_hash = _file_sha256(old_path)
        new_hash = _file_sha256(new_path)
        identical = old_hash == new_hash

        members1 = self._tar_members(old_path)
        members2 = self._tar_members(new_path)
        set1, set2 = set(members1.keys()), set(members2.keys())
        removed = sorted(set1 - set2)
        added = sorted(set2 - set1)
        modified = sorted(
            n for n in (set1 & set2) if members1[n] != members2[n]
        )

        yml_diff = self._compare_tar_yml_contents(old_path, new_path)
        changed = (
            not identical
            or added
            or removed
            or modified
            or yml_diff.get("has_changes")
        )

        return {
            "kind": "TAR",
            "old_name": old_path.name,
            "new_name": new_path.name,
            "old_path": str(old_path),
            "new_path": str(new_path),
            "old_size": old_size,
            "new_size": new_size,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "identical": identical,
            "changed": changed,
            "added": added,
            "removed": removed,
            "modified": modified,
            "yml_diff": yml_diff,
            "old_url": (self.nexus_meta or {}).get("previous_urls", {}).get("tar", ""),
            "new_url": (self.nexus_meta or {}).get("latest_urls", {}).get("tar", ""),
        }

    def _compare_artifact(self, old_path: Path, new_path: Path, art_spec: dict) -> Dict[str, Any]:
        compare_type = art_spec.get("compare", "jar")
        kind = art_spec.get("kind", "File")
        if compare_type == "jar":
            result = self._compare_jar_files(old_path, new_path)
            result["kind"] = kind
            return result
        if compare_type == "tar":
            result = self._compare_tar_files(old_path, new_path)
            result["kind"] = kind
            return result
        if compare_type == "text":
            return self._compare_text_file(old_path, new_path, kind)
        raise ValueError(f"Unknown compare type: {compare_type}")

    def compare_nexus_products(self, progress_callback=None):
        """Compare all downloaded Nexus products (playbook, webapp-ces, etc.)."""
        self.results = []
        self.product_comparisons = []
        self.nexus_compare_mode = True
        products = (self.nexus_meta or {}).get("products") or []
        self._log(f"Starting DEX Nexus comparison ({len(products)} product(s))", "INFO")

        from uxf_compare import _find_java_decompiler
        self.decompiler_type, self.decompiler_path = _find_java_decompiler()

        total_steps = sum(len(p.get("artifact_specs") or []) for p in products) or 1
        step = 0
        for prod in products:
            label = prod.get("label", prod.get("product_id", ""))
            self._log(f"--- Comparing {label} ---", "INFO")
            prev_dir = prod["previous_dir"]
            latest_dir = prod["latest_dir"]
            product_entry = {
                "product_id": prod.get("product_id"),
                "label": label,
                "previous_version": prod.get("previous_version"),
                "latest_version": prod.get("latest_version"),
                "previous_urls": prod.get("previous_urls", {}),
                "latest_urls": prod.get("latest_urls", {}),
                "artifacts": [],
            }
            for art_spec in prod.get("artifact_specs") or []:
                step += 1
                self._update_progress(
                    f"Comparing {label} {art_spec.get('kind', '')}...",
                    step,
                    total_steps,
                )
                old_path = self._find_artifact_by_suffix(prev_dir, art_spec["suffix"])
                new_path = self._find_artifact_by_suffix(latest_dir, art_spec["suffix"])
                result = self._compare_artifact(old_path, new_path, art_spec)
                product_entry["artifacts"].append(result)
                if art_spec.get("compare") == "jar" and art_spec.get("key") == "jar":
                    self.jar_result = result
                if art_spec.get("compare") == "tar":
                    self.tar_result = result
            self.product_comparisons.append(product_entry)

        self._populate_results_from_all_products()
        self._update_progress("Comparison complete!", total_steps, total_steps)
        return self.results

    def compare_digital_playbook_builds(self, progress_callback=None):
        """Legacy: compare playbook only from folder1/folder2."""
        return self.compare_nexus_products(progress_callback)

    def _populate_results_from_all_products(self):
        """Sync all product artifact compares into results list."""
        self.only_in_1 = []
        self.only_in_2 = []
        self.version_changes = []
        self.size_changes = []
        self.member_diffs = {}
        self.class_diffs = {}
        self.in_both = []

        for product in self.product_comparisons:
            label = product.get("label", "")
            for res in product.get("artifacts") or []:
                kind = res.get("kind", "File")
                prefix = f"[{label}] " if label else ""
                if res.get("changed"):
                    self.results.append(
                        f"{prefix}{kind} MODIFIED: {res['old_name']} -> {res['new_name']} "
                        f"(size {res['old_size']:,} -> {res['new_size']:,})"
                    )
                    key = f"{label}/{res['new_name']}" if label else res["new_name"]
                    if res.get("member_diff"):
                        self.member_diffs[key] = res.get("member_diff", {})
                    if res.get("class_diffs"):
                        self.class_diffs[key] = res["class_diffs"]
                    self.size_changes.append(
                        {
                            "name": key,
                            "old_size": res["old_size"],
                            "new_size": res["new_size"],
                            "old_crc": None,
                            "new_crc": None,
                            "old_path": res["old_path"],
                            "new_path": res["new_path"],
                        }
                    )
                else:
                    self.results.append(f"{prefix}{kind} UNCHANGED: {res.get('old_name', '')}")
                    self.in_both.append(res.get("old_name", kind))

    def compare_folders(self):
        if self.nexus_meta and self.nexus_meta.get("products"):
            return self.compare_nexus_products()
        if self.nexus_meta:
            return self.compare_digital_playbook_builds()
        results = super().compare_folders()
        self._compare_tar_legacy()
        return results

    def _compare_tar_legacy(self):
        """TAR compare for manual folder path mode."""
        t1 = sorted(Path(self.folder1).glob("*.tar"))
        t2 = sorted(Path(self.folder2).glob("*.tar"))
        if len(t1) == 1 and len(t2) == 1:
            self.tar_result = self._compare_tar_files(t1[0], t2[0])
            if self.tar_result.get("changed"):
                tr = self.tar_result
                self.size_changes.append(
                    {
                        "name": f"{tr['new_name']} (tar)",
                        "old_size": tr["old_size"],
                        "new_size": tr["new_size"],
                        "old_crc": None,
                        "new_crc": None,
                        "old_path": tr["old_path"],
                        "new_path": tr["new_path"],
                    }
                )
                self.results.append(f"MODIFIED TAR: {tr['old_name']}")

    def _product_meta_html(self, product: dict) -> str:
        prev_urls = product.get("previous_urls") or {}
        latest_urls = product.get("latest_urls") or {}
        rows = []
        for key, url in sorted(prev_urls.items()):
            if key in ("release_train", "full_version"):
                continue
            rows.append(f"<dt>Previous {html.escape(key)} URL</dt><dd><a href=\"{html.escape(url)}\">{html.escape(url)}</a></dd>")
        for key, url in sorted(latest_urls.items()):
            if key in ("release_train", "full_version"):
                continue
            rows.append(f"<dt>Latest {html.escape(key)} URL</dt><dd><a href=\"{html.escape(url)}\">{html.escape(url)}</a></dd>")
        return (
            f"<h3 style='color:#9a3412;margin:24px 0 8px 0;'>{html.escape(product.get('label', ''))}</h3>"
            f"<p style='margin:0 0 8px 0;'>Previous: <strong>{html.escape(product.get('previous_version', ''))}</strong> "
            f"| Latest: <strong>{html.escape(product.get('latest_version', ''))}</strong></p>"
            f"<dl>{''.join(rows)}</dl>"
        )

    def _yml_diff_tables_html(self, yml_diff: dict) -> str:
        if not yml_diff:
            return ""
        parts = [
            "<h3 style='color:#9a3412;margin:20px 0 8px 0;'>YAML files inside TAR (.yml / .yaml)</h3>",
            f"<p style='margin:0 0 12px 0;font-size:13px;color:#64748b;'>"
            f"Previous: <strong>{yml_diff.get('total_previous', 0)}</strong> YAML file(s) | "
            f"Latest: <strong>{yml_diff.get('total_latest', 0)}</strong> YAML file(s)</p>",
        ]
        if not yml_diff.get("has_changes"):
            parts.append(
                "<div class='success-box'><p>All YAML files inside the TAR are identical.</p></div>"
            )
            return "".join(parts)

        md = {
            "added": yml_diff.get("added", []),
            "removed": yml_diff.get("removed", []),
            "modified": yml_diff.get("modified", []),
        }
        parts.append(self._archive_details_html({"member_diff": md, "changed": True}, "TAR (YAML)"))
        return "".join(parts)

    def _war_jar_tables_html(self, res: dict) -> str:
        jar_diff = res.get("jar_only_diff")
        if not jar_diff:
            return ""
        has_jar_changes = jar_diff.get("added") or jar_diff.get("removed") or jar_diff.get("modified")
        parts = [
            "<h3 style='color:#9a3412;margin:20px 0 8px 0;'>JAR files inside WAR</h3>",
            f"<p style='margin:0 0 12px 0;font-size:13px;color:#64748b;'>"
            f"Comparing <code>{html.escape(res.get('old_name', ''))}</code> vs "
            f"<code>{html.escape(res.get('new_name', ''))}</code> — .jar members only.</p>",
        ]
        if not has_jar_changes:
            parts.append(
                "<div class='success-box'><p>All JAR files inside the WAR are identical.</p></div>"
            )
            return "".join(parts)
        parts.append(
            self._archive_details_html(
                {"member_diff": jar_diff, "changed": True, "class_diffs": []},
                "WAR (JAR)",
            )
        )
        return "".join(parts)

    def _product_section_html(self, product: dict) -> str:
        summary_rows = "".join(self._artifact_summary_row(res) for res in product.get("artifacts") or [])
        details = []
        for res in product.get("artifacts") or []:
            kind = res.get("kind", "File")
            if kind == "TAR":
                details.append(self._tar_details_html_for(res))
                yml_diff = res.get("yml_diff")
                if yml_diff is not None:
                    details.append(f"<div class='section'>{self._yml_diff_tables_html(yml_diff)}</div>")
            elif res.get("is_war") or kind == "WAR":
                details.append(self._war_jar_tables_html(res))
            elif res.get("changed") and (res.get("member_diff") or kind in ("JAR", "Sources JAR", "POM")):
                details.append(
                    f"<div class='section'><h3>{html.escape(kind)} comparison details</h3>"
                    + self._archive_details_html(res, kind)
                    + "</div>"
                )
        changed_count = sum(1 for r in product.get("artifacts") or [] if r.get("changed"))
        total = len(product.get("artifacts") or [])
        return f"""
        <div class="section product-section">
            {self._product_meta_html(product)}
            <h4>Artifact summary</h4>
            <table class="comparison-table">
                <thead><tr><th>Type</th><th>Previous file</th><th>Latest file</th><th>Previous size</th><th>Latest size</th><th>Diff</th><th>Status</th></tr></thead>
                <tbody>{summary_rows}</tbody>
            </table>
            {''.join(details)}
            {"<div class='success-box'><p>All artifacts identical for this product.</p></div>" if changed_count == 0 and total else ""}
        </div>"""

    def _tar_details_html_for(self, tr: dict) -> str:
        if not tr:
            return ""
        parts = [
            "<h3 style='color:#9a3412;margin:0 0 8px 0;'>TAR archive overview</h3>",
            f"<p style='margin:0 0 12px 0;font-size:13px;'>"
            f"<code>{html.escape(tr.get('old_name', ''))}</code> vs "
            f"<code>{html.escape(tr.get('new_name', ''))}</code></p>",
        ]
        if not tr.get("changed"):
            parts.append("<div class='success-box'><p>TAR archives are identical (hash match).</p></div>")
            return f"<div class='section'>{''.join(parts)}</div>"

        if tr.get("removed"):
            rem_rows = "".join(
                f"<tr><td>{i}</td><td><code>{html.escape(x)}</code></td><td>Removed</td></tr>"
                for i, x in enumerate(tr["removed"][:100], start=1)
            )
            parts.append(
                f"<h4>Removed from TAR ({len(tr['removed'])})</h4>"
                f"<table class='comparison-table'><thead><tr><th>#</th><th>Path</th><th>Status</th></tr></thead>"
                f"<tbody>{rem_rows}</tbody></table>"
            )
        if tr.get("added"):
            add_rows = "".join(
                f"<tr><td>{i}</td><td><code>{html.escape(x)}</code></td><td>Added</td></tr>"
                for i, x in enumerate(tr["added"][:100], start=1)
            )
            parts.append(
                f"<h4>Added to TAR ({len(tr['added'])})</h4>"
                f"<table class='comparison-table'><thead><tr><th>#</th><th>Path</th><th>Status</th></tr></thead>"
                f"<tbody>{add_rows}</tbody></table>"
            )
        if tr.get("modified"):
            mod_rows = "".join(
                f"<tr><td>{i}</td><td><code>{html.escape(x)}</code></td><td>Size changed</td></tr>"
                for i, x in enumerate(tr["modified"][:100], start=1)
            )
            parts.append(
                f"<h4>Modified in TAR — size ({len(tr['modified'])})</h4>"
                f"<table class='comparison-table'><thead><tr><th>#</th><th>Path</th><th>Note</th></tr></thead>"
                f"<tbody>{mod_rows}</tbody></table>"
            )
        return f"<div class='section'>{''.join(parts)}</div>"

    def generate_report(self) -> str:
        if not self.nexus_compare_mode:
            return self._brand(super().generate_report())
        lines = [
            "=" * 72,
            "DEX NEXUS COMPARISON REPORT",
            "=" * 72,
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        for product in self.product_comparisons:
            lines.append(f"=== {product.get('label', '')} ===")
            lines.append(f"  Previous version: {product.get('previous_version', '')}")
            lines.append(f"  Latest version: {product.get('latest_version', '')}")
            for res in product.get("artifacts") or []:
                lines.append(f"  --- {res.get('kind', '')} ---")
                lines.append(f"    Previous: {res['old_name']} ({res['old_size']:,} bytes)")
                lines.append(f"    Latest:   {res['new_name']} ({res['new_size']:,} bytes)")
                lines.append(f"    Status:   {'CHANGED' if res['changed'] else 'IDENTICAL'}")
                yml_diff = res.get("yml_diff")
                if yml_diff and yml_diff.get("has_changes"):
                    lines.append(f"    YAML in TAR: +{len(yml_diff.get('added', []))} "
                                 f"-{len(yml_diff.get('removed', []))} "
                                 f"~{len(yml_diff.get('modified', []))}")
                jar_only = res.get("jar_only_diff")
                if jar_only and (jar_only.get("added") or jar_only.get("removed") or jar_only.get("modified")):
                    lines.append(f"    JARs in WAR: +{len(jar_only.get('added', []))} "
                                 f"-{len(jar_only.get('removed', []))} "
                                 f"~{len(jar_only.get('modified', []))}")
            lines.append("")
        lines.extend(self.results)
        lines.append("=" * 72)
        lines.append("END OF DEX COMPARISON REPORT")
        return "\n".join(lines)

    def _artifact_summary_row(self, res: Dict[str, Any]) -> str:
        status = "✅ Identical" if not res.get("changed") else "⚠️ Changed"
        status_color = "#166534" if not res.get("changed") else "#b45309"
        size_diff = res["new_size"] - res["old_size"]
        size_str = f"+{size_diff:,}" if size_diff > 0 else f"{size_diff:,}"
        return f"""
        <tr>
            <td><strong>{html.escape(res['kind'])}</strong></td>
            <td>{html.escape(res['old_name'])}</td>
            <td>{html.escape(res['new_name'])}</td>
            <td style="text-align:right;">{res['old_size']:,}</td>
            <td style="text-align:right;">{res['new_size']:,}</td>
            <td style="text-align:right;">{size_str}</td>
            <td style="color:{status_color};font-weight:600;">{status}</td>
        </tr>"""

    def _jar_modified_summary_table_html(self, modified: list) -> str:
        rows = []
        for idx, m in enumerate(modified, start=1):
            delta = m.get("size_delta", m["new_size"] - m["old_size"])
            delta_str = f"+{delta:,}" if delta > 0 else f"{delta:,}"
            crc_note = "Yes" if m.get("crc_changed") else "No"
            content_rows = m.get("content_rows") or []
            content_note = (
                f"{len(content_rows)} line(s) differ — see table below"
                if content_rows
                else "Identical bytes"
            )
            rows.append(
                f"<tr>"
                f"<td style='text-align:center;'>{idx}</td>"
                f"<td><code>{html.escape(m['name'])}</code></td>"
                f"<td style='text-align:right;'>{m['old_size']:,}</td>"
                f"<td style='text-align:right;'>{m['new_size']:,}</td>"
                f"<td style='text-align:right;'>{delta_str}</td>"
                f"<td style='font-family:Consolas,monospace;font-size:11px;'>{m.get('old_crc', '')}</td>"
                f"<td style='font-family:Consolas,monospace;font-size:11px;'>{m.get('new_crc', '')}</td>"
                f"<td>{crc_note}</td>"
                f"<td>{html.escape(content_note)}</td>"
                f"</tr>"
            )
        return (
            f"<table class='comparison-table jar-modified-summary'>"
            f"<thead><tr>"
            f"<th>#</th><th>Member path</th><th>Previous size</th><th>Latest size</th>"
            f"<th>Size Δ</th><th>Previous CRC</th><th>Latest CRC</th><th>CRC changed</th>"
            f"<th>Content comparison</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        )

    def _jar_member_content_table_html(self, member: dict, member_idx: int) -> str:
        content_rows = member.get("content_rows") or []
        if not content_rows:
            return ""
        change_styles = {
            "added": ("#166534", "#f0fdf4"),
            "removed": ("#b91c1c", "#fef2f2"),
            "modified": ("#b45309", "#fffbeb"),
            "binary": ("#7c2d12", "#fff7ed"),
        }
        body_rows = []
        for row in content_rows:
            change = row.get("change", "modified")
            color, bg = change_styles.get(change, ("#334155", "#f8fafc"))
            prev_cell = html.escape(row.get("previous", ""))
            latest_cell = html.escape(row.get("latest", ""))
            body_rows.append(
                f"<tr style='background:{bg};'>"
                f"<td style='color:{color};font-weight:600;text-transform:capitalize;'>{html.escape(change)}</td>"
                f"<td style='text-align:center;font-family:Consolas,monospace;'>{html.escape(str(row.get('line_prev', '')))}</td>"
                f"<td style='font-family:Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word;'>{prev_cell}</td>"
                f"<td style='text-align:center;font-family:Consolas,monospace;'>{html.escape(str(row.get('line_latest', '')))}</td>"
                f"<td style='font-family:Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word;'>{latest_cell}</td>"
                f"</tr>"
            )
        anchor = f"jar-member-{member_idx}"
        truncated = ""
        if len(content_rows) >= 200:
            truncated = "<p class='hint' style='font-size:12px;color:#64748b;'>Showing first 200 changed lines.</p>"
        return (
            f"<div class='jar-content-block' id='{anchor}' style='margin:16px 0 24px 0;'>"
            f"<h5 style='margin:0 0 8px 0;color:#9a3412;'>Content comparison: <code>{html.escape(member['name'])}</code></h5>"
            f"{truncated}"
            f"<table class='comparison-table jar-content-diff'>"
            f"<thead><tr>"
            f"<th>Change</th><th>Line (previous)</th><th>Previous content</th>"
            f"<th>Line (latest)</th><th>Latest content</th>"
            f"</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody></table>"
            f"</div>"
        )

    def _archive_details_html(self, artifact_result: Dict[str, Any], archive_label: str) -> str:
        jr = artifact_result
        if not jr or not jr.get("changed"):
            return ""
        md = jr.get("member_diff", {})
        parts = []
        if md.get("added"):
            add_rows = "".join(
                f"<tr><td>{i}</td><td><code>{html.escape(n)}</code></td><td>Added in latest {html.escape(archive_label)}</td></tr>"
                for i, n in enumerate(md["added"][:50], start=1)
            )
            parts.append(
                f"<h4>Added inside {html.escape(archive_label)} ({len(md['added'])})</h4>"
                f"<table class='comparison-table'><thead><tr><th>#</th><th>Member path</th><th>Status</th></tr></thead>"
                f"<tbody>{add_rows}</tbody></table>"
            )
        if md.get("removed"):
            rem_rows = "".join(
                f"<tr><td>{i}</td><td><code>{html.escape(n)}</code></td><td>Removed in latest {html.escape(archive_label)}</td></tr>"
                for i, n in enumerate(md["removed"][:50], start=1)
            )
            parts.append(
                f"<h4>Removed from {html.escape(archive_label)} ({len(md['removed'])})</h4>"
                f"<table class='comparison-table'><thead><tr><th>#</th><th>Member path</th><th>Status</th></tr></thead>"
                f"<tbody>{rem_rows}</tbody></table>"
            )
        modified = md.get("modified") or []
        if modified:
            parts.append(
                f"<h4>Modified inside {html.escape(archive_label)} ({len(modified)}) — summary</h4>"
                + self._jar_modified_summary_table_html(modified)
            )
            parts.append(
                f"<h4 style='margin-top:20px;'>Modified inside {html.escape(archive_label)} ({len(modified)}) — contents comparison</h4>"
            )
            for idx, member in enumerate(modified, start=1):
                parts.append(self._jar_member_content_table_html(member, idx))
        class_diffs = jr.get("class_diffs") or []
        if class_diffs:
            parts.append(f"<h4>Class file diffs ({len(class_diffs)})</h4>")
            for cd in class_diffs[:5]:
                parts.append(f"<p><code>{html.escape(cd.get('class', ''))}</code></p>")
        return "".join(parts)

    def _jar_details_html(self) -> str:
        return self._archive_details_html(self.jar_result or {}, "JAR")

    def _tar_details_html(self) -> str:
        tr = self.tar_result
        if not tr or not tr.get("changed"):
            return ""
        parts = []
        if tr.get("removed"):
            items = "".join(f"<li style='color:#b91c1c;'>{html.escape(x)}</li>" for x in tr["removed"][:30])
            parts.append(f"<h4>Removed from TAR ({len(tr['removed'])})</h4><ul>{items}</ul>")
        if tr.get("added"):
            items = "".join(f"<li style='color:#166534;'>{html.escape(x)}</li>" for x in tr["added"][:30])
            parts.append(f"<h4>Added to TAR ({len(tr['added'])})</h4><ul>{items}</ul>")
        if tr.get("modified"):
            items = "".join(f"<li>{html.escape(x)}</li>" for x in tr["modified"][:30])
            parts.append(f"<h4>Modified in TAR ({len(tr['modified'])})</h4><ul>{items}</ul>")
        return "".join(parts)

    def generate_html_report(self) -> str:
        if not self.nexus_compare_mode:
            return self._brand(super().generate_html_report())

        products = self.product_comparisons or []
        total_artifacts = sum(len(p.get("artifacts") or []) for p in products)
        total_changes = sum(
            1
            for p in products
            for r in (p.get("artifacts") or [])
            if r.get("changed")
        )
        product_sections = "".join(self._product_section_html(p) for p in products)

        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>DEX Nexus Comparison Report</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 20px;
            background: linear-gradient(135deg, #7c2d12 0%, #c2410c 100%); min-height: 100vh; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #9a3412 0%, #ea580c 100%); color: white; padding: 28px 32px; }}
        .header h1 {{ margin: 0 0 8px 0; font-size: 26px; }}
        .header .subtitle {{ opacity: 0.95; font-size: 14px; }}
        .meta {{ padding: 20px 32px; background: #fff7ed; border-bottom: 1px solid #fed7aa; font-size: 13px; }}
        .meta dt {{ font-weight: 700; color: #9a3412; margin-top: 10px; }}
        .meta dd {{ margin: 4px 0 0 0; word-break: break-all; font-family: Consolas, monospace; font-size: 12px; }}
        .summary {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; padding: 20px 32px; background: #ffedd5; }}
        .summary-card {{ background: white; padding: 16px; border-radius: 10px; text-align: center; }}
        .summary-card .number {{ font-size: 28px; font-weight: 700; color: #c2410c; }}
        .summary-card .label {{ font-size: 12px; color: #64748b; margin-top: 4px; }}
        .content {{ padding: 24px 32px 32px; }}
        .section {{ margin-bottom: 28px; }}
        .section h2 {{ color: #9a3412; border-bottom: 2px solid #fdba74; padding-bottom: 8px; }}
        .comparison-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        .comparison-table th, .comparison-table td {{ border: 1px solid #e2e8f0; padding: 10px; text-align: left; }}
        .comparison-table th {{ background: #fff7ed; color: #9a3412; }}
        .jar-modified-summary td, .jar-content-diff td {{ vertical-align: top; }}
        .success-box {{ background: #f0fdf4; border: 2px solid #86efac; border-radius: 12px; padding: 20px; color: #166534; }}
        ul {{ margin: 8px 0; padding-left: 20px; font-size: 13px; }}
        .back-link {{ display: inline-block; margin-top: 24px; padding: 10px 20px; background: linear-gradient(135deg, #9a3412 0%, #ea580c 100%); color: white; text-decoration: none; border-radius: 8px; font-weight: 600; transition: transform 0.2s; }}
        .back-link:hover {{ transform: translateY(-2px); background: linear-gradient(135deg, #c2410c 0%, #f97316 100%); }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>DEX Nexus Comparison</h1>
            <div class="subtitle">Nexus download and compare — digital-playbook and digital-webapp-ces</div>
        </div>
        <div class="summary">
            <div class="summary-card"><div class="number">{total_artifacts}</div><div class="label">Artifacts compared</div></div>
            <div class="summary-card"><div class="number">{total_changes}</div><div class="label">Changed</div></div>
            <div class="summary-card"><div class="number">{total_artifacts - total_changes}</div><div class="label">Unchanged</div></div>
        </div>
        <div class="content">
            {product_sections}
            {"<div class='success-box'><h3>No differences</h3><p>All downloaded artifacts are identical (hash match).</p></div>" if total_changes == 0 and total_artifacts else ""}
            <a href="/" class="back-link">← Back to Home</a>
        </div>
    </div>
</body>
</html>"""


def run_dex_comparison(
    folder1: str,
    folder2: str,
    session_id=None,
    log_callback=None,
    progress_callback=None,
    previous_nexus_version: Optional[str] = None,
    latest_nexus_version: Optional[str] = None,
    webapp_ces_previous_version: Optional[str] = None,
    webapp_ces_latest_version: Optional[str] = None,
    **options,
) -> tuple:
    """Download from Nexus (when versions given), compare artifacts, return comparer for HTML report."""
    from report_naming import build_report_filename

    report_user = (
        options.get("report_user")
        or options.get("client_identity")
        or "unknown"
    )
    temp_work = None
    meta = None

    prev_v = (previous_nexus_version or "").strip()
    latest_v = (latest_nexus_version or "").strip()
    webapp_prev = (webapp_ces_previous_version or "").strip()
    webapp_latest = (webapp_ces_latest_version or "").strip()

    product_versions = {}
    if prev_v or latest_v:
        if not prev_v or not latest_v:
            raise ValueError(
                "Enter both previous and latest Nexus digital-playbook versions, or leave both empty."
            )
        product_versions["digital-playbook"] = (prev_v, latest_v)
    if webapp_prev or webapp_latest:
        if not webapp_prev or not webapp_latest:
            raise ValueError(
                "Enter both previous and latest Nexus digital-webapp-ces versions, or leave both empty."
            )
        product_versions["digital-webapp-ces"] = (webapp_prev, webapp_latest)

    if product_versions:
        temp_work = tempfile.mkdtemp(prefix="dex_nexus_")
        meta = prepare_dex_nexus_products(
            product_versions,
            work_root=Path(temp_work),
            log_callback=log_callback,
            progress_callback=progress_callback,
        )
        folder1 = meta["products"][0]["previous_dir"]
        folder2 = meta["products"][0]["latest_dir"]
    elif not folder1 or not folder2:
        raise ValueError(
            "Enter Nexus versions for digital-playbook and/or digital-webapp-ces, "
            "or provide Old build and New build paths."
        )

    comparer = DEXComparer(folder1 or ".", folder2 or ".")
    comparer.nexus_meta = meta
    comparer.suppress_warnings = options.get("suppress_warnings", True)
    comparer.show_missing_only = options.get("show_missing_only", False)
    comparer.ignore_timestamp_only_differences = options.get("ignore_timestamp_only", True)

    if log_callback:
        comparer.set_log_callback(log_callback)
    if progress_callback:
        comparer.set_progress_callback(progress_callback)

    path_hint = None
    if product_versions:
        if "digital-playbook" in product_versions:
            pv, lv = product_versions["digital-playbook"]
            path_hint = f"digital-playbook_{pv}_vs_{lv}"
        elif "digital-webapp-ces" in product_versions:
            pv, lv = product_versions["digital-webapp-ces"]
            path_hint = f"digital-webapp-ces_{pv}_vs_{lv}"
    _rd = Path(str(options.get("report_dir") or "."))
    output_filename = build_report_filename(
        "dex_patch_comparison",
        folder1 or "",
        folder2 or "",
        report_user,
        path_hint=path_hint,
        report_dir=_rd,
    )

    try:
        if progress_callback and not meta:
            progress_callback("Comparing builds...", 1, 1)
        results = comparer.compare_folders()
        return comparer, results, output_filename
    finally:
        if temp_work and os.path.isdir(temp_work):
            try:
                shutil.rmtree(temp_work, ignore_errors=True)
            except Exception:
                pass
