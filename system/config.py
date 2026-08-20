"""
config.py
---------
Central configuration for the Vaulter AI Property Intelligence System.
All paths, settings, and constants live here.

Cross-platform: automatically detects Windows or Mac and sets the correct paths.
To adapt this project to a new machine, only this file needs to be updated.

Secrets (.env) live in confidentials/, relative to the project folder on every OS.
outlook_token.json no longer applies -- email ingestion was removed in the 2026-07
rebuild; delete that file if it's still sitting in confidentials/ on this machine.

NEVER put real credentials directly in this file.
"""

import os
import shutil
import sys
from pathlib import Path
from dotenv import load_dotenv

# ─── Project Root ─────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

# ─── Secrets Folder ───────────────────────────────────────────────
# Canonical location is the project folder itself (same on every OS, and
# what the installer/setup wizard writes to) -- this used to be a
# DIFFERENT hardcoded path on Windows only, which was a real landmine:
# setup instructions told people to create confidentials/ inside the
# project, but the code only ever read from the hardcoded path, so
# secrets could look "set up" and silently never load, with no error.
#
# Windows still checks that old hardcoded path as a fallback -- ONLY if
# it already has a real .env in it and the project folder doesn't --
# so the one existing setup that predates this fix keeps working
# unchanged, without switching it out from under that machine.
_PROJECT_SECRETS_DIR = BASE_DIR / "confidentials"

if sys.platform == "win32":
    _LEGACY_WIN_SECRETS_DIR = Path(r"C:\Users") / os.environ.get("USERNAME", "YourName") / "Vaulter AI" / "confidentials"
    if not (_PROJECT_SECRETS_DIR / ".env").exists() and (_LEGACY_WIN_SECRETS_DIR / ".env").exists():
        SECRETS_DIR = _LEGACY_WIN_SECRETS_DIR
    else:
        SECRETS_DIR = _PROJECT_SECRETS_DIR
else:
    SECRETS_DIR = _PROJECT_SECRETS_DIR

SECRETS_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(SECRETS_DIR / ".env", override=True)

# ─── OneDrive: shared folder + document corpus ────────────────────
# Two different things live under the same synced OneDrive-for-Business
# account root, and they are NOT interchangeable:
#
#   SHARED_DIR ("Vaulter AI Shared")  -- this system's own OUTPUT. Screening
#       workbooks, proximity exports, update packages. Shared team-wide so one
#       person's screening run is visible to everyone instead of sitting only
#       on their own machine. This system writes here.
#
#   CORPUS_DIR -- the firm's actual SharePoint document library:
#       !PROPERTIES/<STATE>/<Property>/, CLOSING MEMOS, entity files.
#       This system only ever READS here, never writes.
#
# CORPUS_DIR is deliberately the document library itself and NOT the OneDrive
# account root. The root also contains the individual's own Desktop, Documents,
# Pictures, and "Microsoft Teams Chat Files" -- personal content that this
# system must never read or index. Scoping to the library subfolder is the
# privacy boundary, and corpus/index.py enforces it on every path it touches.
#
# Auto-detects "OneDrive - <Org>" (standard OneDrive-for-Business naming --
# same folder name for everyone, different C:\Users\<name>\ per person).
# Override either with VAULTER_SHARED_DIR / VAULTER_CORPUS_DIR in
# confidentials/.env if your OneDrive is named or located differently.

_LOCAL_FALLBACK_DIR = (BASE_DIR / "data" / "shared_fallback_not_synced").resolve()

# Just the standard OneDrive-for-Business prefix -- the organisation's own
# name is deliberately not here (see CORPUS_SUBFOLDER below for why). Real
# roots are found from OneDrive's own environment variables first, and by
# "<this prefix> - <Org>" glob only as a fallback.
ONEDRIVE_PREFIX      = "OneDrive"
SHARED_SUBFOLDER     = "Vaulter AI Shared"

# The document library's own folder name is deliberately NOT written here.
# This repo is public, and the library's display name identifies the firm's
# SharePoint site -- real account/tenant detail, the same category as a real
# Windows username. It is read from confidentials/.env (gitignored) when set,
# and otherwise detected by SHAPE rather than by name; see
# _find_corpus_subfolder. Set VAULTER_CORPUS_SUBFOLDER (just the folder name)
# or VAULTER_CORPUS_DIR (the full path) to pin it explicitly.
CORPUS_SUBFOLDER = os.getenv("VAULTER_CORPUS_SUBFOLDER", "").strip()

# Folders OneDrive creates for the individual, never a document library.
# Matched case-insensitively, and by prefix so "Microsoft <app> Chat Files"
# variants are all covered without listing each one as Microsoft adds them.
_PERSONAL_ONEDRIVE_FOLDERS = (
    "desktop", "documents", "pictures", "attachments", "meetings",
    "recordings", "apps", "microsoft", "music", "videos", "notebooks",
)


def _looks_like_personal_root(d: Path) -> bool:
    """
    True if `d` is somebody's OneDrive account root rather than a document
    library -- it holds the folders OneDrive creates for the individual.

    This exists because of a bug that nearly shipped on 2026-08-19. Detection
    identifies the firm's library as "the folder containing SHARED_SUBFOLDER",
    and on a real teammate's machine an EMPTY SHARED_SUBFOLDER was sitting at
    her OneDrive **root** (created by her own install, back when a missing
    library made it fall back there). Asking OneDrive's records which synced
    folder contained it therefore answered "the whole account root" -- which
    would have indexed her Desktop, Documents and Pictures. The marker is a
    good signal; it is not a good enough signal to override the one boundary
    this module exists to hold.

    Two of the three names is the test, not one: a library could plausibly
    contain a folder called Documents, but not Desktop and Pictures as well.
    """
    try:
        names = {c.name.lower() for c in d.iterdir() if c.is_dir()}
    except OSError:
        return False
    return sum(1 for n in ("desktop", "documents", "pictures") if n in names) >= 2


def _detect_onedrive_root() -> Path | None:
    """
    The synced OneDrive-for-Business account root, or None if not found.

    The organisation's own name is not written here, for the same reason the
    library's isn't (see CORPUS_SUBFOLDER above) -- it's the tenant identifier,
    and this repo is public. Not a loss: OneDrive publishes its real root in an
    environment variable, which is authoritative and beats any hardcoded guess
    anyway. The name-shaped fallbacks below match the standard
    OneDrive-for-Business pattern ("OneDrive - <Org>") by glob, so they work
    for any organisation rather than only this one.
    """
    candidates = []
    if sys.platform == "win32":
        # OneDrive itself publishes its root in env vars -- the authoritative
        # answer, correct even when the folder lives on another drive, under a
        # relocated profile, or under an unexpected tenant name.
        # OneDriveCommercial is the work account specifically; plain OneDrive
        # can point at a personal account, so it comes second and only counts
        # if its folder name looks like a business root ("OneDrive - <org>").
        # The glob stays as the last fallback for a machine where OneDrive is
        # synced but the env vars are missing (e.g. a service context).
        for var in ("OneDriveCommercial", "OneDrive"):
            v = os.environ.get(var, "").strip()
            if v and (var == "OneDriveCommercial" or " - " in Path(v).name):
                candidates.append(Path(v))
        username = os.environ.get("USERNAME", "YourName")
        profile = Path(os.environ.get("USERPROFILE", rf"C:\Users\{username}"))
        candidates.extend(sorted(profile.glob(f"{ONEDRIVE_PREFIX} - *")))
    else:
        home = Path.home()
        # Modern OneDrive for Mac syncs under ~/Library/CloudStorage/ with the
        # spaces stripped ("OneDrive-<Org>"); older versions/some configs use
        # ~/OneDrive - <Org> directly.
        candidates.extend(sorted((home / "Library" / "CloudStorage").glob(f"{ONEDRIVE_PREFIX}-*"))
                          if (home / "Library" / "CloudStorage").is_dir() else [])
        candidates.extend(sorted(home.glob(f"{ONEDRIVE_PREFIX} - *")))

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


