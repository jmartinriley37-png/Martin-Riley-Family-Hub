"""Repository hygiene: no secrets, database files or real connection strings in anything Git would commit."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
FORBIDDEN_NAMES = re.compile(r"(\.sqlite3?(-wal|-shm)?$|\.db$|\.dump$|\.pgdump$|\.backup$|\.sql\.gz$|\.pem$|\.key$|\.p12$|\.pfx$|\.pgpass$|(^|/)\.env(\..*)?$|pg_service\.conf$)")
ALLOWED_NAMES = {".env.example"}
URL_WITH_PASSWORD = re.compile(r"postgres(?:ql)?://([^:/\s'\"@]+):([^@\s'\"]+)@([^/\s'\"?]+)")
PLACEHOLDER_PASSWORD = re.compile(r"^(PASSWORD|USER|p|pw|%s|\*+|<[^>]*>|\$\{?\w+\}?|\{.*\}|.*(SECRET|secret|PLACEHOLDER).*)$")
TOKENS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"npg_[A-Za-z0-9]{8,}"),            # Neon-style generated passwords
    re.compile(r"vercel_blob_rw_[A-Za-z0-9_]+"),
    re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{16,}"),
)
REQUIRED_IGNORES = ("data/", "*.sqlite3*", ".env", ".env.*", "!.env.example", "*.dump", "*.backup", "*.sql.gz", ".pgpass", "*.pem", "*.key", "*.db", "*.pgdump")


def committable_files():
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [name for name in out.splitlines() if (ROOT / name).is_file()]


class SecretHygieneTests(unittest.TestCase):
    def test_no_database_dump_key_or_env_files_would_be_committed(self):
        bad = [name for name in committable_files() if FORBIDDEN_NAMES.search(name) and name not in ALLOWED_NAMES]
        self.assertEqual(bad, [])

    def test_gitignore_blocks_the_sensitive_patterns(self):
        lines = {line.strip() for line in (ROOT / ".gitignore").read_text().splitlines()}
        self.assertEqual([p for p in REQUIRED_IGNORES if p not in lines], [])

    def test_no_real_connection_urls_or_tokens_in_committable_files(self):
        findings = []
        for name in committable_files():
            try:
                text = (ROOT / name).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for user, password, host in URL_WITH_PASSWORD.findall(line):
                    if not PLACEHOLDER_PASSWORD.match(password):
                        findings.append(f"{name}:{number}: connection URL with a non-placeholder password")
                for pattern in TOKENS:
                    if pattern.search(line):
                        findings.append(f"{name}:{number}: token-like value ({pattern.pattern[:20]}...)")
        self.assertEqual(findings, [])

    def test_env_example_is_placeholders_only(self):
        for line in (ROOT / ".env.example").read_text().splitlines():
            line = line.lstrip("# ").strip()
            if line.split("=", 1)[-1].startswith(("postgres://", "postgresql://")):
                self.assertRegex(line, r"USER:PASSWORD@(HOST|127\.0\.0\.1)", line)

    def test_detector_catches_a_real_looking_secret(self):
        sample = "HUB_DATABASE_URL=postgresql://app_owner:" + "npg_" + "AbCdEf123456@ep-cool-name.neon.tech/db"
        user, password, host = URL_WITH_PASSWORD.findall(sample)[0]
        self.assertFalse(PLACEHOLDER_PASSWORD.match(password))
        self.assertTrue(any(p.search(sample) for p in TOKENS))


if __name__ == "__main__":
    unittest.main()
