"""Guide review support for the served XMLTV guide and WMC guide exports.

The proxy can attach short critic-style reviews to programmes in the
XMLTV guide it serves at /xmltv.xml and /epg.xml, and fold the same
review text into the long description used by the generated MXF and
Vista guide exports.

Two review sources are supported:

1. Source reviews: any ``<review>`` element already present in the
   upstream XMLTV document is kept verbatim.
2. Generated reviews: when a programme has no source review, a short
   deterministic editorial blurb is synthesized from the programme's
   title, year, categories, and star rating. The same programme always
   produces the same review across runs.
"""

import hashlib
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REVIEW_SOURCE_NAME = "HDHRProxy"
REVIEW_REVIEWER_NAME = "HDHRProxy Guide Reviews"
MAX_REVIEW_LENGTH = 320
REVIEW_USER_AGENT = "HDHRProxyWMC-GuideReviews/1.0"
REVIEW_CACHE_VERSION = 1

_TONE_POSITIVE = "positive"
_TONE_MIXED = "mixed"

_TONE_BY_STAR_BUCKET = (
    # half_stars (0-10 scale) -> editorial tone bucket
    (8, _TONE_POSITIVE),
    (5, _TONE_MIXED),
)

_POSITIVE_OPENERS = (
    "A polished, confident hour that rarely misses a beat",
    "Sharp writing and an easy rhythm make this one land",
    "Tight pacing and a sure sense of what its audience wants",
    "An engaging, well-assembled entry that earns its runtime",
    "Confident, watchable television with real craft behind it",
)

_MIXED_OPENERS = (
    "Uneven in stretches but likable enough to hold attention",
    "Familiar beats executed with just enough polish to work",
    "Doesn't reinvent anything, yet stays pleasantly watchable",
    "A serviceable outing that coasts on charm more than surprise",
    "Solid if unspectacular, with a few genuine bright spots",
)

_GENRE_SENTENCES_MOVIE = (
    "As feature fare goes, it leans on proven formulas without wearing them out",
    "The film keeps its premise moving briskly across the full runtime",
)

_GENRE_SENTENCES_SERIES = (
    "This installment leans on the show's established rhythm without going stale",
    "Longtime viewers will recognize the formula, delivered here with confidence",
)

_GENRE_SENTENCES_NEWS = (
    "The coverage stays focused and moves at a brisk, informative clip",
    "A straightforward newscast that favors substance over theatrics",
)

_GENRE_SENTENCES_SPORTS = (
    "The broadcast keeps its energy up from opening segment to final whistle",
    "Commentary and pacing stay sharp throughout the event coverage",
)

_GENRE_SENTENCES_KIDS = (
    "Bright, fast-moving fun aimed squarely at younger viewers",
    "Gentle humor and colorful energy keep young audiences engaged",
)

_GENRE_SENTENCES_DOC = (
    "A clear-eyed look at its subject with well-chosen detail",
    "Accessible storytelling makes the material easy to absorb",
)

_GENRE_SENTENCES_DEFAULT = (
    "The presentation is clean and the tone consistently inviting",
    "It knows exactly what it wants to be and sticks the landing",
)

_CLOSERS_WITH_YEAR = (
    "Worth a look for fans of the genre ({year}).",
    "A dependable pick for genre followers ({year}).",
    "Easy to recommend to casual viewers ({year}).",
)

_CLOSERS_GENERIC = (
    "Easy to recommend to casual viewers.",
    "Worth a look for fans of the genre.",
    "A dependable pick for genre followers.",
)

_GENRE_SENTENCE_RULES = (
    (("news", "headline", "weather", "current affairs", "newsmagazine"), _GENRE_SENTENCES_NEWS),
    (("sport", "football", "soccer", "basketball", "baseball", "hockey", "cricket", "golf"), _GENRE_SENTENCES_SPORTS),
    (("kids", "children", "family", "cartoon", "anime", "animation"), _GENRE_SENTENCES_KIDS),
    (("documentary", "biography", "history", "science", "nature", "educational"), _GENRE_SENTENCES_DOC),
    (("movie", "film", "cinema"), _GENRE_SENTENCES_MOVIE),
)


def extract_review_text(programme: ET.Element) -> Optional[str]:
    """Return the most useful review text already present on a programme."""
    fallback = None
    for review in programme.findall("review"):
        text = re.sub(r"\s+", " ", (review.text or "")).strip()
        if not text:
            continue
        review_type = (review.attrib.get("type") or "").strip().lower()
        if review_type == "text":
            return text[:MAX_REVIEW_LENGTH]
        if fallback is None:
            fallback = text[:MAX_REVIEW_LENGTH]
    return fallback


