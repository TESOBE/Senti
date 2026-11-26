"""A service's container image, scanned for known vulnerabilities with Trivy, as another source of findings.

A service names its published image in senti.toml (`image = "docker.io/openbankproject/obp-api:latest"`). Senti asks
the registry for the image's digest every CHECK_SECONDS, and scans it with Trivy when the digest has changed, or when
the last scan is RESCAN_SECONDS old (new vulnerabilities are published every day). Trivy reads the image straight from
the registry: Senti needs no Docker.

Trivy tells apart what an image is built from and what the service adds to it:
- the base image: the operating system's packages (Trivy's class os-pkgs), such as Alpine's in eclipse-temurin:25-jre-alpine;
  fixed by building on a newer base image;
- the app: the libraries the service ships (lang-pkgs), such as the jars in OBP-API's app/lib; fixed by moving a
  dependency to a version that fixes them.

Senti keeps only HIGH and CRITICAL vulnerabilities that have a fixed version: a problem nobody can fix yet is not a
suggestion. Those of the base image make one finding; those of the app make one finding per library, with all its
vulnerabilities. A finding the scan no longer reports is marked fixed, unless the person has acted on it or dismissed
it. Every scan's counts, by part and severity, kept or not, are kept with the instance (`senti image`).

The scan runs in the first instance of each image. The Trivy command is SENTINEL_TRIVY_COMMAND (default `trivy`); its
vulnerability database is kept in Trivy's own cache, and its temporary files under work/ in Senti's folder.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

from .config import Config
from .home import in_home
from .store import Store, now

logger = logging.getLogger(__name__)

CHECK_SECONDS = 3600  # how often the registry is asked whether the image has changed
RESCAN_SECONDS = 86400  # an unchanged image is scanned again daily: vulnerabilities are published every day
SCAN_TIMEOUT_SECONDS = 1800  # the first scan downloads Trivy's database, which takes a while
KEPT_SEVERITIES = ("CRITICAL", "HIGH")
IMPACT = {"CRITICAL": 5, "HIGH": 4}
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
KEY_PREFIX = "image:"
BASE, APP = "base image", "app"
PART = {"os-pkgs": BASE, "lang-pkgs": APP}
STATE_KEY = "image_scan"
LISTED = 12  # vulnerabilities named in a finding's evidence, at most
TEMP_DIR = "work/trivy-tmp"  # Trivy copies files out of each layer: on disk, not in a small /tmp
MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"))
REFERENCE = re.compile(r"^(?:(?P<registry>[^/]+\.[^/]+|localhost(?::\d+)?)/)?(?P<repository>[a-z0-9]+(?:(?:[._]|__|-+|/)[a-z0-9]+)*)"
                       r"(?::(?P<tag>[\w][\w.-]{0,127}))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?$")


def trivy_command() -> str:
    return os.environ.get("SENTINEL_TRIVY_COMMAND", "trivy")


def unavailable_reason() -> str | None:
    """Why Senti cannot scan images here, or None."""
    if not shutil.which(trivy_command()):
        return f"Trivy is not installed ({trivy_command()} is not found): see https://trivy.dev"
    return None


def parse_reference(image: str) -> tuple[str, str, str]:
    """An image reference as (registry host, repository, tag or digest): Docker Hub's names filled in."""
    m = REFERENCE.match(image.strip())
    if not m:
        raise ValueError(f"{image!r} is not an image reference such as docker.io/openbankproject/obp-api:latest")
    registry = m["registry"] or "docker.io"
    repository = m["repository"]
    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"
        if "/" not in repository:
            repository = f"library/{repository}"
    return registry, repository, m["digest"] or m["tag"] or "latest"