ONEDRIVE_ROOT = _detect_onedrive_root()


def _dir_has_content(d: Path) -> bool:
    """Any file anywhere under d. Never raises -- runs at import time."""
    try:
        return any(d.rglob("*.*"))
    except OSError:
        return False


# WHY corpus detection failed, when it did -- "none_syncing" (no SharePoint
# library on this machine at all), "ambiguous" (several, and nothing identified
# ours), "unreadable", or "" when it succeeded. This exists because a single
# "library not available" flag has several causes and every message that guessed
# one got it wrong for somebody: telling a person their files aren't syncing when
# they plainly are sends them to fix a problem they do not have. Same rule the
# rest of this project follows -- describe the symptom, or distinguish the cause.
CORPUS_UNRESOLVED_REASON = ""


# The SharePoint address of the firm's document library, when the package that
# built this install knew it. Written into confidentials/.env by
# build_handoff.py; absent on a public clone, which is why nothing here
# hardcodes it. It is the ONLY identifier of the library that is the same on
# everybody's machine -- folder names vary, this does not.
LIBRARY_URL = os.getenv("VAULTER_LIBRARY_URL", "").strip()


def _narrow_to_library(folder: Path) -> Path:
    """
    Given a folder that IS the firm's library or CONTAINS it, return the one
    that actually is it.

    Two signals, strongest first, and the second one is the point:

      1. It holds this system's own shared folder. That is a marker we put
         there ourselves, so it is proof rather than inference.
      2. Its name carries the distinctive word from VAULTER_CORPUS_HINT. This
         is what makes a machine work when the marker has not synced into the
         library yet, or when the library is named differently here than on the
         machine that built the package -- which is the normal case, not the
         exception. Confirmed 2026-07-29 that colleagues see different names
         for the same library.

    Only ever descends ONE level, and only accepts a single unambiguous answer.
    Two candidates means this cannot tell them apart, and picking one would be
    guessing at which folder holds the firm's documents.

    Returns the folder it was given when nothing better is found, because the
    caller has already established the library is at or below it.
    """
    try:
        if (folder / SHARED_SUBFOLDER).is_dir():
            return folder
        children = [c for c in folder.iterdir() if c.is_dir()]
    except OSError:
        return folder

    marked = [c for c in children if _has_shared_folder(c)]
    if len(marked) == 1:
        return marked[0]

    hint = os.getenv("VAULTER_CORPUS_HINT", "").strip().lower()
    if hint:
        # The hint may equally describe the folder we are already standing in
        # (the maintainer's own mount is named for it), in which case there is
        # nothing to descend into.
        if hint in folder.name.lower() and not marked:
            return folder
        named = [c for c in children if hint in c.name.lower()]
        if len(named) == 1:
            return named[0]

    return folder


def _has_shared_folder(d: Path) -> bool:
    """Whether d contains this system's own shared folder. Never raises."""
    try:
        return (d / SHARED_SUBFOLDER).is_dir()
    except OSError:
        return False


def _library_from_onedrive_records() -> Path | None:
    r"""
    Ask OneDrive itself where it put the firm's document library.

    Windows keeps a map of every synced library -- local folder to SharePoint
    address -- under HKCU\Software\SyncEngines\Providers\OneDrive. Reading it
    beats looking around the OneDrive folder for three reasons:

      * it identifies the library by its SHAREPOINT ADDRESS, which is identical
        on every machine in the firm, rather than by a local folder name, which
        is not. Confirmed 2026-07-29 that colleagues see different spellings;
        confirmed 2026-08-13 that "Add shortcut to My files" produces a name of
        an entirely different shape;
      * it finds a library mounted OUTSIDE the OneDrive account folder, which
        nothing that walks that folder can ever do;
      * it is OneDrive's own answer rather than an inference from what is
        lying on disk.

    Returns None if the address is unknown, the records are unreadable, or the
    library simply is not synced here -- all normal, all handled by the
    detection that follows. Never raises: this runs at import time.
    """
    if sys.platform != "win32":
        return None

    def _norm(u: str) -> str:
        return u.strip().rstrip("/").lower()

    want = _norm(LIBRARY_URL) if LIBRARY_URL else ""

    # Every library OneDrive is syncing, as (address, local folder) pairs.
    mounts = []
    try:
        import winreg
        key = r"Software\SyncEngines\Providers\OneDrive"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as sub:
                        url = _norm(str(winreg.QueryValueEx(sub, "UrlNamespace")[0]))
                        mount = Path(str(winreg.QueryValueEx(sub, "MountPoint")[0]))
                        if mount.is_dir():
                            mounts.append((url, mount))
                except OSError:
                    continue
    except Exception:
        return None

    # The address matches exactly -- the strongest signal for WHICH library,
    # but it says nothing about WHERE IN IT this machine is mounted, and that
    # distinction cost a real teammate a working install (2026-08-20).
    #
    # OneDrive lets you sync a whole library or just a folder inside it, and
    # both record the SAME library address. The maintainer synced the firm's
    # folder itself; she synced the library above it. So the address matched
    # her mount perfectly and we handed back a folder one level too high --
    # which put our own shared folder out of view, made the team's data look
    # missing, and set the file index walking the wrong tree. Everything she
    # needed was on disk the whole time.
    #
    # So: trust the address to identify the library, then look at what is
    # actually there before deciding which folder it is.
    if want:
        for url, mount in mounts:
            if url == want:
                return _narrow_to_library(mount)

    # No address configured, or none matched. Fall back to asking the same
    # question of OneDrive's list that the folder search asks of the disk:
    # which of these libraries contains this system's own folder?
    #
    # Added 2026-08-19, and it is the answer to "what if the library is
    # somewhere else entirely -- another folder, another drive?". OneDrive
    # already knows where it mounted every library, so nothing has to be
    # guessed or hunted for: this finds it at any path, on any drive, at no
    # filesystem-walking cost at all. The previous version gave up here unless
    # VAULTER_LIBRARY_URL happened to be set, which many machines do not have
    # -- including the maintainer's own, measured the day this was written.
    #
    # Checked at the mount itself and one level inside it, because syncing a
    # parent site's default library mounts the parent and puts ours inside.
    # That is two directory listings per synced library, not a search.
    # Keyed by resolved path, because the SAME folder is often reachable more
    # than one way: OneDrive commonly records both a library and the account
    # root that contains it, so a plain list found the firm's library twice and
    # then refused it as "two candidates". Caught on the maintainer's own
    # machine, which records three entries for one library.
    found = {}
    for _url, mount in mounts:
        try:
            # A mount that is an account ROOT is never the library, however
            # convincing the marker inside it looks -- see
            # _looks_like_personal_root for the machine this was found on.
            if (mount / SHARED_SUBFOLDER).is_dir() and not _looks_like_personal_root(mount):
                found[mount.resolve()] = mount
                continue
            for child in mount.iterdir():
                if (child.is_dir() and (child / SHARED_SUBFOLDER).is_dir()
                        and not _looks_like_personal_root(child)):
                    found[child.resolve()] = child
        except OSError:
            continue

    # Exactly one, or nothing. Two GENUINELY different folders would mean this
    # cannot tell them apart, and guessing which one holds the firm's documents
    # is the thing this whole function exists to avoid.
    if len(found) == 1:
        return next(iter(found.values()))
    return None