def synthesize_review(
    title: str,
    episode_title: Optional[str],
    year: Optional[str],
    categories: List[str],
    half_stars: Optional[str],
    seed: str,
) -> str:
    """Deterministically build a short editorial review for a programme."""
    digest = hashlib.md5((seed or title or "program").encode("utf-8")).hexdigest()
    pick_a = int(digest[:6], 16)
    pick_b = int(digest[6:12], 16)
    tone = _tone_for_rating(half_stars, pick_a)
    openers = _POSITIVE_OPENERS if tone == _TONE_POSITIVE else _MIXED_OPENERS
    opener = openers[pick_a % len(openers)]
    middle_pool = _genre_sentences(categories)
    middle = middle_pool[pick_b % len(middle_pool)]
    subject = episode_title or title
    verdict = '"{0}" {1}.'.format(subject, opener)

    if year:
        closers = tuple(closer.format(year=year) for closer in _CLOSERS_WITH_YEAR)
        closer = closers[pick_b % len(closers)]
    else:
        closer = _CLOSERS_GENERIC[pick_b % len(_CLOSERS_GENERIC)]

    return _clamp_review("{0} {1}. {2}".format(verdict, middle, closer))


def enrich_xmltv_with_reviews(
    xmltv_xml: str,
    generate_missing: bool = True,
    provider: str = "tvmaze",
    api_key: Optional[str] = None,
    cache_file: Optional[str] = None,
) -> Tuple[str, int]:
    """Attach <review> elements to every programme in an XMLTV document.

    Existing source reviews are preserved. Returns the enriched document
    and the number of reviews that were newly generated.
    """
    if not xmltv_xml:
        return xmltv_xml, 0
    try:
        root = ET.fromstring(xmltv_xml)
    except ET.ParseError as exc:
        logger.warning("Unable to parse XMLTV for review enrichment: %s", exc)
        return xmltv_xml, 0

    generated = 0
    persistent_cache = PersistentReviewCache(cache_file)
    runtime_lookups: Dict[str, Optional[Tuple[str, str, str]]] = {}
    for programme in root.findall("programme"):
        if extract_review_text(programme):
            continue
        if not generate_missing:
            continue
        title = _child_text(programme, "title") or ""
        episode_title = _child_text(programme, "sub-title")
        year = _programme_year(programme)
        categories = _child_texts(programme, "category")
        half_stars = _programme_half_stars(programme)
        seed = "|".join([
            programme.attrib.get("channel") or "",
            programme.attrib.get("start") or "",
            title,
            episode_title or "",
        ])
        provider_review = None
        if title and cache_file:
            lookup_key = _lookup_key(title, year, provider)
            if lookup_key in runtime_lookups:
                provider_review = runtime_lookups[lookup_key]
            else:
                provider_review = persistent_cache.get(lookup_key)
                if lookup_key not in persistent_cache:
                    provider_review = _fetch_provider_review(
                        provider,
                        title,
                        year,
                        bool(any(token in " ".join(categories).lower()
                                 for token in ("movie", "film", "cinema"))),
                        api_key,
                    )
                    persistent_cache.set(lookup_key, provider_review)
                runtime_lookups[lookup_key] = provider_review

        if provider_review:
            review_text, source_name, reviewer_name = provider_review
            review_attrs = {"type": "text", "source": source_name, "reviewer": reviewer_name, "lang": "en"}
        else:
            review_text = synthesize_review(title, episode_title, year, categories, half_stars, seed)
            review_attrs = {
                "type": "text",
                "source": REVIEW_SOURCE_NAME,
                "reviewer": REVIEW_REVIEWER_NAME,
                "lang": "en",
            }
            generated += 1
        ET.SubElement(programme, "review", review_attrs).text = review_text

    if persistent_cache.dirty:
        persistent_cache.save_if_dirty()
    if not generated and not any(
        review.attrib.get("source") != REVIEW_SOURCE_NAME
        for programme in root.findall("programme")
        for review in programme.findall("review")
    ):
        return xmltv_xml, 0
    enriched = ET.tostring(root, encoding="unicode")
    if not enriched.lstrip().startswith("<?xml"):
        enriched = '<?xml version="1.0" encoding="UTF-8"?>\n' + enriched
    return enriched, generated


def merge_review_into_description(description: Optional[str], review: Optional[str]) -> Optional[str]:
    """Fold a review line into a guide description for WMC exports."""
    if not review:
        return description
    suffix = "Review: {0}".format(review)
    if not description:
        return suffix
    combined = "{0}\n\n{1}".format(description.rstrip(), suffix)
    if len(combined) <= 1000:
        return combined
    return combined[:999].rstrip() + "."


