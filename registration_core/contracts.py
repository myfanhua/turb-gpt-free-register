from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class MailboxProvider(Protocol):
    """Registration flow's narrow mailbox contract.

    Provider implementations remain outside this package. A later mailbox-pool
    adapter only needs to create an object satisfying these two operations.
    """

    def create_mailbox(self) -> str:
        """Return the mailbox address reserved for the current registration."""

    def wait_for_otp(
        self,
        email: str,
        timeout: int,
        issued_after: float | None = None,
    ) -> str:
        """Return the next usable OTP for email or raise TimeoutError."""


@runtime_checkable
class RegistrationLifecycleProvider(MailboxProvider, Protocol):
    """Optional mailbox lifecycle hooks used by RegistrationRunner.

    A pooled provider owns its lease and receives exactly one terminal callback
    after its protocol flow reaches a result or raises an error.
    """

    def registration_succeeded(self, result: Mapping[str, Any]) -> None:
        """Commit the leased mailbox after a successful registration."""

    def registration_failed(self, error: Exception, *, consumed: bool) -> None:
        """Classify a failed registration and settle the leased mailbox."""
