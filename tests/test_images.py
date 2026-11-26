from dataclasses import replace

import pytest

from senti import images
from senti.store import Store

IMAGE = "docker.io/openbankproject/obp-api:latest"
DIGEST = "sha256:" + "6b" * 32


def vuln(vid, package, installed, fixed, severity, path=""):
    return {"VulnerabilityID": vid, "PkgName": package, "InstalledVersion": installed, "FixedVersion": fixed,
            "Severity": severity, "Title": f"{package} problem", "PkgPath": path,
            "PrimaryURL": f"https://avd.aquasec.com/nvd/{vid.lower()}"}


def report(base=None, app=None):
    return {"Metadata": {"OS": {"Family": "alpine", "Name": "3.24.4"}, "RepoDigests": [f"openbankproject/obp-api@{DIGEST}"]},
            "Results": [
                {"Target": f"{IMAGE} (alpine 3.24.4)", "Class": "os-pkgs", "Type": "alpine", "Vulnerabilities": base or []},
                {"Target": "Java", "Class": "lang-pkgs", "Type": "jar", "Vulnerabilities": app or []},
            ]}


BASE = [vuln("CVE-2026-1", "zlib", "1.3.2-r1", "1.4-r0", "HIGH"),
        vuln("CVE-2026-2", "libexpat", "2.10.0-r0", "2.11.0-r0", "MEDIUM")]
APP = [vuln("CVE-2026-54291", "org.postgresql:postgresql", "42.7.11", "42.7.12", "HIGH", "app/lib/postgresql-42.7.11.jar"),
       vuln("CVE-2026-68497", "com.fasterxml.jackson.core:jackson-databind", "2.18.7", "2.21.7, 2.18.11", "CRITICAL",
            "app/lib/jackson-databind-2.18.7.jar"),
       vuln("CVE-2026-91776", "com.fasterxml.jackson.core:jackson-databind", "2.18.7", "2.18.11", "HIGH",
            "app/lib/jackson-databind-2.18.7.jar"),
       vuln("CVE-2026-3", "org.jsoup:jsoup", "1.18.3", "", "HIGH", "app/lib/jsoup-1.18.3.jar"),  # no fix yet
       vuln("CVE-2026-4", "com.google.guava:guava", "32.0.0-jre", "33.7.2", "MEDIUM")]


@pytest.fixture
def scanned(config, monkeypatch):
    cfg = replace(config, image=IMAGE, obp_api_source="")
    state = {"report": report(BASE, APP), "digest": DIGEST, "scans": 0}

    def scan(image):
        state["scans"] += 1
        return state["report"]

    monkeypatch.setattr(images, "scan", scan)
    monkeypatch.setattr(images, "registry_digest", lambda image: state["digest"])
    return cfg, state


def rows(cfg):
    store = Store(cfg.db_path)
    try:
        return {r["title"]: dict(r) for r in store.findings()}
    finally:
        store.close()


def test_image_references_are_read_as_docker_would():
    assert images.parse_reference(IMAGE) == ("registry-1.docker.io", "openbankproject/obp-api", "latest")
    assert images.parse_reference("postgres") == ("registry-1.docker.io", "library/postgres", "latest")
    assert images.parse_reference("ghcr.io/tesobe/senti:1.2") == ("ghcr.io", "tesobe/senti", "1.2")
    assert images.parse_reference(f"quay.io/a/b@{DIGEST}") == ("quay.io", "a/b", DIGEST)
    with pytest.raises(ValueError):
        images.parse_reference("not a reference")


def test_base_image_and_app_are_told_apart_and_counted():
    found = images.vulnerabilities(report(BASE, APP))
    counts = images.counts(found)
    assert counts[images.BASE] == {"total": 2, "fixable": 2, "critical": 0, "high": 1, "medium": 1, "low": 0, "unknown": 0}
    assert counts[images.APP]["total"] == 5 and counts[images.APP]["fixable"] == 4 and counts[images.APP]["critical"] == 1