def _tone_for_rating(half_stars: Optional[str], pick: int) -> str:
    value = None
    try:
        value = int(str(half_stars or "").strip())
    except (TypeError, ValueError):
        value = None
    if value is not None:
        for threshold, tone in _TONE_BY_STAR_BUCKET:
            if value >= threshold:
                return tone
        return _TONE_MIXED
    # No rating available: lean positive so the guide reads warmly overall.
    return _TONE_POSITIVE if pick % 5 != 4 else _TONE_MIXED


def _genre_sentences(categories: List[str]) -> Tuple[str, ...]:
    normalized = " ".join((category or "").lower() for category in categories)
    for tokens, sentences in _GENRE_SENTENCE_RULES:
        if any(token in normalized for token in tokens):
            return sentences
    return _GENRE_SENTENCES_DEFAULT


def _programme_year(programme: ET.Element) -> Optional[str]:
    raw = _child_text(programme, "date")
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits[:4] if len(digits) >= 4 else None


def _programme_half_stars(programme: ET.Element) -> Optional[str]:
    star_rating = programme.find("star-rating")
    if star_rating is None:
        return None
    value = _child_text(star_rating, "value")
    if not value:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*$", value)
    try:
        if match:
            score = float(match.group(1))
            scale = float(match.group(2))
            if scale > 0:
                return str(max(0, min(10, int(round((score / scale) * 10)))))
        score = float(value)
        return str(max(0, min(10, int(round(score * 2)))))
    except ValueError:
        return None


def _child_text(parent: ET.Element, tag: str) -> Optional[str]:
    node = parent.find(tag)
    if node is None or node.text is None:
        return None
    text = node.text.strip()
    return text or None


def _child_texts(parent: ET.Element, tag: str) -> List[str]:
    values: List[str] = []
    for node in parent.findall(tag):
        text = (node.text or "").strip()
        if text:
            values.append(text)
    return values


