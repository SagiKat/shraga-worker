"""
OneDrive Utility Functions for Windows

Provides:
  - find_onedrive_root()       : Discover the local OneDrive for Business sync root folder
  - local_path_to_web_url()    : Convert a local synced file path to its OneDrive/SharePoint web URL
  - get_onedrive_account_info(): Read OneDrive account metadata from the Windows registry
  - get_sync_engine_mappings() : Read SyncEngines registry for MountPoint <-> URLNamespace mappings

Tested on Windows with OneDrive for Business (corporate/organizational accounts).

Registry keys used:
  HKCU\\Software\\Microsoft\\OneDrive\\Accounts\\Business1   - account metadata
  HKCU\\SOFTWARE\\SyncEngines\\Providers\\OneDrive           - sync mount/URL mappings
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# 0. Helpers
# ---------------------------------------------------------------------------

def _path_looks_like_file(p: str | Path) -> bool:
    """
    Infer whether *p* refers to a file (as opposed to a folder) based on
    whether the last path component has a file extension (suffix).

    This avoids calling ``Path.is_file()`` which hits the filesystem and
    returns ``False`` when the file has not synced yet (OneDrive race).

    Examples
    --------
    >>> _path_looks_like_file("C:/OneDrive/Sessions/task1/result.md")
    True
    >>> _path_looks_like_file("C:/OneDrive/Sessions/task1")
    False
    >>> _path_looks_like_file("C:/OneDrive/Sessions/.gitignore")
    False
    >>> _path_looks_like_file("C:/OneDrive/Sessions/.config.json")
    True
    """
    suffix = Path(p).suffix  # e.g. ".md", ".txt", "" for folders
    # Pure dotfiles like ".gitignore" have suffix="" and stem=".gitignore"
    # in Python's pathlib, so they look like folders. This is acceptable
    # for OneDrive scenarios where we care about normal files (result.md,
    # TASK.md, etc.) not dotfiles.
    if not suffix:
        return False
    return True


# ---------------------------------------------------------------------------
# 1. Data classes
# ---------------------------------------------------------------------------

@dataclass
class OneDriveAccountInfo:
    """Metadata for a single OneDrive account read from the registry."""
    account_name: str          # e.g. "Business1", "Personal"
    user_folder: str           # e.g. r"C:\Users\sagik\OneDrive - Microsoft"
    is_business: bool
    user_email: Optional[str] = None
    display_name: Optional[str] = None          # org display name
    spo_resource_id: Optional[str] = None       # e.g. https://tenant-my.sharepoint.com/
    service_endpoint_uri: Optional[str] = None  # e.g. https://tenant-my.sharepoint.com/personal/user_domain_com/_api
    configured_tenant_id: Optional[str] = None


@dataclass
class SyncEngineMapping:
    """A single SyncEngine provider entry mapping a local mount point to a URL namespace."""
    provider_key: str          # e.g. "b129d3e618b04311b29555fa8f5777fe"
    mount_point: str           # Local path  e.g. r"C:\Users\sagik\OneDrive - Microsoft"
    url_namespace: str         # SharePoint URL e.g. "https://tenant-my.sharepoint.com/personal/user_dom/Documents/"
    library_type: Optional[str] = None   # "mysite", "personal", "teamsite"
    web_url: Optional[str] = None


# ---------------------------------------------------------------------------
# 2. Registry readers  (Windows-only)
# ---------------------------------------------------------------------------

def _is_windows() -> bool:
    return sys.platform == "win32"


def get_onedrive_account_info() -> list[OneDriveAccountInfo]:
    """
    Read all OneDrive account entries from the Windows registry.

    Registry path: HKCU\\Software\\Microsoft\\OneDrive\\Accounts\\<AccountName>

    Returns a list of OneDriveAccountInfo for every account found (Business1, Business2, Personal, ...).
    """
    if not _is_windows():
        return []

    import winreg

    accounts: list[OneDriveAccountInfo] = []
    base_path = r"Software\Microsoft\OneDrive\Accounts"

    try:
        base_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base_path)
    except OSError:
        return accounts

    idx = 0
    while True:
        try:
            account_name = winreg.EnumKey(base_key, idx)
        except OSError:
            break
        idx += 1

        try:
            acct_key = winreg.OpenKey(base_key, account_name)
        except OSError:
            continue

        def _read(name: str) -> Optional[str]:
            try:
                val, _ = winreg.QueryValueEx(acct_key, name)
                return str(val) if val else None
            except OSError:
                return None

        user_folder = _read("UserFolder") or ""
        # Expand %UserProfile% etc.
        user_folder = os.path.expandvars(user_folder)

        is_business = _read("Business") == "1"

        accounts.append(OneDriveAccountInfo(
            account_name=account_name,
            user_folder=user_folder,
            is_business=is_business,
            user_email=_read("UserEmail"),
            display_name=_read("DisplayName"),
            spo_resource_id=_read("SPOResourceId"),
            service_endpoint_uri=_read("ServiceEndpointUri"),
            configured_tenant_id=_read("ConfiguredTenantId"),
        ))

        winreg.CloseKey(acct_key)

    winreg.CloseKey(base_key)
    return accounts


def get_sync_engine_mappings() -> list[SyncEngineMapping]:
    """
    Read OneDrive SyncEngine provider entries from the Windows registry.

    Registry path: HKCU\\SOFTWARE\\SyncEngines\\Providers\\OneDrive\\<ProviderKey>

    Each entry contains:
      - MountPoint   : local filesystem path
      - UrlNamespace  : corresponding SharePoint/OneDrive URL(s), comma-separated
      - LibraryType   : "mysite" | "personal" | "teamsite"
      - WebUrl        : SharePoint web URL for the library

    These entries are the authoritative mapping between local paths and web URLs.
    """
    if not _is_windows():
        return []

    import winreg

    mappings: list[SyncEngineMapping] = []
    base_path = r"SOFTWARE\SyncEngines\Providers\OneDrive"

    try:
        base_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base_path)
    except OSError:
        return mappings

    idx = 0
    while True:
        try:
            provider_name = winreg.EnumKey(base_key, idx)
        except OSError:
            break
        idx += 1

        try:
            prov_key = winreg.OpenKey(base_key, provider_name)
        except OSError:
            continue

        def _read(name: str) -> Optional[str]:
            try:
                val, _ = winreg.QueryValueEx(prov_key, name)
                return str(val) if val else None
            except OSError:
                return None

        mount_point = _read("MountPoint") or ""
        url_namespace = _read("UrlNamespace") or ""

        # UrlNamespace can be comma-separated (multiple URLs); take the first SharePoint one
        url_parts = [u.strip() for u in url_namespace.split(",") if u.strip()]
        primary_url = ""
        for u in url_parts:
            if "sharepoint.com" in u:
                primary_url = u
                break
        if not primary_url and url_parts:
            primary_url = url_parts[0]

        mappings.append(SyncEngineMapping(
            provider_key=provider_name,
            mount_point=mount_point,
            url_namespace=primary_url,
            library_type=_read("LibraryType"),
            web_url=_read("WebUrl"),
        ))

        winreg.CloseKey(prov_key)

    winreg.CloseKey(base_key)
    return mappings


# ---------------------------------------------------------------------------
# 3. find_onedrive_root()
# ---------------------------------------------------------------------------

class OneDriveRootNotFoundError(Exception):
    """Raised when no OneDrive sync root folder can be determined."""


def find_onedrive_root(*, business_only: bool = True) -> str:
    """
    Discover the local OneDrive sync root folder.

    Resolution order:
      1. ONEDRIVE_SESSIONS_DIR environment variable  (explicit override)
      2. OneDriveCommercial environment variable      (set by OneDrive client for business accounts)
      3. OneDrive environment variable                (set by OneDrive client; may be personal OR business)
      4. Windows Registry: HKCU\\Software\\Microsoft\\OneDrive\\Accounts\\Business1\\UserFolder
      5. Windows Registry: HKCU\\SOFTWARE\\SyncEngines\\Providers\\OneDrive (scan MountPoints)
      6. Scan typical filesystem paths:
           C:\\Users\\{username}\\OneDrive - {OrgName}\\
           C:\\Users\\{username}\\OneDrive\\

    Parameters
    ----------
    business_only : bool
        If True (default), prefer business/commercial accounts over personal.

    Returns
    -------
    str
        Absolute path to the OneDrive sync root folder.

    Raises
    ------
    OneDriveRootNotFoundError
        If no valid OneDrive root folder could be found.
    """

    # --- Strategy 1: Explicit override ---
    override = os.environ.get("ONEDRIVE_SESSIONS_DIR")
    if override and os.path.isdir(override):
        return override

    # --- Strategy 2: %OneDriveCommercial% (business only) ---
    commercial = os.environ.get("OneDriveCommercial")
    if commercial and os.path.isdir(commercial):
        return commercial

    # --- Strategy 3: %OneDrive% (could be personal or business) ---
    generic = os.environ.get("OneDrive")
    if generic and os.path.isdir(generic):
        # If we need business only, verify it looks like a business folder
        # Business folders typically have " - OrgName" suffix
        if not business_only or " - " in os.path.basename(generic):
            return generic

    # --- Strategy 4: Registry - OneDrive Accounts ---
    if _is_windows():
        for acct in get_onedrive_account_info():
            if business_only and not acct.is_business:
                continue
            if acct.user_folder and os.path.isdir(acct.user_folder):
                return acct.user_folder

    # --- Strategy 5: Registry - SyncEngines ---
    if _is_windows():
        for mapping in get_sync_engine_mappings():
            if business_only and mapping.library_type not in ("mysite", "personal"):
                continue
            if mapping.mount_point and os.path.isdir(mapping.mount_point):
                return mapping.mount_point

    # --- Strategy 6: Filesystem scan ---
    user_home = Path.home()
    # Look for "OneDrive - *" folders (business pattern)
    if business_only:
        candidates = sorted(user_home.glob("OneDrive - *"))
        for candidate in candidates:
            if candidate.is_dir():
                return str(candidate)

    # Also try plain "OneDrive" folder (personal or business without org name)
    plain = user_home / "OneDrive"
    if plain.is_dir():
        return str(plain)

    # --- Strategy 3 fallback: accept %OneDrive% even if it didn't match business heuristic ---
    if generic and os.path.isdir(generic):
        return generic

    raise OneDriveRootNotFoundError(
        "Could not find a OneDrive sync root folder. "
        "Set the ONEDRIVE_SESSIONS_DIR environment variable to an explicit path, "
        "or ensure OneDrive for Business is installed and syncing."
    )


# ---------------------------------------------------------------------------
# 4. local_path_to_web_url()
# ---------------------------------------------------------------------------

def local_path_to_web_url(
    local_path: str | Path,
    *,
    view_in_browser: bool = True,
) -> Optional[str]:
    """
    Convert a local file/folder path inside a OneDrive sync folder to its
    SharePoint/OneDrive web URL.

    This uses the SyncEngines registry to find the exact URL namespace mapping.

    Parameters
    ----------
    local_path : str or Path
        Absolute local path to a synced file or folder.
    view_in_browser : bool
        If True, returns a direct SharePoint document library URL that opens
        the file or folder in the OneDrive web UI. If False, returns a raw
        SharePoint document URL.

    Returns
    -------
    str or None
        The web URL, or None if the path is not inside any known OneDrive sync folder.

    Examples
    --------
    >>> local_path_to_web_url(r"C:\\Users\\sagik\\OneDrive - Microsoft\\Sessions\\task1\\result.md")
    'https://microsofteur-my.sharepoint.com/personal/sagik_microsoft_com/Documents/Sessions/task1/result.md'
    """
    local_path = Path(local_path).resolve()
    local_str = str(local_path)

    # --- Method 1: Use SyncEngines registry (most reliable) ---
    for mapping in get_sync_engine_mappings():
        mount = mapping.mount_point
        if not mount:
            continue
        mount_resolved = str(Path(mount).resolve())

        # Check if local_path is under this mount point
        if not local_str.lower().startswith(mount_resolved.lower()):
            continue

        # Compute relative path (forward slashes)
        relative = local_str[len(mount_resolved):].lstrip(os.sep).replace("\\", "/")

        url_ns = mapping.url_namespace.rstrip("/")
        # url_ns is like: https://tenant-my.sharepoint.com/personal/user_domain/Documents
        # The relative path is appended directly.

        if view_in_browser and mapping.web_url:
            # Build a direct SharePoint document library URL.
            # Previous approach used _layouts/15/onedrive.aspx?id=...&view=0
            # which caused "empty folder" rendering issues in OneDrive web UI.
            # Direct URLs (https://host/personal/user/Documents/path) work
            # reliably for both files and folders.
            parsed = urllib.parse.urlparse(url_ns)
            doc_path = parsed.path.rstrip("/")  # /personal/user/Documents
            full_path = f"{doc_path}/{relative}" if relative else doc_path

            encoded_path = urllib.parse.quote(full_path)
            result_url = f"{parsed.scheme}://{parsed.netloc}{encoded_path}"
            return result_url
        else:
            # Direct document URL
            encoded_relative = urllib.parse.quote(relative)
            return f"{url_ns}/{encoded_relative}" if relative else url_ns

    # --- Method 2: Fallback using account info ---
    for acct in get_onedrive_account_info():
        if not acct.user_folder or not acct.service_endpoint_uri:
            continue
        mount_resolved = str(Path(acct.user_folder).resolve())
        if not local_str.lower().startswith(mount_resolved.lower()):
            continue

        relative = local_str[len(mount_resolved):].lstrip(os.sep).replace("\\", "/")

        # Parse the service endpoint to reconstruct the URL
        # ServiceEndpointUri: https://tenant-my.sharepoint.com/personal/user_domain/_api
        endpoint = acct.service_endpoint_uri
        # Remove /_api suffix
        base = endpoint.rsplit("/_api", 1)[0]  # https://tenant-my.sharepoint.com/personal/user_domain
        parsed = urllib.parse.urlparse(base)
        site_path = parsed.path.rstrip("/")      # /personal/user_domain
        doc_path = f"{site_path}/Documents/{relative}" if relative else f"{site_path}/Documents"

        if view_in_browser:
            encoded = urllib.parse.quote(doc_path)
            result_url = f"{parsed.scheme}://{parsed.netloc}{encoded}"
            return result_url
        else:
            encoded_relative = urllib.parse.quote(relative)
            return f"{parsed.scheme}://{parsed.netloc}{site_path}/Documents/{encoded_relative}"

    return None


def web_url_to_local_path(web_url: str) -> Optional[str]:
    """
    Reverse mapping: given a SharePoint/OneDrive web URL (or direct document URL),
    return the corresponding local file path if the content is synced locally.

    Returns None if no matching sync mapping is found.
    """
    parsed = urllib.parse.urlparse(web_url)

    # Extract the document path from the URL
    # Could be a direct URL: https://host/personal/user/Documents/path/file.md
    # Or a onedrive.aspx URL with ?id= parameter
    doc_path = None
    if "_layouts/15/onedrive.aspx" in parsed.path:
        params = urllib.parse.parse_qs(parsed.query)
        if "id" in params:
            doc_path = urllib.parse.unquote(params["id"][0])
    else:
        doc_path = urllib.parse.unquote(parsed.path)

    if not doc_path:
        return None

    for mapping in get_sync_engine_mappings():
        if not mapping.url_namespace or not mapping.mount_point:
            continue
        ns_parsed = urllib.parse.urlparse(mapping.url_namespace)
        ns_path = ns_parsed.path.rstrip("/")  # e.g. /personal/user/Documents

        if doc_path.startswith(ns_path):
            relative = doc_path[len(ns_path):].lstrip("/")
            local = os.path.join(mapping.mount_point, relative.replace("/", os.sep))
            return local

    return None


# ---------------------------------------------------------------------------
# 5. Graph API helper  (for creating sharing links, etc.)
# ---------------------------------------------------------------------------

def get_graph_api_file_url(relative_path: str, user_principal_name: Optional[str] = None) -> str:
    """
    Construct a Microsoft Graph API URL to address a file by path in the user's OneDrive.

    Parameters
    ----------
    relative_path : str
        Path relative to the OneDrive root, using forward slashes.
        E.g. "Sessions/task1/result.md"
    user_principal_name : str, optional
        The user's UPN (e.g. "sagik@microsoft.com"). If None, uses /me/drive.

    Returns
    -------
    str
        Graph API URL like:
        https://graph.microsoft.com/v1.0/me/drive/root:/Sessions/task1/result.md:

    Notes
    -----
    To use this URL, you need a valid OAuth2 bearer token with Files.Read or
    Files.ReadWrite scope. Example with the `requests` library::

        import requests
        url = get_graph_api_file_url("Sessions/task1/result.md")
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        item = resp.json()
        web_url = item.get("webUrl")  # Direct browser URL to the file
    """
    encoded_path = urllib.parse.quote(relative_path)
    if user_principal_name:
        upn_encoded = urllib.parse.quote(user_principal_name)
        return f"https://graph.microsoft.com/v1.0/users/{upn_encoded}/drive/root:/{encoded_path}:/"
    else:
        return f"https://graph.microsoft.com/v1.0/me/drive/root:/{encoded_path}:/"


def get_graph_api_sharing_link_url(drive_item_id: str, drive_id: Optional[str] = None) -> str:
    """
    Construct the Graph API URL for creating a sharing link for a drive item.

    Parameters
    ----------
    drive_item_id : str
        The ID of the driveItem (obtained from a previous Graph API call).
    drive_id : str, optional
        The drive ID. If None, uses /me/drive.

    Returns
    -------
    str
        Graph API URL for the createLink action.

    Notes
    -----
    POST to this URL with body::

        {
            "type": "view",          # or "edit"
            "scope": "organization"  # or "anonymous", "users"
        }

    Example with requests::

        url = get_graph_api_sharing_link_url(item_id)
        resp = requests.post(url, json={"type": "view", "scope": "organization"},
                             headers={"Authorization": f"Bearer {token}"})
        link = resp.json()["link"]["webUrl"]
    """
    if drive_id:
        return f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{drive_item_id}/createLink"
    else:
        return f"https://graph.microsoft.com/v1.0/me/drive/items/{drive_item_id}/createLink"


# ---------------------------------------------------------------------------
# 6. CLI Interface
# ---------------------------------------------------------------------------

def create_session_folder(title: str, task_id: str) -> Path:
    """
    Create an isolated OneDrive session folder for a task.

    Folder structure: {OneDrive root}/Shraga Sessions/{title}_{task_id_short}/

    Parameters
    ----------
    title : str
        Human-readable task title.  Sanitised for the filesystem.
    task_id : str
        Unique task identifier.  The first 8 characters are used as a short suffix.

    Returns
    -------
    Path
        Absolute path to the newly-created session folder.

    Raises
    ------
    OneDriveRootNotFoundError
        If no OneDrive sync root can be found.
    """
    # Sanitise title for filesystem safety
    safe_name = "".join(c if c.isalnum() or c in ("-", "_", " ") else "_" for c in title)
    safe_name = safe_name.strip()[:50]  # Limit length
    task_id_short = task_id[:8] if task_id else "no_id"
    folder_name = f"{safe_name}_{task_id_short}"

    onedrive_root = find_onedrive_root()
    sessions_root = Path(onedrive_root) / "Shraga Sessions"
    sessions_root.mkdir(exist_ok=True)
    session_folder = sessions_root / folder_name
    session_folder.mkdir(exist_ok=True, parents=True)
    return session_folder


def _build_parser() -> "argparse.ArgumentParser":
    """Construct the argparse parser with subcommands."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="onedrive_utils",
        description="OneDrive utility CLI for discovering roots, creating session folders, and resolving web URLs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- get-root --------------------------------------------------------
    sp_root = subparsers.add_parser(
        "get-root",
        help="Discover and print the local OneDrive sync root folder.",
    )
    sp_root.add_argument(
        "--include-personal",
        action="store_true",
        default=False,
        help="Include personal (non-business) OneDrive accounts in the search.",
    )

    # --- create-session --------------------------------------------------
    sp_session = subparsers.add_parser(
        "create-session",
        help="Create a session folder under the OneDrive root.",
    )
    sp_session.add_argument(
        "--title",
        required=True,
        help="Human-readable task title (used as folder name prefix).",
    )
    sp_session.add_argument(
        "--id",
        required=True,
        dest="task_id",
        help="Unique task identifier (first 8 chars used as folder suffix).",
    )

    # --- get-url ---------------------------------------------------------
    sp_url = subparsers.add_parser(
        "get-url",
        help="Convert a local synced file/folder path to its SharePoint/OneDrive web URL.",
    )
    sp_url.add_argument(
        "--path",
        required=True,
        dest="local_path",
        help="Absolute local path inside a OneDrive sync folder.",
    )
    sp_url.add_argument(
        "--direct",
        action="store_true",
        default=False,
        help="Return a direct document URL instead of a browser-view URL.",
    )

    return parser


def _cli_main(argv: list[str] | None = None) -> int:
    """
    Entry-point for the CLI.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns
    -------
    int
        Exit code (0 = success, 1 = error).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "get-root":
            root = find_onedrive_root(business_only=not args.include_personal)
            print(root)

        elif args.command == "create-session":
            folder = create_session_folder(title=args.title, task_id=args.task_id)
            print(folder)

        elif args.command == "get-url":
            url = local_path_to_web_url(
                args.local_path,
                view_in_browser=not args.direct,
            )
            if url is None:
                print(
                    "ERROR: The given path is not inside any known OneDrive sync folder.",
                    file=sys.stderr,
                )
                return 1
            print(url)

    except OneDriveRootNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