# How far below the OneDrive root to look for the library, and how many folder
# listings that search may spend. Both are limits on purpose.
#
# Unlimited depth sounds strictly better and is not. This runs at import time,
# so its cost lands on the FIRST TOOL CALL OF EVERY CONVERSATION -- the same
# place five seconds was just removed from. Worse, the library holds hundreds of
# thousands of files that exist locally only as OneDrive placeholders, so an
# unbounded walk would list its entire tree, and a walk of the whole drive would
# read the person's private folders -- the exact boundary this module exists to
# keep. So: a few levels, a hard ceiling on listings, and OneDrive's own records
# (see _library_from_onedrive_records) for a library that is somewhere else
# entirely. Those records give the exact path on any drive at no walking cost,
# which is what actually answers "what if it is not under OneDrive at all";
# VAULTER_CORPUS_DIR pins it by hand for anything stranger still.
# Three rounds of descent, which reaches a library up to FOUR levels below the
# OneDrive root (measured, not inferred: level 4 is found, level 5 is not).
_MAX_LIBRARY_SEARCH_DEPTH = 3
_MAX_LIBRARY_SEARCH_LISTINGS = 200


def _search_below(roots: list) -> list:
    """
    Folders under `roots` that contain this system's own folder, searched
    breadth-first to _MAX_LIBRARY_SEARCH_DEPTH and stopping after
    _MAX_LIBRARY_SEARCH_LISTINGS directory listings.

    Breadth-first so the shallowest match wins, and a matching folder is never
    descended into -- without that, finding the library would immediately walk
    the library, which is the expensive thing this avoids.
    """
    matches, listings, level = [], 0, list(roots)
    for _depth in range(_MAX_LIBRARY_SEARCH_DEPTH):
        nxt = []
        for parent in level:
            if listings >= _MAX_LIBRARY_SEARCH_LISTINGS:
                return matches
            try:
                children = [c for c in parent.iterdir() if c.is_dir()]
            except OSError:
                continue
            listings += 1
            for child in children:
                try:
                    if (child / SHARED_SUBFOLDER).is_dir():
                        # Same refusal as the records route: a folder holding
                        # someone's Desktop and Pictures is not the library.
                        if not _looks_like_personal_root(child):
                            matches.append(child)   # found: do not go inside it
                    else:
                        nxt.append(child)
                except OSError:
                    continue
        if matches or not nxt:
            break
        level = nxt
    return matches


