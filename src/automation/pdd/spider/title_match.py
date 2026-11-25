"""Exact historical title matching with whitespace and width normalization."""
import re
import unicodedata


def title_key(title):
    return re.sub(r'[\s\u200b-\u200f\ufeff]', '', unicodedata.normalize('NFKC', str(title or '')))


def choose_title_match(title, candidates):
    key = title_key(title)
    matches = [dict(row) for row in candidates if key and title_key(row['title']) == key
               and str(row.get('platform_product_id') or '').strip()
               and str(row.get('platform_product_link') or '').strip()]
    ids = {str(row['platform_product_id']).strip() for row in matches}
    evidence = {'method': 'title_exact', 'title': title, 'normalized_title': key}
    if len(ids) > 1:
        return 'conflict', None, dict(evidence, candidate_ids=sorted(row['id'] for row in matches))
    if not matches:
        return 'unmatched', None, evidence
    # Prefer a complete identity, then the latest historical observation of the same platform ID.
    candidate = max(matches, key=lambda row: (
        bool(str(row.get('listing_date') or '').strip()), str(row.get('capture_date') or ''),
        str(row.get('updated_at') or ''), row['id']))
    for field in ('platform_product_id', 'platform_product_link', 'listing_date'):
        if candidate.get(field) is not None:
            candidate[field] = str(candidate[field]).strip()
    return 'matched', candidate, evidence