def test_high_and_critical_with_a_fix_become_one_base_finding_and_one_per_library(scanned):
    cfg, state = scanned
    result = images.check(cfg)
    assert (result["kept"], result["added"], result["os"], result["digest"]) == (4, 3, "alpine 3.24.4", DIGEST)
    found = rows(cfg)
    base = found["The base image (alpine 3.24.4) has 1 known vulnerability with a fix"]
    assert "CVE-2026-1" in base["evidence"] and "libexpat" not in base["evidence"] and "zlib to 1.4-r0" in base["suggested_fix"]
    jackson = found["com.fasterxml.jackson.core:jackson-databind 2.18.7 has 2 known vulnerabilities"]
    assert jackson["impact"] == 5 and jackson["category"] == "security" and "2.18.11" in jackson["suggested_fix"]
    assert jackson["evidence"].startswith(f"Trivy ({IMAGE}) reports") and "app/lib/jackson-databind-2.18.7.jar" in jackson["evidence"]
    assert "org.postgresql:postgresql 42.7.11 has 1 known vulnerability" in found
    assert not any("jsoup" in t or "guava" in t for t in found)  # no fix yet, or only medium


def test_an_unchanged_image_is_not_scanned_again_until_a_day_has_passed(scanned, monkeypatch):
    cfg, state = scanned
    images.check(cfg)
    assert images.check(cfg)["skipped"] and state["scans"] == 1
    state["digest"] = "sha256:" + "7c" * 32  # a new image was published
    images.check(cfg)
    assert state["scans"] == 2
    now = images.now()
    monkeypatch.setattr(images, "now", lambda: now + images.RESCAN_SECONDS + 1)
    images.check(cfg)
    assert state["scans"] == 3


def test_what_the_scan_no_longer_reports_is_marked_fixed_unless_dismissed(scanned):
    cfg, state = scanned
    images.check(cfg)
    found = rows(cfg)
    store = Store(cfg.db_path)
    store.add_feedback(found["org.postgresql:postgresql 42.7.11 has 1 known vulnerability"]["id"], "dismissed", "not used", 7)
    store.commit()
    store.close()
    state["report"] = report([], APP[1:3])  # a new base image, and postgresql moved on
    result = images.check(cfg, force=True)
    found = rows(cfg)
    assert result["fixed"] == 1 and found["The base image (alpine 3.24.4) has 1 known vulnerability with a fix"]["status"] == "fixed"
    assert found["org.postgresql:postgresql 42.7.11 has 1 known vulnerability"]["status"] == "dismissed"
    assert found["com.fasterxml.jackson.core:jackson-databind 2.18.7 has 2 known vulnerabilities"]["status"] != "fixed"


def test_the_last_scan_is_described_with_both_parts(scanned):
    cfg, _ = scanned
    text = images.describe(images.check(cfg))
    assert "- base image: 2 known vulnerabilities (1 high, 1 medium), 2 with a fix" in text
    assert "- app: 5 known vulnerabilities (1 critical, 3 high, 1 medium), 4 with a fix" in text
    assert "Kept as findings (high or critical, with a fix): 4." in text


def test_each_image_is_scanned_in_its_first_instance(config):
    a, b, c = replace(config, name="a", image=IMAGE), replace(config, name="b", image=IMAGE), replace(config, name="c")
    assert images.instances([a, b, c]) == [a]


def test_the_image_is_set_on_the_service(tmp_path, monkeypatch):
    from senti.config import Config
    monkeypatch.chdir(tmp_path)
    (tmp_path / "senti.toml").write_text(f'''
[[service]]
name = "obp-api"
kind = "obp-api"
image = "{IMAGE}"

[[environment]]
name = "local"

[[instance]]
service = "obp-api"
environment = "local"
url = "http://localhost:8080"
discover = false
''')
    assert Config.from_env("obp-api-local").image == IMAGE