def registry_digest(image: str) -> str:
    """The digest the registry serves for the image now, asked anonymously (as `docker pull` would for a public
    image); raises httpx.HTTPError or ValueError when it cannot be told."""
    registry, repository, reference = parse_reference(image)
    if reference.startswith("sha256:"):
        return reference
    url = f"https://{registry}/v2/{repository}/manifests/{reference}"
    headers = {"Accept": MANIFEST_TYPES}
    response = httpx.head(url, headers=headers, timeout=30)
    if response.status_code == 401:  # the registry wants a token, even for a public image: ask for one
        challenge = response.headers.get("WWW-Authenticate", "")
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if not challenge.lower().startswith("bearer") or "realm" not in fields:
            raise ValueError(f"{registry} asks for a login Senti does not have")
        params = {k: fields[k] for k in ("service", "scope") if k in fields}
        params.setdefault("scope", f"repository:{repository}:pull")
        token = httpx.get(fields["realm"], params=params, timeout=30)
        token.raise_for_status()
        headers["Authorization"] = "Bearer " + (token.json().get("token") or token.json().get("access_token", ""))
        response = httpx.head(url, headers=headers, timeout=30)
    response.raise_for_status()
    if not (digest := response.headers.get("Docker-Content-Digest")):
        raise ValueError(f"{registry} did not say the digest of {image}")
    return digest


def scan(image: str) -> dict:
    """Trivy's report on the image, as JSON. Raises RuntimeError when Trivy fails."""
    temp = Path(in_home(TEMP_DIR))
    temp.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(
        [trivy_command(), "image", "--quiet", "--scanners", "vuln", "--image-src", "remote", "--format", "json", image],
        capture_output=True, text=True, timeout=SCAN_TIMEOUT_SECONDS, env={**os.environ, "TMPDIR": str(temp)})
    if out.returncode != 0:
        raise RuntimeError(f"Trivy could not scan {image}: {(out.stderr or out.stdout).strip()[-1000:]}")
    return json.loads(out.stdout)


def vulnerabilities(report: dict) -> list[dict]:
    """Every vulnerability in Trivy's report, as {part, target, package, installed, fixed, id, severity, title, path,
    url}: part is BASE or APP."""
    found = []
    for result in report.get("Results") or []:
        part = PART.get(result.get("Class", ""))
        if not part:
            continue
        for v in result.get("Vulnerabilities") or []:
            found.append({"part": part, "target": result.get("Target", ""), "package": v.get("PkgName", ""),
                          "installed": v.get("InstalledVersion", ""), "fixed": v.get("FixedVersion", ""),
                          "id": v.get("VulnerabilityID", ""), "severity": v.get("Severity", "UNKNOWN"),
                          "title": (v.get("Title") or "").strip(), "path": v.get("PkgPath", ""),
                          "url": v.get("PrimaryURL", "")})
    return found


def counts(found: list[dict]) -> dict:
    """How many vulnerabilities each part has, by severity, and how many of them have a fixed version."""
    out = {}
    for part in (BASE, APP):
        mine = [v for v in found if v["part"] == part]
        out[part] = {"total": len(mine), "fixable": sum(1 for v in mine if v["fixed"]),
                     **{s.lower(): sum(1 for v in mine if v["severity"] == s) for s in SEVERITIES}}
    return out


def _kept(v: dict) -> bool:
    return v["severity"] in KEPT_SEVERITIES and bool(v["fixed"])


def _listed(items: list[dict]) -> list[str]:
    items = sorted(items, key=lambda v: (-IMPACT[v["severity"]], v["id"]))
    lines = [f"- {v['id']} ({v['severity'].lower()}) in {v['package']} {v['installed']}, fixed in {v['fixed']}"
             + (f": {v['title']}" if v["title"] else "") + (f" {v['url']}" if v["url"] else "") for v in items[:LISTED]]
    return lines + ([f"- and {len(items) - LISTED} more"] if len(items) > LISTED else [])


