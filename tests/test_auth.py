import re
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from mythtv_web import create_app

INITIAL_PASSWORD = "known-initial-password"
UPDATED_ADMIN_PASSWORD = "updated-admin-password"


def make_app(tmp_path):
    app = create_app(
        {
            "AUTH_DATABASE_PATH": str(tmp_path / "users.sqlite"),
            "SECRET_KEY": "test-only-secret-key",
            "INITIAL_ADMIN_PASSWORD": INITIAL_PASSWORD,
        }
    )
    app.testing = True
    return app


def csrf_token(response):
    return re.search(
        r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)
    ).group(1)


def login(client, username, password):
    page = client.get("/login")
    return client.post(
        "/login",
        data={
            "csrf_token": csrf_token(page),
            "username": username,
            "password": password,
        },
    )


def rotate_password(client, current_password, new_password):
    page = client.get("/change-password")
    return client.post(
        "/change-password",
        data={
            "csrf_token": csrf_token(page),
            "current_password": current_password,
            "new_password": new_password,
            "confirm_password": new_password,
        },
    )


def user_id(app, username):
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        return connection.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()[0]


def test_initial_admin_must_change_password_and_anonymous_routes_are_restricted(
    tmp_path,
):
    app = make_app(tmp_path)
    client = app.test_client()

    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 302
    assert client.get("/api/programs").status_code == 401
    assert client.get("/login").status_code == 200

    response = login(client, "admin", INITIAL_PASSWORD)
    assert response.status_code == 302
    assert response.location.endswith("/change-password")
    session_cookie = next(
        value
        for value in response.headers.getlist("Set-Cookie")
        if value.startswith("session=")
    )
    expires = parsedate_to_datetime(
        session_cookie.split("Expires=", 1)[1].split(";", 1)[0]
    )
    remaining_lifetime = (expires - datetime.now(timezone.utc)).total_seconds()
    assert 31_535_990 <= remaining_lifetime <= 31_536_000
    assert client.get("/").location.endswith("/change-password")

    response = rotate_password(client, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD)
    assert response.status_code == 302
    assert response.location.endswith("/")
    assert client.get("/").status_code == 200
    assert client.get("/admin/users").status_code == 200


def test_admin_manages_accounts_and_regular_users_cannot_manage_users(tmp_path):
    app = make_app(tmp_path)
    admin = app.test_client()
    login(admin, "admin", INITIAL_PASSWORD)
    rotate_password(admin, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD)

    page = admin.get("/admin/users")
    response = admin.post(
        "/admin/users",
        data={
            "csrf_token": csrf_token(page),
            "username": "viewer",
            "password": "temporary-user-password",
            "role": "user",
        },
    )
    assert response.status_code == 302
    viewer_id = user_id(app, "viewer")

    page = admin.get("/admin/users")
    response = admin.post(
        f"/admin/users/{viewer_id}/role",
        data={"csrf_token": csrf_token(page), "role": "admin"},
    )
    assert response.status_code == 302
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        assert (
            connection.execute(
                "SELECT role FROM users WHERE id = ?", (viewer_id,)
            ).fetchone()[0]
            == "admin"
        )

    page = admin.get("/admin/users")
    admin.post(
        f"/admin/users/{viewer_id}/role",
        data={"csrf_token": csrf_token(page), "role": "user"},
    )

    page = admin.get("/admin/users")
    response = admin.post(
        f"/admin/users/{viewer_id}/password",
        data={
            "csrf_token": csrf_token(page),
            "password": "replacement-user-password",
            "must_change_password": "on",
        },
    )
    assert response.status_code == 302

    viewer = app.test_client()
    response = login(viewer, "viewer", "replacement-user-password")
    assert response.location.endswith("/change-password")
    rotate_password(viewer, "replacement-user-password", "permanent-user-password")
    assert viewer.get("/").status_code == 200
    assert viewer.get("/admin/users").status_code == 403

    page = admin.get("/admin/users")
    admin.post(
        f"/admin/users/{viewer_id}/delete",
        data={"csrf_token": csrf_token(page)},
    )
    assert admin.get("/admin/users").status_code == 200
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM users WHERE username = 'viewer'"
            ).fetchone()
            is None
        )


def test_admin_management_requires_csrf_and_preserves_last_admin(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client, "admin", INITIAL_PASSWORD)
    rotate_password(client, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD)
    admin_id = user_id(app, "admin")

    assert client.post("/admin/users", data={"username": "forged"}).status_code == 400
    page = client.get("/admin/users")
    response = client.post(
        f"/admin/users/{admin_id}/role",
        data={"csrf_token": csrf_token(page), "role": "user"},
    )
    assert response.status_code == 302
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        assert (
            connection.execute(
                "SELECT role FROM users WHERE id = ?", (admin_id,)
            ).fetchone()[0]
            == "admin"
        )

    response = client.post(
        f"/admin/users/{admin_id}/delete",
        data={"csrf_token": csrf_token(page)},
    )
    assert response.status_code == 302
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (admin_id,)
            ).fetchone()
            is not None
        )
