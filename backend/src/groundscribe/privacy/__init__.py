"""Security, privacy and the boundaries material must not cross (phase 13).

The flags themselves live in :mod:`groundscribe.domain.confidentiality`, next to
the rows that carry them. What lives here is the *enforcement*: reading a
project's flagged material, deciding what a provider may see, what a retention
mode keeps, and what an export may contain.
"""

from groundscribe.privacy.export import (
    ExportedArticle,
    ExportFormat,
    ExportIntegrityError,
    render_article,
)
from groundscribe.privacy.material import MINIMUM_SPAN, restricted_spans

__all__ = [
    "MINIMUM_SPAN",
    "ExportFormat",
    "ExportIntegrityError",
    "ExportedArticle",
    "render_article",
    "restricted_spans",
]
