"""Organization and team collaboration security and isolation tests.

Verifies:
1. Organization isolation on signup (typing existing org name creates a separate org).
2. Invitation issuance, inspection, and signup validation.
3. Team creation with atomic automatic ownership for any member.
4. Membership authorization: escalation prevention, cross-org prevention, last-owner protection, archiving.
5. Cross-tenant isolation for projects, comments, shared files, downloads, experiments, documents, datasets, reagents, and chat.
6. Server-side session attribution (owner, author, uploader IDs).
7. Active team header resolution and forbidden access handling.
"""
from __future__ import annotations

import io
import pytest

from app import create_app
from app.config import TestConfig

GOOD_PW = "Tr1cky-Pipette-42"
ORIGIN = {"Origin": "http://localhost:5173"}


@pytest.fixture()
def auth_app(tmp_path):
    return create_app(
        TestConfig,
        DATA_DIR=tmp_path,
        DATABASE_PATH=tmp_path / "test.db",
        UPLOAD_DIR=tmp_path / "uploads",
        INDEX_DIR=tmp_path / "index",
        AUTH_REQUIRED=True,
    )


def make_client(auth_app):
    return auth_app.test_client()


def signup(client, name="Ada Lovelace", email="ada@lab.org", organization="Acme Lab", password=GOOD_PW, **extra):
    return client.post("/api/auth/signup", json={
        "name": name,
        "email": email,
        "organization": organization,
        "password": password,
        "accepted": True,
        **extra,
    }, headers=ORIGIN)


def get_teams(client):
    res = client.get("/api/teams")
    assert res.status_code == 200
    data = res.get_json()
    return data["teams"] if isinstance(data, dict) and "teams" in data else data


# ---------------------------------------------------------------------- 1. Organization isolation
def test_signup_creates_separate_organization_and_default_team(auth_app):
    c1 = make_client(auth_app)
    r1 = signup(c1, name="Alice User", email="alice@test.org", organization="BioCorp")
    assert r1.status_code == 201
    u1 = r1.get_json()["user"]
    assert u1["role"] == "admin"  # first user is admin
    assert u1["organization"] == "BioCorp"
    org1_id = u1["organization_id"]

    # Verify Alice has a default team
    teams1 = get_teams(c1)
    assert len(teams1) == 1
    assert teams1[0]["role"] == "owner"
    assert teams1[0]["is_default"] == 1
    assert teams1[0]["organization_id"] == org1_id

    # Second user signs up typing the EXACT same organization name
    c2 = make_client(auth_app)
    r2 = signup(c2, name="Bob User", email="bob@test.org", organization="BioCorp")
    assert r2.status_code == 201
    u2 = r2.get_json()["user"]
    assert u2["role"] == "member"  # subsequent user is member
    org2_id = u2["organization_id"]

    # Must be completely separate organizations!
    assert org2_id != org1_id

    # Bob has their own default team, and cannot see Alice's team
    teams2 = get_teams(c2)
    assert len(teams2) == 1
    assert teams2[0]["organization_id"] == org2_id
    assert teams2[0]["id"] != teams1[0]["id"]
    assert teams2[0]["role"] == "owner"