def _find_corpus_subfolder(onedrive_root: Path) -> Path | None:
    """
    The firm's synced SharePoint document library, found by shape not by name.

    Detected rather than hardcoded for two reasons. The first is
    confidentiality: this repo is public and the library's display name is
    real tenant detail (found 2026-08-11 sitting in tracked code). The second
    predates it -- the exact name isn't reliable anyway. Confirmed 2026-07-29
    that colleagues see different capitalization, so one machine's exact
    spelling was never safe to assume elsewhere.

    The shape rule: OneDrive names a synced SharePoint library
    "<Org> - <SiteName>", while every folder it creates for the individual is
    a plain single name (Desktop, Documents, Pictures, Microsoft Teams Chat
    Files). So a name containing " - ", excluding this system's own shared
    folder and the known personal ones, identifies a library without naming
    any specific one.

    Same discipline as CoStar column resolution and _detect_shared_dir: match
    the concept, and refuse rather than guess when genuinely ambiguous. A
    wrong answer here would point the whole system at the wrong folder, or --
    worse -- at personal content it must never index.
    """
    # An explicitly configured name always wins, and skips detection entirely.
    if CORPUS_SUBFOLDER:
        exact = onedrive_root / CORPUS_SUBFOLDER
        if exact.is_dir():
            return exact
        # The SAME name, one level down. The handoff package pre-sets this name
        # from the machine that built it, where the library sits at the account
        # root -- so on a machine where OneDrive nested it (inside `Documents`,
        # say) the pre-set name is CORRECT but the path is not, and this used to
        # report the named library as missing from a computer that has it.
        # Checked before giving up, and only ever accepting a folder of exactly
        # the configured name, so this cannot drift onto something else.
        try:
            for parent in onedrive_root.iterdir():
                if not parent.is_dir() or parent.name == SHARED_SUBFOLDER:
                    continue
                nested = parent / CORPUS_SUBFOLDER
                if nested.is_dir():
                    return nested
        except OSError:
            pass
        print(f"WARNING: VAULTER_CORPUS_SUBFOLDER is set to "
              f"'{CORPUS_SUBFOLDER}' but no such folder exists under "
              f"{onedrive_root}. Falling back to auto-detection.",
              file=sys.stderr)
        # Recorded so the setup wizard can SAY this, rather than reporting on
        # whatever library it fell back to as though that were the intended
        # one. This warning goes to stderr and scrolls past; the person then
        # sees a later step confidently describing the wrong library. Found
        # 2026-08-14 -- the package now names the library, so "the named one
        # is not on this machine" became a distinct and likely state.
        globals()["CORPUS_UNRESOLVED_REASON"] = "configured_missing"

    global CORPUS_UNRESOLVED_REASON

    # Ask OneDrive first. This is the only check that identifies the library by
    # something every machine in the firm agrees on, so it beats anything based
    # on where a folder sits or what it is called.
    from_records = _library_from_onedrive_records()
    if from_records is not None:
        return from_records

    try:
        top_level = [d for d in onedrive_root.iterdir()
                     if d.is_dir() and d.name != SHARED_SUBFOLDER]
        # Everything OneDrive put at the account root that could plausibly be a
        # library -- i.e. not this system's own shared folder, and not one of
        # the personal folders OneDrive makes for the individual. NOTE this is
        # deliberately NOT filtered by name shape yet; see below.
        possible = [
            d for d in top_level
            if not d.name.lower().startswith(_PERSONAL_ONEDRIVE_FOLDERS)
        ]
    except OSError:
        CORPUS_UNRESOLVED_REASON = "unreadable"
        return None

    # CONTENT BEATS NAME, and it is tried FIRST -- ours is the library that
    # contains the team's shared folder, whatever it happens to be called.
    #
    # This used to run only as a tie-break among names already matching the
    # "<Org> - <Site>" shape below, which made it useless in the case it was
    # written for. OneDrive only uses that shape when a library is added with
    # "Sync"; "Add shortcut to My files" names the folder after the library
    # itself, with no " - " anywhere -- so the firm's own library was invisible
    # to the shape filter and could never reach the content check. Found
    # 2026-08-13 after a teammate whose machine reported two synced libraries,
    # neither of which was ours.
    def _found(d):
        # Clear any earlier failure. A route succeeding AFTER an earlier one
        # failed means the earlier failure is no longer the story -- and
        # leaving it set made setup report a problem it had just solved:
        # a machine carrying a pre-set library name that does not match, but
        # whose library detection then finds it anyway, was still told the
        # library was not on the computer. Found 2026-08-18.
        global CORPUS_UNRESOLVED_REASON
        CORPUS_UNRESOLVED_REASON = ""
        return d

    with_shared = [d for d in possible if (d / SHARED_SUBFOLDER).is_dir()]
    if len(with_shared) == 1:
        return _found(with_shared[0])

    # AND ONE LEVEL DEEPER, because the library is not always at the top.
    #
    # How OneDrive lays this out depends on WHAT was synced. Sync the firm's
    # library itself and it lands at the account root, which is the only shape
    # this used to handle. Sync the parent site's default "Documents" library
    # instead -- an equally normal thing to click -- and the firm's library
    # arrives as a FOLDER INSIDE it, one level down, holding exactly the same
    # documents.
    #
    # Measured on a real teammate's machine 2026-08-18, and it explains every
    # symptom she had: the content check found nothing at the top level, so
    # detection fell through to the name-shape rule, which matched the parent
    # "<Org> - Documents" folder and indexed THAT -- picking up her real
    # property documents (they were inside it) while never finding the team
    # folder one level below. Everything looked half-right, which is why it
    # took three rounds to see.
    #
    # Two levels is deliberately the limit: each extra level is a directory
    # listing over OneDrive placeholders, and nothing legitimate is deeper.
    # This descends into EVERY top-level folder, personal ones included, and
    # that distinction is the bug this fixes (found 2026-08-19 on a second
    # teammate's machine). The nested search used to walk `possible`, which
    # excludes anything called Desktop/Documents/Pictures/... -- so a library
    # sitting inside a folder literally named `Documents` was skipped before
    # the search began. Measured: her layout was NOT FOUND on the code of the
    # day, while both known-good layouts were.
    #
    # Descending into a personal folder is not the same as indexing one, and
    # only the second is dangerous. What gets returned is the CHILD, and only
    # a child that itself contains the team's shared folder -- a specific
    # signal this system put there, not a guess about what a folder is. The
    # personal-folder exclusion still applies in full to candidates chosen by
    # NAME below, where there is no such signal to rely on.
    if not with_shared:
        deeper = _search_below(top_level)
        if len(deeper) == 1:
            return _found(deeper[0])
        with_shared = deeper

    # LOOK FOR THE LIBRARY BY ITS OWN DISTINCTIVE WORD, at the top level and one
    # folder down (2026-08-20). Until now the hint was only a tie-break between
    # two same-shaped names at the top level, which meant it could not help in
    # the case it matters most: the firm's folder sitting inside a synced parent
    # library, under a name that does not match the one the package was built
    # with. That is the normal shape, not the exception -- OneDrive names a
    # folder after whatever was synced, and colleagues sync different levels.
    #
    # Deliberately AFTER the marker searches above, so a folder proven to hold
    # this system's own shared folder always wins over one that merely has the
    # right word in its name. And deliberately requiring exactly one match:
    # two folders carrying the word means this cannot tell them apart, and
    # guessing which holds the firm's documents is the whole thing to avoid.
    #
    # Personal folders are still excluded as candidates -- descending through
    # one to reach a library is fine, returning one never is.
    hint_word = os.getenv("VAULTER_CORPUS_HINT", "").strip().lower()
    if not with_shared and hint_word:
        by_name = [d for d in possible if hint_word in d.name.lower()]
        if not by_name:
            for parent in top_level:
                try:
                    by_name += [c for c in parent.iterdir()
                                if c.is_dir() and hint_word in c.name.lower()
                                and not _looks_like_personal_root(c)]
                except OSError:
                    continue
        if len(by_name) == 1:
            return _found(by_name[0])

    candidates = [d for d in possible if " - " in d.name]

    if not candidates:
        CORPUS_UNRESOLVED_REASON = "none_syncing"

    if len(candidates) == 1:
        return _found(candidates[0])

    if len(candidates) > 1:
        # More than one library synced and the content check above did not
        # single one out (either none holds the shared folder, or oddly several
        # do). Last resort: a name fragment the firm can set once in
        # confidentials/.env (gitignored), e.g. VAULTER_CORPUS_HINT=<site>.
        # Kept out of the code and out of .env.template, both of which are
        # public; a hint here is opt-in and never travels with the source.
        hint = os.getenv("VAULTER_CORPUS_HINT", "").strip().lower()
        if hint:
            hinted = [d for d in candidates if hint in d.name.lower()]
            if len(hinted) == 1:
                return _found(hinted[0])

        # Deliberately does NOT print the folder names: this message can reach
        # a log or a screen share, and the names are the tenant detail being
        # protected. The count is enough to tell the user to pick one.
        print(f"WARNING: found {len(candidates)} synced SharePoint libraries "
              f"under {onedrive_root}, and none of them contains a "
              f"'{SHARED_SUBFOLDER}' folder, so this can't tell which is the "
              f"firm's document library. Set VAULTER_CORPUS_SUBFOLDER (the "
              f"folder name), VAULTER_CORPUS_DIR (the full path), or "
              f"VAULTER_CORPUS_HINT (any distinctive word from the folder "
              f"name) in confidentials/.env.", file=sys.stderr)
        CORPUS_UNRESOLVED_REASON = "ambiguous"
    return None


