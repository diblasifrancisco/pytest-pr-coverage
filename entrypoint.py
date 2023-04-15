import json
import os
import sys
from datetime import datetime, timezone
from itertools import groupby

import requests
from tomark import Tomark

REQUIRED_PERCENTAGE = sys.argv[1]
PR_NUMBER = sys.argv[2]


RESULT_SUCCESS = ":white_check_mark: **PR coverage {}%**"
RESULT_FAILURE = ":x: **The pr has not the minimun required coverage {}% < {}%**"


def get_annotation_message(start_line, end_line):
    if end_line == start_line:
        return "Added line #L{} not covered by tests".format(start_line)
    return "Added lines #L{}-{} not covered by tests".format(start_line, end_line)


def get_missing_range(range_list):
    for _, b in groupby(enumerate(range_list), lambda pair: pair[1] - pair[0]):
        b = list(b)
        yield {"start_line": b[0][1], "end_line": b[-1][1]}


def create_single_annotation(error, file_path):
    start_line = error["start_line"]
    end_line = error["end_line"]
    message = get_annotation_message(start_line, end_line)
    return dict(
        path=file_path,
        start_line=start_line,
        end_line=end_line,
        annotation_level="warning",
        message=message,
    )


class CheckRun:
    GITHUB_API = os.environ["GITHUB_API_URL"]
    GITHUB_HEAD_REF = os.environ["GITHUB_HEAD_REF"]
    ACCEPT_HEADER_VALUE = "application/vnd.github.v3+json"
    AUTH_HEADER_VALUE = "token {}".format(os.environ["GITHUB_TOKEN"])

    # This is the max annotations Github API allows.
    MAX_ANNOTATIONS = 50

    def __init__(self):
        self.repo_full_name = os.environ["GITHUB_REPOSITORY"]
        self.annotations = []
        self.modified_lines = {}
        self.total_modified_lines = 0
        self.total_missing_lines = 0
        self.files = []
        self.coverage_per_file = []
        self.result = ""
        self.file_content = {}
        self.files_with_missing_lines = ""
        self.total_files_with_missing_lines = 0

    def create_annotations(self):
        """
        Reads the range of missing covered lines and create github annotations
        Counts the total missing lines per file
        Counts the total modified lines per file.
        """
        for file_path in self.files:
            file_data = self.coverage_output["files"].get(file_path)
            self.coverage_per_file.append(
                {
                    "File": file_path,
                    "Coverage": "{}%".format(
                        round(file_data["summary"]["percent_covered"], 2)
                    ),
                }
            )
            # filter lines that are on the PR
            missing_lines = list(
                set(self.modified_lines[file_path]) & set(file_data["missing_lines"])
            )

            self.total_modified_lines += len(self.modified_lines[file_path])
            self.total_missing_lines += len(missing_lines)

            if len(missing_lines) == 0:
                continue
            if not self.files_with_missing_lines:
                self.files_with_missing_lines = "\n"

            self.files_with_missing_lines += " - {}\n".format(file_path)
            self.total_files_with_missing_lines += 1
            for missing_range in get_missing_range(missing_lines):
                annotation = create_single_annotation(missing_range, file_path)
                self.annotations.append(annotation)
                if len(self.annotations) == 50:
                    return

    def get_summary(self):
        """
        Creates a summary with the following information:

         - Number of analyzed files
         - Number of files with missing covered lines
         - Number of missing ranges/lines,
         - Coverage table per file,
         - Result of the analisis
        """
        number_of_annotations = len(self.annotations)
        coverage_table = (
            Tomark.table(self.coverage_per_file) if self.coverage_per_file else ""
        )
        files = ""
        if self.files_with_missing_lines:
            files = "{} \n".format(self.files_with_missing_lines)

        summary = "### Coverage Report\n Total files: {}\n  Files with uncovered lines: {}\n {} Missing coverage line ranges: {}\n {}\n{}\n".format(
            self.total_files,
            self.total_files_with_missing_lines,
            files,
            number_of_annotations if number_of_annotations < 50 else "50+",
            coverage_table,
            self.result,
        )
        return summary

    def pr_has_minimum_coverage(self):
        if self.percentage_covered >= int(REQUIRED_PERCENTAGE):
            return True
        return False

    def get_conclusion(self):
        """
        Sets the status of the check-run
        """
        if not self.pr_has_minimum_coverage():
            return "action_required"
        return "success"

    def get_changed_lines_per_file(self):
        """
        Reads a file and format the result of a git diff.

        i.e:
         - input
            @@ -119,6 +119,43 @@ class TestUpdateDisputeStatus(VDSTestBase):
                        status_create.assert_called_once()
                        add_note.assert_not_called()

            +    def test_update_dispute_status_add_user_note_and_timeline_item_for_accept_status_(
            +        self,
            +    ):

            +        previous_status_mock = DisputePayPalStatus(

         - result
        {
            "disputes/common/tests/utils/test_sha_utils.py": [1,2,3],
            "disputes/common/tests/utils/sha_utils.py": [2,4,5,6],
        }
        """
        for filename, content in self.file_content.items():
            with open("{}.txt".format(filename), "w") as text_file:
                text_file.write(content)

            line_count = 0
            line = None
            with open("{}.txt".format(filename)) as text_file:
                self.modified_lines[filename] = []
                for row in text_file:
                    if row.startswith("@@"):
                        formatted_line = row.split("+")[1].split(",")
                        line = int(formatted_line[0])
                        line_count = 0
                        continue
                    elif row.startswith("+"):
                        # skip blankline or comment
                        if not (
                            row.strip() == "+"
                            or row.strip().startswith('"""')
                            or row.strip().startswith("#")
                        ):
                            self.modified_lines[filename].append(line + line_count)
                    elif row.startswith("-"):
                        continue
                    line_count += 1

    def post_comment(self):
        """
        Create a github comment a post it on the given PR
        Official docs https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28#create-a-check-run
        """
        response = requests.post(
            "{}/repos/{}/issues/{}/comments".format(
                self.GITHUB_API, self.repo_full_name, PR_NUMBER
            ),
            headers={
                "Accept": self.ACCEPT_HEADER_VALUE,
                "Authorization": self.AUTH_HEADER_VALUE,
            },
            json={"body": self.get_summary()},
        )
        response.raise_for_status()

    def post_annotations(self):
        """
        Create a github annotation a post it on the given PR
        Official docs https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28#create-a-check-run
        """
        payload = {
            "name": "pytest-coverage",
            "head_sha": self.GITHUB_HEAD_REF,
            "status": "completed",
            "conclusion": self.get_conclusion(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "output": {
                "title": "Coverage Result",
                "summary": self.get_summary(),
                "text": "Coverage results",
                "annotations": self.annotations,
            },
        }
        response = requests.post(
            "{}/repos/{}/check-runs".format(self.GITHUB_API, self.repo_full_name),
            headers={
                "Accept": self.ACCEPT_HEADER_VALUE,
                "Authorization": self.AUTH_HEADER_VALUE,
            },
            json=payload,
        )
        response.raise_for_status()

    def calculate_total_coverage(self):
        """
        Calculates the percentage of the PR coverage and sets the result
        """
        if self.total_modified_lines == 0:
            self.percentage_covered = 100
        else:
            lines_covered = self.total_modified_lines - self.total_missing_lines
            self.percentage_covered = round(
                lines_covered * 100 / self.total_modified_lines, 2
            )

        if self.pr_has_minimum_coverage():
            self.result = RESULT_SUCCESS.format(
                self.percentage_covered,
            )
        else:
            self.result = RESULT_FAILURE.format(
                self.percentage_covered, REQUIRED_PERCENTAGE
            )

    def get_pr_info(self):
        response = requests.get(
            "{}/repos/{}/pulls/{}/files".format(
                self.GITHUB_API, self.repo_full_name, PR_NUMBER
            ),
            headers={
                "Accept": self.ACCEPT_HEADER_VALUE,
                "Authorization": self.AUTH_HEADER_VALUE,
            },
        )
        files = response.json()
        response.raise_for_status()
        self.total_files = 0
        for file in files:
            self.total_files += 1
            filename = file["filename"]
            if not self.coverage_output["files"].get(filename):
                continue
            self.files.append(filename)
            self.file_content[filename] = file["patch"]

    def run_coverage(self):
        with open("coverage.json") as coverage_output_file:
            self.coverage_output = json.loads(coverage_output_file.read())
        self.get_pr_info()
        self.get_changed_lines_per_file()
        self.create_annotations()
        self.calculate_total_coverage()
        self.post_comment()
        self.post_annotations()


if __name__ == "__main__":
    CheckRun().run_coverage()