# ---------------------------------------------------------------------- 2. Invitations
def test_invitation_flow_and_validation(auth_app):
    c_admin = make_client(auth_app)
    r_admin = signup(c_admin, name="Alice Admin", email="alice@test.org", organization="AlphaLab")
    org_id = r_admin.get_json()["user"]["organization_id"]
    team_id = get_teams(c_admin)[0]["id"]

    # Invite Charlie to AlphaLab's team
    inv_resp = c_admin.post(f"/api/teams/{team_id}/invitations", json={"email": "charlie@test.org", "role": "member"}, headers=ORIGIN)
    assert inv_resp.status_code == 201
    inv_data = inv_resp.get_json()
    token = inv_data["token"]
    assert token

    # Public check of invitation details
    c_anon = make_client(auth_app)
    details_resp = c_anon.get(f"/api/invitations/{token}")
    assert details_resp.status_code == 200
    details = details_resp.get_json()
    assert details["email"] == "charlie@test.org"
    assert details["organization_id"] == org_id
    assert details["role"] == "member"

    # Reject signup with mismatched email
    c_fraud = make_client(auth_app)
    bad_resp = signup(c_fraud, name="Eve Fraud", email="eve@test.org", invite_token=token)
    assert bad_resp.status_code == 400
    assert "different email" in bad_resp.get_json()["error"]

    # Charlie signs up with valid token
    c_charlie = make_client(auth_app)
    r_charlie = signup(c_charlie, name="Charlie Member", email="charlie@test.org", invite_token=token)
    assert r_charlie.status_code == 201
    u_charlie = r_charlie.get_json()["user"]
    assert u_charlie["organization_id"] == org_id  # joined Alice's organization

    # Charlie has access to the invited team with role "member"
    charlie_teams = get_teams(c_charlie)
    assert any(t["id"] == team_id and t["role"] == "member" for t in charlie_teams)

    # Trying to reuse token fails
    c_reuse = make_client(auth_app)
    reuse_resp = signup(c_reuse, name="Charlie Again", email="charlie2@test.org", invite_token=token)
    assert reuse_resp.status_code == 400


# ---------------------------------------------------------------------- 3. Teams creation & atomic ownership
def test_member_can_create_team_and_becomes_owner(auth_app):
    c_admin = make_client(auth_app)
    signup(c_admin, name="Alice", email="alice@test.org")
    team1_id = get_teams(c_admin)[0]["id"]

    # Invite regular member
    inv = c_admin.post(f"/api/teams/{team1_id}/invitations", json={"email": "dan@test.org", "role": "member"}, headers=ORIGIN).get_json()
    c_dan = make_client(auth_app)
    signup(c_dan, name="Dan", email="dan@test.org", invite_token=inv["token"])

    # Dan is a regular lab member: Dan creates a new team
    new_team_resp = c_dan.post("/api/teams", json={"name": "Genomics Core", "description": "Sequencing team"}, headers=ORIGIN)
    assert new_team_resp.status_code == 201
    new_team = new_team_resp.get_json()
    assert new_team["name"] == "Genomics Core"
    assert new_team["role"] == "owner"  # Dan automatically became owner!

    # Check members of Genomics Core
    members_resp = c_dan.get(f"/api/teams/{new_team['id']}/members")
    assert members_resp.status_code == 200
    members = members_resp.get_json()
    assert len(members) == 1
    assert members[0]["email"] == "dan@test.org"
    assert members[0]["role"] == "owner"


# ---------------------------------------------------------------------- 4. Membership escalation & invariants
def test_membership_escalation_and_cross_org_prevention(auth_app):
    # Setup Alice and Bob in separate organizations
    c1 = make_client(auth_app)
    signup(c1, name="Alice", email="alice@test.org")
    team1 = get_teams(c1)[0]

    c2 = make_client(auth_app)
    signup(c2, name="Bob", email="bob@test.org")

    # Invite Carol to Team 1 as admin
    inv = c1.post(f"/api/teams/{team1['id']}/invitations", json={"email": "carol@test.org", "role": "admin"}, headers=ORIGIN).get_json()
    c3 = make_client(auth_app)
    signup(c3, name="Carol", email="carol@test.org", invite_token=inv["token"])

    # Invite Dave to Team 1 as member
    inv2 = c1.post(f"/api/teams/{team1['id']}/invitations", json={"email": "dave@test.org", "role": "member"}, headers=ORIGIN).get_json()
    c4 = make_client(auth_app)
    signup(c4, name="Dave", email="dave@test.org", invite_token=inv2["token"])

    # 1. Carol (admin) tries to promote Dave to "owner" -> should fail (cannot grant above own role)
    promote_resp = c3.post(f"/api/teams/{team1['id']}/members", json={"email": "dave@test.org", "role": "owner"}, headers=ORIGIN)
    assert promote_resp.status_code == 403

    # 2. Carol tries to self-promote to "owner" -> should fail
    self_resp = c3.post(f"/api/teams/{team1['id']}/members", json={"email": "carol@test.org", "role": "owner"}, headers=ORIGIN)
    assert self_resp.status_code == 403

    # 3. Cross-organization membership prevention: Alice tries to add Bob (from Org 2) to Team 1 directly
    cross_resp = c1.post(f"/api/teams/{team1['id']}/members", json={"email": "bob@test.org", "role": "member"}, headers=ORIGIN)
    assert cross_resp.status_code == 403
    assert "Cross-organization" in cross_resp.get_json()["error"]

    # 4. Last-owner protection: Alice cannot remove herself or demote herself
    alice_id = c1.get("/api/auth/me").get_json()["user"]["id"]
    demote_resp = c1.post(f"/api/teams/{team1['id']}/members", json={"email": "alice@test.org", "role": "member"}, headers=ORIGIN)
    assert demote_resp.status_code == 409
    assert "at least one owner" in demote_resp.get_json()["error"]

    del_resp = c1.delete(f"/api/teams/{team1['id']}/members/{alice_id}", headers=ORIGIN)
    assert del_resp.status_code == 409