def _detect_shared_dir() -> Path:
    override = os.getenv("VAULTER_SHARED_DIR", "").strip()
    if override:
        return Path(override)

    # PREFERRED: inside the firm's document library.
    #
    # The library is a synced SharePoint library -- every teammate with access
    # already has it on disk. Putting the shared folder there means a new
    # teammate gets the portfolio data, the CoStar drop folder and everyone's
    # output automatically, with no folder to share and no "Add shortcut to My
    # files" click. That click was the last manual step in onboarding, and the
    # one most likely to be skipped or done wrong.
    #
    # The carve-out that makes this safe: corpus/index.py skips this folder by
    # name, so nothing in it is ever indexed and it can never surface in a
    # document search. It sits inside the library on disk, but it is NOT part
    # of the document corpus -- this system's own space, walled off.
    #
    # Only used if it already exists. This code never creates it inside the
    # library: doing so would mean every install silently writing a new folder
    # into the firm's document store. Whoever sets Vaulter AI up creates it
    # once, deliberately.
    if ONEDRIVE_ROOT:
        corpus = _find_corpus_subfolder(ONEDRIVE_ROOT)
        if corpus:
            in_library = corpus / SHARED_SUBFOLDER
            try:
                if in_library.is_dir():
                    return in_library
            except OSError:
                pass

    if ONEDRIVE_ROOT:
        exact = ONEDRIVE_ROOT / SHARED_SUBFOLDER
        if _dir_has_content(exact):
            return exact

        # The exact name is missing or empty. That's the signature of a
        # specific, likely situation, not a rare one: "Vaulter AI Shared" is an
        # ordinary folder in one person's OneDrive, not a synced SharePoint
        # library, so a teammate only gets it by being shared the folder and
        # using OneDrive's "Add shortcut to My files". But THIS code's own
        # mkdir below will already have created an empty folder under the exact
        # name on first run -- so that shortcut collides and OneDrive lands it
        # as "Vaulter AI Shared 1". Preferring a same-prefixed sibling that
        # actually has content is what stops the empty decoy winning forever.
        #
        # Same shape as _find_corpus_subfolder's own detection: match the
        # concept, and refuse to guess when genuinely ambiguous.
        try:
            variants = [d for d in ONEDRIVE_ROOT.iterdir()
                        if d.is_dir() and d != exact
                        and d.name.startswith(SHARED_SUBFOLDER)
                        and _dir_has_content(d)]
        except OSError:
            variants = []

        if len(variants) == 1:
            print(f"NOTE: using '{variants[0].name}' as the team's shared folder — "
                  f"'{SHARED_SUBFOLDER}' exists but is empty, which usually means a "
                  f"shared-folder shortcut was added alongside it.", file=sys.stderr)
            return variants[0]
        if len(variants) > 1:
            print(f"WARNING: found {len(variants)} candidate shared folders under "
                  f"{ONEDRIVE_ROOT} ({[v.name for v in variants]}) — can't tell which is "
                  f"the team's. Using '{SHARED_SUBFOLDER}'; set VAULTER_SHARED_DIR in "
                  f"confidentials/.env to pick one explicitly.", file=sys.stderr)

        # NOTHING at the OneDrive root holds the team folder. Do NOT return this
        # path, and above all do not let the mkdir below CREATE it (2026-08-19).
        #
        # The root was the folder's real home until 2026-08-03, when it moved
        # INSIDE the document library so that syncing the library would bring it
        # along and nobody would need a shared-folder shortcut. Returning the
        # root afterwards was a leftover from the old design, and it did active
        # harm rather than nothing:
        #
        #   * it created an empty folder in the person's OneDrive that nobody
        #     asked for, which then looks like the team's to anyone browsing;
        #   * SHARED_DIR_IS_FALLBACK stayed False, so the health check's blunt
        #     "NOT connected" never fired and the machine read as fine;
        #   * UPDATES_DIR lives under here, so the machine quietly read its
        #     update channel from its own empty folder and was NEVER offered an
        #     update -- meaning no fix could reach it, including a fix for this.
        #     Found on a real teammate's machine 2026-08-19.
        #
        # The local fallback is the honest answer instead: it keeps the program
        # working, it is named for what it is, and it makes the health check say
        # the team folder is not connected -- which is true. A root folder that
        # genuinely HAS content is still preferred, above, so a machine set up
        # before the move keeps working exactly as it did.
        return _LOCAL_FALLBACK_DIR

    # OneDrive not found on this machine -- fall back to a local folder so
    # nothing crashes, but this means screening results won't actually be
    # shared with the team until VAULTER_SHARED_DIR is set correctly.
    return _LOCAL_FALLBACK_DIR

SHARED_DIR = _detect_shared_dir()
try:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
except OSError as e:
    # This runs at config.py's IMPORT time -- every entry point imports
    # this module first, so an unguarded failure here (a transient
    # OneDrive sync/permission hiccup, a network path briefly
    # unreachable, etc.) would crash the ENTIRE application before it
    # can even start. Fall back to a local folder instead -- screening
    # results just won't be shared with the team until this is resolved,
    # same degraded-but-working behavior as "OneDrive not found at all."
    # Deliberately sys.stderr, never stdout/logging: this runs before
    # main.py sets up file-only logging in MCP mode, and any stray stdout
    # write here would corrupt the MCP stdio connection to Claude Desktop.
    print(f"WARNING: could not create shared folder at {SHARED_DIR} ({e}) -- "
          f"falling back to a local-only folder. Screening results will not "
          f"be shared with the team until this is fixed.", file=sys.stderr)
    SHARED_DIR = _LOCAL_FALLBACK_DIR
    SHARED_DIR.mkdir(parents=True, exist_ok=True)

# Whether SHARED_DIR is actually the real OneDrive-backed folder, or the
# local fallback used when OneDrive wasn't found / couldn't be written to.
# Exposed so the health-check tool (mcp_server.py) can report "silently
# fell back to local" without reaching into this module's private
# _LOCAL_FALLBACK_DIR/_detect_shared_dir internals.
SHARED_DIR_IS_FALLBACK = (SHARED_DIR == _LOCAL_FALLBACK_DIR)


# The firm's document library. Read-only, and deliberately NOT mkdir'd:
# if it isn't there, that means OneDrive isn't syncing the document library on
# this machine, which is a real condition for check_system_health to report
# -- creating an empty folder would just hide it and make every search
# silently return nothing.
_corpus_override = os.getenv("VAULTER_CORPUS_DIR", "").strip()
if _corpus_override:
    CORPUS_DIR = Path(_corpus_override)
elif ONEDRIVE_ROOT:
    CORPUS_DIR = _find_corpus_subfolder(ONEDRIVE_ROOT)
else:
    CORPUS_DIR = None

CORPUS_AVAILABLE = CORPUS_DIR is not None and CORPUS_DIR.is_dir()

# Local (per-machine) cache of the corpus's file/folder NAMES only -- never
# file contents. See corpus/index.py for why this exists: the library is
# synced as OneDrive Files On-Demand placeholders, so walking it live is
# slow, and reading contents downloads them.
#
# SQLite rather than JSON because the library turned out to hold ~400,000
# files -- a JSON index is ~60MB and would have to be parsed in full on
# every single search.
CORPUS_INDEX_FILE = (BASE_DIR / "data" / "corpus_index.db").resolve()

# ─── Data Folders ─────────────────────────────────────────────────

DATA_DIR       = (BASE_DIR / "data").resolve()
LOG_DIR        = DATA_DIR / "logs"

# Where a CoStar export or broker spreadsheet gets dropped so screen_listings
# can find it by filename. Just a folder -- nothing watches it. The old
# data/watched_folder/<State>/<Property>/ tree and its watcher thread are
# gone; documents live in CORPUS_DIR now and are read in place.
DROP_DIR = DATA_DIR / "drop"
DROP_DIR.mkdir(parents=True, exist_ok=True)

# Pre-rebuild locations, still searched when resolving a CoStar file by name
# so exports already sitting on an existing machine don't become invisible
# after an update. Nothing writes here any more.
LEGACY_WATCH_DIR     = DATA_DIR / "watched_folder"
LEGACY_PROCESSED_DIR = DATA_DIR / "processed"

