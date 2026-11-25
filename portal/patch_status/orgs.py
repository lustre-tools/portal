"""Who a change's owner works for, for the Org column.

The mapping is the deployment's (PORTAL_PATCH_STATUS_ORGS), not part of
the portal: which domains belong to which organisation is a site's own
knowledge, and an override may name a person. A JSON file:

    {
      "domain_map":      {"example.com": "Example Corp", ...},
      "email_overrides": {"someone@gmail.com": "Example Corp", ...},
      "name_overrides":  {"Some Name": "Example Corp", ...}
    }

An email override wins, then the exact domain, then each parent domain
(mail.example.com -> example.com), then the display name; otherwise
"Other". Without a file there is no Org column.
"""

from __future__ import annotations

import json
from typing import Optional

UNKNOWN = "Other"


class Orgs:
    def __init__(self, domains: dict, emails: dict, names: dict):
        self.domains = {k.lower(): v for k, v in domains.items()}
        self.emails = {k.lower(): v for k, v in emails.items()}
        self.names = dict(names)

    @classmethod
    def load(cls, path: Optional[str]) -> Optional["Orgs"]:
        if not path:
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(
            data.get("domain_map") or {},
            data.get("email_overrides") or {},
            data.get("name_overrides") or {},
        )

    def lookup(self, email: str, name: str = "") -> str:
        email = (email or "").strip().lower()
        if email in self.emails:
            return self.emails[email]
        domain = email.rpartition("@")[2] if "@" in email else ""
        parts = domain.split(".") if domain else []
        for i in range(len(parts) - 1):
            org = self.domains.get(".".join(parts[i:]))
            if org:
                return org
        return self.names.get((name or "").strip(), UNKNOWN)