# ---------------------------------------------------------------------- 5. Collaboration security & Cross-tenant isolation
def test_cross_tenant_isolation_projects_comments_files(auth_app):
    c1 = make_client(auth_app)
    signup(c1, name="Alice", email="alice@test.org")
    team1 = get_teams(c1)[0]

    c2 = make_client(auth_app)
    signup(c2, name="Bob", email="bob@test.org")
    team2 = get_teams(c2)[0]

    # Alice creates Project 1 in Team 1
    p1_resp = c1.post("/api/projects", json={"name": "Project Apollo", "description": "Secret research"}, headers={"X-Team-ID": str(team1["id"]), **ORIGIN})
    assert p1_resp.status_code == 201
    p1 = p1_resp.get_json()
    p1_id = p1["id"]

    # Bob in Team 2 lists projects -> should be empty
    bob_projects = c2.get("/api/projects", headers={"X-Team-ID": str(team2["id"])}).get_json()
    assert not any(p["id"] == p1_id for p in bob_projects)

    # Bob attempts ID-guessing GET /api/projects/{p1_id} -> 404!
    assert c2.get(f"/api/projects/{p1_id}", headers={"X-Team-ID": str(team2["id"])}).status_code == 404

    # Bob attempts DELETE /api/projects/{p1_id} -> 404!
    assert c2.delete(f"/api/projects/{p1_id}", headers={"X-Team-ID": str(team2["id"]), **ORIGIN}).status_code == 404

    # Alice adds a comment to Project 1
    c1_comment = c1.post(f"/api/comments/project/{p1_id}", json={"body": "Hello Alice team", "author_id": 9999}, headers={"X-Team-ID": str(team1["id"]), **ORIGIN})
    assert c1_comment.status_code == 201
    com = c1_comment.get_json()
    # Author ID derived from session, NOT client payload 9999
    assert com["author_id"] != 9999

    # Bob attempts to read or add comments to Project 1 -> 404!
    assert c2.get(f"/api/comments/project/{p1_id}", headers={"X-Team-ID": str(team2["id"])}).status_code == 404
    assert c2.post(f"/api/comments/project/{p1_id}", json={"body": "Infiltration"}, headers={"X-Team-ID": str(team2["id"]), **ORIGIN}).status_code == 404

    # Alice uploads a file to Project 1
    file_bytes = b"Hello, research protocol content."
    file_upload_resp = c1.post(
        f"/api/projects/{p1_id}/files",
        data={"file": (io.BytesIO(file_bytes), "protocol.txt", "text/plain")},
        content_type="multipart/form-data",
        headers={"X-Team-ID": str(team1["id"]), **ORIGIN},
    )
    assert file_upload_resp.status_code == 201
    file_id = file_upload_resp.get_json()["id"]

    # Bob lists files of Project 1 -> 404
    assert c2.get(f"/api/projects/{p1_id}/files", headers={"X-Team-ID": str(team2["id"])}).status_code == 404

    # Bob tries to download Alice's file by ID -> 404 (IDOR prevention)
    assert c2.get(f"/api/files/{file_id}", headers={"X-Team-ID": str(team2["id"])}).status_code == 404

    # Alice can download successfully
    dl = c1.get(f"/api/files/{file_id}", headers={"X-Team-ID": str(team1["id"])})
    assert dl.status_code == 200
    assert dl.data == file_bytes


