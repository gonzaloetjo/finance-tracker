from __future__ import annotations


def mask_iban(iban: str | None) -> str | None:
    """Return a display-safe IBAN reference, preserving only prefix and last 4."""
    if not iban:
        return None
    compact = "".join(str(iban).split())
    if len(compact) <= 8:
        return compact[:2] + "..." if compact else None
    return f"{compact[:4]}...{compact[-4:]}"


__all__ = ["mask_iban"]
