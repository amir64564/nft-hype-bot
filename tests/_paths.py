"""Location of the real X regression inputs. Committed copies live in tests/fixtures/x_ingest/ so a
clean clone can run the suite (x_ingest/ is runtime data and git-ignored)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "x_ingest"


def ingest_path(name: str) -> pathlib.Path:
    p = FIXTURE_DIR / f"{name}.json"
    return p if p.exists() else ROOT / "x_ingest" / f"{name}.json"