def findings(image: str, digest: str, os_name: str, found: list[dict], dockerfiles: list[str]) -> list[dict]:
    """The base image's kept vulnerabilities as one finding, and the app's as one finding per library."""
    kept = [v for v in found if _kept(v)]
    scanned = f"Trivy ({image}) reports, in the image at {digest[:19]}"
    made = []
    base = [v for v in kept if v["part"] == BASE]
    if base:
        worst = max(IMPACT[v["severity"]] for v in base)
        packages = sorted({v["package"] for v in base})
        made.append({
            "key": KEY_PREFIX + hashlib.sha1(f"{image}|base".encode()).hexdigest()[:16],
            "category": "security",
            "title": f"The base image ({os_name}) has {len(base)} known vulnerabilit{'ies' if len(base) != 1 else 'y'} "
                     f"with a fix",
            "summary": f"The operating system packages the image is built on ({', '.join(packages)}) have known "
                       "vulnerabilities that newer versions fix. They come with the base image, not with the service's "
                       "own code.",
            "evidence": "\n".join([f"{scanned}, in the base image ({os_name}):"] + _listed(base)),
            "hypothesis": "The image was built on a base image whose packages have since been fixed.",
            "files": dockerfiles,
            "signature_ids": [],
            "suggested_fix": "Build the image again on the latest base image (its tag may already point at a fixed "
                             f"one), or move the Dockerfile's FROM to a newer base. Packages to update: "
                             + ", ".join(f"{p} to {sorted({v['fixed'] for v in base if v['package'] == p})[-1]}"
                                         for p in packages) + ".",
            "impact": worst, "confidence": 0.8, "effort": 1, "trend": "stable"})
    libraries: dict[str, list[dict]] = {}
    for v in kept:
        if v["part"] == APP:
            libraries.setdefault(v["package"], []).append(v)
    for package, items in libraries.items():
        worst = max(IMPACT[v["severity"]] for v in items)
        installed = sorted({v["installed"] for v in items})
        fixed = sorted({f.strip() for v in items for f in v["fixed"].split(",")})
        paths = sorted({v["path"] for v in items if v["path"]})
        ids = sorted({v["id"] for v in items})
        made.append({
            "key": KEY_PREFIX + hashlib.sha1(f"{image}|{package}".encode()).hexdigest()[:16],
            "category": "security",
            "title": f"{package} {', '.join(installed)} has {len(ids)} known vulnerabilit{'ies' if len(ids) != 1 else 'y'}",
            "summary": f"The service ships the library {package} at a version with known vulnerabilities that a newer "
                       "version fixes. Whether the service's own code reaches them is not checked: read how it uses "
                       "the library first.",
            "evidence": "\n".join([f"{scanned}, in the app's libraries ({', '.join(paths) or 'the app'}):"]
                                  + _listed(items)),
            "hypothesis": f"{package} {', '.join(installed)} comes in through the build's dependencies, directly or "
                          "through another library.",
            "files": ["the build's dependency file (pom.xml, build.sbt, package.json ...)"],
            "signature_ids": [],
            "suggested_fix": f"Move {package} to a version that fixes them ({' or '.join(fixed)}): if it comes in "
                             "through another library, move that one, or pin this one in the build.",
            "impact": worst, "confidence": 0.7, "effort": 2, "trend": "stable"})
    return made


def record(config: Config, image: str, made: list[dict]) -> dict:
    """Add or update the findings; mark fixed those of this image the scan no longer reports, unless acted on or
    dismissed. Returns how many were added, updated and marked fixed."""
    store = Store(config.db_path)
    try:
        keys = {f["key"] for f in made}
        before = {r["key"] for r in store.db.execute("SELECT key FROM findings WHERE key LIKE ?", (KEY_PREFIX + "%",))}
        for f in made:
            store.upsert_finding({**f, "trend": "stable" if f["key"] in before else "new"})
        gone = [r["id"] for r in store.db.execute(
            "SELECT id, key FROM findings WHERE key LIKE ? AND evidence LIKE ? "
            "AND status IN ('open', 'suggested', 'accepted', 'later')",
            (KEY_PREFIX + "%", f"Trivy ({image})%")) if r["key"] not in keys]
        for finding_id in gone:
            store.add_feedback(finding_id, "fixed", "Trivy no longer reports it in the image.", 0, from_sentinel=True)
        store.commit()
    finally:
        store.close()
    return {"added": len(keys - before), "updated": len(keys & before), "fixed": len(gone)}


