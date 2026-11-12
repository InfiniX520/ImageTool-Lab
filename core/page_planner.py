"""Pure, one-time expansion of exact original metadata into page contracts."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from core.task_contracts import ContractError, OutputSpec, PageSpec, QualitySpec

_ID = re.compile(r"[0-9]{1,32}")
_PAGE = re.compile(r"^([0-9]{1,32})_p([0-9]{1,4})(?:[_.].*)?$")
_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_DEVICES = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


class OriginalMetadataError(ContractError):
    """A source adapter must fill missing metadata; previews never fill it."""


def _component(value: str, limit: int = 80) -> str:
    value = unicodedata.normalize("NFC", str(value))
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", value).strip(" .")[:limit].rstrip(" .")
    if not value or value in {".", ".."}:
        value = "untitled"
    if _DEVICES.match(value):
        value = "_" + value
    return value


def plan_original_pages(illust_id: str, page_count: int, original_urls,
                        output: OutputSpec, *, quality: QualitySpec = QualitySpec(),
                        artist_id: str = "", title: str = "", artist_name: str = "",
                        publication_date: str = "") -> tuple[PageSpec, ...]:
    """Use each metadata URL exactly once; no requests, guessing or recursion.

    The App/Ajax source adapter supplies the ordered original URL list. In
    first-page mode it may supply p0 alone. A full-page request requires the
    entire declared list, including pages with different extensions/paths.
    """
    if not isinstance(illust_id, str) or not _ID.fullmatch(illust_id):
        raise OriginalMetadataError("invalid_artwork_identity")
    if type(page_count) is not int or not 1 <= page_count <= 10000:
        raise OriginalMetadataError("unknown_page_count")
    if not isinstance(output, OutputSpec) or not isinstance(quality, QualitySpec):
        raise TypeError("Validated output and quality contracts are required")
    if not isinstance(original_urls, (list, tuple)) or not original_urls:
        raise OriginalMetadataError("original_urls_missing")
    if len(original_urls) not in ({page_count} if quality.all_pages else {1, page_count}):
        raise OriginalMetadataError("original_page_coverage_incomplete")
    if artist_id and (not isinstance(artist_id, str) or not _ID.fullmatch(artist_id)):
        raise OriginalMetadataError("invalid_artist_identity")
    selected = original_urls if quality.all_pages else original_urls[:1]
    result, outputs = [], set()
    for index, url in enumerate(selected):
        if not isinstance(url, str):
            raise OriginalMetadataError("original_url_missing")
        try:
            source_path = PurePosixPath(unquote(urlsplit(url).path))
            identity = _PAGE.fullmatch(source_path.name)
            if "/img-original/" not in str(source_path) or not identity:
                raise OriginalMetadataError("original_identity_missing")
            if identity.group(1) != illust_id or int(identity.group(2)) != index:
                raise OriginalMetadataError("original_page_identity_mismatch")
            extension = source_path.suffix.lower()
            if extension not in _EXTENSIONS:
                raise OriginalMetadataError("original_format_unsupported")
            image_date = publication_date
            if not image_date:
                match = re.search(r'/([0-9]{4})/([0-9]{2})/([0-9]{2})/', str(source_path))
                if match:
                    try:image_date = date.fromisoformat('-'.join(match.groups())).isoformat()
                    except ValueError:pass
            if image_date:
                try:image_date = date.fromisoformat(image_date).strftime('%Y%m%d')
                except (TypeError, ValueError):raise OriginalMetadataError('invalid_publication_date') from None
            stem = output.name_template.format(illust_id=illust_id, page_index=index,
                artist_id=artist_id, artist_name=_component(artist_name), title=_component(title),
                id=illust_id, page=index, user_id=artist_id, user=_component(artist_name), date=image_date)
            truncated = len(stem) > 120
            stem = _component(stem, 120)
            if (truncated or not any(field in output.name_template for field in ('{id}', '{illust_id}'))
                    or not any(field in output.name_template for field in ('{page}', '{page_index}'))):
                stem = f"{stem}_{illust_id}_p{index}"
            folder = (artist_id or "unknown_artist") + "/" if output.folder_by_artist else ""
            relative = str(PurePosixPath(output.root_ref.relative_path) / (folder + stem + extension))
            ref = replace(output.root_ref, relative_path=relative)
            spec = PageSpec(index, ref, url)
        except (ValueError, TypeError) as exc:
            if isinstance(exc, OriginalMetadataError):
                raise
            raise OriginalMetadataError("invalid_original_page") from exc
        if ref.target_key in outputs:
            raise OriginalMetadataError("page_output_collision")
        outputs.add(ref.target_key)
        result.append(spec)
    return tuple(result)