# CoStar listing screener & proximity search — both produce outputs that are
# SHARED (under SHARED_DIR) on purpose so one person's analysis run is visible
# to the whole team instead of sitting only on their own machine, avoiding
# duplicate API calls and letting the team benefit from each run.
#
# There used to be a local OUTPUT_DIR (data/output) here too. Screening output
# moved to SHARED_DIR and nothing read the local one any more -- it just kept
# creating an empty folder. MEETINGS_DIR went the same way: it was for the
# meeting-transcript feature that was never built and has since been retired,
# and because it lived under SHARED_DIR it created an empty "meetings" folder
# in the team's OneDrive on every import, on every machine.
# ── Shape of the shared folder (2026-08-03) ───────────────────────────────
# It had grown to eight sibling folders at the top level, mixing three very
# different things: what a person DROPS IN, what they should GO LOOK AT, and
# machinery nobody should ever need to open. Now grouped so the top level
# answers "where do I put things / where do I find results" at a glance:
#
#   Vaulter AI Shared/
#     CoStar Drop/           <- inputs, deliberately kept at the top level so
#     Smartsheet Portfolio/     they stay easy to find and drop files into
#     property_summaries/    <- long-lived team KNOWLEDGE, not run output
#     output/                <- what a RUN produces, regenerated each time
#       proximity/  screening/  screening_decisions/
#     system/                <- machinery; nobody should need to open this
#       geo_cache/  org_settings/  updates/
#
# Inputs stay at the top on purpose: burying the drop folder is exactly the
# problem that made teammates paste files into the conversation instead (see
# COSTAR_DROP_DIR below for what that cost).
#
# `output/` means "produced by a run" (2026-08-10). Anything in it can be
# deleted and regenerated by re-running the thing that made it. That's why
# property_summaries moved OUT to the top level -- it's curated knowledge
# that took human review to write and can't be regenerated -- while
# screening_decisions stays IN, as a sibling of screening/ rather than mixed
# into it: the notes belong to a run, but must survive that run being redone.
SHARED_OUTPUT_DIR = SHARED_DIR / "output"
SHARED_SYSTEM_DIR = SHARED_DIR / "system"

PROXIMITY_OUTPUT_DIR  = SHARED_OUTPUT_DIR / "proximity"
SCREENING_OUTPUT_DIR  = SHARED_OUTPUT_DIR / "screening"

# The team's own notes on what they decided about a screen, kept in their own
# folder rather than mixed in with the workbooks: one is machine output that
# gets regenerated, the other is human judgment that must never be
# overwritten by a re-run. Each notes file is named after the screening run
# it belongs to (fit_screen_<export>.md beside fit_screen_<export>.xlsx), so
# the pairing is obvious in a file listing without opening anything.
SCREENING_DECISIONS_DIR = SHARED_OUTPUT_DIR / "screening_decisions"

PROXIMITY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SCREENING_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SCREENING_DECISIONS_DIR.mkdir(parents=True, exist_ok=True)

# Basemap tiles and Overpass/federal lookups, cached per rounded bounding box
# so two properties in the same area don't re-fetch the same data. Was
# hardcoded as `Path(SHARED_DIR) / "geo_cache"` in three separate modules,
# against this file's own "nothing else hardcodes a path" rule -- which is
# also why it was the one shared folder that couldn't be moved without
# hunting down every copy.
GEO_CACHE_DIR = SHARED_SYSTEM_DIR / "geo_cache"
GEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Per-property research summaries -- built 2026-07-30 as a byproduct of
# vaulter-document-reader's normal work, never a separate ingestion pass.
# The first real question about a property costs a full document read; that
# agent writes what it found here, cited, so every later question (from any
# user) reads a few hundred tokens instead of re-reading the same documents.
# Deliberately lazy and demand-driven -- properties nobody asks about never
# get a file here, so this never becomes a second full-corpus copy.
#
# TOP LEVEL, not under output/ (moved 2026-08-10). `output/` is for what a
# RUN produces -- screening workbooks, proximity exports, regenerated every
# time someone re-runs them. These summaries are the opposite: long-lived
# team knowledge, written once by a human-reviewed read and appended to over
# months. Filing them under "output" invited exactly the wrong mental model
# (disposable, machine-generated) for the most carefully-curated thing the
# team has. The same reasoning covers `_passed-on-deals.md`, which lives
# here too.
PROPERTY_SUMMARIES_DIR = SHARED_DIR / "property_summaries"
PROPERTY_SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

# The firm's own portfolio data: the Smartsheet Project Master export, the
# hand-verified coordinates table, and the property-details fallback list.
#
# SHARED as of 2026-08-03, and for a measured reason. The handoff package
# (scripts/build_handoff.py) deliberately ships no firm data, so a brand-new
# teammate's install had none of this -- `python main.py stats` on a fresh
# machine reported "Portfolio: unavailable", every property question came back
# empty, and proximity-by-name refused for every property, until someone
# hand-delivered them files. Dropping the Smartsheet export here once makes it
# work for the whole team, through the same OneDrive folder they already trust
# with screening and proximity output.
#
# Local copies under DATA_DIR/project_master still win (see portfolio.py's
# _portfolio_dirs) -- this is the fallback that makes a fresh install useful,
# not a replacement for a file someone deliberately put on their own machine.
SMARTSHEET_PORTFOLIO_DIR = SHARED_DIR / "Smartsheet Portfolio"
SMARTSHEET_PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)

# Where CoStar exports and broker spreadsheets get dropped for screening.
#
# SHARED as of 2026-08-03, and for a measured reason. The local drop folder
# (DROP_DIR below) sits at <install>/system/data/drop -- somewhere no
# non-technical person will ever navigate to. The observed consequence: a
# teammate attached exports to the Claude conversation instead, so Claude
# base64-encoded them and passed them through as file_content_b64, which cost
# ~43,000 tokens for one real 216-row export purely to hand the file over. A
# folder people can actually find is the fix for that.
#
# In OneDrive beside every other Vaulter AI folder, so "everything this system
# touches lives in Vaulter AI Shared" stays true and there is one place to
# look. Shared also means one person's export is screenable by the whole team
# without re-sending it.
#
# This folder is searched FIRST (see _resolve_costar_source), so it is the
# source of truth. DROP_DIR is only a fallback now, and exists for one real
# reason: a file pasted into a conversation has to land somewhere, and it must
# not land here -- one person's paste should not appear in the team's folder.
# It is otherwise expected to stay empty.
COSTAR_DROP_DIR = SHARED_DIR / "CoStar Drop"
COSTAR_DROP_DIR.mkdir(parents=True, exist_ok=True)

# Priority 4 (docs/MULTI_USER_TRANSITION.md) — auto-update. UPDATES_DIR is
# where release.py (run by whoever ships a reviewed fix) publishes a new
# version's code package + version marker; every instance's scheduler
# reads from there. Deliberately the same shared OneDrive location as
# everything else shared across the team, not a new channel.
# PENDING_UPDATE_DIR is LOCAL (per machine) -- where an update gets
# staged once downloaded, before a human decides to actually apply it.
UPDATES_DIR        = SHARED_SYSTEM_DIR / "updates"
PENDING_UPDATE_DIR = DATA_DIR / "pending_update"

UPDATES_DIR.mkdir(parents=True, exist_ok=True)
PENDING_UPDATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Org-wide settings (e.g. a new feature's API key) distribution -- a
# separate channel from the code-update one above, on purpose. A code
# update package deliberately never includes confidentials/ (see
# release.py's EXCLUDED_DIR_NAMES) specifically so one person's own
# filled-in .env can never accidentally ship to every other
# instance. Org-wide settings need
# the opposite property -- everyone SHOULD end up with the same
# value -- so this uses its own small, deliberate publish tool
# (scripts/push_org_setting.py, run by the maintainer only) rather
# than reusing release.py's blanket packaging.
# ORG_SETTINGS_DIR is SHARED (every instance reads it, from
# check_system_health -- there is no scheduler any more).
# PENDING_SETTINGS_DIR is LOCAL -- staged here once downloaded, and
# only written into confidentials/.env after a human says yes (see
# apply_pending_settings in mcp_server.py).
ORG_SETTINGS_DIR     = SHARED_SYSTEM_DIR / "org_settings"
PENDING_SETTINGS_DIR = DATA_DIR / "pending_settings"

ORG_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
PENDING_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)

