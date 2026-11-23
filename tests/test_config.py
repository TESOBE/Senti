import pytest

from senti.config import Config, ConfigError
from senti.home import in_home


def test_one_default_instance_without_a_list(config, monkeypatch):
    monkeypatch.delenv("SENTINEL_INSTANCES", raising=False)
    [c] = Config.all_from_env()
    assert c.name == "default" and c.db_path == in_home("sentinel.db") and c.digest_dir == in_home("digests")


def test_each_instance_takes_prefixed_settings_or_the_plain_ones(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "local, staging-eu")
    monkeypatch.setenv("OBP_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("STAGING_EU_OBP_BASE_URL", "https://staging.example.com")
    monkeypatch.setenv("STAGING_EU_OBP_API_SOURCE", "/src/staging")
    local, staging = Config.all_from_env()
    assert (local.name, local.obp_base_url, local.db_path) == ("local", "http://localhost:8080", in_home("sentinel-local.db"))
    assert staging.obp_base_url == "https://staging.example.com" and staging.obp_api_source == "/src/staging"
    assert staging.digest_dir == in_home("digests/staging-eu")


def test_bad_instance_names_are_refused(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "Local Box")
    with pytest.raises(ConfigError):
        Config.all_from_env()


def test_instances_never_share_a_database(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "a,b")
    monkeypatch.setenv("SENTINEL_DB", "sentinel.db")
    monkeypatch.setenv("B_SENTINEL_DB", "/data/b.db")
    a, b = Config.all_from_env()
    assert (a.db_path, b.db_path) == (in_home("sentinel-a.db"), "/data/b.db")  # in Senti's folder, unless absolute


# The OBP-API versions Sentinel builds its paths from: each setting, with the default it has. Sentinel is a test
# tool, so a setting may name any version; there is no lowest one.
VERSION_SETTINGS = {
    "OBP_LOG_CACHE_API_VERSION": ("log_cache_api_version", "v5.1.0"),
    "OBP_TELEMETRY_API_VERSION": ("telemetry_api_version", "v7.0.0"),
    "OBP_AGGREGATE_METRICS_API_VERSION": ("aggregate_metrics_api_version", "v6.0.0"),
    "OBP_ROOT_API_VERSION": ("root_api_version", "v5.1.0"),
}


@pytest.mark.parametrize("setting", VERSION_SETTINGS)
def test_a_version_setting_has_a_default(config, monkeypatch, setting):
    monkeypatch.delenv(setting, raising=False)
    field, default = VERSION_SETTINGS[setting]
    assert getattr(Config.from_env(), field) == default


@pytest.mark.parametrize("setting", VERSION_SETTINGS)
@pytest.mark.parametrize("version", ["v3.1.0", "v4.0.0", "v5.0.0", "v6.0.0", "v7.0.0", "v10.0.0"])
def test_a_version_setting_takes_any_version_as_written(config, monkeypatch, setting, version):
    monkeypatch.setenv(setting, version)
    assert getattr(Config.from_env(), VERSION_SETTINGS[setting][0]) == version


def test_the_version_of_an_instance_takes_its_own_prefixed_setting(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "local, staging")
    monkeypatch.setenv("OBP_TELEMETRY_API_VERSION", "v7.0.0")
    monkeypatch.setenv("STAGING_OBP_TELEMETRY_API_VERSION", "v4.0.0")
    local, staging = Config.all_from_env()
    assert (local.telemetry_api_version, staging.telemetry_api_version) == ("v7.0.0", "v4.0.0")



def test_paths_are_in_sentis_folder_whatever_folder_it_runs_from(monkeypatch, tmp_path):
    """Claude once ran `senti digest` from a service's checkout, and Senti made empty databases there."""
    senti, elsewhere = tmp_path / "senti", tmp_path / "service"
    (senti / ".claude/agents").mkdir(parents=True)
    (senti / ".claude/agents/senti-analyst.md").write_text("agent")
    (senti / ".env").write_text("SENTINEL_INSTANCES=local\n")
    elsewhere.mkdir()
    monkeypatch.delenv("SENTINEL_INSTANCES", raising=False)
    monkeypatch.chdir(senti / ".claude")  # inside Senti's folder: found by its agent files
    monkeypatch.delenv("SENTI_HOME")
    assert in_home("x.db") == str(senti / "x.db")
    monkeypatch.setenv("SENTI_HOME", str(senti))  # as `senti` sets it for what it starts
    monkeypatch.chdir(elsewhere)
    [local] = Config.all_from_env()  # its .env is read from Senti's folder too
    assert local.db_path == str(senti / "sentinel-local.db") and local.digest_dir == str(senti / "digests/local")


def test_senti_will_not_run_outside_its_folder(monkeypatch, tmp_path):
    from senti.home import remember

    monkeypatch.delenv("SENTI_HOME")
    monkeypatch.chdir(tmp_path)
    assert "is not in Senti's folder" in remember()
    (tmp_path / "senti.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    assert remember() is None and __import__("os").environ["SENTI_HOME"] == str(tmp_path.resolve())
