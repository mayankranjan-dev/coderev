import pytest

import main


@pytest.fixture
def dist(tmp_path, monkeypatch):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html><body>app shell</body></html>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(main.frontend_app, "dist_dir", root)
    return root


def test_index_is_served(client, dist):
    response = client.get("/")
    assert response.status_code == 200
    assert "app shell" in response.text


def test_assets_are_served(client, dist):
    response = client.get("/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_api_routes_win_over_the_mount(client, dist, auth):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/reviews").status_code == 403
    assert client.get("/api/reviews", headers=auth).status_code == 200


def test_unknown_path_is_404(client, dist):
    assert client.get("/api/does-not-exist").status_code == 404
    assert client.get("/nope.txt").status_code == 404


def test_missing_dist_gives_a_clear_message(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main.frontend_app, "dist_dir", tmp_path / "missing")
    response = client.get("/")
    assert response.status_code == 503
    assert "npm run build" in response.text
    # The API keeps working without a build.
    assert client.get("/api/health").status_code == 200


def test_mount_is_the_last_route():
    assert main.app.routes[-1].name == "frontend"
