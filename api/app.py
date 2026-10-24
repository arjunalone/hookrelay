#The entrypoint. Creates the Flask app, connects the database, registers the routes. This is the file you run to start the API.

"""
The entrypoint. This is the file you run to start the API.

For now it just boots Flask and exposes one route: /health,
which proves the app is running and can reach the database.
Real routes (POST /events, POST /endpoints) get registered
here once we write them in the next steps.
"""

from flask import Flask, jsonify
from sqlalchemy import text
from api.config import Config
from api.routes.endpoints import endpoints_bp
from api.extensions import get_session
from api.routes.events import events_bp
from api.routes.deliveries import deliveries_bp
from api.routes.dashboard import dashboard_bp
from api.routes.index import index_bp

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.register_blueprint(index_bp)
    app.register_blueprint(endpoints_bp)
    app.register_blueprint(events_bp)
    app.register_blueprint(deliveries_bp)
    app.register_blueprint(dashboard_bp)

    @app.route("/health")
    def health():
        # Proves the app can actually reach Postgres, not just that
        # Flask itself is running.
        try:
            session = get_session()
            session.execute(text("SELECT 1"))
            session.close()
            db_status = "connected"
        except Exception as e:
            db_status = f"error: {e}"

        return jsonify({"status": "ok", "database": db_status})

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=Config.DEBUG)
