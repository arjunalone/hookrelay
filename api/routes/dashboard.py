"""
The HTML dashboard itself (US-3.1). It renders a single page that reads
from the /deliveries and /deliveries/stats JSON routes, so the browser
does the filtering and polling and the server stays a plain API.

Deliberately server-rendered with no JS framework and no new
dependencies - this is an internal ops view, not a product surface.
"""

from flask import Blueprint, render_template

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")