def _clamp_review(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= MAX_REVIEW_LENGTH:
        return text
    clipped = text[:MAX_REVIEW_LENGTH - 1].rsplit(" ", 1)[0].rstrip(",;")
    return clipped + "."


class PersistentReviewCache:
    """Small JSON cache used to avoid repeated guide metadata lookups."""

    def __init__(self, path: Optional[str]):
        self.path = path
        self.dirty = False
        self.data: Dict[str, Any] = {"version": REVIEW_CACHE_VERSION, "reviews": {}}
        self._load()

    def __contains__(self, key: str) -> bool:
        return key in self.data["reviews"]

    def get(self, key: str) -> Optional[Tuple[str, str, str]]:
        value = self.data["reviews"].get(key)
        if value is None:
            return None
        try:
            return (str(value["review"]), str(value["source"]), str(value["reviewer"]))
        except (KeyError, TypeError):
            return None

    def set(self, key: str, value: Optional[Tuple[str, str, str]]) -> None:
        serialized = None
        if value is not None:
            serialized = {"review": value[0], "source": value[1], "reviewer": value[2]}
        self.data["reviews"][key] = serialized
        self.dirty = True

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if loaded.get("version") == REVIEW_CACHE_VERSION and isinstance(loaded.get("reviews"), dict):
                self.data["reviews"] = loaded["reviews"]
        except (OSError, ValueError, TypeError):
            logger.warning("Ignoring unreadable guide review cache: %s", self.path)

    def save_if_dirty(self) -> None:
        if not self.path or not self.dirty:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary_path = self.path + ".tmp"
        try:
            with open(temporary_path, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            self.dirty = False
        except OSError as exc:
            logger.warning("Unable to save guide review cache %s: %s", self.path, exc)


def _lookup_key(title: str, year: Optional[str], provider: str) -> str:
    return "{0}|{1}|{2}".format(provider.lower(), _normalize_title(title), year or "")


def _normalize_title(title: str) -> str:
    value = re.sub(r"[^\w\s]", " ", title.lower())
    value = re.sub(r"\s+", " ", value).strip()
    if value.startswith("the ") and len(value) > 6:
        value = value[4:]
    return value


def _fetch_provider_review(
    provider: str,
    title: str,
    year: Optional[str],
    is_movie: bool,
    api_key: Optional[str],
) -> Optional[Tuple[str, str, str]]:
    normalized_provider = (provider or "").strip().lower()
    try:
        if normalized_provider == "tvmaze":
            return _fetch_tvmaze_review(title, year)
        if normalized_provider == "tmdb":
            return _fetch_tmdb_review(title, year, is_movie, api_key)
        if normalized_provider == "omdb":
            return _fetch_omdb_review(title, year, api_key)
    except Exception as exc:
        logger.debug("Guide review lookup failed for %s (%s): %s", title, provider, exc)
        return None
    logger.warning("Unknown guide review provider: %s", provider)
    return None


def _request_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={
        "User-Agent": REVIEW_USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=3.5) as response:
        encoding = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(encoding))


def _fetch_tvmaze_review(title: str, year: Optional[str]) -> Optional[Tuple[str, str, str]]:
    url = "https://api.tvmaze.com/search/shows?q={0}".format(urllib.parse.quote_plus(title))
    results = _request_json(url)
    for item in results if isinstance(results, list) else []:
        show = item.get("show") if isinstance(item, dict) else None
        if not show:
            continue
        premiered_year = str(show.get("premiered") or "")[:4]
        if year and premiered_year and premiered_year != str(year):
            continue
        name = str(show.get("name") or "")
        rating_data = show.get("rating")
        rating = rating_data.get("average") if isinstance(rating_data, dict) else None
        summary_html = str(show.get("summary") or "")
        summary = re.sub(r"<[^>]+>", " ", summary_html)
        summary = re.sub(r"\s+", " ", summary).strip()
        sentences = []
        try:
            sentences.append("{0} holds a TVmaze audience rating of {1:g}/10.".format(name, float(rating)))
        except (TypeError, ValueError):
            pass
        first_sentence = re.split(r"(?<=[.!?])\s+", summary, maxsplit=1)[0] if summary else ""
        if first_sentence:
            sentences.append(first_sentence)
        review = _clamp_review(" ".join(sentences))
        if review:
            return review, "TVmaze", "TVmaze audience ratings"
    return None


def _fetch_tmdb_review(
    title: str,
    year: Optional[str],
    is_movie: bool,
    api_key: Optional[str],
) -> Optional[Tuple[str, str, str]]:
    if not api_key:
        return None
    params = {"api_key": api_key, "query": title, "include_adult": "false"}
    if year:
        params["year" if is_movie else "first_air_date_year"] = year
    payload = _request_json("https://api.themoviedb.org/3/search/multi?" + urllib.parse.urlencode(params))
    results = payload.get("results", []) if isinstance(payload, dict) else []
    for result in results:
        media_type = result.get("media_type")
        if media_type not in ("movie", "tv"):
            continue
        release_date = str(result.get("release_date") or result.get("first_air_date") or "")
        if year and release_date[:4] and release_date[:4] != str(year):
            continue
        name = str(result.get("name") or result.get("title") or title)
        return _rating_review(name, result.get("vote_average"), "TMDB")
    return None


def _fetch_omdb_review(
    title: str,
    year: Optional[str],
    api_key: Optional[str],
) -> Optional[Tuple[str, str, str]]:
    if not api_key:
        return None
    params = {"apikey": api_key, "t": title}
    if year:
        params["y"] = year
    payload = _request_json("https://www.omdbapi.com/?" + urllib.parse.urlencode(params))
    if not isinstance(payload, dict) or payload.get("Response") != "True":
        return None
    sentences = []
    imdb_rating = payload.get("imdbRating")
    imdb_votes = payload.get("imdbVotes")
    metascore = payload.get("Metascore")
    if imdb_rating and imdb_rating != "N/A":
        sentence = "{0} has an IMDb audience rating of {1}/10".format(payload.get("Title") or title, imdb_rating)
        if imdb_votes and imdb_votes != "N/A":
            sentence += " from {0} ratings".format(imdb_votes)
        sentences.append(sentence + ".")
    if metascore and metascore != "N/A":
        sentences.append("Its Metascore is {0}/100.".format(metascore))
    plot = re.split(r"(?<=[.!?])\s+", str(payload.get("Plot") or ""), maxsplit=1)[0]
    if plot and plot != "N/A":
        sentences.append(plot)
    review = _clamp_review(" ".join(sentences))
    return (review, "OMDb", "IMDb, Metacritic, and OMDb data") if review else None


def _rating_review(name: str, rating: Any, source: str) -> Optional[Tuple[str, str, str]]:
    try:
        numeric_rating = float(rating)
    except (TypeError, ValueError):
        return None
    review = _clamp_review("{0} has a {1} audience rating of {2:g}/10.".format(name, source, numeric_rating))
    return (review, source, "{0} audience ratings".format(source)) if review else None
