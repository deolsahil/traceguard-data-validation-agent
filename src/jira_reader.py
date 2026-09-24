import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


_BLOCK_TYPES = {
    "doc",
    "paragraph",
    "heading",
    "blockquote",
    "codeBlock",
    "panel",
    "bulletList",
    "orderedList",
    "table",
    "tableRow",
    "tableCell",
    "tableHeader",
}

ISSUE_FIELDS = [
    "summary",
    "description",
    "status",
    "project",
    "issuetype",
    "labels",
    "components",
    "fixVersions",
    "updated",
    "attachment",
]


def jira_content_to_text(content: Any) -> str:
    """
    Convert Jira plain text or Atlassian Document Format into readable text.
    """

    if content is None:
        return ""

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        return "".join(jira_content_to_text(item) for item in content).strip()

    if not isinstance(content, dict):
        return str(content).strip()

    node_type = content.get("type")

    if node_type == "text":
        return str(content.get("text", ""))

    if node_type == "hardBreak":
        return "\n"

    if node_type == "rule":
        return "\n---\n"

    if node_type == "mention":
        attributes = content.get("attrs") or {}

        return str(
            attributes.get("text")
            or attributes.get("displayName")
            or attributes.get("id")
            or "@unknown"
        )

    if node_type == "emoji":
        attributes = content.get("attrs") or {}

        return str(
            attributes.get("text")
            or attributes.get("shortName")
            or ""
        )

    children = content.get("content") or []

    rendered = "".join(
        jira_content_to_text(child)
        for child in children
    )

    if node_type == "listItem":
        return f"- {rendered.strip()}\n"

    if node_type in _BLOCK_TYPES and rendered:
        if not rendered.endswith("\n"):
            rendered += "\n"

    return rendered


def clean_text(value: str) -> str:
    """
    Remove excessive blank lines while preserving readable formatting.
    """

    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    cleaned_lines: list[str] = []
    previous_blank = False

    for line in lines:
        line = line.rstrip()

        if not line.strip():
            if cleaned_lines and not previous_blank:
                cleaned_lines.append("")

            previous_blank = True
            continue

        cleaned_lines.append(line)
        previous_blank = False

    return "\n".join(cleaned_lines).strip()


