"""
Read-only MCP server over the ByteForge Aegis admin API.

WHY THIS EXISTS
---------------
Agents working on Aegis and on tenant integrations repeatedly need to answer
"what is actually configured in prod?" — is the webhook URL set, is
allow_self_registration on, which sites exist, does this user exist. Without a
tool that question is answered by hand-rolled curls carrying the master API
key, or not answered at all — a tenant provisioning request was once signed
off partly on assertion, because the config could not be read back.

READ-ONLY BY CONSTRUCTION
-------------------------
Every tool below maps to a GET. The Aegis client this wraps also exposes
create_site, update_site, delete_site, delete_user and friends; none of them
are reachable from here, and none should be added. If a mutation is ever
wanted it belongs in a separate, separately-authorized server — the value of
this one is that an agent can be handed it without any possibility of
changing production state.

CREDENTIALS
-----------
AEGIS_MASTER_API_KEY spans every site. Tool responses include the per-site
secrets (tenant_api_key, webhook_secret, mailgun_api_key) because callers
auditing an integration need to confirm they are set and matching — this was
a deliberate choice by @jmazzahacks over returning presence booleans. The
consequence to keep in mind: anything read here lands in the caller's
transcript, so responses should not be pasted into tickets or other shared
surfaces.
"""
import json
import os
import uuid as uuid_module
from typing import Any, Dict, List, Optional

import requests
from byteforge_aegis_client import AegisClient, AegisClientConfig
from byteforge_aegis_client.exceptions import AegisError
from byteforge_aegis_models import Site
from mcp.server.fastmcp import FastMCP

# FastMCP's constructor args take precedence over its pydantic-settings env
# defaults, so host/port must be read here and passed explicitly. Omitting
# them binds the container to 127.0.0.1 and makes it unreachable even with
# FASTMCP_HOST=0.0.0.0 set in the environment.
#
# MCP_HOST/MCP_PORT are honoured as fallbacks purely so they are not dead
# config: they are the generic names the deployment template sets, but
# FastMCP itself reads only the FASTMCP_* pair. Without this fallback,
# setting MCP_PORT=9000 would silently change nothing and the container would
# still listen on 8000 — a quiet mismatch between compose and reality.
mcp = FastMCP(
    "byteforge_aegis",
    host=os.getenv("FASTMCP_HOST", os.getenv("MCP_HOST", "127.0.0.1")),
    port=int(os.getenv("FASTMCP_PORT", os.getenv("MCP_PORT", "8000"))),
    stateless_http=True,
)

HTTP_TIMEOUT_SECONDS = 15


def _api_url() -> str:
    """Base URL of the Aegis instance this server reads."""
    api_url = os.getenv("AEGIS_API_URL")
    if not api_url:
        raise RuntimeError("AEGIS_API_URL is not set")
    return api_url.rstrip("/")


def _client() -> AegisClient:
    """Build a master-key client.

    Built per call rather than at import: a missing key then surfaces as a
    tool-level error the caller can read, instead of killing the container at
    startup with a traceback nobody sees.
    """
    master_api_key = os.getenv("AEGIS_MASTER_API_KEY")
    if not master_api_key:
        raise RuntimeError("AEGIS_MASTER_API_KEY is not set")
    config = AegisClientConfig(api_url=_api_url(), master_api_key=master_api_key)
    return AegisClient(config)


def _is_uuid(value: str) -> bool:
    """True if the value parses as a UUID."""
    try:
        uuid_module.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _resolve_site_uuid(client: AegisClient, site: str) -> str:
    """Accept either a site UUID or a domain and return the UUID.

    The admin endpoint addresses sites by UUID only (utils/identifiers.py
    resolve_site rejects anything that is not a well-formed UUID), so a domain
    has to be translated first via the public by-domain lookup. Callers
    overwhelmingly know the domain and not the UUID, so doing this here saves
    every caller the same two-step dance.
    """
    if _is_uuid(site):
        return site
    return client.get_site_by_domain(site).uuid


