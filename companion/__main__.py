import os
import sys

# --demo: synthetic data for screenshots and UI work; never connects to Schwab, refuses orders.
if "--demo" in sys.argv:
    os.environ["EDGE_TRADER_DEMO"] = "1"
# --private: for screen recordings; hides carried positions, their orders and the account value.
if "--private" in sys.argv:
    os.environ["EDGE_TRADER_PRIVATE"] = "1"

from .app import main  # noqa: E402

main()