class JiraReader:
    def __init__(self) -> None:
        load_dotenv()

        self.base_url = os.getenv("JIRA_BASE_URL", "").strip().rstrip("/")
        self.api_version = os.getenv("JIRA_API_VERSION", "3").strip()
        self.email = os.getenv("JIRA_EMAIL", "").strip()
        self.api_token = os.getenv("JIRA_API_TOKEN", "").strip()
        self.bearer_token = os.getenv("JIRA_BEARER_TOKEN", "").strip()

        self.verify_ssl = (
            os.getenv("JIRA_VERIFY_SSL", "true").strip().lower()
            in {"true", "1", "yes"}
        )

        self.timeout_seconds = int(
            os.getenv("JIRA_TIMEOUT_SECONDS", "30")
        )

        self._validate_configuration()

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": (
                    "e2e-tagging-traceability/0.1 "
                    "(Jira traceability integration)"
                ),
            }
        )

        if self.bearer_token:
            self.session.headers.update(
                {
                    "Authorization": f"Bearer {self.bearer_token}",
                }
            )
        else:
            self.session.auth = (
                self.email,
                self.api_token,
            )

    def _validate_configuration(self) -> None:
        if not self.base_url:
            raise ValueError("JIRA_BASE_URL is missing in .env")

        if self.api_version not in {"2", "3"}:
            raise ValueError(
                "JIRA_API_VERSION must be either 2 or 3"
            )

        has_cloud_auth = bool(self.email and self.api_token)
        has_bearer_auth = bool(self.bearer_token)

        if has_cloud_auth and has_bearer_auth:
            raise ValueError(
                "Configure either Jira Cloud authentication or "
                "JIRA_BEARER_TOKEN, not both"
            )

        if not has_cloud_auth and not has_bearer_auth:
            raise ValueError(
                "Jira credentials are missing in .env"
            )

    def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = (
            f"{self.base_url}/rest/api/"
            f"{self.api_version}/{endpoint.lstrip('/')}"
        )

        try:
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout_seconds,
                verify=self.verify_ssl,
            )

            response.raise_for_status()

            return response.json()

        except requests.HTTPError as error:
            response_text = response.text[:1000]

            raise RuntimeError(
                f"Jira request failed with HTTP "
                f"{response.status_code}.\n"
                f"URL: {response.url}\n"
                f"Response: {response_text}"
            ) from error

        except requests.RequestException as error:
            raise RuntimeError(
                f"Unable to connect to Jira: {error}"
            ) from error

    def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        url = (
            f"{self.base_url}/rest/api/"
            f"{self.api_version}/issue/{issue_key.strip().upper()}/comment"
        )
        try:
            response = self.session.post(
                url,
                json={"body": body.strip()},
                timeout=self.timeout_seconds,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            return response.json()
        except Exception as error:
            raise RuntimeError(f"Failed to post comment: {error}") from error

    def search_issues(
        self,
        jql: str,
        max_total: int = 500,
        board_id: int | None = None,
        required_label: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return all issues matching a JQL query, with comments fetched.

        When board_id is given, uses the Agile REST API
        (/rest/agile/1.0/board/{id}/issue) so results are scoped to that
        board — works on Jira Data Center where boardSprints() JQL is absent.
        """
        issues: list[dict[str, Any]] = []
        start_at = 0
        page_size = 50

        if board_id:
            base_endpoint = f"../../agile/1.0/board/{board_id}/issue"
        else:
            base_endpoint = None

        while len(issues) < max_total:
            params: dict[str, Any] = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": min(page_size, max_total - len(issues)),
                "fields": ",".join(ISSUE_FIELDS),
            }

            if base_endpoint:
                url = (
                    f"{self.base_url}/rest/agile/1.0/board/{board_id}/issue"
                )
                try:
                    response = self.session.get(
                        url,
                        params=params,
                        timeout=self.timeout_seconds,
                        verify=self.verify_ssl,
                    )
                    response.raise_for_status()
                    payload = response.json()
                except requests.HTTPError as error:
                    raise RuntimeError(
                        f"Jira Agile request failed with HTTP "
                        f"{response.status_code}.\n"
                        f"URL: {response.url}\n"
                        f"Response: {response.text[:1000]}"
                    ) from error
                except requests.RequestException as error:
                    raise RuntimeError(
                        f"Unable to connect to Jira: {error}"
                    ) from error
            else:
                payload = self._get("search", params=params)

            page = payload.get("issues") or []
            total = int(payload.get("total", 0))

            for raw_issue in page:
                key = raw_issue.get("key", "")
                raw_labels = (raw_issue.get("fields") or {}).get("labels") or []
                needs_comments = (
                    key and (not required_label or required_label in raw_labels)
                )
                comments = self.read_all_comments(key) if needs_comments else []
                issues.append(
                    self._normalize_issue(issue=raw_issue, comments=comments)
                )

            start_at += len(page)
            if not page or start_at >= total:
                break

        return issues

    def read_issue(self, issue_key: str) -> dict[str, Any]:
        issue_key = issue_key.strip().upper()

        issue = self._get(
            f"issue/{issue_key}",
            params={
                "fields": ",".join(ISSUE_FIELDS),
            },
        )

        comments = self.read_all_comments(issue_key)

        return self._normalize_issue(
            issue=issue,
            comments=comments,
        )

    def read_all_comments(
        self,
        issue_key: str,
    ) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []

        start_at = 0
        max_results = 100

        while True:
            payload = self._get(
                f"issue/{issue_key}/comment",
                params={
                    "startAt": start_at,
                    "maxResults": max_results,
                    "orderBy": "created",
                },
            )

            page = payload.get("comments") or []
            comments.extend(page)

            total = int(payload.get("total", len(comments)))

            if not page:
                break

            start_at += len(page)

            if start_at >= total:
                break

        return comments

    def _normalize_issue(
        self,
        issue: dict[str, Any],
        comments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        fields = issue.get("fields") or {}

        description = clean_text(
            jira_content_to_text(
                fields.get("description")
            )
        )

        normalized_comments = []

        for comment in comments:
            author = comment.get("author") or {}

            normalized_comments.append(
                {
                    "id": comment.get("id"),
                    "author": (
                        author.get("displayName")
                        or author.get("emailAddress")
                        or author.get("accountId")
                        or "Unknown"
                    ),
                    "created": comment.get("created"),
                    "updated": comment.get("updated"),
                    "body": clean_text(
                        jira_content_to_text(
                            comment.get("body")
                        )
                    ),
                }
            )

        return {
            "jira_key": issue.get("key"),
            "jira_id": issue.get("id"),
            "summary": fields.get("summary"),
            "description": description,
            "status": self._nested_name(fields.get("status")),
            "project": self._nested_name(fields.get("project")),
            "issue_type": self._nested_name(fields.get("issuetype")),
            "labels": fields.get("labels") or [],
            "components": [
                component.get("name")
                for component in fields.get("components") or []
                if component.get("name")
            ],
            "fix_versions": [
                version.get("name")
                for version in fields.get("fixVersions") or []
                if version.get("name")
            ],
            "fix_version_details": [
                {
                    "id": version.get("id"),
                    "name": version.get("name"),
                    "released": version.get("released"),
                    "archived": version.get("archived"),
                    "release_date": version.get("releaseDate"),
                }
                for version in fields.get("fixVersions") or []
            ],
            "updated": fields.get("updated"),
            "comment_count": len(normalized_comments),
            "comments": normalized_comments,
            "attachments": [
                {
                    "id": attachment.get("id"),
                    "filename": attachment.get("filename"),
                    "mime_type": attachment.get("mimeType"),
                    "size": attachment.get("size"),
                    "content_url": attachment.get("content"),
                }
                for attachment in fields.get("attachment") or []
                if attachment.get("content")
            ],
        }

    def download_attachment(self, content_url: str, max_bytes: int = 10 * 1024 * 1024) -> bytes | None:
        """
        Download an attachment's raw bytes using the session's existing auth.

        Returns None on failure, but prints why. A silently skipped data model
        means the agent validates fewer columns than the ticket asked for and
        still reports a pass — so the failure has to be visible.
        """
        try:
            response = self.session.get(
                content_url,
                timeout=self.timeout_seconds,
                verify=self.verify_ssl,
                stream=True,
            )
            response.raise_for_status()
            chunks, total = [], 0
            for chunk in response.iter_content(8192):
                total += len(chunk)
                if total > max_bytes:
                    print(f"  [attachment] skipped — larger than {max_bytes // (1024*1024)} MB")
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
        except Exception as error:
            print(f"  [attachment] download failed: {error}")
            return None

    @staticmethod
    def _nested_name(value: Any) -> str | None:
        if isinstance(value, dict):
            return value.get("name")

        if isinstance(value, str):
            return value

        return None


def save_result(
    issue_key: str,
    result: dict[str, Any],
    output_path: str | None,
) -> Path:
    if output_path:
        path = Path(output_path)
    else:
        path = Path("output") / f"{issue_key.upper()}.json"

    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read one Jira ticket and all of its comments."
        )
    )

    parser.add_argument(
        "issue_key",
        help="Jira issue key, for example DEMO-123",
    )

    parser.add_argument(
        "--output",
        help=(
            "Optional output path. Defaults to "
            "output/<ISSUE_KEY>.json"
        ),
    )

    arguments = parser.parse_args()

    try:
        reader = JiraReader()

        print(
            f"Reading Jira ticket "
            f"{arguments.issue_key.upper()}..."
        )

        result = reader.read_issue(arguments.issue_key)

        output_path = save_result(
            issue_key=arguments.issue_key,
            result=result,
            output_path=arguments.output,
        )

        print(f"Ticket: {result['jira_key']}")
        print(f"Summary: {result['summary']}")
        print(f"Status: {result['status']}")
        print(f"Comments read: {result['comment_count']}")
        print(f"Saved to: {output_path}")

        return 0

    except Exception as error:
        print(
            f"Error: {error}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