def _ok(payload: Any) -> str:
    """Serialize a successful result."""
    return json.dumps(payload, indent=2, sort_keys=True)


def _sites_to_admin_dicts(sites: List[Site]) -> List[Dict[str, Any]]:
    """Serialize sites including their secrets."""
    results: List[Dict[str, Any]] = []
    for site in sites:
        results.append(site.to_admin_dict())
    return results


@mcp.tool()
def aegis_health() -> str:
    """Report the Aegis backend's health and deployed version.

    Returns the raw /api/health body, which carries the running version (e.g.
    {"status": "healthy", "service": "auth-service", "version": "68"}). Use
    this to confirm which build is live before reasoning about
    version-dependent behaviour.
    """
    # Deliberately not client.health_check(): the HealthStatus model parses
    # only `status` and discards `service` and `version`, and the version is
    # the entire reason to call this. Raw GET until that model is widened.
    try:
        response = requests.get(f"{_api_url()}/api/health", timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        return _ok(response.json())
    except (requests.RequestException, ValueError, RuntimeError) as e:
        return f"ERROR: health check failed: {e}"


@mcp.tool()
def aegis_list_sites() -> str:
    """List every site (tenant) on this Aegis instance, including secrets.

    Includes tenant_api_key, webhook_secret and mailgun_api_key for each site.
    """
    try:
        client = _client()
        return _ok(_sites_to_admin_dicts(client.list_sites()))
    except (AegisError, RuntimeError) as e:
        return f"ERROR: could not list sites: {e}"


@mcp.tool()
def aegis_get_site(site: str) -> str:
    """Get one site's full configuration, including its secrets.

    Args:
        site: The site's UUID, or its domain (e.g.
            "tenant.example.com"). A domain is resolved to a UUID
            automatically.

    Returns every stored field: frontend_url, verification_redirect_url,
    allow_self_registration, webhook_url, deletion_protected, the Mailgun
    overrides, and the tenant_api_key / webhook_secret / mailgun_api_key
    values.
    """
    try:
        client = _client()
        site_uuid = _resolve_site_uuid(client, site)
        return _ok(client.get_site(site_uuid).to_admin_dict())
    except (AegisError, RuntimeError) as e:
        return f"ERROR: could not get site '{site}': {e}"


@mcp.tool()
def aegis_list_users(site: str) -> str:
    """List all users on a site.

    Args:
        site: The site's UUID or its domain.

    Returns each user's uuid, email, role, verification state and timestamps.
    Password hashes are never returned by the API.
    """
    try:
        client = _client()
        site_uuid = _resolve_site_uuid(client, site)
        users = client.list_users_by_site(site_uuid)
        results: List[Dict[str, Any]] = []
        for user in users:
            results.append(user.to_dict())
        return _ok(results)
    except (AegisError, RuntimeError) as e:
        return f"ERROR: could not list users for site '{site}': {e}"


@mcp.tool()
def aegis_find_user(site: str, email: str) -> str:
    """Find a user on a site by email address.

    Args:
        site: The site's UUID or its domain.
        email: Exact email address. Matching is case-insensitive, because
            Aegis stores emails lowercased per site.

    Implemented as a filter over the site's user list rather than a direct
    lookup: the single-user endpoint (GET /api/sites/<site>/users/<user>) is
    tenant-key gated, and this server holds only the master key. That makes
    this O(users on the site) — fine at current tenant sizes, worth revisiting
    if a site ever grows large.
    """
    try:
        client = _client()
        site_uuid = _resolve_site_uuid(client, site)
        needle = email.strip().lower()
        match: Optional[Dict[str, Any]] = None
        for user in client.list_users_by_site(site_uuid):
            if user.email.lower() == needle:
                match = user.to_dict()
                break
        if match is None:
            return _ok({"found": False, "email": email, "site_uuid": site_uuid})
        return _ok({"found": True, "user": match})
    except (AegisError, RuntimeError) as e:
        return f"ERROR: could not look up '{email}' on site '{site}': {e}"


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)
