"""Async WordPress + Paid Memberships Pro REST API client."""

from __future__ import annotations

import secrets
from base64 import b64encode
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.utils.logging import mask_email
from app.utils.retry import async_retry_with_backoff

logger = structlog.get_logger()


class WordPressAPIError(Exception):
    """Raised when a WordPress API call fails after retries."""

    def __init__(self, message: str, status_code: int | None = None, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


@dataclass
class WPUser:
    """Minimal WordPress user representation."""

    id: int
    email: str
    username: str
    created: bool = False  # True if we just created this user


class WordPressClient:
    """Async HTTP client for WordPress REST API and PMPro endpoints.

    Uses WordPress Application Passwords for authentication.
    """

    def __init__(self, base_url: str, username: str, app_password: str):
        self.base_url = base_url.rstrip("/")
        self.wp_api = f"{self.base_url}/wp-json/wp/v2"
        self.pmpro_api = f"{self.base_url}/wp-json/pmpro/v1"

        # WordPress Application Password auth
        credentials = b64encode(f"{username}:{app_password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        }

    # ---- User management ----

    @async_retry_with_backoff(max_retries=3)
    async def find_user_by_email(self, email: str) -> WPUser | None:
        """Look up a WordPress user by email address."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.wp_api}/users",
                headers=self.headers,
                params={"search": email, "context": "edit"},
            )

            if resp.status_code != 200:
                # Body stays on the exception, out of the logs.
                logger.warning(
                    "wp.user.search_failed",
                    email=mask_email(email),
                    status=resp.status_code,
                )
                return None

            users = resp.json()
            for user in users:
                if user.get("email", "").lower() == email.lower():
                    logger.info("wp.user.found", email=mask_email(email), wp_user_id=user["id"])
                    return WPUser(id=user["id"], email=user["email"], username=user["username"])

        return None

    @async_retry_with_backoff(max_retries=3)
    async def create_user(self, email: str, username: str | None = None) -> WPUser:
        """Create a new WordPress user with 'subscriber' role.

        Generates a random password. The user sets their own through the
        normal WordPress password reset flow on first login.
        """
        if username is None:
            # Derive username from email (before @), deduplicate if needed
            username = email.split("@")[0].lower().replace(".", "").replace("+", "")

        password = secrets.token_urlsafe(24)

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.wp_api}/users",
                headers=self.headers,
                json={
                    "email": email,
                    "username": username,
                    "password": password,
                    "roles": ["subscriber"],
                },
            )

            if resp.status_code == 201:
                user_data = resp.json()
                logger.info("wp.user.created", email=mask_email(email), wp_user_id=user_data["id"])
                return WPUser(
                    id=user_data["id"],
                    email=email,
                    username=user_data.get("username", username),
                    created=True,
                )

            # Handle duplicate username
            if resp.status_code == 400 and "existing_user_login" in resp.text:
                username = f"{username}_{secrets.token_hex(3)}"
                return await self.create_user(email, username)

            raise WordPressAPIError(
                f"Failed to create user {email}",
                status_code=resp.status_code,
                response_body=resp.text[:500],
            )

    async def get_or_create_user(self, email: str) -> WPUser:
        """Find an existing user by email, or create a new one."""
        user = await self.find_user_by_email(email)
        if user:
            return user
        return await self.create_user(email)

    # ---- PMPro membership management ----

    @async_retry_with_backoff(max_retries=3)
    async def assign_membership_level(self, user_id: int, level_id: int) -> dict[str, Any]:
        """Grant a PMPro membership level to a WordPress user.

        This calls the PMPro REST API to change the user's membership level.
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.pmpro_api}/change_membership_level",
                headers=self.headers,
                json={
                    "user_id": user_id,
                    "level_id": level_id,
                },
            )

            if resp.status_code in (200, 201):
                result = resp.json()
                logger.info(
                    "pmpro.level.assigned",
                    wp_user_id=user_id,
                    level_id=level_id,
                    response=result,
                )
                return result

            raise WordPressAPIError(
                f"Failed to assign level {level_id} to user {user_id}",
                status_code=resp.status_code,
                response_body=resp.text[:500],
            )

    @async_retry_with_backoff(max_retries=3)
    async def cancel_membership(self, user_id: int) -> dict[str, Any]:
        """Cancel a user's PMPro membership (set level to 0)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.pmpro_api}/change_membership_level",
                headers=self.headers,
                json={
                    "user_id": user_id,
                    "level_id": 0,  # 0 = no membership
                },
            )

            if resp.status_code in (200, 201):
                result = resp.json()
                logger.info("pmpro.membership.cancelled", wp_user_id=user_id)
                return result

            raise WordPressAPIError(
                f"Failed to cancel membership for user {user_id}",
                status_code=resp.status_code,
                response_body=resp.text[:500],
            )

    @async_retry_with_backoff(max_retries=3)
    async def get_membership_level(self, user_id: int) -> dict[str, Any] | None:
        """Get a user's current PMPro membership level."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.pmpro_api}/get_membership_level_for_user",
                headers=self.headers,
                params={"user_id": user_id},
            )

            if resp.status_code == 200:
                return resp.json()

            return None
