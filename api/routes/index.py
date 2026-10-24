"""
The browsable front page.

Every other route in this API is a machine endpoint, and the ones that
create things (POST /events, POST /endpoints) are POST-only by design.
That means typing the hostname into a browser used to land on a raw
Werkzeug 404, or a 405 if you guessed /events. This route gives the app
a real home: delivery stats, a working "send a test event" form, the
registered endpoints, and a route reference.
"""

from flask import Blueprint, render_template

index_bp = Blueprint("index", __name__)


@index_bp.route("/")
def index():
    return render_template("index.html")
