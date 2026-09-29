"""
What a document IS, worked out from its name and folder alone.

Built 2026-09-29. The file list already holds every document's name, folder,
size and last-changed date, and reading a document to learn more about it
would download it (see index.py's header). But a lot more is sitting in the
name and folder than search was using:

  * The document's own DATE. 198,111 of the ~450,000 files under !PROPERTIES
    begin with a YYMMDD prefix ("220419 Neighboring Hotel Public Hearing
    Notice.pdf"). That is the date the firm filed it under, and unlike the
    drive's last-changed date it does not move when OneDrive re-syncs a file --
    which on one property made years of old documents look as though they
    arrived this year.
  * The document's KIND -- an LOI, an amendment, a title commitment, a
    settlement statement -- from words the firm already puts in names.
  * The PROPERTY folder it sits in, from the !PROPERTIES/<STATE>/<property>/
    layout.

Nothing here opens a file, and nothing is stored: it is computed when a search
result is shown, so there is no second copy of the list to go stale and no
change to the file list's shape on anyone's machine. Every field is "" when
the name does not say, never a guess.
"""
from __future__ import annotations

import re
from datetime import date

# First match wins, so the more specific kinds come first ("purchase
# agreement amendment" is an amendment; "title commitment" is not a
# commitment letter). Matched as whole words against the lowercased name.
_KINDS = [
    ("amendment",           r"\bamend(ment|ed)?\b|\b\d+(st|nd|rd|th) amend"),
    ("letter of intent",    r"\bloi\b|letter of intent"),
    ("purchase agreement",  r"\bpsa\b|purchase (and sale )?agreement|\bpurchase agmt\b"),
    ("title",               r"\btitle (commitment|policy|report)\b|\bcommitment for title\b|\bowner'?s policy\b"),
    ("survey",              r"\balta\b|\bsurvey\b"),
    ("plat",                r"\bplat\b|\bfinal map\b|\btentative (tract )?map\b|\bttm\b"),
    ("environmental",       r"\bphase (i|1|ii|2)\b|\besa\b|\benvironmental\b"),
    ("appraisal",           r"\bappraisal\b|\bbov\b"),
    ("settlement statement",r"\bsettlement statement\b|\bclosing statement\b|\bhud\b|\balta statement\b"),
    ("deed",                r"\bdeed\b"),
    ("easement",            r"\beasement\b"),
    ("escrow",              r"\bescrow\b"),
    ("loan",                r"\bloan\b|\bpromissory note\b|\bdeed of trust\b|\blender\b"),
    ("budget",              r"\bbudget\b|\bpro ?forma\b"),
    ("invoice",             r"\binvoice\b|\binv\b"),
    ("zoning",              r"\bzoning\b|\brezon(e|ing)\b|\bentitlement"),
    ("agreement",           r"\bagreement\b|\bagmt\b|\bcontract\b"),
    ("notice",              r"\bnotice\b"),
    ("letter",              r"\bletter\b|\bltr\b"),
    ("site plan",           r"\bsite plan\b|\bconcept plan\b"),
    ("map",                 r"\bmap\b|\baerial\b"),
    ("report",              r"\breport\b|\bstudy\b|\banalysis\b"),
]
_KINDS = [(k, re.compile(p)) for k, p in _KINDS]

# Words that change how a document should be READ, not what it is. An
# executed amendment and a redlined draft of the same amendment imply opposite
# things about where a deal stands.
_STATUS = [
    ("executed", re.compile(r"\bexecuted\b|\bfully executed\b|\bsigned\b")),
    ("draft",    re.compile(r"\bdraft\b|\bredline[sd]?\b|\brevised\b|\bcomments?\b")),
]

_PREFIX8 = re.compile(r"^((?:19|20)\d{2})(\d{2})(\d{2})(?=[ _\-.])")
_PREFIX6 = re.compile(r"^(\d{2})(\d{2})(\d{2})(?=[ _\-.])")


def name_date(name: str) -> str:
    """
    The date in a filename's YYMMDD or YYYYMMDD prefix, as YYYY-MM-DD, or ""
    when there is none or it is not a real date. A six-digit prefix is read
    as 20YY only when that is not in the future, else 19YY -- the library's
    oldest documents date from the late 1990s.
    """
    name = (name or "").strip()
    m = _PREFIX8.match(name)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _PREFIX6.match(name)
        if not m:
            return ""
        yy, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = 2000 + yy if 2000 + yy <= date.today().year + 1 else 1900 + yy
    try:
        found = date(y, mo, d)
    except ValueError:
        return ""
    if found.year < 1990 or found > date(date.today().year + 1, 12, 31):
        return ""
    return found.isoformat()


def kind(name: str) -> str:
    """
    The document kind its name states, or "". An email is labelled as one
    ("email about plat"): a message discussing a plat is not the plat, and
    this library holds over 100,000 archived emails.
    """
    low = (name or "").lower().replace("_", " ")
    found = next((label for label, pat in _KINDS if pat.search(low)), "")
    if low.endswith((".msg", ".eml")):
        return f"email about {found}" if found else "email"
    return found


def status(name: str) -> str:
    """'executed', 'draft', or "" -- from the name alone."""
    low = (name or "").lower().replace("_", " ")
    for label, pat in _STATUS:
        if pat.search(low):
            return label
    return ""


def property_folder(path: str) -> str:
    """The folder right under !PROPERTIES/<STATE>/, or "" outside that layout."""
    parts = (path or "").replace("\\", "/").split("/")
    if len(parts) >= 4 and parts[0].lower() == "!properties":
        return parts[2]
    return ""


def describe(path: str, name: str) -> dict:
    return {"name_date": name_date(name), "kind": kind(name),
            "status": status(name), "property": property_folder(path)}
