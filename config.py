import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN   = os.environ["BOT_TOKEN"]
MASTER_CODE = os.environ["MASTER_CODE"]
ADMIN_ID    = int(os.getenv("ADMIN_ID", "0"))
DB_PATH     = os.getenv("DB_PATH", "salon_bot.db")
