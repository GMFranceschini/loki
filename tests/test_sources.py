"""Parsing of PubMed XML and bioRxiv JSON, including the records we drop."""

from xml.etree import ElementTree as ET

import pytest

from loki.sources.biorxiv import parse_preprint
from loki.sources.pubmed import _is_excluded, parse_article

ARTICLE_XML = """
<PubmedArticle>
  <MedlineCitation>
    <PMID>12345678</PMID>
    <Article>
      <Journal>
        <ISSN>0028-0836</ISSN>
        <Title>Nature</Title>
        <ISOAbbreviation>Nature</ISOAbbreviation>
        <JournalIssue><PubDate><Year>2026</Year><Month>Nov</Month><Day>10</Day></PubDate></JournalIssue>
      </Journal>
      <ArticleTitle>Prime editing of <i>human</i> stem cells.</ArticleTitle>
      <Abstract>
        <AbstractText Label="BACKGROUND">Editing is hard.</AbstractText>
        <AbstractText Label="RESULTS">We did it.</AbstractText>
      </Abstract>
      <AuthorList>
        <Author><LastName>Doe</LastName><Initials>J</Initials></Author>
        <Author><CollectiveName>The Consortium</CollectiveName></Author>
      </AuthorList>
      <ArticleDate><Year>2026</Year><Month>11</Month><Day>12</Day></ArticleDate>
      <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
    </Article>
  </MedlineCitation>
  <PubmedData>
    <History>
      <PubMedPubDate PubStatus="entrez"><Year>2026</Year><Month>11</Month><Day>14</Day></PubMedPubDate>
    </History>
    <ArticleIdList><ArticleId IdType="doi">10.1038/s41586-026-00001-X</ArticleId></ArticleIdList>
  </PubmedData>
</PubmedArticle>
"""


@pytest.fixture
def parsed():
    return parse_article(ET.fromstring(ARTICLE_XML))


