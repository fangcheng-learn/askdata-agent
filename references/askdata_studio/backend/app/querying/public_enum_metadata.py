"""Bind explicit enum tables from an allowlisted public dictionary to its schema.

This parser does not discover files, execute examples, or import surrounding
prose. Unknown tables/fields and conflicting pre-existing enums are untouched.
"""
from copy import deepcopy
import hashlib
import re


def enrich_enum_metadata(payload: dict, markdown: str, *, source: str) -> dict:
    result = deepcopy(payload)
    tables = {str(t.get('name') or t.get('id')): t for t in result.get('tables', [])}
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    sections = re.split(r'(?m)^###\s+[^\n`]*`([^`]+)`[^\n]*\n', markdown)
    for offset in range(1, len(sections), 2):
        table = tables.get(sections[offset])
        if not table:
            continue
        fields = {f['name']: f for f in table.get('fields', [])}
        blocks = re.findall(r'(?m)(?:^\|[^\n]*\|[ \t]*\n?)+', re.split(r'(?m)^#{1,2}\s', sections[offset + 1], maxsplit=1)[0])
        for block in blocks:
            rows = [[cell.strip() for cell in line.strip().strip('|').split('|')]
                    for line in block.splitlines() if line.strip()]
            if len(rows) < 3 or not all(re.fullmatch(r':?-{3,}:?', c.replace(' ', '')) for c in rows[1]):
                continue
            header = rows[0]
            field = fields.get(header[0].strip('`'))
            if not field or len(header) < 2:
                continue
            meanings = {}
            for row in rows[2:]:
                literal = re.fullmatch(r'`([^`]+)`', row[0])
                if not literal or len(row) != len(header) or literal[1] in meanings:
                    meanings = {}; break
                meanings[literal[1]] = dict(zip(header[1:], row[1:]))
            if not meanings:
                continue
            existing = field.get('enum_values')
            if existing and set(existing) != set(meanings):
                field['enum_metadata_conflict'] = {'source': source, 'content_hash': digest}
                continue
            field['enum_values'] = list(meanings)
            field['enum_descriptions'] = meanings
            field['enum_metadata_source'] = {'source': source, 'content_hash': digest}
    return result