def _dockerfiles(source: str) -> list[str]:
    """The Dockerfiles in the service's checkout, where the base image is named."""
    if not source:
        return ["Dockerfile"]
    out = subprocess.run(["git", "-C", source, "ls-files"], capture_output=True, text=True)
    found = [p for p in out.stdout.splitlines() if re.search(r"(^|/)Dockerfile[^/]*$", p)]
    return found or ["Dockerfile"]


def check(config: Config, force: bool = False) -> dict:
    """Scan the instance's image when it has changed or the last scan is a day old (or `force`), and bring its
    findings up to date. Returns what was done, kept with the instance."""
    image = config.image
    store = Store(config.db_path)
    try:
        last = store.get_state(STATE_KEY) or {}
    finally:
        store.close()
    try:
        digest = registry_digest(image)
    except (httpx.HTTPError, ValueError) as e:  # scan all the same, on the daily schedule
        logger.info("Could not ask the registry for %s's digest: %s", image, e)
        digest = None
    same = last.get("image") == image and digest and last.get("digest") == digest
    if not force and last.get("ok") and (same or digest is None) and now() - last.get("scanned_at", 0) < RESCAN_SECONDS:
        return {**last, "skipped": True}
    report = scan(image)
    found = vulnerabilities(report)
    digest = digest or next(iter((report.get("Metadata") or {}).get("RepoDigests") or []), "").rsplit("@", 1)[-1]
    os_info = (report.get("Metadata") or {}).get("OS") or {}
    os_name = " ".join(x for x in (os_info.get("Family"), os_info.get("Name")) if x) or "its operating system"
    done = record(config, image, findings(image, digest or "?", os_name, found, _dockerfiles(config.obp_api_source)))
    result = {"ok": True, "image": image, "digest": digest, "os": os_name, "scanned_at": now(),
              "counts": counts(found), "kept": sum(1 for v in found if _kept(v)), **done}
    store = Store(config.db_path)
    try:
        store.set_state(STATE_KEY, result)
        store.commit()
    finally:
        store.close()
    return result


def describe(result: dict) -> str:
    """A scan's result in words, base image and app apart."""
    lines = [f"{result['image']} ({result.get('digest') or 'digest unknown'}), on {result['os']}:"]
    for part, c in result["counts"].items():
        by = ", ".join(f"{c[s.lower()]} {s.lower()}" for s in SEVERITIES if c[s.lower()])
        lines.append(f"- {part}: {c['total']} known vulnerabilities" + (f" ({by}), {c['fixable']} with a fix" if c["total"] else ""))
    lines.append(f"Kept as findings (high or critical, with a fix): {result['kept']}. "
                 f"Findings {result['added']} added, {result['updated']} updated, {result['fixed']} marked fixed.")
    return "\n".join(lines)


def instances(configs: list[Config]) -> list[Config]:
    """For each image, the first instance whose service names it."""
    seen, chosen = set(), []
    for c in configs:
        if c.image and c.image not in seen:
            seen.add(c.image)
            chosen.append(c)
    return chosen


def check_forever(configs: list[Config], stopped: threading.Event) -> None:
    chosen = instances(configs)
    if not chosen:
        return
    if reason := unavailable_reason():
        logger.warning("Not scanning container images: %s", reason)
        return
    delay = 60  # the first check soon after Senti starts
    while not stopped.wait(delay):
        delay = CHECK_SECONDS
        for config in chosen:
            try:
                result = check(config)
                if not result.get("skipped"):
                    logger.info("Scanned %s (%s): %s kept, %s added, %s fixed", config.image, config.name,
                                result["kept"], result["added"], result["fixed"])
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
                logger.warning("Could not scan %s: %s", config.image, e)
