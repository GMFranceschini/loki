"""Load and validate config/interests.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from .paths import INTERESTS_FILE


class ConfigError(Exception):
    """The config file is missing or malformed."""


@dataclass(frozen=True)
class Topic:
    name: str
    weight: float
    prose: str
    pubmed: str

    @property
    def embed_text(self) -> str:
        """Combined text, kept for reference and for the hashing fallback."""
        return f"{self.name}. {self.prose}".strip()


@dataclass(frozen=True)
class Scoring:
    relevance_weight: float = 0.7
    impact_weight: float = 0.3
    sim_floor: float = 0.10
    sim_ceiling: float = 0.40
    best_topic_share: float = 0.8
    min_relevance: float = 0.15

    # How much the topic NAME counts in its embedding, against its prose.
    # The name is short but precise ("Hi-C"), the prose is long but diffuse.
    topic_name_weight: float = 0.35

    # Tagging. A tag is only shown when the topic genuinely matches, so a
    # paper can carry several tags or none at all.
    tag_min_sim: float = 0.20      # absolute floor on centred similarity
    tag_relative: float = 0.80     # a second tag must reach this share of the best
    max_tags: int = 3
    impact_factor_cap: float = 60.0
    preprint_impact: float = 0.33
    unknown_journal_impact: float = 0.20

    # Digest windows are widened by this many days on both sides, because a
    # paper worth reading today was often posted or indexed a little earlier,
    # and journals backdate. 0 restores strict calendar windows.
    window_padding_days: int = 10


@dataclass(frozen=True)
class PubMedConfig:
    enabled: bool = True
    max_per_topic: int = 200
    contact_email: str = ""
    api_key: str = ""

    def resolved_api_key(self) -> str:
        return os.environ.get("NCBI_API_KEY", "").strip() or self.api_key.strip()


@dataclass(frozen=True)
class BioRxivConfig:
    enabled: bool = True
    servers: tuple[str, ...] = ("biorxiv", "medrxiv")
    categories: tuple[str, ...] = ()
    max_per_run: int = 3000


@dataclass(frozen=True)
class SummaryConfig:
    """Settings for the compact summary shown when a card is expanded."""

    enabled: bool = True
    max_sentences: int = 3
    max_chars: int = 420


@dataclass(frozen=True)
class EmbeddingConfig:
    backend: str = "model2vec"
    model: str = "minishlab/potion-base-8M"


@dataclass(frozen=True)
class Config:
    topics: tuple[Topic, ...]
    scoring: Scoring = field(default_factory=Scoring)
    pubmed: PubMedConfig = field(default_factory=PubMedConfig)
    biorxiv: BioRxivConfig = field(default_factory=BioRxivConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)

    @property
    def topic_names(self) -> list[str]:
        return [t.name for t in self.topics]

    def fingerprint(self) -> str:
        """Changes whenever the topics or scoring change, so stored scores can
        be invalidated and recomputed."""
        import hashlib

        payload = "|".join(f"{t.name}:{t.weight}:{t.prose}" for t in self.topics)
        payload += "||" + repr(self.scoring) + "||" + repr(self.embedding)
        payload += "||" + repr(self.summary)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _subdict(raw: dict[str, Any], *path: str) -> dict[str, Any]:
    node: Any = raw
    for key in path:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return node if isinstance(node, dict) else {}


def _build(cls, raw: dict[str, Any]):
    """Instantiate a frozen dataclass, ignoring unknown keys."""
    known = {f.name for f in cls.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in raw.items() if k in known}
    for key in ("servers", "categories"):
        if key in kwargs and isinstance(kwargs[key], list):
            kwargs[key] = tuple(kwargs[key])
    return cls(**kwargs)


def load_config(path=None) -> Config:
    path = path or INTERESTS_FILE
    if not path.exists():
        raise ConfigError(f"No config file at {path}")

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    raw_topics = raw.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ConfigError(
            f"{path} defines no topics. Add at least one entry under 'topics:' "
            "with a name, weight, prose and pubmed query."
        )

    topics: list[Topic] = []
    for i, item in enumerate(raw_topics, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"Topic #{i} in {path} is not a mapping")
        name = str(item.get("name") or "").strip()
        prose = " ".join(str(item.get("prose") or "").split())
        if not name:
            raise ConfigError(f"Topic #{i} in {path} has no name")
        if not prose:
            raise ConfigError(f"Topic '{name}' has no prose; relevance needs it")
        try:
            weight = float(item.get("weight", 1.0))
        except (TypeError, ValueError):
            raise ConfigError(f"Topic '{name}' has a non-numeric weight") from None
        if weight <= 0:
            raise ConfigError(f"Topic '{name}' must have a weight above zero")
        topics.append(
            Topic(
                name=name,
                weight=weight,
                prose=prose,
                pubmed=" ".join(str(item.get("pubmed") or "").split()),
            )
        )

    names = [t.name for t in topics]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"Duplicate topic names in {path}: {', '.join(sorted(dupes))}")

    scoring = _build(Scoring, _subdict(raw, "scoring"))
    total_weight = scoring.relevance_weight + scoring.impact_weight
    if total_weight <= 0:
        raise ConfigError("relevance_weight + impact_weight must be above zero")

    return Config(
        topics=tuple(topics),
        scoring=scoring,
        pubmed=_build(PubMedConfig, _subdict(raw, "sources", "pubmed")),
        biorxiv=_build(BioRxivConfig, _subdict(raw, "sources", "biorxiv")),
        embedding=_build(EmbeddingConfig, _subdict(raw, "embedding")),
        summary=_build(SummaryConfig, _subdict(raw, "summary")),
    )


# ---------------------------------------------------------------- writing


def validate_topic_payload(items: Any) -> list[dict[str, Any]]:
    """Check topics submitted from the UI before anything is written.

    Raises ConfigError with a message meant for the person editing them.
    """
    if not isinstance(items, list) or not items:
        raise ConfigError("Add at least one topic before saving.")
    if len(items) > 40:
        raise ConfigError("That is more than 40 topics; please keep the list shorter.")

    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"Topic #{index} is malformed.")

        name = " ".join(str(item.get("name") or "").split())
        prose = " ".join(str(item.get("prose") or "").split())
        pubmed = " ".join(str(item.get("pubmed") or "").split())

        if not name:
            raise ConfigError(f"Topic #{index} needs a name.")
        if len(name) > 80:
            raise ConfigError(f"The name of topic '{name[:40]}…' is too long (max 80 characters).")
        if len(prose) < 20:
            raise ConfigError(
                f"Topic '{name}' needs a longer description. "
                "Write a sentence or two: it is what papers are matched against."
            )
        if len(prose) > 2000:
            raise ConfigError(f"The description of topic '{name}' is too long (max 2000 characters).")
        if len(pubmed) > 2000:
            raise ConfigError(f"The PubMed query for topic '{name}' is too long (max 2000 characters).")

        key = name.casefold()
        if key in seen:
            raise ConfigError(f"Two topics are both called '{name}'. Names must be unique.")
        seen.add(key)

        try:
            weight = float(item.get("weight", 1.0))
        except (TypeError, ValueError):
            raise ConfigError(f"The weight for topic '{name}' must be a number.") from None
        if not 0 < weight <= 100:
            raise ConfigError(f"The weight for topic '{name}' must be above 0 and at most 100.")

        cleaned.append(
            {"name": name, "weight": round(weight, 3), "prose": prose, "pubmed": pubmed}
        )
    return cleaned


def save_topics(items: list[dict[str, Any]], path=None) -> list[dict[str, Any]]:
    """Write topics back to interests.yaml, keeping comments and other settings.

    The file is written to a temporary file and then moved into place, so an
    interrupted write cannot leave a half-written config behind. The previous
    version is kept as interests.yaml.bak.
    """
    from ruamel.yaml import YAML
    from ruamel.yaml.scalarstring import LiteralScalarString

    path = path or INTERESTS_FILE
    cleaned = validate_topic_payload(items)

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.width = 88
    yaml_rt.indent(mapping=2, sequence=2, offset=0)

    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            document = yaml_rt.load(handle) or {}
    else:
        document = {}

    def wrap(text: str) -> Any:
        # Long prose reads better as a literal block than as one long line.
        return LiteralScalarString(text + "\n") if len(text) > 70 else text

    document["topics"] = [
        {
            "name": topic["name"],
            "weight": topic["weight"],
            "prose": wrap(topic["prose"]),
            "pubmed": wrap(topic["pubmed"]) if topic["pubmed"] else "",
        }
        for topic in cleaned
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        yaml_rt.dump(document, handle)

    # Confirm the new file parses before it replaces the working one.
    try:
        load_config(temp)
    except ConfigError as exc:
        temp.unlink(missing_ok=True)
        raise ConfigError(f"The new topics did not validate, so nothing was changed: {exc}") from exc

    if path.exists():
        import shutil

        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    temp.replace(path)
    return cleaned
