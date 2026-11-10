"""Trading journal sync with Trading Journal AI (github.com/simonro/Trading-Journal-AI).

The app loads it optionally (`companion.journal.routes.register`); a build without this package
has no journal endpoints and no journal panel.
"""
from .sync import (ET, Journal, JournalError, _collapse, aid, is_local_url, now_iso)  # noqa: F401