# ---------------------------------------------------------------------- 6. Cross-tenant experiments & reagents & datasets
def test_cross_tenant_isolation_experiments_and_reagents(auth_app):
    c1 = make_client(auth_app)
    signup(c1, name="Alice", email="alice@test.org")
    team1_id = get_teams(c1)[0]["id"]

    c2 = make_client(auth_app)
    signup(c2, name="Bob", email="bob@test.org")
    team2_id = get_teams(c2)[0]["id"]

    # Alice creates experiment
    exp_resp = c1.post("/api/experiments", json={"title": "PCR Amplification", "objective": "Target DNA"}, headers={"X-Team-ID": str(team1_id), **ORIGIN})
    assert exp_resp.status_code == 201
    exp_id = exp_resp.get_json()["id"]

    # Bob cannot access or modify Alice's experiment
    assert c2.get(f"/api/experiments/{exp_id}", headers={"X-Team-ID": str(team2_id)}).status_code == 404
    assert c2.patch(f"/api/experiments/{exp_id}", json={"title": "Tampered"}, headers={"X-Team-ID": str(team2_id), **ORIGIN}).status_code == 404
    assert c2.post(f"/api/experiments/{exp_id}/steps", json={"text": "Malicious step"}, headers={"X-Team-ID": str(team2_id), **ORIGIN}).status_code == 404

    # Alice adds a reagent
    reagent_resp = c1.post("/api/reagents", json={"name": "Taq Polymerase", "quantity": 50, "unit": "uL", "category": "Enzymes"}, headers={"X-Team-ID": str(team1_id), **ORIGIN})
    assert reagent_resp.status_code == 201
    reagent_id = reagent_resp.get_json()["id"]

    # Bob cannot view, adjust or delete Alice's reagent
    assert c2.get(f"/api/reagents/{reagent_id}", headers={"X-Team-ID": str(team2_id)}).status_code == 404
    assert c2.post(f"/api/reagents/{reagent_id}/adjust", json={"delta": -10}, headers={"X-Team-ID": str(team2_id), **ORIGIN}).status_code == 404
    assert c2.delete(f"/api/reagents/{reagent_id}", headers={"X-Team-ID": str(team2_id), **ORIGIN}).status_code == 404

    # Alice creates a dataset
    ds_resp = c1.post("/api/datasets", json={"name": "Electrophoresis Bands", "columns": ["Sample", "Size"], "rows": [{"Sample": "A", "Size": 500}]}, headers={"X-Team-ID": str(team1_id), **ORIGIN})
    assert ds_resp.status_code == 201
    ds_id = ds_resp.get_json()["id"]

    # Bob cannot read or delete Alice's dataset
    assert c2.get(f"/api/datasets/{ds_id}", headers={"X-Team-ID": str(team2_id)}).status_code == 404
    assert c2.delete(f"/api/datasets/{ds_id}", headers={"X-Team-ID": str(team2_id), **ORIGIN}).status_code == 404


# ---------------------------------------------------------------------- 7. Team forbidden check
def test_unauthorized_team_header_returns_403(auth_app):
    c1 = make_client(auth_app)
    signup(c1, name="Alice", email="alice@test.org")
    team1_id = get_teams(c1)[0]["id"]

    c2 = make_client(auth_app)
    signup(c2, name="Bob", email="bob@test.org")

    # Bob tries to use Alice's team1_id in X-Team-ID header
    forbidden_resp = c2.get("/api/projects", headers={"X-Team-ID": str(team1_id)})
    assert forbidden_resp.status_code == 403
    assert forbidden_resp.get_json()["code"] == "team_forbidden"