# Where each install leaves a small note about itself -- which version it is
# running, when it was last active, and whether its library/portfolio/index
# are in good shape. One file per machine, written by check_system_health at
# the start of a conversation (there is no background process, per this
# project's own rule), and read back by get_install_status.
#
# Shared rather than local for the obvious reason: the whole point is to see
# machines OTHER than this one. It lives under SHARED_SYSTEM_DIR ("machinery;
# nobody should need to open this") -- the readable view is what
# get_install_status reports in the conversation, not these raw files.
#
# There WAS an HTML page too, dropped 2026-08-19. It was a file, so it only
# refreshed when someone asked for it -- and asking already produces the current
# answer in the conversation. A page that can only ever be as fresh as the last
# request, for a feature whose whole job is spotting stale machines, was the
# weaker half of the pair and worth deleting rather than maintaining.
#
# Nothing here is confidential -- a Windows account name, a computer name, a
# version and some yes/no flags -- but note it never travels to this repo: it
# is written to OneDrive only, and the repo has no copy to ignore.
INSTALLS_DIR = SHARED_SYSTEM_DIR / "installs"

INSTALLS_DIR.mkdir(parents=True, exist_ok=True)

# Which release channel this instance follows -- "general" (the default)
# only picks up versions that have been explicitly promoted after a
# canary check; "canary" picks up every new release immediately, before
# it's been confirmed healthy anywhere else. Set on a small number of
# designated machines only (e.g. the maintainer's own), via
# confidentials/.env -- most instances should stay on "general".
VAULTER_UPDATE_CHANNEL = os.getenv("VAULTER_UPDATE_CHANNEL", "general").strip().lower()
if VAULTER_UPDATE_CHANNEL not in ("general", "canary"):
    VAULTER_UPDATE_CHANNEL = "general"

# ─── OCR Settings ─────────────────────────────────────────────────
# Auto-detected rather than hardcoded to one exact install location --
# a hardcoded exact-version path (e.g. a specific "poppler-26.02.0")
# silently broke for anyone whose installer put a different version
# anywhere else, and a hardcoded Homebrew path broke on Intel Macs
# (Homebrew installs to /usr/local on Intel, /opt/homebrew on Apple
# Silicon -- the old hardcoded path only ever covered the latter).
#
# Searches PATH first (covers a standard install on either platform/
# architecture), then a few common install locations, including the
# OLD hardcoded paths so a setup that predates this fix keeps working
# unchanged. TESSERACT_PATH falls back to plain "tesseract" and
# POPPLER_PATH to None if genuinely not found anywhere -- extractor.py's
# pytesseract/pdf2image calls already treat both as "just search PATH at
# the moment OCR actually runs" in that case, so this degrades to
# exactly the same behavior a bare `tesseract`/`pdftoppm` on PATH would
# give, rather than crashing on an invalid hardcoded path. Only
# scanned-PDF OCR is affected if the tools truly aren't installed;
# digital-text PDFs never touch this at all.


def _find_executable(names: list, extra_dirs: list) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for d in extra_dirs:
        for name in names:
            candidate = d / name
            if candidate.exists():
                return str(candidate)
    return ""


def _find_poppler_bin_dir(extra_dirs: list) -> str:
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        return str(Path(pdftoppm).parent)
    for d in extra_dirs:
        if (d / "pdftoppm.exe").exists() or (d / "pdftoppm").exists():
            return str(d)
    return ""


