"""Tests never read the developer .env: results must not depend on whose machine runs them."""
import os

os.environ["EDUSYNC_NO_DOTENV"] = "1"
