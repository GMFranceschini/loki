"""Config loading, validation and comment-preserving saves."""

import pytest
import yaml

from loki.config import ConfigError, load_config, save_topics, validate_topic_payload

GOOD_PROSE = "CRISPR genome editing and base editing in primary human cells and tissues."


def write(path, document):
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


@pytest.fixture
def config_file(tmp_path):
    return write(
        tmp_path / "interests.yaml",
        {
            "topics": [
                {"name": "Genome editing", "weight": 1.0, "prose": GOOD_PROSE, "pubmed": "CRISPR[tiab]"}
            ],
            "scoring": {"relevance_weight": 0.6, "impact_weight": 0.4},
            "sources": {"pubmed": {"max_per_topic": 50}, "biorxiv": {"servers": ["biorxiv"]}},
        },
    )


# ---------------------------------------------------------------- loading


def test_loads_topics_and_settings(config_file):
    config = load_config(config_file)
    assert config.topic_names == ["Genome editing"]
    assert config.scoring.relevance_weight == 0.6
    assert config.pubmed.max_per_topic == 50
    assert config.biorxiv.servers == ("biorxiv",)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="No config file"):
        load_config(tmp_path / "absent.yaml")


def test_no_topics_is_reported(tmp_path):
    path = write(tmp_path / "c.yaml", {"topics": []})
    with pytest.raises(ConfigError, match="no topics"):
        load_config(path)


def test_topic_without_prose_is_reported(tmp_path):
    path = write(tmp_path / "c.yaml", {"topics": [{"name": "X", "weight": 1}]})
    with pytest.raises(ConfigError, match="no prose"):
        load_config(path)


def test_duplicate_topic_names_are_reported(tmp_path):
    path = write(
        tmp_path / "c.yaml",
        {"topics": [{"name": "A", "prose": GOOD_PROSE}, {"name": "A", "prose": GOOD_PROSE}]},
    )
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(path)


def test_invalid_yaml_is_reported(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("topics: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_unknown_keys_are_ignored(tmp_path):
    path = write(
        tmp_path / "c.yaml",
        {"topics": [{"name": "A", "prose": GOOD_PROSE}], "scoring": {"made_up_key": 1}},
    )
    assert load_config(path).scoring.relevance_weight == 0.7  # the default


def test_fingerprint_changes_with_topics(config_file, tmp_path):
    first = load_config(config_file).fingerprint()
    write(
        tmp_path / "interests.yaml",
        {"topics": [{"name": "Other", "weight": 1.0, "prose": GOOD_PROSE}]},
    )
    assert load_config(config_file).fingerprint() != first


# ---------------------------------------------------------------- validation


def test_valid_payload_is_cleaned():
    cleaned = validate_topic_payload(
        [{"name": "  Spaced  name ", "weight": "2", "prose": f"  {GOOD_PROSE}  "}]
    )
    assert cleaned[0]["name"] == "Spaced name"
    assert cleaned[0]["weight"] == 2.0


@pytest.mark.parametrize(
    "payload,message",
    [
        ([], "at least one topic"),
        ("not a list", "at least one topic"),
        ([{"name": "", "prose": GOOD_PROSE}], "needs a name"),
        ([{"name": "A", "prose": "tiny"}], "longer description"),
        ([{"name": "A", "prose": GOOD_PROSE}, {"name": "a", "prose": GOOD_PROSE}], "unique"),
        ([{"name": "A", "prose": GOOD_PROSE, "weight": 0}], "above 0"),
        ([{"name": "A", "prose": GOOD_PROSE, "weight": "heavy"}], "must be a number"),
        ([{"name": "A" * 100, "prose": GOOD_PROSE}], "too long"),
    ],
)
def test_bad_payloads_are_rejected(payload, message):
    with pytest.raises(ConfigError, match=message):
        validate_topic_payload(payload)


# ---------------------------------------------------------------- saving


def test_save_round_trips(tmp_path):
    path = tmp_path / "interests.yaml"
    path.write_text(
        "# A leading comment\ntopics:\n"
        f"  - name: Old\n    weight: 1.0\n    prose: {GOOD_PROSE}\n"
        "scoring:\n  # keep me\n  relevance_weight: 0.55\n  impact_weight: 0.45\n",
        encoding="utf-8",
    )
    save_topics([{"name": "New", "weight": 2.0, "prose": GOOD_PROSE, "pubmed": "x[tiab]"}], path)

    reloaded = load_config(path)
    assert reloaded.topic_names == ["New"]
    assert reloaded.topics[0].weight == 2.0
    # Settings outside the topics list survive untouched.
    assert reloaded.scoring.relevance_weight == 0.55


def test_save_preserves_comments(tmp_path):
    path = tmp_path / "interests.yaml"
    path.write_text(
        "# A leading comment\ntopics:\n"
        f"  - name: Old\n    prose: {GOOD_PROSE}\n"
        "scoring:\n  # keep me\n  relevance_weight: 0.5\n",
        encoding="utf-8",
    )
    save_topics([{"name": "New", "weight": 1.0, "prose": GOOD_PROSE}], path)
    text = path.read_text()
    assert "# A leading comment" in text
    assert "# keep me" in text


def test_save_writes_a_backup(tmp_path):
    path = tmp_path / "interests.yaml"
    path.write_text(f"topics:\n  - name: Old\n    prose: {GOOD_PROSE}\n", encoding="utf-8")
    save_topics([{"name": "New", "weight": 1.0, "prose": GOOD_PROSE}], path)
    backup = path.with_suffix(".yaml.bak")
    assert backup.exists()
    assert "Old" in backup.read_text()


def test_rejected_save_leaves_the_file_alone(tmp_path):
    path = tmp_path / "interests.yaml"
    original = f"topics:\n  - name: Old\n    prose: {GOOD_PROSE}\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError):
        save_topics([{"name": "Bad", "prose": "short"}], path)
    assert path.read_text() == original
    assert not path.with_suffix(".yaml.tmp").exists()


def test_save_creates_a_file_that_did_not_exist(tmp_path):
    path = tmp_path / "fresh.yaml"
    save_topics([{"name": "New", "weight": 1.0, "prose": GOOD_PROSE}], path)
    assert load_config(path).topic_names == ["New"]
