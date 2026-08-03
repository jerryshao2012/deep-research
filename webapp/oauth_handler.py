"""OAuth2 authentication handlers for third-party providers.

Registers clients for Google and GitHub authentication, manages active user sessions,
and handles cookie lifetimes and state checks.
"""

import asyncio
import os

from authlib.integrations.starlette_client import OAuth
from starlette.config import Config
from starlette.requests import Request

from webapp.auth_store import SQLiteAuthStore, create_auth_store

# OAuth configuration from environment variables
config = Config(
    environ={
        "GOOGLE_CLIENT_ID": os.environ.get("GOOGLE_CLIENT_ID", ""),
        "GOOGLE_CLIENT_SECRET": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        "GITHUB_CLIENT_ID": os.environ.get("GITHUB_CLIENT_ID", ""),
        "GITHUB_CLIENT_SECRET": os.environ.get("GITHUB_CLIENT_SECRET", ""),
        "SECRET_KEY": os.environ.get("OAUTH_SECRET_KEY", os.urandom(32).hex()),
    }
)

oauth = OAuth(config)

# Register Google OAuth provider
google = oauth.register(
    name="google",
    client_id=config("GOOGLE_CLIENT_ID"),
    client_secret=config("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


# Register GitHub OAuth providers dynamically
def _parse_multi_env(env_str: str) -> dict[str, str]:
    res = {}
    if not env_str:
        return res
    for item in env_str.split(","):
        if ":" in item:
            parts = item.split(":", 1)
            res[parts[0].strip()] = parts[1].strip()
    return res


github_ids = _parse_multi_env(os.environ.get("GITHUB_CLIENT_IDS", ""))
github_secrets = _parse_multi_env(os.environ.get("GITHUB_CLIENT_SECRETS", ""))

# Register specific domain clients
for domain, client_id in github_ids.items():
    client_secret = github_secrets.get(domain, "")
    client_name = f"github_{domain.replace('.', '_').replace('-', '_')}"
    oauth.register(
        name=client_name,
        client_id=client_id,
        client_secret=client_secret,
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "user:email"},
    )

# Register default/fallback GitHub OAuth provider
default_github_id = config("GITHUB_CLIENT_ID")
default_github_secret = config("GITHUB_CLIENT_SECRET")

if not default_github_id and github_ids:
    first_domain = list(github_ids.keys())[0]
    default_github_id = github_ids[first_domain]
    default_github_secret = github_secrets.get(first_domain, "")

github = oauth.register(
    name="github",
    client_id=default_github_id,
    client_secret=default_github_secret,
    access_token_url="https://github.com/login/oauth/access_token",
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "user:email"},
)


class OAuthUserManager:
    """Compatibility facade over the configured durable auth store."""

    def __init__(self, store: SQLiteAuthStore | None = None):
        """Use an explicit store or build one from environment configuration."""
        self._store = store

    @property
    def store(self):
        """Build configured store lazily on first authentication operation."""
        if self._store is None:
            self._store = create_auth_store()
        return self._store

    def create_session(
            self,
            user_data: dict,
            provider: str,
            auth_method: str = "oauth",
    ) -> str:
        """Create a new session and return its raw bearer token."""
        return self.store.create_session(user_data, provider, auth_method=auth_method)

    def validate_session(self, session_token: str) -> dict | None:
        """Validate session token and return user data if valid.

        Implements a sliding window: if the session has less than 1 hour
        remaining, automatically extend the expiry by 24 hours from now.
        """
        return self.store.validate_session(session_token)

    def refresh_session(self, session_token: str) -> dict | None:
        """Explicitly extend a session's expiry by 24 hours.

        Returns the user data if the session was found and refreshed,
        None if the session doesn't exist or is already expired.
        """
        return self.store.refresh_session(session_token)

    def cleanup_expired_sessions(self) -> int:
        """Remove a bounded batch of expired sessions."""
        return self.store.cleanup_expired_sessions()

    def remove_session(self, session_token: str) -> str | None:
        """Remove a specific session and return the user identity if it existed."""
        return self.store.remove_session(session_token)


# Global user manager instance
user_manager = OAuthUserManager()