if sys.platform == "win32":
    _username = os.environ.get("USERNAME", "YourName")
    _tesseract_dirs = [
        Path(r"C:\Program Files\Tesseract-OCR"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR"),
        Path(r"C:\Users") / _username / r"AppData\Local\Programs\Tesseract-OCR",
        Path(r"C:\Users") / _username / r"Packages\Tesseract-OCR",  # old hardcoded location
    ]
    TESSERACT_PATH = _find_executable(["tesseract.exe", "tesseract"], _tesseract_dirs) or "tesseract"

    # setup_wizard.py's auto-installer (2026-08-03) extracts the official
    # release zip to AppData\Local\Programs\poppler\poppler-<version>\Library\bin
    # -- verified against the real release's own internal folder layout, not
    # assumed. Checked first since it's where a fresh auto-install lands;
    # Packages\poppler* is kept for any machine set up before that existed.
    _poppler_dirs = []
    _programs_poppler_dir = Path(r"C:\Users") / _username / r"AppData\Local\Programs\poppler"
    if _programs_poppler_dir.exists():
        _poppler_dirs += [d / "Library" / "bin" for d in _programs_poppler_dir.glob("poppler*") if d.is_dir()]
    _packages_dir = Path(r"C:\Users") / _username / "Packages"
    if _packages_dir.exists():
        _poppler_dirs += [d / "Library" / "bin" for d in _packages_dir.glob("poppler*") if d.is_dir()]
    POPPLER_PATH = _find_poppler_bin_dir(_poppler_dirs) or None
else:
    _mac_dirs = [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    TESSERACT_PATH = _find_executable(["tesseract"], _mac_dirs) or "tesseract"
    POPPLER_PATH = _find_poppler_bin_dir(_mac_dirs) or None

if TESSERACT_PATH == "tesseract" and not shutil.which("tesseract"):
    print("WARNING: Tesseract OCR was not found anywhere -- scanned/image-only PDF pages "
          "will not be readable until it's installed. See README.md's Setup section.",
          file=sys.stderr)
if POPPLER_PATH is None:
    print("WARNING: Poppler was not found anywhere -- scanned/image-only PDF pages "
          "will not be readable until it's installed. See README.md's Setup section.",
          file=sys.stderr)

# Make both OCR tools reachable by NAME (pdftoppm, tesseract) for anything this
# process spawns, not just for code that imports the explicit paths above.
# Found 2026-08-04: poppler was installed but on no PATH, so every shell,
# helper script, and subprocess that tried `pdftoppm` failed and had to invent
# its own fallback -- the tool existed and went unused. extract.py never needed
# this (it passes poppler_path= explicitly); this is for everything else.
# Process-local only: os.environ here never touches the user's persistent PATH.
for _tool_dir in (POPPLER_PATH, Path(TESSERACT_PATH).parent if TESSERACT_PATH != "tesseract" else None):
    if _tool_dir and str(_tool_dir) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{_tool_dir}{os.pathsep}" + os.environ.get("PATH", "")

# ══════════════════════════════════════════════════════════════════
# API Keys — there are none
# ══════════════════════════════════════════════════════════════════
# This project calls no paid or keyed service. Document search is local,
# ranking is arithmetic, ground truth is federal open data (FEMA, Census,
# USGS), proximity is OpenStreetMap, and the qualitative read happens in the
# Claude conversation that asked for it. A blank confidentials/.env works.
#
# What used to be here, and why it went:
#   OUTLOOK_CLIENT_ID / TENANT_ID / CLIENT_SECRET -- email ingestion removed.
#   GOOGLE_MAPS_API_KEY   -- ground truth is federal open data now.
#   ANTHROPIC_API_KEY     -- the 4-phase pipeline made its own Claude API
#       calls; it is deleted, and nothing here calls an LLM.
#   GOOGLE_PLACES_API_KEY -- the proximity tool moved to keyless Overpass and
#       stopped reading it. The constant outlived its last reader.
#
# Adding a key back means adding a real dependency on someone's billing. Weigh
# that against a free equivalent first -- every one of the above had one.

# ══════════════════════════════════════════════════════════════════
# MCP Server
# ══════════════════════════════════════════════════════════════════

# No API key or port here on purpose: each staff member runs their own
# fully-local instance of this server, launched directly by their own
# Claude Desktop via stdio (see mcp_server.py's header). Nothing is
# exposed over a network, so there's no request to gate with a shared
# secret and no port to listen on -- the real access boundary is simply
# "is this your own computer, logged in as you."

# ─── Proximity Search ────────────────────────────────────────────
# Categories and settings for the proximity_search MCP tool.
# Edit PROXIMITY_CATEGORIES to add/remove/change search categories.
#
# `osm_tags` is what the tool actually queries — OpenStreetMap key=value
# pairs, fetched via Overpass, free and keyless. `google_types` is retained
# only as documentation of what each category originally meant under the
# Google Places API; nothing reads it any more.
# OSM tag reference: https://wiki.openstreetmap.org/wiki/Map_features
#
# Note the ordering matters: a feature is classified into the FIRST category
# whose tags it matches (see proximity_tool._classify), so narrower
# categories must come before broader ones that would also match them.

PROXIMITY_DEFAULT_RADIUS_MILES          = 5.0
PROXIMITY_SUMMARY_RESULTS_PER_CATEGORY  = 10
PROXIMITY_GEOCODING_TIMEOUT             = 10
PROXIMITY_PLACES_REQUEST_DELAY          = 0.15

PROXIMITY_CATEGORIES = [
    {"label": "Shopping Mall & Outlets",       "icon": "🏬", "color": "#C0392B",
     "google_types": ["shopping_mall"],
     "osm_tags": ["shop=mall"]},
    {"label": "Grocery & Specialty Food",      "icon": "🛍️", "color": "#27AE60",
     "google_types": ["grocery_or_supermarket", "supermarket"],
     "osm_tags": ["shop=supermarket", "shop=greengrocer", "shop=butcher"]},
    {"label": "Retail & Big Box",              "icon": "🛒", "color": "#E74C3C",
     "google_types": ["supermarket", "department_store", "shopping_mall",
                      "home_goods_store", "hardware_store", "warehouse_store"],
     "osm_tags": ["shop=department_store", "shop=doityourself", "shop=hardware",
                  "shop=wholesale", "shop=furniture", "shop=variety_store"]},
    {"label": "Hospitality",                   "icon": "🏨", "color": "#9B59B6",
     "google_types": ["lodging"],
     "osm_tags": ["tourism=hotel", "tourism=motel", "tourism=resort"]},
    {"label": "Industrial & Logistics",        "icon": "🏭", "color": "#F39C12",
     "google_types": ["storage", "moving_company"],
     "osm_tags": ["landuse=industrial", "building=warehouse",
                  "industrial=warehouse", "amenity=storage_rental"]},
    {"label": "Major Corporate HQ",            "icon": "🏢", "color": "#2C3E50",
     "google_types": ["corporate_office"],
     "osm_tags": ["office=company", "office=corporate"]},
    {"label": "Technology & Innovation",       "icon": "💻", "color": "#1A5276",
     "google_types": ["electronics_store"],
     "osm_tags": ["office=it", "shop=electronics", "office=research"]},
    {"label": "Healthcare",                    "icon": "🏥", "color": "#2ECC71",
     "google_types": ["hospital", "doctor", "pharmacy", "health"],
     "osm_tags": ["amenity=hospital", "amenity=clinic", "amenity=doctors",
                  "amenity=pharmacy"]},
    {"label": "School & University",           "icon": "🎓", "color": "#3498DB",
     "google_types": ["school", "university", "secondary_school"],
     "osm_tags": ["amenity=school", "amenity=university", "amenity=college"]},
    {"label": "Military Base",                 "icon": "🪖", "color": "#6D4C41",
     "google_types": ["local_government_office"],
     "osm_tags": ["landuse=military", "military=base"]},
    {"label": "Government & Civic",            "icon": "🏛️", "color": "#5D6D7E",
     "google_types": ["city_hall", "local_government_office",
                      "courthouse", "post_office", "fire_station"],
     "osm_tags": ["amenity=townhall", "amenity=courthouse", "amenity=post_office",
                  "amenity=fire_station", "amenity=police", "office=government"]},
    {"label": "Sports & Entertainment",        "icon": "🏟️", "color": "#E67E22",
     "google_types": ["stadium", "amusement_park", "movie_theater", "casino"],
     "osm_tags": ["leisure=stadium", "tourism=theme_park", "amenity=cinema",
                  "amenity=casino", "leisure=sports_centre"]},
    {"label": "Gas & Convenience",             "icon": "⛽", "color": "#F1C40F",
     "google_types": ["gas_station", "convenience_store"],
     "osm_tags": ["amenity=fuel", "shop=convenience"]},
    {"label": "Restaurant & QSR",              "icon": "🍔", "color": "#1ABC9C",
     "google_types": ["restaurant", "meal_takeaway", "cafe", "bakery"],
     "osm_tags": ["amenity=restaurant", "amenity=fast_food", "amenity=cafe",
                  "shop=bakery"]},
    {"label": "Financial Services",            "icon": "🏦", "color": "#2E86C1",
     "google_types": ["bank", "atm"],
     "osm_tags": ["amenity=bank", "amenity=atm"]},
    {"label": "Parks & Recreation",            "icon": "🌳", "color": "#229954",
     "google_types": ["park", "campground"],
     "osm_tags": ["leisure=park", "tourism=camp_site", "leisure=nature_reserve"]},
    {"label": "Transportation & Infrastructure","icon": "🛣️", "color": "#717D7E",
     "google_types": ["transit_station", "bus_station", "train_station",
                      "airport", "subway_station"],
     "osm_tags": ["amenity=bus_station", "railway=station", "aeroway=aerodrome",
                  "public_transport=station"]},
    # No Google equivalent -- OSM maps physical infrastructure that Google
    # Places largely does not, and for raw land these nuisance/utility
    # factors often matter more than nearby businesses.
    {"label": "Utilities & Nuisance",          "icon": "⚡", "color": "#8E44AD",
     "google_types": [],
     "osm_tags": ["power=substation", "power=plant", "man_made=wastewater_plant",
                  "amenity=waste_transfer_station", "landuse=landfill",
                  "landuse=quarry", "man_made=water_tower"]},
]

# Logging: there was a LOG_LEVEL constant here, read only by the scrapers and
# the scheduler. Those are gone; main.py sets its own level directly.
