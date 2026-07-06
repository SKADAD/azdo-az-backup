"""A minimal in-process Azure DevOps REST stub for end-to-end tests.

Serves one source project ("Alpha") with work items (history, links,
attachments, comments), a git repo, classification trees and a test plan —
and accepts everything a restore into a second project ("Beta") in the same
collection performs, recording it for assertions.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse


class StubState:
    def __init__(self):
        self.base_url = None            # set once the server knows its port
        self.repo_root = None           # dir for git repos (set by fixture)
        self.src_repo_url = None        # file:// URL of the Alpha repo
        self.beta_created = False
        self.next_wi_id = 100
        self.created_work_items = {}    # new_id -> {"type": str, "fields": {}}
        self.relation_patches = {}      # new_id -> [patch ops]
        self.comments = {}              # new_id -> [comment texts]
        self.uploaded_attachments = {}  # filename -> bytes
        self.classification_nodes = []  # (group, parent_path, name, attributes)
        self.created_repos = {}         # name -> {"id", "dir"}
        self.repo_patches = {}          # repo_id -> body
        self.created_plans = []
        self.created_suites = []
        self.suite_case_adds = []       # (plan_id, suite_id, [work item ids])
        self.created_configurations = []
        self.created_variables = []
        self.next_suite_id = 500

    # ------------------------------------------------------------ fixtures

    ATTACHMENT_CONTENT = b"hello log content"

    def project_alpha(self):
        return {
            "id": "proj-alpha", "name": "Alpha", "state": "wellFormed",
            "capabilities": {
                "versioncontrol": {"sourceControlType": "Git"},
                "processTemplate": {"templateName": "Agile",
                                    "templateTypeId": "tpl-agile"},
            },
        }

    def attachment_url(self):
        return f"{self.base_url}/myorg/_apis/wit/attachments/att-0001"

    def wi_url(self, wid):
        return f"{self.base_url}/myorg/_apis/wit/workItems/{wid}"

    def work_items(self):
        att_rel = {"rel": "AttachedFile", "url": self.attachment_url(),
                   "attributes": {"name": "log.txt"}}
        return {
            1: {
                "id": 1, "rev": 3,
                "fields": {
                    "System.WorkItemType": "Bug",
                    "System.Title": "Crash on save",
                    "System.State": "Active",
                    "System.AreaPath": "Alpha\\Team A",
                    "System.IterationPath": "Alpha\\Sprint 1",
                    "System.TeamProject": "Alpha",
                    "System.Parent": 2,
                    "System.CommentCount": 1,
                    "WEF_ABC123_Kanban.Column": "Doing",
                },
                "relations": [
                    att_rel,
                    {"rel": "System.LinkTypes.Hierarchy-Reverse",
                     "url": self.wi_url(2), "attributes": {"name": "Parent"}},
                ],
            },
            2: {
                "id": 2, "rev": 1,
                "fields": {
                    "System.WorkItemType": "User Story",
                    "System.Title": "Save documents",
                    "System.State": "New",
                    "System.AreaPath": "Alpha",
                    "System.IterationPath": "Alpha",
                    "System.TeamProject": "Alpha",
                    "System.CommentCount": 0,
                },
                "relations": [
                    {"rel": "System.LinkTypes.Hierarchy-Forward",
                     "url": self.wi_url(1), "attributes": {"name": "Child"}},
                    {"rel": "System.LinkTypes.Related",
                     "url": self.wi_url(3), "attributes": {"name": "Related"}},
                ],
            },
            3: {
                "id": 3, "rev": 2,
                "fields": {
                    "System.WorkItemType": "Test Case",
                    "System.Title": "Save works",
                    "System.State": "Design",
                    "System.AreaPath": "Alpha\\Team A",
                    "System.IterationPath": "Alpha\\Sprint 1",
                    "System.TeamProject": "Alpha",
                    "System.CommentCount": 0,
                    "Microsoft.VSTS.TCM.Steps": "<steps><step id='1'/></steps>",
                },
                "relations": [
                    {"rel": "System.LinkTypes.Related",
                     "url": self.wi_url(2), "attributes": {"name": "Related"}},
                ],
            },
        }

    def revisions(self, wid):
        wi = self.work_items()[wid]
        revs = []
        for r in range(1, wi["rev"] + 1):
            rev = {"id": wid, "rev": r,
                   "fields": dict(wi["fields"], **{"System.Rev": r}),
                   "relations": wi["relations"] if r == wi["rev"] else []}
            revs.append(rev)
        return revs

    def comments_for(self, wid):
        if wid == 1:
            return [{"id": 11, "text": "Repro attached.",
                     "createdBy": {"displayName": "Alice"},
                     "createdDate": "2025-01-02T03:04:05Z"}]
        return []

    def classification(self, group):
        if group == "areas":
            return {"name": "Alpha", "structureType": "area", "children": [
                {"name": "Team A", "children": []}]}
        return {"name": "Alpha", "structureType": "iteration", "children": [
            {"name": "Sprint 1",
             "attributes": {"startDate": "2025-01-01T00:00:00Z",
                            "finishDate": "2025-01-14T00:00:00Z"},
             "children": []}]}

    def repos_alpha(self):
        return [{"id": "repo-1111-2222", "name": "web",
                 "remoteUrl": self.src_repo_url,
                 "defaultBranch": "refs/heads/main", "isDisabled": False}]

    def test_plan(self):
        return {"id": 1, "name": "Release plan", "areaPath": "Alpha",
                "iteration": "Alpha\\Sprint 1", "rootSuite": {"id": 10}}

    def suites(self):
        return [
            {"id": 10, "name": "Release plan", "suiteType": "staticTestSuite"},
            {"id": 11, "name": "Regression", "suiteType": "staticTestSuite",
             "parentSuite": {"id": 10}},
        ]

    def suite_cases(self, suite_id):
        if suite_id == 11:
            return [{"workItem": {"id": 3, "name": "Save works"}}]
        return []


def _make_handler(state: StubState):

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence
            pass

        # -------------------------------------------------- plumbing

        def _body(self):
            return self._raw_body

        def _json_body(self):
            return json.loads(self._raw_body) if self._raw_body else None

        def _send(self, payload, status=200, content_type="application/json",
                  raw=None):
            data = raw if raw is not None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _not_found(self, msg="not found"):
            self._send({"message": msg}, status=404)

        # -------------------------------------------------- routing

        def _route(self, method):
            # Always drain the body up front — an unread body corrupts the
            # next request on the keep-alive connection.
            length = int(self.headers.get("Content-Length") or 0)
            self._raw_body = self.rfile.read(length) if length else b""
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            self.query = parse_qs(parsed.query)
            for pattern, handler in ROUTES.get(method, []):
                m = re.fullmatch(pattern, path)
                if m:
                    handler(self, *m.groups())
                    return
            self._send({"message": f"no stub route for {method} {path}"},
                       status=500)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

        def do_PUT(self):
            self._route("PUT")

        # -------------------------------------------------- GET handlers

        def get_project(self, name):
            if name.lower() == "alpha" or name == "proj-alpha":
                self._send(state.project_alpha())
            elif name.lower() == "beta" and state.beta_created:
                self._send({"id": "proj-beta", "name": "Beta",
                            "state": "wellFormed"})
            else:
                self._not_found(f"project {name}")

        def get_classification(self, project, group):
            if project.lower() == "alpha":
                self._send(state.classification(group.lower()))
            else:
                self._not_found()

        def get_revisions(self, project, wid):
            skip = int(self.query.get("$skip", ["0"])[0])
            revs = state.revisions(int(wid)) if skip == 0 else []
            self._send({"value": revs, "count": len(revs)})

        def get_comments(self, project, wid):
            comments = state.comments_for(int(wid))
            self._send({"comments": comments, "count": len(comments)})

        def get_attachment(self, att_id):
            self._send(None, content_type="application/octet-stream",
                       raw=state.ATTACHMENT_CONTENT)

        def get_repos(self, project):
            if project.lower() == "alpha":
                self._send({"value": state.repos_alpha()})
            else:
                self._send({"value": []})

        def get_plans(self, project):
            if project.lower() == "alpha":
                self._send({"value": [state.test_plan()]})
            else:
                self._send({"value": []})

        def get_suites(self, project, plan_id):
            self._send({"value": state.suites()})

        def get_suite_cases(self, project, plan_id, suite_id):
            self._send({"value": state.suite_cases(int(suite_id))})

        def get_testconf(self, project, kind):
            if project.lower() == "alpha":
                if kind.lower() == "configurations":
                    self._send({"value": [{"id": 1, "name": "Windows",
                                           "values": [], "state": "active",
                                           "isDefault": True}]})
                else:
                    self._send({"value": [{"id": 1, "name": "Browser",
                                           "values": ["Edge"]}]})
            else:
                self._send({"value": []})

        def get_processes(self):
            self._send({"value": [{"id": "tpl-agile", "name": "Agile"}]})

        def get_operation(self, op_id):
            self._send({"id": op_id, "status": "succeeded"})

        # -------------------------------------------------- POST handlers

        def post_wiql(self, project):
            ids = sorted(state.work_items())
            self._send({"workItems": [{"id": i} for i in ids]})

        def post_batch(self, project):
            body = self._json_body()
            items = [state.work_items()[i] for i in body["ids"]
                     if i in state.work_items()]
            self._send({"value": items, "count": len(items)})

        def post_create_project(self):
            state.beta_created = True
            self._send({"id": "op-1", "status": "queued"})

        def post_classification(self, project, group, parent=None):
            body = self._json_body() or {}
            state.classification_nodes.append(
                (group.lower(), parent or "", body.get("name"),
                 body.get("attributes")))
            self._send({"name": body.get("name")})

        def post_attachment_upload(self, project):
            filename = self.query.get("fileName", ["?"])[0]
            state.uploaded_attachments[filename] = self._body()
            n = len(state.uploaded_attachments)
            self._send({"id": f"new-att-{n}",
                        "url": f"{state.base_url}/myorg/_apis/wit/attachments/new-att-{n}"})

        def post_comment(self, project, wid):
            body = self._json_body() or {}
            state.comments.setdefault(int(wid), []).append(body.get("text", ""))
            self._send({"id": len(state.comments[int(wid)])})

        def post_create_repo(self, project):
            body = self._json_body() or {}
            name = body["name"]
            target = state.repo_root / f"target_{name}.git"
            subprocess.run(["git", "init", "--bare", "-q", str(target)],
                           check=True, capture_output=True)
            repo_id = f"new-repo-{len(state.created_repos) + 1}"
            state.created_repos[name] = {"id": repo_id, "dir": target}
            self._send({"id": repo_id, "name": name,
                        "remoteUrl": target.as_uri()})

        def post_create_plan(self, project):
            body = self._json_body() or {}
            plan_id = 50 + len(state.created_plans)
            root_id = state.next_suite_id
            state.next_suite_id += 1
            state.created_plans.append(dict(body, id=plan_id,
                                            rootSuite={"id": root_id}))
            self._send({"id": plan_id, "name": body.get("name"),
                        "rootSuite": {"id": root_id}})

        def post_create_suite(self, project, plan_id):
            body = self._json_body() or {}
            suite_id = state.next_suite_id
            state.next_suite_id += 1
            state.created_suites.append(dict(body, id=suite_id,
                                             plan=int(plan_id)))
            self._send({"id": suite_id, "name": body.get("name")})

        def post_suite_cases(self, project, plan_id, suite_id):
            body = self._json_body() or []
            ids = [entry["workItem"]["id"] for entry in body]
            state.suite_case_adds.append((int(plan_id), int(suite_id), ids))
            self._send({"value": []})

        def post_testconf(self, project, kind):
            body = self._json_body() or {}
            if kind.lower() == "configurations":
                state.created_configurations.append(body)
            else:
                state.created_variables.append(body)
            self._send(dict(body, id=99))

        # -------------------------------------------------- PATCH handlers

        def patch_create_wi(self, project, wi_type):
            patches = self._json_body() or []
            fields = {p["path"].split("/fields/")[1]: p["value"]
                      for p in patches if p["path"].startswith("/fields/")}
            wid = state.next_wi_id
            state.next_wi_id += 1
            state.created_work_items[wid] = {"type": wi_type, "fields": fields}
            self._send({"id": wid, "rev": 1, "fields": fields})

        def patch_wi(self, project, wid):
            patches = self._json_body() or []
            state.relation_patches.setdefault(int(wid), []).extend(patches)
            self._send({"id": int(wid), "rev": 2})

        def patch_repo(self, project, repo_id):
            state.repo_patches[repo_id] = self._json_body()
            self._send({"id": repo_id})

    P = r"/myorg"
    ROUTES = {
        "GET": [
            (P + r"/_apis/projects/([^/]+)", Handler.get_project),
            (P + r"/_apis/process/processes", Handler.get_processes),
            (P + r"/_apis/operations/([^/]+)", Handler.get_operation),
            (P + r"/([^/]+)/_apis/wit/classificationnodes/([^/]+)",
             Handler.get_classification),
            (P + r"/([^/]+)/_apis/wit/workItems/(\d+)/revisions",
             Handler.get_revisions),
            (P + r"/([^/]+)/_apis/wit/workItems/(\d+)/comments",
             Handler.get_comments),
            (P + r"/_apis/wit/attachments/([^/]+)", Handler.get_attachment),
            (P + r"/([^/]+)/_apis/git/repositories", Handler.get_repos),
            (P + r"/([^/]+)/_apis/testplan/plans", Handler.get_plans),
            (P + r"/([^/]+)/_apis/testplan/(configurations|variables)",
             Handler.get_testconf),
            (P + r"/([^/]+)/_apis/testplan/Plans/(\d+)/suites",
             Handler.get_suites),
            (P + r"/([^/]+)/_apis/testplan/Plans/(\d+)/Suites/(\d+)/TestCase",
             Handler.get_suite_cases),
        ],
        "POST": [
            (P + r"/_apis/projects", Handler.post_create_project),
            (P + r"/([^/]+)/_apis/wit/wiql", Handler.post_wiql),
            (P + r"/([^/]+)/_apis/wit/workitemsbatch", Handler.post_batch),
            (P + r"/([^/]+)/_apis/wit/classificationnodes/([^/]+)/(.+)",
             Handler.post_classification),
            (P + r"/([^/]+)/_apis/wit/classificationnodes/([^/]+)",
             Handler.post_classification),
            (P + r"/([^/]+)/_apis/wit/attachments",
             Handler.post_attachment_upload),
            (P + r"/([^/]+)/_apis/wit/workItems/(\d+)/comments",
             Handler.post_comment),
            (P + r"/([^/]+)/_apis/git/repositories", Handler.post_create_repo),
            (P + r"/([^/]+)/_apis/testplan/plans", Handler.post_create_plan),
            (P + r"/([^/]+)/_apis/testplan/(configurations|variables)",
             Handler.post_testconf),
            (P + r"/([^/]+)/_apis/testplan/Plans/(\d+)/suites",
             Handler.post_create_suite),
            (P + r"/([^/]+)/_apis/testplan/Plans/(\d+)/Suites/(\d+)/TestCase",
             Handler.post_suite_cases),
        ],
        "PATCH": [
            (P + r"/([^/]+)/_apis/wit/workitems/\$(.+)",
             Handler.patch_create_wi),
            (P + r"/([^/]+)/_apis/wit/workitems/(\d+)", Handler.patch_wi),
            (P + r"/([^/]+)/_apis/git/repositories/([^/]+)",
             Handler.patch_repo),
        ],
        "PUT": [],
    }

    return Handler


def start_stub(repo_root):
    """Start the stub server; returns (state, base_url, shutdown_fn)."""
    state = StubState()
    state.repo_root = repo_root
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    port = server.server_address[1]
    state.base_url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()

    return state, state.base_url, shutdown