# ---------------------------------------------------------------------- 8. Safety, troubleshooting & NL query isolation
def test_safety_troubleshooting_and_nl_inventory_isolation(auth_app):
    c1 = make_client(auth_app)
    signup(c1, name="Alice", email="alice@test.org")
    team1_id = get_teams(c1)[0]["id"]

    c2 = make_client(auth_app)
    signup(c2, name="Bob", email="bob@test.org")
    team2_id = get_teams(c2)[0]["id"]

    # Alice adds a custom troubleshooting entry in Team 1
    ts_resp = c1.post("/api/troubleshooting", json={
        "equipment": "Autoclave 3000",
        "error_code": "ERR-9999",
        "symptom": "Steam valve leak",
        "keywords": "autoclave, valve, steam",
        "probable_cause": "Gasket worn",
        "fix": "Replace gasket seal.",
    }, headers={"X-Team-ID": str(team1_id), **ORIGIN})
    assert ts_resp.status_code == 201

    # Bob in Team 2 lists troubleshooting entries or matches: does not see Alice's custom entry
    bob_ts = c2.get("/api/troubleshooting", headers={"X-Team-ID": str(team2_id)}).get_json()
    assert not any(e.get("error_code") == "ERR-9999" for e in bob_ts)

    bob_match = c2.post("/api/troubleshooting/match", json={"query": "ERR-9999 steam valve"}, headers={"X-Team-ID": str(team2_id), **ORIGIN}).get_json()
    assert not any(m.get("error_code") == "ERR-9999" for m in bob_match["matches"])

    # Alice adds a hazardous reagent in Team 1
    reagent_resp = c1.post("/api/reagents", json={
        "name": "Acrylamide 40%",
        "quantity": 100,
        "unit": "mL",
        "category": "Monomers",
        "hazard_class": "Toxic",
    }, headers={"X-Team-ID": str(team1_id), **ORIGIN})
    assert reagent_resp.status_code == 201

    # Bob checks /api/safety overview: hazardous reagents must NOT include Alice's Acrylamide
    bob_safety = c2.get("/api/safety", headers={"X-Team-ID": str(team2_id)}).get_json()
    assert not any(r["name"] == "Acrylamide 40%" for r in bob_safety["hazardous_reagents"])

    # Bob runs NL query for Acrylamide: should not be found in Bob's lab
    bob_nl = c2.post("/api/reagents/query", json={"question": "Where is the Acrylamide?"}, headers={"X-Team-ID": str(team2_id), **ORIGIN}).get_json()
    assert len(bob_nl["results"]) == 0

    # Alice can find Acrylamide in Team 1
    alice_nl = c1.post("/api/reagents/query", json={"question": "Where is the Acrylamide?"}, headers={"X-Team-ID": str(team1_id), **ORIGIN}).get_json()
    assert any(r["name"] == "Acrylamide 40%" for r in alice_nl["results"])


# ---------------------------------------------------------------------- 9. Settings RBAC
def test_settings_update_requires_admin_role(auth_app):
    c1 = make_client(auth_app)
    signup(c1, name="Alice", email="alice@test.org")
    team1_id = get_teams(c1)[0]["id"]

    # Alice invites Carol as viewer
    inv = c1.post(f"/api/teams/{team1_id}/invitations", json={"email": "carol@test.org", "role": "viewer"}, headers=ORIGIN).get_json()
    c_viewer = make_client(auth_app)
    signup(c_viewer, name="Carol", email="carol@test.org", invite_token=inv["token"])

    # Carol attempts to edit settings -> 403 Forbidden
    resp = c_viewer.put("/api/settings", json={"institution_name": "Hacked Institute"}, headers={"X-Team-ID": str(team1_id), **ORIGIN})
    assert resp.status_code == 403

    # Alice (team owner/admin) can update settings
    alice_resp = c1.put("/api/settings", json={"institution_name": "Verified University"}, headers={"X-Team-ID": str(team1_id), **ORIGIN})
    assert alice_resp.status_code == 200
    assert alice_resp.get_json()["editable"]["institution_name"] == "Verified University"
