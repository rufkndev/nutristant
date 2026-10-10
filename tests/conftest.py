import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("ALLOWED_USER_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
