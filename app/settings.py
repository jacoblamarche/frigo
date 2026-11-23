import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from starlette.datastructures import Headers


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    # courriel → prénom affiché
    people: dict[str, str] = field(default_factory=dict)
    # Seules ces IP peuvent poser X-Email : sans ça, n'importe qui sur le LAN pourrait
    # joindre le port directement et se faire passer pour l'un de nous.
    trusted_proxies: frozenset[str] = frozenset()
    # Développement local, sans oauth2-proxy devant : tout le monde est cette personne.
    dev_user: str = ""
    # Jeton attendu sur /mcp. Vide : le serveur MCP est désactivé.
    mcp_token: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        people = json.loads(os.environ.get("FRIGO_PEOPLE") or "{}")
        return cls(
            data_dir=Path(os.environ.get("FRIGO_DATA_DIR", "data")),
            people={email.strip().lower(): name for email, name in people.items()},
            trusted_proxies=frozenset(
                ip.strip() for ip in os.environ.get("FRIGO_TRUSTED_PROXIES", "").split(",") if ip
            ),
            dev_user=os.environ.get("FRIGO_DEV_USER", ""),
            mcp_token=os.environ.get("FRIGO_MCP_TOKEN", ""),
        )

    @property
    def names(self) -> list[str]:
        """Les prénoms à qui on peut confier une tâche."""
        names = list(self.people.values())
        if self.dev_user and self.dev_user not in names:
            names.append(self.dev_user)
        return names

    def person_for(self, client_host: str, headers: Headers) -> str | None:
        if self.dev_user:
            return self.dev_user
        if client_host not in self.trusted_proxies:
            return None
        return self.people.get(headers.get("x-email", "").strip().lower())


def bearer_ok(headers: Headers, token: str) -> bool:
    supplied = headers.get("authorization", "").removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(supplied.encode(), token.encode())


def same_origin(headers: Headers) -> bool:
    """Refuse un POST venu d'un autre site. Sans en-tête Origin, ce n'est pas un navigateur."""
    origin = headers.get("origin")
    return origin is None or urlsplit(origin).netloc == headers.get("host")