async def handle_google_callback(request: Request) -> dict:
    """Handle Google OAuth callback and return user info."""
    try:
        token = await google.authorize_access_token(request)
        userinfo = token.get("userinfo")
        if not userinfo:
            raise Exception("No userinfo found in Google token")
        subject = userinfo.get("sub")
        if (
                not isinstance(subject, str)
                or not subject
                or subject != subject.strip()
                or len(subject) > 255
                or not subject.isascii()
                or subject.lower() in {"none", "null"}
                or any(
            ord(character) < 0x21 or ord(character) > 0x7E
            for character in subject
        )
        ):
            raise ValueError("invalid Google subject")

        user_data = {
            "identity": f"google:{subject}",
            "email": userinfo.get("email"),
            "name": userinfo.get("name"),
            "picture": userinfo.get("picture"),
            "provider": "google",
            "email_verified": userinfo.get("email_verified", False),
            "locale": userinfo.get("locale"),
            "given_name": userinfo.get("given_name"),
            "family_name": userinfo.get("family_name"),
            "raw_token": token,
        }

        # Create session
        session_token = await asyncio.to_thread(
            user_manager.create_session,
            user_data,
            "google",
        )
        user_data["session_token"] = session_token

        return user_data

    except Exception as e:
        raise Exception(f"Google OAuth failed: {str(e)}")


async def handle_github_callback(request: Request) -> dict:
    """Handle GitHub OAuth callback and return user info."""
    try:
        client_name = request.session.pop("oauth_github_client_name", "github")
        github_client = getattr(oauth, client_name, github)

        token = await github_client.authorize_access_token(request)

        # Get user info from GitHub API
        resp = await github_client.get("user", token=token)
        user_info = resp.json()
        subject = user_info.get("id")
        if (
                isinstance(subject, bool)
                or not isinstance(subject, int)
                or subject <= 0
                or len(str(subject)) > 255
        ):
            raise ValueError("invalid GitHub subject")

        # Get user emails
        email_resp = await github_client.get("user/emails", token=token)
        emails = email_resp.json()

        # Find primary email
        primary_email = next(
            (e["email"] for e in emails if e.get("primary")),
            emails[0]["email"] if emails else None,
        )

        user_data = {
            "identity": f"github:{subject}",
            "username": user_info.get("login"),
            "email": primary_email,
            "name": user_info.get("name") or user_info.get("login"),
            "avatar_url": user_info.get("avatar_url"),
            "provider": "github",
            "bio": user_info.get("bio"),
            "location": user_info.get("location"),
            "company": user_info.get("company"),
            "blog": user_info.get("blog"),
            "followers": user_info.get("followers"),
            "following": user_info.get("following"),
            "public_repos": user_info.get("public_repos"),
            "created_at": user_info.get("created_at"),
            "raw_token": token,
        }

        # Create session
        session_token = await asyncio.to_thread(
            user_manager.create_session,
            user_data,
            "github",
        )
        user_data["session_token"] = session_token

        return user_data

    except Exception as e:
        raise Exception(f"GitHub OAuth failed: {str(e)}")


async def get_oauth_login_url(
        request: Request, provider: str, redirect_uri: str
) -> str:
    """Generate OAuth login URL for the specified provider."""
    if provider == "google":
        rv = await google.create_authorization_url(redirect_uri=redirect_uri)
        await google.save_authorize_data(request, redirect_uri=redirect_uri, **rv)
        return rv["url"]
    elif provider == "github":
        from urllib.parse import urlparse

        frontend_url = request.session.get("oauth_frontend_url", "")
        domain = ""
        if frontend_url:
            try:
                domain = urlparse(frontend_url).netloc.lower()
            except Exception:
                pass

        github_client = github
        target_domain = None
        for d in github_ids.keys():
            if d != "default" and (
                    domain == d or domain.endswith("." + d) or d.endswith("." + domain)
            ):
                target_domain = d
                break

        if target_domain:
            client_name = f"github_{target_domain.replace('.', '_').replace('-', '_')}"
            github_client = getattr(oauth, client_name, github)

        request.session["oauth_github_client_name"] = github_client.name

        rv = await github_client.create_authorization_url(redirect_uri=redirect_uri)
        await github_client.save_authorize_data(
            request, redirect_uri=redirect_uri, **rv
        )
        return rv["url"]
    else:
        raise ValueError(f"Unsupported provider: {provider}")


def handle_logout(session_token: str) -> str | None:
    """Handle user logout by removing session and returning user identity.

    Returns the user identity if the session was found and removed, None otherwise.
    The caller (auth.py) should use this identity to clean up _logged_oauth_users.
    """
    identity = user_manager.remove_session(session_token)
    if identity:
        # Also trigger cleanup of any other expired sessions
        user_manager.cleanup_expired_sessions()
    return identity
