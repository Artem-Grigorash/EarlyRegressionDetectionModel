from datetime import datetime
from typing import Any

from .http import JsonClient

BUILD_FIELDS = (
    "id,buildTypeId,number,status,state,statusText,branchName,composite,personal,"
    "queuedDate,startDate,finishDate,triggered(type,date,user(username)),"
    "revisions(revision(version,vcsBranchName)),"
    "testOccurrences(count,passed,failed,ignored,muted,newFailed)"
)
BUCKET_FIELDS = "id,buildTypeId,status,state,statusText,composite,startDate,finishDate"
TEST_FIELDS = (
    "id,name,status,duration,muted,currentlyMuted,newFailure,ignored,test(id),build(id,buildTypeId)"
)
CHANGE_FIELDS = "id,version,username,date,comment,parentRevisions,files(file(file,changeType))"


def tc_date(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S%z")


class TeamCity:
    def __init__(self, client: JsonClient):
        self._c = client

    def finished_builds(self, build_type: str, since: datetime, page: int = 1000) -> list[dict[str, Any]]:
        """Finished builds of build_type on all branches (incl. refs/patronus/*) since the given time."""
        builds: list[dict[str, Any]] = []
        while True:
            locator = (
                f"buildType:(id:{build_type}),branch:(policy:ALL_BRANCHES,default:any),defaultFilter:false,"
                f"state:finished,finishDate:(date:{tc_date(since)},condition:after),"
                f"count:{page},start:{len(builds)}"
            )
            batch = self._c.get(
                "app/rest/builds", locator=locator, fields="build(id,branchName,status,startDate,finishDate)"
            ).get("build", [])
            builds += batch
            if len(batch) < page:
                return builds

    def build(self, build_id: int) -> dict[str, Any]:
        return self._c.get(f"app/rest/builds/id:{build_id}", fields=BUILD_FIELDS)

    def buckets(self, build_id: int) -> list[dict[str, Any]]:
        """All builds of the chain behind a composite build: test buckets, nested composites, compilation."""
        locator = f"defaultFilter:false,snapshotDependency:(to:(id:{build_id}),includeInitial:false),count:10000"
        return self._c.get("app/rest/builds", locator=locator, fields=f"build({BUCKET_FIELDS})").get("build", [])

    def test_occurrences(self, build_id: int, page: int) -> list[dict[str, Any]]:
        """Test occurrences of a build; for a composite build, of its whole chain."""
        tests: list[dict[str, Any]] = []
        while True:
            batch = self._c.get(
                "app/rest/testOccurrences",
                locator=f"build:(id:{build_id}),count:{page},start:{len(tests)}",
                fields=f"testOccurrence({TEST_FIELDS})",
            ).get("testOccurrence", [])
            tests += batch
            if len(batch) < page:
                return tests

    def vcs_changes(self, build_id: int) -> list[dict[str, Any]]:
        return self._c.get(
            "app/rest/changes", locator=f"build:(id:{build_id}),count:10000", fields=f"change({CHANGE_FIELDS})"
        ).get("change", [])
