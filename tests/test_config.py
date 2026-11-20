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


# ---------------------------------------------------------------- padding


def test_padding_accepts_a_single_number(tmp_path):
    from loki.config import _parse_padding

    assert _parse_padding(10) == (10, ())


def test_padding_accepts_a_mapping_per_window():
    from loki.config import _parse_padding

    default, overrides = _parse_padding({"day": 5, "week": 10, "month": 10})
    assert dict(overrides) == {"day": 5, "week": 10, "month": 10}
    assert default == 0


def test_padding_mapping_supports_a_default():
    from loki.config import _parse_padding

    default, overrides = _parse_padding({"day": 5, "default": 10})
    assert default == 10
    assert dict(overrides) == {"day": 5}


def test_padding_rejects_a_negative_number():
    from loki.config import ConfigError, _parse_padding

    with pytest.raises(ConfigError, match="cannot be negative"):
        _parse_padding(-5)
    with pytest.raises(ConfigError, match="cannot be negative"):
        _parse_padding({"day": -1})


def test_padding_rejects_an_unknown_window():
    from loki.config import ConfigError, _parse_padding

    with pytest.raises(ConfigError, match="unknown window"):
        _parse_padding({"fortnight": 14})


def test_padding_rejects_nonsense():
    from loki.config import ConfigError, _parse_padding

    with pytest.raises(ConfigError, match="whole number"):
        _parse_padding("soon")


def test_scoring_reports_padding_per_window():
    from loki.config import Scoring

    scoring = Scoring(window_padding_days=10, window_padding=(("day", 5),))
    assert scoring.padding_by_window == {"day": 5, "week": 10, "month": 10}
    assert scoring.padding_for("day") == 5
    assert scoring.padding_for("month") == 10


# ---------------------------------------------------------------- weights


def test_weights_are_stored_normalised():
    from loki.config import validate_weights

    assert validate_weights({"relevance_weight": 55, "impact_weight": 45}) == {
        "relevance_weight": 0.55, "impact_weight": 0.45
    }


def test_no_weights_means_leave_them_alone():
    from loki.config import validate_weights

    assert validate_weights(None) is None
    assert validate_weights({}) is None


def test_weights_must_come_as_a_pair():
    from loki.config import ConfigError, validate_weights

    with pytest.raises(ConfigError, match="both"):
        validate_weights({"relevance_weight": 1.0})


def test_weights_cannot_both_be_zero():
    from loki.config import ConfigError, validate_weights

    with pytest.raises(ConfigError, match="some weight"):
        validate_weights({"relevance_weight": 0, "impact_weight": 0})


def test_weights_reject_the_negative_and_the_unreadable():
    from loki.config import ConfigError, validate_weights

    with pytest.raises(ConfigError, match="negative"):
        validate_weights({"relevance_weight": -1, "impact_weight": 2})
    with pytest.raises(ConfigError, match="must be a number"):
        validate_weights({"relevance_weight": "lots", "impact_weight": 1})


def test_one_side_may_carry_everything():
    """Ranking by topic match alone is a legitimate choice."""
    from loki.config import validate_weights

    assert validate_weights({"relevance_weight": 1, "impact_weight": 0}) == {
        "relevance_weight": 1.0, "impact_weight": 0.0
    }


def test_saving_weights_keeps_the_comments_and_the_rest(tmp_path):
    import shutil
    from pathlib import Path

    from loki.config import load_config, save_topics

    path = tmp_path / "interests.yaml"
    shutil.copy2(Path("config/interests.yaml"), path)
    before = load_config(path)
    topics = [
        {"name": t.name, "weight": t.weight, "prose": t.prose, "pubmed": t.pubmed}
        for t in before.topics
    ]

    save_topics(topics, path, weights={"relevance_weight": 0.4, "impact_weight": 0.6})

    after = load_config(path)
    assert (after.scoring.relevance_weight, after.scoring.impact_weight) == (0.4, 0.6)
    # Everything else in the file survives, comments included.
    assert after.scoring.padding_by_window == before.scoring.padding_by_window
    assert after.storage.retention_days == before.storage.retention_days
    assert "# Final score = relevance_weight" in path.read_text()