def test_core_fields(parsed):
    assert parsed["ext_id"] == "12345678"
    assert parsed["journal"] == "Nature"
    assert parsed["journal_abbrev"] == "Nature"
    assert parsed["issn"] == "0028-0836"
    assert parsed["is_preprint"] is False
    assert parsed["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"


def test_title_keeps_nested_markup_text(parsed):
    assert parsed["title"] == "Prime editing of human stem cells"


def test_structured_abstract_is_joined_with_labels(parsed):
    assert "Background: Editing is hard." in parsed["abstract"]
    assert "Results: We did it." in parsed["abstract"]


def test_authors_include_collective_names(parsed):
    assert parsed["authors"] == ["Doe J", "The Consortium"]


def test_doi_is_lowercased(parsed):
    assert parsed["doi"] == "10.1038/s41586-026-00001-x"


def test_discovery_date_is_the_entrez_date(parsed):
    # The digest windows key off the date PubMed indexed the paper.
    assert parsed["published_on"] == "2026-11-14"
    # The real publication date is kept separately for display.
    assert parsed["pub_date"] == "2026-11-12"


def test_article_without_a_pmid_is_dropped():
    xml = ARTICLE_XML.replace("<PMID>12345678</PMID>", "<PMID></PMID>")
    assert parse_article(ET.fromstring(xml)) is None


def test_missing_abstract_becomes_none():
    xml = ARTICLE_XML[: ARTICLE_XML.index("<Abstract>")] + ARTICLE_XML[ARTICLE_XML.index("</Abstract>") + 11 :]
    assert parse_article(ET.fromstring(xml))["abstract"] is None


def test_medline_date_falls_back_to_january():
    xml = ARTICLE_XML.replace(
        "<PubDate><Year>2026</Year><Month>Nov</Month><Day>10</Day></PubDate>",
        "<PubDate><MedlineDate>2026 Nov-Dec</MedlineDate></PubDate>",
    ).replace("<ArticleDate><Year>2026</Year><Month>11</Month><Day>12</Day></ArticleDate>", "")
    assert parse_article(ET.fromstring(xml))["pub_date"] == "2026-01-01"


# ---------------------------------------------------------------- exclusions


def make_article(types=("Journal Article",), title="A real paper"):
    article = ET.Element("Article")
    type_list = ET.SubElement(article, "PublicationTypeList")
    for value in types:
        ET.SubElement(type_list, "PublicationType").text = value
    return article, title


@pytest.mark.parametrize(
    "types,title,excluded",
    [
        (("Journal Article",), "CRISPR editing of cells", False),
        (("Retraction of Publication",), "Some paper", True),
        (("Editorial",), "On the future of biology", True),
        (("Published Erratum",), "Anything", True),
        (("Journal Article",), "Retraction notice to: CRISPR paper", True),
        (("Journal Article",), "Corrigendum: base editing", True),
        (("Journal Article",), "Author Correction: spatial omics", True),
        # A real paper whose title merely starts with a similar word.
        (("Journal Article",), "Correcting gene expression with CRISPR", False),
        (("Journal Article",), "Retinal organoids model disease", False),
    ],
)
def test_notices_are_excluded_but_real_papers_are_not(types, title, excluded):
    article, _ = make_article(types, title)
    assert _is_excluded(article, title) is excluded


# ---------------------------------------------------------------- preprints


PREPRINT = {
    "doi": "10.1101/2026.11.13.766444",
    "title": "A preprint about spatial transcriptomics.",
    "authors": "Wang, W.; Zhao, X.; Li, Q.",
    "date": "2026-11-15",
    "version": "2",
    "category": "Genomics",
    "abstract": "We did spatial work.",
}


def test_preprint_fields():
    record = parse_preprint(PREPRINT, "biorxiv")
    assert record["source"] == "biorxiv"
    assert record["is_preprint"] is True
    assert record["ext_id"] == record["doi"] == "10.1101/2026.11.13.766444"
    assert record["authors"] == ["Wang, W.", "Zhao, X.", "Li, Q."]
    assert record["published_on"] == "2026-11-15"
    assert record["category"] == "genomics"
    assert record["url"].endswith("v2")


def test_preprints_carry_no_journal():
    # This is what makes the scorer apply the flat preprint impact score.
    record = parse_preprint(PREPRINT, "biorxiv")
    assert record["journal"] is None
    assert record["issn"] is None


@pytest.mark.parametrize(
    "broken",
    [
        {**PREPRINT, "doi": ""},
        {**PREPRINT, "title": ""},
        {**PREPRINT, "date": "not-a-date"},
    ],
)
def test_malformed_preprints_are_dropped(broken):
    assert parse_preprint(broken, "biorxiv") is None


# ----------------------------------------------------- long author lists


def _article_with(n_authors: int) -> ET.Element:
    """The fixture article, but with a long author list."""
    root = ET.fromstring(ARTICLE_XML)
    author_list = root.find("MedlineCitation/Article/AuthorList")
    for author in list(author_list):
        author_list.remove(author)
    for i in range(n_authors):
        author = ET.SubElement(author_list, "Author")
        ET.SubElement(author, "LastName").text = f"Author{i:03d}"
        ET.SubElement(author, "Initials").text = "X"
    return root


def test_a_short_author_list_is_kept_whole():
    parsed = parse_article(_article_with(5))
    assert len(parsed["authors"]) == 5
    assert parsed["author_count"] == 5


def test_a_long_author_list_keeps_the_last_author():
    # The senior author is the last name on a biology paper, so a plain
    # truncation would drop the most informative one.
    parsed = parse_article(_article_with(300))
    assert parsed["authors"][0] == "Author000 X"
    assert parsed["authors"][-1] == "Author299 X"
    assert len(parsed["authors"]) == 25
    assert parsed["author_count"] == 300


def test_the_middle_of_a_long_author_list_is_what_goes():
    parsed = parse_article(_article_with(300))
    assert parsed["authors"][23] == "Author023 X"
    assert "Author024 X" not in parsed["authors"]


def test_preprint_author_lists_are_trimmed_the_same_way():
    item = dict(PREPRINT)
    item["authors"] = "; ".join(f"Author{i:03d}, X." for i in range(60))
    record = parse_preprint(item, "biorxiv")
    assert record["authors"][0] == "Author000, X."
    assert record["authors"][-1] == "Author059, X."
    assert len(record["authors"]) == 25
    assert record["author_count"] == 60
